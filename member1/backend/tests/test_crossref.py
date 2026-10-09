import asyncio

import httpx
import pytest

from app.modules.intelligence.contracts import IdeaSpec, ResearchOptions
from app.modules.intelligence.demo import DemoLLM
from app.modules.intelligence.errors import IntelligenceError
from app.modules.intelligence.pipeline import IntelligenceService
from app.modules.intelligence.providers import CrossrefSearchProvider, make_search_provider
from app.modules.intelligence.retrieval import KeywordRetriever
from app.modules.intelligence.settings import Settings


def record(doi="10.1234/example", **changes):
    return {"DOI": doi, "title": ["Textbook resale market study"], "publisher": "Example publisher",
            "abstract": "<jats:p>Students expressed interest in a used textbook exchange pilot; willingness to pay remains unknown.</jats:p>", **changes}


def response(items):
    return httpx.Response(200, json={"status": "ok", "message": {"items": items}})


def fake_clock(provider):
    class Clock:
        now = 10.0

        def __init__(self):
            self.pauses = []

        async def pause(self, seconds):
            self.pauses.append(seconds)
            self.now += seconds

    clock = Clock()
    provider._clock = lambda: clock.now
    provider._pause = clock.pause
    return clock


@pytest.mark.asyncio
async def test_public_abstract_search_preserves_plain_text_and_builds_safe_doi_citations():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.host == "api.crossref.org"
        assert request.url.path == "/works"
        assert request.url.params["query.bibliographic"] == "used textbooks India"
        assert "query" not in request.url.params
        assert request.url.params["filter"] == "has-abstract:true"
        assert request.url.params["rows"] == "12"
        assert "mailto" not in request.url.params
        assert "authorization" not in request.headers
        assert "educational research prototype" in request.headers["user-agent"]
        return response([record("10.1234/EXAMPLE?part=1", title=["<i>Student</i> textbook resale study"],
                                abstract="<jats:abstract><jats:p>  Reported\n <b>6%</b> interest &amp; uncertainty. </jats:p></jats:abstract>",
                                URL="http://127.0.0.1/private")])

    provider = CrossrefSearchProvider(Settings(), httpx.MockTransport(handler))
    docs = await provider.search(["used textbooks"], ResearchOptions(region="India"))
    assert len(calls) == 1  # Neither DOI resolver nor deposited URL is fetched.
    assert len(docs) == 1
    assert docs[0].source.url == "https://doi.org/10.1234/example%3Fpart%3D1"
    assert docs[0].source.title == "Student textbook resale study (abstract)"
    assert docs[0].source.publisher == "Example publisher (Crossref abstract metadata)"
    assert docs[0].source.kind == "web"
    assert docs[0].source.retrieved_at.tzinfo
    assert docs[0].content == "Reported 6% interest & uncertainty."


@pytest.mark.asyncio
async def test_queries_and_sources_are_bounded_with_case_insensitive_doi_deduplication():
    calls = []

    def handler(request):
        calls.append(request)
        return response([record("10.1234/EXAMPLE"), record("10.1234/example")])

    provider = CrossrefSearchProvider(Settings(), httpx.MockTransport(handler))
    fake_clock(provider)
    docs = await provider.search(["textbook resale", "textbook resale"] + [f"textbook rental {i}" for i in range(20)], ResearchOptions())
    assert len(calls) == 1
    assert len(docs) == 1
    calls.clear()
    assert await provider.search([], ResearchOptions()) == []
    assert not calls
    provider = CrossrefSearchProvider(Settings(), httpx.MockTransport(lambda _: response([
        record(f"10.1234/example{i}") for i in range(20)
    ])))
    fake_clock(provider)
    assert len(await provider.search(["textbook resale"], ResearchOptions(max_sources=8))) == 8
    assert len(await provider.search(["textbook resale"], ResearchOptions(max_sources=2))) == 2


@pytest.mark.asyncio
async def test_shared_adapter_serializes_requests_for_crossref_public_concurrency_limit():
    current = peak = 0
    starts = []

    async def handler(request):
        nonlocal current, peak
        starts.append(clock.now)
        current += 1
        peak = max(peak, current)
        await asyncio.sleep(0.02)
        current -= 1
        return response([record()])

    provider = CrossrefSearchProvider(Settings(), httpx.MockTransport(handler))
    clock = fake_clock(provider)
    outputs = await asyncio.gather(
        provider.search(["textbook resale"], ResearchOptions(max_sources=1)),
        provider.search(["textbook exchange"], ResearchOptions(max_sources=1)),
    )
    assert peak == 1
    assert starts == [10.0, 15.0]
    assert clock.pauses == [5.0]
    assert all(len(docs) == 1 for docs in outputs)


@pytest.mark.asyncio
@pytest.mark.parametrize("items", [[], [record()], [record("10.1234/cars", title=["Used car resale"], abstract="Vehicle resale prices.")]])
async def test_primary_query_is_the_only_request_even_when_sources_are_underfilled(items):
    calls = []

    def handler(request):
        calls.append(request)
        return response(items)

    provider = CrossrefSearchProvider(Settings(), httpx.MockTransport(handler))
    documents = await provider.search(["used textbook resale", "textbook rental", "textbook affordability"],
                                      ResearchOptions(max_sources=8))
    assert len(calls) == 1
    assert calls[0].url.params["query.bibliographic"] == "used textbook resale"
    assert calls[0].url.params["rows"] == "12"
    assert len(documents) < 8


@pytest.mark.asyncio
@pytest.mark.parametrize("items", [
    [None], [{"DOI": "https://private-secret.example.org", "title": ["bad"], "abstract": "bad"}],
    [record("10.12/invalid-prefix")], [record("10.1234/white space")],
    [record(title="malformed title")], [record(abstract=[])],
    [record(abstract='<script>private-secret</script>')],
    [record(abstract='<!DOCTYPE x SYSTEM "https://private-secret.example.org"><x>&remote;</x>')],
    [record(abstract='<!ENTITY remote SYSTEM "file:///private-secret"><x>&remote;</x>')],
    [record(abstract="x" * 60001)],
])
async def test_malformed_records_and_entity_declarations_fail_without_leaking_payloads(items):
    provider = CrossrefSearchProvider(Settings(), httpx.MockTransport(lambda _: response(items)))
    with pytest.raises(IntelligenceError) as error:
        await provider.search(["query"], ResearchOptions())
    assert error.value.code == "PROVIDER_INVALID_RESPONSE"
    assert "private-secret" not in error.value.message


@pytest.mark.asyncio
async def test_empty_results_are_valid_and_missing_publisher_remains_labelled_metadata():
    provider = CrossrefSearchProvider(Settings(), httpx.MockTransport(lambda _: response([])))
    assert await provider.search(["query"], ResearchOptions()) == []
    provider = CrossrefSearchProvider(Settings(), httpx.MockTransport(lambda _: response([record(publisher=None)])))
    fake_clock(provider)
    docs = await provider.search(["textbook resale"], ResearchOptions())
    assert docs[0].source.publisher == "Crossref abstract metadata"


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [[], {}, {"status": "error", "message": {"items": []}},
                                      {"status": "ok", "message": {"items": {}}}])
async def test_bad_json_envelope_is_not_treated_as_empty_search(data):
    provider = CrossrefSearchProvider(Settings(), httpx.MockTransport(lambda _: httpx.Response(200, json=data)))
    with pytest.raises(IntelligenceError) as error:
        await provider.search(["query"], ResearchOptions())
    assert error.value.code == "PROVIDER_INVALID_RESPONSE"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code,retryable", [
    (403, "PROVIDER_ACCESS_DENIED", False), (429, "PROVIDER_RATE_LIMIT", True),
    (503, "PROVIDER_UNAVAILABLE", True), (302, "PROVIDER_REQUEST_REJECTED", False),
])
async def test_provider_errors_are_typed_sanitized_and_never_retried(status, code, retryable):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="private-secret provider diagnostic")

    provider = CrossrefSearchProvider(Settings(), httpx.MockTransport(handler))
    with pytest.raises(IntelligenceError) as error:
        await provider.search(["query"], ResearchOptions())
    assert error.value.code == code
    assert error.value.retryable is retryable
    assert "private-secret" not in error.value.message
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [
    httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1), headers={"content-type": "application/json"}),
    httpx.Response(200, content=b"{private-secret malformed", headers={"content-type": "application/json"}),
    httpx.Response(200, text="<html>private-secret</html>", headers={"content-type": "text/html"}),
])
async def test_body_size_invalid_json_and_wrong_content_type_fail_explicitly(reply):
    provider = CrossrefSearchProvider(Settings(), httpx.MockTransport(lambda _: reply))
    with pytest.raises(IntelligenceError) as error:
        await provider.search(["query"], ResearchOptions())
    assert error.value.code == "PROVIDER_INVALID_RESPONSE"


@pytest.mark.asyncio
async def test_timeout_is_typed_and_sanitized():
    def handler(request):
        raise httpx.ReadTimeout("private-secret diagnostic", request=request)

    provider = CrossrefSearchProvider(Settings(), httpx.MockTransport(handler))
    with pytest.raises(IntelligenceError) as error:
        await provider.search(["query"], ResearchOptions())
    assert error.value.code == "PROVIDER_TIMEOUT"
    assert error.value.retryable
    assert "private-secret" not in error.value.message


@pytest.mark.asyncio
async def test_abstract_scope_warning_reaches_saved_context_and_analysis_model():
    class RecordingModel(DemoLLM):
        def __init__(self):
            super().__init__(delay=0)
            self.payloads = {}

        async def generate_json(self, task, payload, schema):
            self.payloads[task] = payload
            return await super().generate_json(task, payload, schema)

    settings = Settings(app_mode="live", search_provider="crossref")
    model = RecordingModel()
    provider = CrossrefSearchProvider(settings, httpx.MockTransport(lambda _: response([record()])))
    fake_clock(provider)
    service = IntelligenceService(settings, model, provider, KeywordRetriever())
    idea = await service.extract("A startup helps students validate their product concepts.")
    analysis = await service.analyze("project_crossref", idea, ResearchOptions())
    assert analysis.context_draft.execution.search_provider == "crossref"
    assert any("current market data" in warning for warning in analysis.context_draft.warnings)
    assert any("requested language coverage" in gap for gap in analysis.context_draft.final_analysis.gaps)
    assert any("regional applicability" in gap for gap in model.payloads["analyze"]["gaps"])


@pytest.mark.asyncio
async def test_analysis_uses_only_executed_primary_query_and_records_incomplete_coverage():
    queries = ["used textbook resale", "textbook affordability", "textbook rental"]

    class RecordingModel(DemoLLM):
        def __init__(self):
            super().__init__(delay=0)
            self.payloads = {}

        async def generate_json(self, task, payload, schema):
            self.payloads[task] = payload
            if task == "queries":
                return {"queries": queries}
            return await super().generate_json(task, payload, schema)

    class RecordingCrossref(CrossrefSearchProvider):
        async def search(self, submitted_queries, options):
            self.submitted_queries = submitted_queries
            return await super().search(submitted_queries, options)

    calls = []

    def handler(request):
        calls.append(request)
        return response([record()])

    settings = Settings(app_mode="live", search_provider="crossref")
    model = RecordingModel()
    provider = RecordingCrossref(settings, httpx.MockTransport(handler))
    service = IntelligenceService(settings, model, provider, KeywordRetriever())
    idea = IdeaSpec(original_text="A marketplace to resell used textbooks.",
                    project_name="BookLoop", problem="Textbooks are costly.",
                    solution="A marketplace to resell used textbooks.", domain="Education")
    result = await service.analyze("project_primary", idea, ResearchOptions(max_sources=3))
    assert provider.submitted_queries == queries[:1]
    assert len(calls) == 1
    assert model.payloads["analyze"]["queries"] == queries[:1]
    assert len(result.context_draft.sources) == 1
    assert any("Other planned research queries" in warning for warning in result.warnings)
    assert any("1 of up to 3" in warning for warning in result.warnings)
    assert any("Only the primary Crossref query" in gap for gap in model.payloads["analyze"]["gaps"])


def test_crossref_is_an_explicit_keyless_live_selection():
    assert make_search_provider(Settings(app_mode="live", search_provider="crossref")).name == "crossref"
    assert make_search_provider(Settings(app_mode="demo", search_provider="crossref")).name == "fixture"


@pytest.mark.asyncio
async def test_crossref_request_starts_are_spaced_and_share_the_same_adapter_budget():
    starts = []

    def handler(request):
        starts.append(clock.now)
        return response([record()])

    provider = CrossrefSearchProvider(Settings(), httpx.MockTransport(handler))
    clock = fake_clock(provider)
    await provider.search(["textbook resale", "textbook exchange", "textbook cost"], ResearchOptions())
    await provider.search(["textbook pricing"], ResearchOptions(max_sources=1))
    await provider.search(["textbook rental"], ResearchOptions(max_sources=1))
    assert starts == [10.0, 15.0, 20.0]
    assert clock.pauses == [5.0, 5.0]


@pytest.mark.asyncio
async def test_slow_crossref_requests_do_not_add_an_unnecessary_pause():
    def handler(request):
        clock.now += 5.5
        return response([record()])

    provider = CrossrefSearchProvider(Settings(), httpx.MockTransport(handler))
    clock = fake_clock(provider)
    await provider.search(["textbook resale"], ResearchOptions())
    await provider.search(["textbook exchange"], ResearchOptions())
    assert clock.pauses == []


@pytest.mark.asyncio
@pytest.mark.parametrize("value,expected", [
    ("0", 0.0), ("60", 60.0), ("0.5", 0.5), ("3600", 3600.0),
    ("3601", None), ("-1", None), ("nan", None), ("inf", None),
    ("private-secret", None), ("Thu, 08 Oct 2026 13:00:00 GMT", None),
])
async def test_numeric_retry_after_is_bounded_sanitized_and_not_retried(value, expected):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, text="private-secret diagnostic", headers={"retry-after": value})

    provider = CrossrefSearchProvider(Settings(), httpx.MockTransport(handler))
    with pytest.raises(IntelligenceError) as error:
        await provider.search(["one", "two"], ResearchOptions())
    assert error.value.code == "PROVIDER_RATE_LIMIT"
    assert error.value.details == ({"retry_after_seconds": expected} if expected is not None else {})
    assert "private-secret" not in str(error.value.details)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("headers,expected", [
    ({"x-rate-limit-limit": "1", "x-rate-limit-interval": "1s"},
     {"rate_limit_requests": 1, "rate_limit_interval_seconds": 1.0}),
    ({"x-rate-limit-limit": "5", "x-rate-limit-interval": "500ms", "retry-after": "60"},
     {"rate_limit_requests": 5, "rate_limit_interval_seconds": 0.5, "retry_after_seconds": 60.0}),
    ({"x-rate-limit-limit": "100000", "x-rate-limit-interval": "1h"},
     {"rate_limit_requests": 100000, "rate_limit_interval_seconds": 3600.0}),
    ({"x-rate-limit-limit": "-1", "x-rate-limit-interval": "2h"}, {}),
    ({"x-rate-limit-limit": "100001", "x-rate-limit-interval": "nan"}, {}),
    ({"x-rate-limit-limit": "private-secret", "x-rate-limit-interval": "private-secret", "retry-after": "private-secret"}, {}),
])
async def test_rate_limit_headers_expose_only_bounded_numeric_diagnostics(headers, expected):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers=headers, text="private-secret diagnostic")

    provider = CrossrefSearchProvider(Settings(), httpx.MockTransport(handler))
    with pytest.raises(IntelligenceError) as error:
        await provider.search(["textbook resale", "textbook rental"], ResearchOptions())
    assert error.value.code == "PROVIDER_RATE_LIMIT"
    assert error.value.details == expected
    assert "private-secret" not in str(error.value.details)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_topic_filter_rejects_vehicle_and_generic_college_papers_and_ranks_specific_title_first():
    rows = [
        record("10.1234/cars", title=["The resale market for used cars"],
               abstract="Car consumers buy and resell vehicles; this is not evidence about education."),
        record("10.1234/college", title=["College students and digital platforms in India"],
               abstract="Students use online learning platforms across college campuses."),
        record("10.1234/background", title=["College textbook adoption"],
               abstract="The study discusses textbook prices and affordability for students."),
        record("10.1234/specific", title=["Textbook resale and affordability"],
               abstract="A campus used textbook exchange was studied; payment intent remains unknown."),
    ]
    provider = CrossrefSearchProvider(Settings(), httpx.MockTransport(lambda _: response(rows)))
    documents = await provider.search(["used textbook resale"], ResearchOptions(max_sources=3, region="India"))
    assert [document.source.url for document in documents] == ["https://doi.org/10.1234/specific", "https://doi.org/10.1234/background"]
    assert documents[0].content == "A campus used textbook exchange was studied; payment intent remains unknown."
    assert len(documents) < 3  # Irrelevant items never fill the requested budget.


@pytest.mark.asyncio
async def test_valid_metadata_with_no_relevant_topic_matches_returns_empty_instead_of_invalid_response():
    provider = CrossrefSearchProvider(Settings(), httpx.MockTransport(lambda _: response([
        record("10.1234/cars", title=["Used car resale prices"], abstract="Vehicle resale was studied."),
        record("10.1234/solar", title=["Solar power plant efficiency"], abstract="Power plant technology was evaluated."),
    ])))
    assert await provider.search(["used textbook resale"], ResearchOptions()) == []


@pytest.mark.asyncio
async def test_reviewed_solution_rejects_a_vehicle_paper_even_if_a_bad_planner_query_matches_it():
    class WrongTopicPlanner(DemoLLM):
        def __init__(self):
            super().__init__(delay=0)
            self.payloads = {}

        async def generate_json(self, task, payload, schema):
            self.payloads[task] = payload
            if task == "queries":
                return {"queries": ["car resale"]}
            return await super().generate_json(task, payload, schema)

    settings = Settings(app_mode="live", search_provider="crossref")
    model = WrongTopicPlanner()
    row = record("10.1234/cars", title=["Used car resale prices"], abstract="Vehicle resale prices were studied.")
    provider = CrossrefSearchProvider(settings, httpx.MockTransport(lambda _: response([row])))
    idea = IdeaSpec(original_text="A campus marketplace for students to buy and sell used textbooks.",
                    project_name="CampusLoop", problem="Students need a way to exchange used textbooks.",
                    solution="A campus marketplace for students to buy and sell used textbooks.",
                    target_users=["College students in India"], domain="Education")
    service = IntelligenceService(settings, model, provider, KeywordRetriever())
    result = await service.analyze("project_wrong_topic", idea, ResearchOptions(max_sources=3))
    assert model.payloads["queries"]["search_provider"] == "crossref"
    context = result.context_draft
    assert context.sources == context.evidence == []
    assert all(claim.kind != "supported" for section in context.final_analysis.sections for claim in section.claims)
    assert any("core topic" in warning for warning in context.warnings)
    assert any("0 of up to 3" in warning for warning in context.warnings)
    assert any("not prove" in gap for gap in context.final_analysis.gaps)
    assert any("No research sources" in gap for gap in context.final_analysis.gaps)


def test_topic_matching_handles_plural_textbooks_and_preserves_original_abstracts():
    document = CrossrefSearchProvider._document(record(title=["Text-book affordability"],
                                                       abstract="Text-books have a purchase cost. Exact wording stays."))
    assert CrossrefSearchProvider._relevance_score("textbooks pricing", document) > 0
    assert document.content == "Text-books have a purchase cost. Exact wording stays."
    assert CrossrefSearchProvider._relevance_score("generic college students India", document) == 0


def test_widely_used_does_not_receive_a_secondhand_or_resale_matching_bonus():
    background = CrossrefSearchProvider._document(record(title=["OA to widely-used textbook"], abstract="Open access to educational materials."))
    specific = CrossrefSearchProvider._document(record(title=["Textbook resale"], abstract="A used textbook exchange was described."))
    assert CrossrefSearchProvider._relevance_score("used textbook resale", specific) > CrossrefSearchProvider._relevance_score("used textbook resale", background)
