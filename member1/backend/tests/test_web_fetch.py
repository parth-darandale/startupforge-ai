"""Network-isolated tests of source fetching, extraction and access boundaries."""

import asyncio
import gzip
import hashlib
import io

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.modules.intelligence import web_fetch
from app.modules.intelligence.contracts import EvidenceItem
from app.modules.intelligence.errors import IntelligenceError
from app.modules.intelligence.figures import extract_figures
from app.modules.intelligence.web_fetch import PublicPageFetcher

REAL_PACE = PublicPageFetcher._pace


PARAGRAPH = (
    "The annual survey covered 12,400 households in India. In 2025, respondents reported "
    "an average annual expenditure of INR 4,600 on printed educational materials. "
    "These figures describe the sampled households and are not a national market-size estimate."
)
HTML = f"""<!doctype html><html><head><title>Education spending survey 2025</title>
<meta property="article:published_time" content="2025-06-12"></head>
<body><nav>Unrelated navigation offers $99 billion in prizes!</nav>
<article><h1>Education spending survey</h1><p>{PARAGRAPH}</p>
<p>This annual report provides demographic information and carefully describes the survey methodology.</p>
<table><tr><th>Metric</th><th>Value</th></tr><tr><td>Sample response rate</td><td>62%</td></tr></table></article>
<script>window.fakeNumber = '$999 billion';</script><footer>Navigation links and terms</footer></body></html>"""
NUMERIC_FREE_TEXT = (
    "This report discusses access to education and the role of libraries in community learning. "
    "Researchers examined accessibility and teaching practices without estimating market demand. "
    "The discussion describes opportunities for further research and explains methodological limitations."
)
NUMERIC_HINT = "999 billion customers"


async def public_resolver(hostname, port):
    return ["93.184.216.34"]


@pytest.fixture(autouse=True)
def isolated_host_state(monkeypatch):
    web_fetch._LOOP_HOSTS.clear()

    async def no_wait(hostname):
        pass

    monkeypatch.setattr(PublicPageFetcher, "_pace", staticmethod(no_wait))


def fetcher(handler, **kwargs):
    return PublicPageFetcher(transport=httpx.MockTransport(handler), resolver=public_resolver, **kwargs)


def standard_response(request):
    if request.url.path == "/robots.txt":
        return httpx.Response(200, text="User-agent: *\nAllow: /\n")
    return httpx.Response(200, text=HTML, headers={"Content-Type": "text/html"})


@pytest.mark.asyncio
async def test_extracts_main_page_numeric_passage_table_and_metadata_with_source_id():
    requests = []

    def handler(request):
        requests.append(request)
        return standard_response(request)

    doc = await fetcher(handler).fetch("https://research.example.org/report/#unused", "Search title")
    assert PARAGRAPH in doc.content
    assert "62%" in doc.content and "Sample response rate" in doc.content
    assert "$999 billion" not in doc.content and "$99 billion" not in doc.content
    assert "Page-declared publication date: 2025-06-12" in doc.content
    assert doc.source.url == "https://research.example.org/report/"
    assert doc.source.title == "Education spending survey 2025"
    assert doc.source.source_id == "src_" + hashlib.sha256(doc.source.url.encode()).hexdigest()[:16]
    assert doc.source.kind == "web"
    assert all(request.url.host == "93.184.216.34" for request in requests)
    assert all(request.headers["Host"] == "research.example.org" for request in requests)
    assert all(request.extensions["sni_hostname"] == "research.example.org" for request in requests)
    assert all("StartupForgeAI" in request.headers["user-agent"] for request in requests)
    assert all(request.headers["accept-encoding"] == "identity" for request in requests)


@pytest.mark.asyncio
async def test_xml_feed_uses_same_pinned_requests_without_becoming_document():
    requests = []
    xml = b'<rss><channel><item><title>Report</title><link>https://example.org/</link></item></channel></rss>'
    def handler(request):
        requests.append(request)
        if request.url.path == '/robots.txt':
            return standard_response(request)
        return httpx.Response(200, content=xml, headers={'Content-Type': 'application/xml'})
    url, body = await fetcher(handler).fetch_xml_feed('https://feed.example.org/news/search?format=rss')
    assert url == 'https://feed.example.org/news/search?format=rss' and body == xml
    assert all(request.url.host == '93.184.216.34' for request in requests)
    assert all(request.headers['Host'] == 'feed.example.org' for request in requests)
    assert all(request.extensions['sni_hostname'] == 'feed.example.org' for request in requests)


@pytest.mark.asyncio
async def test_xml_feed_robots_denial_prevents_feed_request():
    requests = []
    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(200, text='User-agent: *\nDisallow: /news/\n')
    with pytest.raises(IntelligenceError) as error:
        await fetcher(handler).fetch_xml_feed('https://feed.example.org/news/search?format=rss')
    assert error.value.code == 'PAGE_ROBOTS_DENIED' and requests == ['/robots.txt']


@pytest.mark.asyncio
@pytest.mark.parametrize('content_type,body,code', [
    ('text/html', b'<html>Challenge</html>', 'SEARCH_INVALID_RESPONSE'),
    ('application/xml', b'x' * (web_fetch.MAX_FEED_BYTES + 1), 'PAGE_TOO_LARGE'),
], ids=['access-html', 'oversized-xml'])
async def test_feed_access_page_and_oversized_response_rejected(content_type, body, code):
    def handler(request):
        if request.url.path == '/robots.txt':
            return standard_response(request)
        return httpx.Response(200, content=body, headers={'Content-Type': content_type})
    with pytest.raises(IntelligenceError) as error:
        await fetcher(handler).fetch_xml_feed('https://feed.example.org/news/search')
    assert error.value.code == code


@pytest.mark.asyncio
async def test_xml_feed_cannot_loosen_normal_publication_content_types():
    def handler(request):
        if request.url.path == '/robots.txt':
            return standard_response(request)
        return httpx.Response(200, content=b'<rss><channel/></rss>', headers={'Content-Type': 'application/xml'})
    with pytest.raises(IntelligenceError) as error:
        await fetcher(handler).fetch('https://feed.example.org/source')
    assert error.value.code == 'PAGE_UNSUPPORTED_CONTENT'


@pytest.mark.asyncio
async def test_source_identity_keeps_distinct_fetched_trailing_slash_paths():
    reader = fetcher(standard_response)
    plain = await reader.fetch("https://research.example.org/report")
    trailing = await reader.fetch("https://RESEARCH.example.org:443/report/#section")
    assert plain.source.url == "https://research.example.org/report"
    assert trailing.source.url == "https://research.example.org/report/"
    assert plain.source.source_id != trailing.source.source_id
    for doc in [plain, trailing]:
        assert doc.source.source_id == "src_" + hashlib.sha256(doc.source.url.encode()).hexdigest()[:16]


@pytest.mark.asyncio
async def test_search_title_hint_is_display_only_and_cannot_create_html_figures():
    def handler(request):
        if request.url.path == "/robots.txt":
            return standard_response(request)
        return httpx.Response(200, text=f"<html><body><article><p>{NUMERIC_FREE_TEXT}</p></article></body></html>",
                              headers={"Content-Type": "text/html"})

    doc = await fetcher(handler).fetch("https://research.example.org/report", NUMERIC_HINT)
    assert doc.source.title == NUMERIC_HINT
    assert NUMERIC_HINT not in doc.content and "999" not in doc.content
    assert "Source page title" not in doc.content
    evidence = EvidenceItem(evidence_id="ev_html", source_id=doc.source.source_id, excerpt=doc.content)
    assert extract_figures([evidence], [doc.source]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "http://127.0.0.1/secret", "http://10.0.0.1/report", "http://169.254.169.254/latest/meta-data",
    "https://[::1]/report", "http://[::ffff:127.0.0.1]/report", "https://localhost/report",
    "https://foo.localhost/report", "https://server.internal/report", "file:///etc/passwd",
    "https://user:password@research.example.org/report", "https://research.example.org:8080/report",
    "https://research.example.org\\@127.0.0.1/report", " https://research.example.org/report",
    "http://224.0.0.1/report", "https://[fe80::1%25eth0]/report",
])
async def test_rejects_non_public_or_credentialed_urls_before_requests(url):
    called = []
    with pytest.raises(IntelligenceError) as error:
        await fetcher(lambda request: called.append(request)).fetch(url)
    assert error.value.code == "PAGE_UNSAFE_URL"
    assert not called


@pytest.mark.asyncio
async def test_rejects_mixed_public_and_private_dns_answers_without_connecting():
    async def mixed(hostname, port):
        return ["93.184.216.34", "192.168.0.1"]

    called = []
    reader = PublicPageFetcher(transport=httpx.MockTransport(lambda request: called.append(request)), resolver=mixed)
    with pytest.raises(IntelligenceError) as error:
        await reader.fetch("https://research.example.org/report")
    assert error.value.code == "PAGE_UNSAFE_URL"
    assert not called


@pytest.mark.asyncio
async def test_dns_rebinding_does_not_change_the_page_connection_after_validation():
    calls = []
    requests = []

    async def changing(hostname, port):
        calls.append(hostname)
        # Page resolves first and robots resolves second. A third lookup would
        # become private, but the page connects to its first checked IP.
        return ["93.184.216.34"] if len(calls) < 3 else ["127.0.0.1"]

    def handler(request):
        requests.append(request)
        return standard_response(request)

    doc = await PublicPageFetcher(transport=httpx.MockTransport(handler), resolver=changing).fetch("https://research.example.org/report")
    assert PARAGRAPH in doc.content
    assert len(calls) == 2
    assert all(request.url.host == "93.184.216.34" for request in requests)


@pytest.mark.asyncio
async def test_redirect_to_private_url_is_blocked_before_following():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/robots.txt":
            return standard_response(request)
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/admin"})

    with pytest.raises(IntelligenceError) as error:
        await fetcher(handler).fetch("https://research.example.org/report")
    assert error.value.code == "PAGE_UNSAFE_URL"
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_robots_redirect_to_private_host_is_also_blocked():
    def handler(request):
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/robots.txt"})

    with pytest.raises(IntelligenceError) as error:
        await fetcher(handler).fetch("https://research.example.org/report")
    assert error.value.code == "PAGE_UNSAFE_URL"


@pytest.mark.asyncio
async def test_each_public_redirect_gets_robots_check_and_keeps_final_source_url():
    hosts = []

    def handler(request):
        hosts.append((request.headers["host"], request.url.path))
        if request.url.path == "/robots.txt":
            return standard_response(request)
        if request.headers["host"] == "research.example.org":
            return httpx.Response(302, headers={"Location": "https://publisher.example.org/final-report"})
        return standard_response(request)

    doc = await fetcher(handler).fetch("https://research.example.org/report")
    assert doc.source.url == "https://publisher.example.org/final-report"
    assert ("publisher.example.org", "/robots.txt") in hosts


@pytest.mark.asyncio
async def test_redirect_count_is_bounded():
    def handler(request):
        if request.url.path == "/robots.txt":
            return standard_response(request)
        return httpx.Response(302, headers={"Location": "/next"})

    with pytest.raises(IntelligenceError) as error:
        await fetcher(handler).fetch("https://research.example.org/report")
    assert error.value.code == "PAGE_REDIRECT_LIMIT"


@pytest.mark.asyncio
async def test_robots_disallow_prevents_page_fetch_and_cache_is_shared_between_instances():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, text="User-agent: StartupForgeAI\nDisallow: /private\n")

    for reader in [fetcher(handler), fetcher(handler)]:
        with pytest.raises(IntelligenceError) as error:
            await reader.fetch("https://research.example.org/private/report")
        assert error.value.code == "PAGE_ROBOTS_DENIED"
    assert calls == ["/robots.txt"]


@pytest.mark.asyncio
async def test_missing_robots_allows_page_but_robots_access_denial_does_not():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return standard_response(request)

    assert PARAGRAPH in (await fetcher(handler).fetch("https://research.example.org/report")).content
    web_fetch._LOOP_HOSTS.clear()
    with pytest.raises(IntelligenceError) as error:
        await fetcher(lambda _: httpx.Response(403)).fetch("https://research.example.org/report")
    assert error.value.code == "PAGE_ACCESS_DENIED"
    assert error.value.details["stage"] == "robots"
    assert "robots.txt endpoint" in str(error.value)


@pytest.mark.asyncio
async def test_robots_html_challenge_is_not_silently_interpreted_as_allow_all():
    with pytest.raises(IntelligenceError) as error:
        await fetcher(lambda _: httpx.Response(200, text="<html><title>Just a moment...</title></html>")).fetch("https://research.example.org/report")
    assert error.value.code == "PAGE_ACCESS_CHALLENGE"


@pytest.mark.asyncio
async def test_host_pacing_is_shared_by_fetcher_instances(monkeypatch):
    monkeypatch.setattr(PublicPageFetcher, "_pace", staticmethod(REAL_PACE))
    first, second = fetcher(standard_response), fetcher(standard_response)
    await first._pace("research.example.org")
    started = web_fetch.time.monotonic()
    await second._pace("research.example.org")
    assert web_fetch.time.monotonic() - started >= 0.95
    assert web_fetch._host_state("research.example.org").interval >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [(403, "PAGE_ACCESS_DENIED"), (401, "PAGE_ACCESS_DENIED"), (429, "PAGE_RATE_LIMIT")])
async def test_access_denial_and_rate_limits_are_explicit_sanitized_errors(status, code):
    def handler(request):
        if request.url.path == "/robots.txt":
            return standard_response(request)
        return httpx.Response(status, text="private publisher diagnostics")

    with pytest.raises(IntelligenceError) as error:
        await fetcher(handler).fetch("https://research.example.org/report")
    assert error.value.code == code
    assert error.value.details["stage"] == "page"
    assert "private publisher diagnostics" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("html", [
    "<title>Just a moment...</title><form id='challenge-form'>Verify you are human</form>",
    "<title>Sign in</title><form><input type='password'></form>",
])
async def test_challenge_and_login_html_are_not_treated_as_evidence(html):
    def handler(request):
        if request.url.path == "/robots.txt":
            return standard_response(request)
        return httpx.Response(200, text=html, headers={"Content-Type": "text/html"})

    with pytest.raises(IntelligenceError) as error:
        await fetcher(handler).fetch("https://research.example.org/report")
    assert error.value.code == "PAGE_ACCESS_CHALLENGE"


class BytesChunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


@pytest.mark.asyncio
async def test_streamed_download_and_compression_expansion_are_bounded(monkeypatch):
    monkeypatch.setattr(web_fetch, "MAX_HTML_BYTES", 1000)

    for compressed in [False, True]:
        web_fetch._LOOP_HOSTS.clear()

        def handler(request):
            if request.url.path == "/robots.txt":
                return standard_response(request)
            data = gzip.compress(b"a" * 50000) if compressed else b"a" * 1001
            headers = {"Content-Type": "text/html"}
            if compressed:
                headers["Content-Encoding"] = "gzip"
            return httpx.Response(200, stream=BytesChunks([data[:20], data[20:]]), headers=headers)

        with pytest.raises(IntelligenceError) as error:
            await fetcher(handler).fetch("https://research.example.org/report")
        assert error.value.code == "PAGE_TOO_LARGE"


@pytest.mark.asyncio
async def test_valid_gzip_stream_is_decoded_without_losing_numeric_passage():
    def handler(request):
        if request.url.path == "/robots.txt":
            return standard_response(request)
        return httpx.Response(200, stream=BytesChunks([gzip.compress(HTML.encode())]),
                              headers={"Content-Type": "text/html", "Content-Encoding": "gzip"})

    doc = await fetcher(handler).fetch("https://research.example.org/report")
    assert PARAGRAPH in doc.content


@pytest.mark.asyncio
async def test_declared_download_size_is_rejected_before_body_read(monkeypatch):
    monkeypatch.setattr(web_fetch, "MAX_HTML_BYTES", 1000)

    def handler(request):
        if request.url.path == "/robots.txt":
            return standard_response(request)
        return httpx.Response(200, stream=BytesChunks([]), headers={"Content-Type": "text/html", "Content-Length": "1001"})

    with pytest.raises(IntelligenceError) as error:
        await fetcher(handler).fetch("https://research.example.org/report")
    assert error.value.code == "PAGE_TOO_LARGE"


def pdf_bytes(pages=1, readable=True, metadata_title="Education expenditure report", text=PARAGRAPH):
    writer = PdfWriter()
    metadata = {"/CreationDate": "D:20250612000000Z"}
    if metadata_title:
        metadata["/Title"] = metadata_title
    writer.add_metadata(metadata)
    for _ in range(pages):
        page = writer.add_blank_page(width=600, height=800)
        if readable:
            font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 10 Tf 30 700 Td ({text}) Tj ET".encode("ascii"))
            page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.asyncio
async def test_pdf_extracts_numeric_passage_and_marks_page_limit():
    def handler(request):
        if request.url.path == "/robots.txt":
            return standard_response(request)
        return httpx.Response(200, content=pdf_bytes(pages=21), headers={"Content-Type": "application/pdf"})

    doc = await fetcher(handler).fetch("https://research.example.org/report.pdf")
    assert doc.source.title == "Education expenditure report"
    assert PARAGRAPH in doc.content
    assert "pages 1-20 of 21" in doc.content
    assert "[PDF page 20]" in doc.content and "[PDF page 21]" not in doc.content
    assert "Partial report" in doc.content
    assert "not a verified publication date" in doc.content


@pytest.mark.asyncio
async def test_search_title_hint_is_display_only_and_cannot_create_pdf_figures():
    def handler(request):
        if request.url.path == "/robots.txt":
            return standard_response(request)
        return httpx.Response(200, content=pdf_bytes(metadata_title=None, text=NUMERIC_FREE_TEXT),
                              headers={"Content-Type": "application/pdf"})

    doc = await fetcher(handler).fetch("https://research.example.org/report.pdf", NUMERIC_HINT)
    assert doc.source.title == NUMERIC_HINT
    assert NUMERIC_HINT not in doc.content and "999" not in doc.content
    assert "Source PDF title" not in doc.content
    evidence = EvidenceItem(evidence_id="ev_pdf", source_id=doc.source.source_id, excerpt=doc.content)
    assert extract_figures([evidence], [doc.source]) == []


@pytest.mark.asyncio
async def test_blank_or_scanned_pdf_does_not_claim_ocr_or_invent_figures():
    def handler(request):
        if request.url.path == "/robots.txt":
            return standard_response(request)
        return httpx.Response(200, content=pdf_bytes(readable=False), headers={"Content-Type": "application/pdf"})

    with pytest.raises(IntelligenceError) as error:
        await fetcher(handler).fetch("https://research.example.org/report.pdf")
    assert error.value.code == "PAGE_NO_READABLE_CONTENT"
    assert "OCR" in str(error.value)


@pytest.mark.asyncio
async def test_timeout_is_bounded_and_sanitized():
    async def slow(hostname, port):
        await asyncio.sleep(0.2)
        return ["93.184.216.34"]

    reader = PublicPageFetcher(transport=httpx.MockTransport(standard_response), resolver=slow, timeout=0.01)
    with pytest.raises(IntelligenceError) as error:
        await reader.fetch("https://research.example.org/report")
    assert error.value.code == "PAGE_TIMEOUT"


def test_content_char_limit_is_explicit():
    content = web_fetch._bounded_content("x" * 61000)
    assert len(content) == 60000
    assert "Partial source" in content


def test_pdf_internal_compression_is_bounded(monkeypatch):
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 10 Tf 30 700 Td (" + b"a" * 5000 + b") Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream.flate_encode())
    output = io.BytesIO()
    writer.write(output)
    monkeypatch.setattr(web_fetch, "MAX_PDF_BYTES", 1000)
    with pytest.raises(IntelligenceError) as error:
        PublicPageFetcher._pdf(output.getvalue(), "Bounded PDF")
    assert error.value.code == "PAGE_TOO_LARGE"
