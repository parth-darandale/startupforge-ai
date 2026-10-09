from urllib.parse import quote

import httpx
import pytest

from app.modules.intelligence.contracts import ResearchOptions
from app.modules.intelligence.demo import DemoLLM
from app.modules.intelligence.errors import IntelligenceError
from app.modules.intelligence.pipeline import IntelligenceService
from app.modules.intelligence.providers import DuckDuckGoLiteSearchProvider, make_search_provider
from app.modules.intelligence.retrieval import KeywordRetriever
from app.modules.intelligence.settings import Settings


def result(url="https://publication.example/report", title="Market report", snippet="Reported 6% growth in 2025."):
    return (f'<tr><td><a class="result-link" href="{url}">{title}</a></td></tr>'
            f'<tr><td class="result-snippet">{snippet}</td></tr><tr><td>publication.example</td></tr>')


def page(results):
    return "<!doctype html><html><body><table>" + results + "</table></body></html>"


@pytest.mark.asyncio
async def test_search_retains_only_normalized_snippet_and_underlying_publication_url():
    calls = []
    redirect = "//duckduckgo.com/l/?uddg=" + quote("https://publication.example/report/#section", safe="") + "&amp;rut=unused"
    html = page(result(redirect, "A &amp; B report", "  Reported\n <b>6%</b> growth &amp; market uncertainty. ") +
                result("https://publication.example/report", "Duplicate", "Duplicate snippet"))

    def handler(request):
        calls.append(request)
        assert request.url.host == "lite.duckduckgo.com"
        assert request.url.path == "/lite/"
        assert request.url.params["q"] == "market statistics India"
        assert "educational research prototype" in request.headers["user-agent"]
        assert "authorization" not in request.headers
        return httpx.Response(200, text=html, headers={"content-type": "text/html; charset=utf-8"})

    provider = DuckDuckGoLiteSearchProvider(Settings(), httpx.MockTransport(handler))
    docs = await provider.search(["  market statistics  "], ResearchOptions(region="India"))
    assert len(calls) == 1  # No destination publication or redirect is fetched.
    assert len(docs) == 1
    assert docs[0].source.url == "https://publication.example/report"
    assert docs[0].source.title == "A & B report (search snippet)"
    assert docs[0].source.publisher == "DuckDuckGo search snippet"
    assert docs[0].source.kind == "web"
    assert docs[0].source.retrieved_at.tzinfo
    assert docs[0].content == "Reported 6% growth & market uncertainty."


@pytest.mark.asyncio
async def test_search_source_and_query_counts_are_bounded_and_deduplicated():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text=page(result()), headers={"content-type": "text/html"})

    provider = DuckDuckGoLiteSearchProvider(Settings(), httpx.MockTransport(handler))
    docs = await provider.search(["duplicate", "duplicate"] + [f"query {i}" for i in range(20)], ResearchOptions())
    assert len(calls) == 3
    assert len(docs) == 1
    calls.clear()
    assert await provider.search([], ResearchOptions()) == []
    assert not calls

    many = page("".join(result(f"https://publication.example/{i}") for i in range(20)))
    provider = DuckDuckGoLiteSearchProvider(Settings(), httpx.MockTransport(lambda _: httpx.Response(200, text=many, headers={"content-type": "text/html"})))
    assert len(await provider.search(["market"], ResearchOptions(max_sources=8))) == 8
    assert len(await provider.search(["market"], ResearchOptions(max_sources=2))) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("html,code", [
    ('<html><form id="challenge-form" action="/anomaly.js">Private challenge diagnostic</form></html>', "SEARCH_CHALLENGE"),
    ("<html>Unfortunately, bots use DuckDuckGo too.</html>", "SEARCH_CHALLENGE"),
    ("<html><body>Unexpected private provider diagnostic</body></html>", "PROVIDER_INVALID_RESPONSE"),
    (page(result("javascript:alert('private-secret')")), "PROVIDER_INVALID_RESPONSE"),
    (page(result("https://user:private-secret@example.org/report")), "PROVIDER_INVALID_RESPONSE"),
    (page('<tr><td><a class="result-link" href="https://example.org/report">Missing snippet</a></td></tr>'), "PROVIDER_INVALID_RESPONSE"),
])
async def test_challenges_markup_changes_and_unsafe_destinations_fail_explicitly(html, code):
    provider = DuckDuckGoLiteSearchProvider(Settings(), httpx.MockTransport(lambda _: httpx.Response(200, text=html, headers={"content-type": "text/html"})))
    with pytest.raises(IntelligenceError) as error:
        await provider.search(["market"], ResearchOptions())
    assert error.value.code == code
    assert "private-secret" not in error.value.message
    assert "diagnostic" not in error.value.message


@pytest.mark.asyncio
async def test_a_result_without_snippet_cannot_inherit_the_next_result_snippet():
    missing = '<tr><td><a class="result-link" href="https://example.org/missing">Missing snippet</a></td></tr>'
    html = page(missing + result())
    provider = DuckDuckGoLiteSearchProvider(Settings(), httpx.MockTransport(lambda _: httpx.Response(200, text=html, headers={"content-type": "text/html"})))
    docs = await provider.search(["market"], ResearchOptions())
    assert [doc.source.url for doc in docs] == ["https://publication.example/report"]


@pytest.mark.asyncio
async def test_recognized_empty_search_is_valid_and_not_a_markup_error():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text='<html><div class="no-results">No results found</div></html>', headers={"content-type": "text/html"})

    provider = DuckDuckGoLiteSearchProvider(Settings(), httpx.MockTransport(handler))
    assert await provider.search(["first", "second"], ResearchOptions()) == []
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code,retryable", [
    (403, "PROVIDER_ACCESS_DENIED", False), (429, "PROVIDER_RATE_LIMIT", True),
    (503, "PROVIDER_UNAVAILABLE", True), (302, "PROVIDER_REQUEST_REJECTED", False),
])
async def test_search_http_errors_are_typed_and_provider_details_are_private(status, code, retryable):
    provider = DuckDuckGoLiteSearchProvider(
        Settings(), httpx.MockTransport(lambda _: httpx.Response(status, text="private-secret provider diagnostic"))
    )
    with pytest.raises(IntelligenceError) as error:
        await provider.search(["market"], ResearchOptions())
    assert error.value.code == code
    assert error.value.retryable is retryable
    assert "private-secret" not in error.value.message


@pytest.mark.asyncio
async def test_search_timeout_is_sanitized():
    def handler(request):
        raise httpx.ReadTimeout("private-secret diagnostic", request=request)

    provider = DuckDuckGoLiteSearchProvider(Settings(), httpx.MockTransport(handler))
    with pytest.raises(IntelligenceError) as error:
        await provider.search(["market"], ResearchOptions())
    assert error.value.code == "PROVIDER_TIMEOUT"
    assert error.value.retryable
    assert "private-secret" not in error.value.message


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1), headers={"content-type": "text/html"}),
    httpx.Response(200, content=b"\xff\xfe", headers={"content-type": "text/html"}),
    httpx.Response(200, json={"unexpected": "private-secret"}),
])
async def test_search_caps_html_size_and_rejects_non_html_or_invalid_encoding(response):
    provider = DuckDuckGoLiteSearchProvider(Settings(), httpx.MockTransport(lambda _: response))
    with pytest.raises(IntelligenceError) as error:
        await provider.search(["market"], ResearchOptions())
    assert error.value.code == "PROVIDER_INVALID_RESPONSE"


def test_duckduckgo_search_is_an_explicit_keyless_live_selection():
    assert make_search_provider(Settings(app_mode="live", search_provider="duckduckgo")).name == "duckduckgo"
    assert make_search_provider(Settings(app_mode="demo", search_provider="duckduckgo")).name == "fixture"


@pytest.mark.asyncio
async def test_snippet_limitations_reach_the_analysis_context_and_model_inputs():
    class RecordingModel(DemoLLM):
        def __init__(self):
            super().__init__(delay=0)
            self.payloads = {}

        async def generate_json(self, task, payload, schema):
            self.payloads[task] = payload
            return await super().generate_json(task, payload, schema)

    settings = Settings(app_mode="live", search_provider="duckduckgo")
    transport = httpx.MockTransport(lambda _: httpx.Response(
        200, text=page(result(snippet="A small startup pilot can test customer demand.")),
        headers={"content-type": "text/html"},
    ))
    model = RecordingModel()
    service = IntelligenceService(settings, model, DuckDuckGoLiteSearchProvider(settings, transport), KeywordRetriever())
    idea = await service.extract("A startup helps students validate their product concepts.")
    analysis = await service.analyze("project_snippet", idea, ResearchOptions())
    context = analysis.context_draft
    assert context.execution.search_provider == "duckduckgo"
    assert context.sources[0].publisher == "DuckDuckGo search snippet"
    assert context.evidence[0].excerpt == "A small startup pilot can test customer demand."
    assert any("full publication" in warning for warning in context.warnings)
    assert any("full publications" in gap for gap in context.final_analysis.gaps)
    assert any("full publications" in gap for gap in model.payloads["analyze"]["gaps"])
