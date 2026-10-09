import pytest
from pydantic import ValidationError

from app.modules.intelligence.contracts import IdeaSpec, ResearchDocument, ResearchOptions, Source
from app.modules.intelligence.errors import IntelligenceError
from app.modules.intelligence.web_research import WebResearchProvider
from app.modules.intelligence.settings import Settings


class RecordingFetcher:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def fetch(self, url, title_hint=""):
        self.calls.append(url)
        if self.fail:
            raise IntelligenceError("PAGE_ACCESS_DENIED", "Page access was denied.")
        return ResearchDocument(source=Source(source_id=url, title=title_hint or "Publication", url=url),
                                content="Full fetched text. The report describes 12 million students in a specified population.")


@pytest.mark.asyncio
async def test_supplied_urls_skip_discovery_deduplicate_and_bound_sources():
    async def forbidden(*args):
        raise AssertionError("Supplied URLs must bypass discovery")
    fetcher = RecordingFetcher()
    provider = WebResearchProvider(Settings(), discovery=forbidden, fetcher=fetcher)
    result = await provider.search_with_diagnostics(["market"], ResearchOptions(max_sources=1, source_urls=[
        "https://example.org/report#first", "https://example.org/report#second", "https://example.org/other",
    ]))
    assert fetcher.calls == ["https://example.org/report"]
    assert len(result.documents) == 1
    assert any("Only supplied" in gap for gap in result.gaps)
    assert any("exceeds max_sources" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_search_snippet_numbers_never_enter_evidence():
    async def discover(query, options):
        return [{"title": "Real publication", "href": "https://example.org/report", "body": "Invented snippet: 999 billion"}]
    provider = WebResearchProvider(Settings(), discovery=discover, fetcher=RecordingFetcher())
    result = await provider.search_with_diagnostics(["market"], ResearchOptions())
    assert "12 million" in result.documents[0].content
    assert "999" not in result.documents[0].content
    assert result.documents[0].source.kind == "web"


@pytest.mark.asyncio
async def test_supplied_and_discovered_paths_keep_distinct_trailing_slashes():
    async def discover(query, options):
        return [{"title": "Report", "href": "https://example.org/report/", "body": "snippet"}]
    fetcher = RecordingFetcher()
    provider = WebResearchProvider(Settings(), discovery=discover, fetcher=fetcher)
    await provider.search(["market"], ResearchOptions(source_urls=["https://example.org/report/#fragment", "https://example.org/report"]))
    assert fetcher.calls == ["https://example.org/report/", "https://example.org/report"]
    fetcher.calls.clear()
    await provider.search(["market"], ResearchOptions())
    assert fetcher.calls == ["https://example.org/report/"]


@pytest.mark.asyncio
async def test_failed_page_is_a_gap_and_does_not_reuse_snippet():
    async def discover(query, options):
        return [{"title": "Report", "href": "https://example.org/report", "body": "99%"}]
    result = await WebResearchProvider(Settings(), discovery=discover, fetcher=RecordingFetcher(fail=True)).search_with_diagnostics(["market"], ResearchOptions())
    assert not result.documents
    assert any("PAGE_ACCESS_DENIED" in warning for warning in result.warnings)
    assert any("contributes no evidence" in gap for gap in result.gaps)


@pytest.mark.asyncio
async def test_search_failure_stops_further_requests_and_is_explicit():
    calls = []
    async def discover(query, options):
        calls.append(query)
        raise IntelligenceError("SEARCH_RATE_LIMIT", "Rate limit.", retryable=True)
    fetcher = RecordingFetcher()
    result = await WebResearchProvider(Settings(), discovery=discover, fetcher=fetcher).search_with_diagnostics(["first", "second"], ResearchOptions())
    assert calls == ["first"]
    assert not fetcher.calls
    assert not result.documents
    assert any("SEARCH_RATE_LIMIT" in warning for warning in result.warnings)
    assert any("market statistics remain unknown" in gap for gap in result.gaps)


@pytest.mark.parametrize("engines", ["auto", "bing", "wrong", "wikipedia", "brave,wrong", ""])
def test_invalid_or_implicit_fallback_engines_rejected(engines):
    with pytest.raises(IntelligenceError) as error:
        WebResearchProvider(Settings(web_discovery_provider="ddgs", web_search_engines=engines), fetcher=RecordingFetcher())
    assert error.value.code == "PROVIDER_CONFIGURATION"


@pytest.mark.parametrize("url", ["file:///C:/test", "https://user:secret@example.org/report", "javascript:alert(1)", "https://example.org:8080/report"])
def test_source_url_contract_rejects_non_web_links(url):
    with pytest.raises(ValidationError):
        ResearchOptions(source_urls=[url])


def test_source_url_contract_is_backward_compatible_and_bounded():
    assert ResearchOptions().source_urls == []
    with pytest.raises(ValidationError):
        ResearchOptions(source_urls=[f"https://example.org/{index}" for index in range(9)])


@pytest.mark.asyncio
async def test_topic_filter_runs_before_source_budget_and_fetches_later_relevant_candidate():
    async def discover(query, options):
        return [{'title': 'Textbook market', 'href': 'https://example.org/unrelated'},
                {'title': 'Textbook resale', 'href': 'https://other.org/relevant'}]
    class Reader(RecordingFetcher):
        async def fetch(self, url, title_hint=''):
            self.calls.append(url)
            text = ('India university education enrolled 80 million students.' if 'unrelated' in url
                    else 'Textbook resale requires checking coursebook editions and condition.')
            return ResearchDocument(source=Source(source_id=url, title=title_hint, url=url), content=text)
    reader = Reader()
    provider = WebResearchProvider(Settings(), discovery=discover, fetcher=reader)
    structured = IdeaSpec(original_text='Students resell coursebooks to each other.', project_name='Textbook Marketplace',
                          problem='Books cost too much.', solution='Textbook resale marketplace.', domain='Education')
    batch = await provider.search_for_idea(['textbook resale India'], ResearchOptions(max_sources=1), structured)
    assert reader.calls == ['https://example.org/unrelated', 'https://other.org/relevant']
    assert len(batch.documents) == 1 and batch.documents[0].source.url.endswith('/relevant')
    assert any('news RSS' in gap for gap in batch.gaps)
    assert any('off-topic' in gap for gap in batch.gaps)


@pytest.mark.asyncio
async def test_denied_host_is_not_retried_for_other_candidate_paths():
    async def discover(query, options):
        return [{'title': 'First', 'href': 'https://example.org/first'},
                {'title': 'Second', 'href': 'https://example.org/second'}]
    reader = RecordingFetcher(fail=True)
    batch = await WebResearchProvider(Settings(), discovery=discover, fetcher=reader).search_with_diagnostics(['topic'], ResearchOptions())
    assert reader.calls == ['https://example.org/first'] and not batch.documents
