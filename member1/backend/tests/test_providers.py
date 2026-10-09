import json
from dataclasses import replace

import httpx
import pytest

from app.modules.intelligence.contracts import ResearchOptions
from app.modules.intelligence.errors import IntelligenceError
from app.modules.intelligence.providers import (
    DemoSearchProvider,
    OllamaLanguageModel,
    OpenAICompatibleLanguageModel,
    TavilySearchProvider,
    WikipediaSearchProvider,
    make_language_model,
    make_search_provider,
)
from app.modules.intelligence.settings import Settings

SCHEMA = {"type": "object", "properties": {"project_name": {"type": "string"}}, "required": ["project_name"]}
LIVE_SETTINGS = Settings(app_mode="live", llm_api_key="private-test-secret", llm_model="account-selected-model")


@pytest.mark.asyncio
async def test_openai_schema_request_and_json_response():
    def handler(request):
        assert str(request.url) == "https://api.openai.com/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer private-test-secret"
        body = json.loads(request.content)
        assert body["model"] == "account-selected-model"
        assert body["response_format"]["json_schema"]["schema"] == SCHEMA
        assert body["response_format"]["json_schema"]["strict"] is False
        assert body["messages"][0]["role"] == "system"
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"project_name":"Demo"}'}, "finish_reason": "stop"}]})

    provider = OpenAICompatibleLanguageModel(LIVE_SETTINGS, httpx.MockTransport(handler))
    assert await provider.generate_json("extract", {"idea_text": "An idea"}, SCHEMA) == {"project_name": "Demo"}


@pytest.mark.asyncio
async def test_openai_json_mode_can_be_selected_explicitly():
    def handler(request):
        assert json.loads(request.content)["response_format"] == {"type": "json_object"}
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    settings = replace(LIVE_SETTINGS, llm_response_format="json_object")
    result = await OpenAICompatibleLanguageModel(settings, httpx.MockTransport(handler)).generate_json("extract", {}, SCHEMA)
    assert result == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("data,code", [
    ({"choices": []}, "PROVIDER_INVALID_RESPONSE"),
    ({"choices": [{"message": {"content": "not-json"}}]}, "MODEL_INVALID_JSON"),
    ({"choices": [{"message": {"content": "[]"}}]}, "MODEL_INVALID_JSON"),
    ({"choices": [{"message": {"refusal": "private provider text"}}]}, "MODEL_REFUSAL"),
    ({"choices": [{"message": {"content": "{}"}, "finish_reason": "length"}]}, "MODEL_TRUNCATED"),
])
async def test_model_rejects_malformed_or_unfinished_outputs(data, code):
    provider = OpenAICompatibleLanguageModel(LIVE_SETTINGS, httpx.MockTransport(lambda _: httpx.Response(200, json=data)))
    with pytest.raises(IntelligenceError) as error:
        await provider.generate_json("extract", {}, SCHEMA)
    assert error.value.code == code
    assert "private provider text" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code,retryable", [
    (401, "PROVIDER_AUTHENTICATION", False),
    (403, "PROVIDER_ACCESS_DENIED", False),
    (429, "PROVIDER_RATE_LIMIT", True),
    (500, "PROVIDER_UNAVAILABLE", True),
    (400, "PROVIDER_REQUEST_REJECTED", False),
])
async def test_http_errors_are_typed_and_never_include_provider_secrets(status, code, retryable):
    provider = OpenAICompatibleLanguageModel(
        LIVE_SETTINGS,
        httpx.MockTransport(lambda _: httpx.Response(status, text="private-test-secret provider diagnostic")),
    )
    with pytest.raises(IntelligenceError) as error:
        await provider.generate_json("extract", {}, SCHEMA)
    assert error.value.code == code
    assert error.value.retryable is retryable
    assert "private-test-secret" not in str(error.value)
    assert not error.value.details


@pytest.mark.asyncio
async def test_timeout_is_sanitized():
    def handler(request):
        raise httpx.ReadTimeout("private-test-secret timeout", request=request)

    with pytest.raises(IntelligenceError) as error:
        await OpenAICompatibleLanguageModel(LIVE_SETTINGS, httpx.MockTransport(handler)).generate_json("extract", {}, SCHEMA)
    assert error.value.code == "PROVIDER_TIMEOUT"
    assert error.value.retryable
    assert "private-test-secret" not in str(error.value)


@pytest.mark.asyncio
async def test_ollama_sends_schema_without_streaming_or_api_key():
    def handler(request):
        assert str(request.url) == "http://127.0.0.1:11434/api/chat"
        assert "authorization" not in request.headers
        body = json.loads(request.content)
        assert body["format"] == SCHEMA
        assert body["stream"] is False
        assert body["model"] == "installed-local-model"
        return httpx.Response(200, json={"message": {"content": '{"project_name":"Local"}'}, "done": True})

    settings = replace(LIVE_SETTINGS, llm_provider="ollama", llm_api_key="", llm_model="installed-local-model")
    result = await OllamaLanguageModel(settings, httpx.MockTransport(handler)).generate_json("extract", {}, SCHEMA)
    assert result["project_name"] == "Local"


@pytest.mark.parametrize("change", [
    {"llm_api_key": ""}, {"llm_model": ""}, {"llm_base_url": "https://user:password@example.org/v1"},
    {"llm_base_url": "javascript:bad"}, {"llm_base_url": "https://api.example.org?api_key=secret"},
])
def test_live_llm_requires_explicit_valid_configuration(change):
    with pytest.raises(IntelligenceError) as error:
        OpenAICompatibleLanguageModel(replace(LIVE_SETTINGS, **change))
    assert error.value.code == "PROVIDER_CONFIGURATION"


def test_search_selection_never_requires_keys_in_demo():
    assert make_search_provider(Settings()).name == "fixture"
    assert make_search_provider(Settings(app_mode="live", search_provider="wikipedia")).name == "wikipedia"
    with pytest.raises(IntelligenceError):
        make_search_provider(Settings(app_mode="live", search_provider="tavily"))
    with pytest.raises(IntelligenceError):
        make_language_model(Settings(app_mode="live"))


@pytest.mark.asyncio
async def test_fixture_documents_are_explicitly_synthetic_and_bounded():
    search = DemoSearchProvider()
    first = await search.search(["student startup"], ResearchOptions(max_sources=3))
    second = await search.search(["another startup"], ResearchOptions(max_sources=3))
    assert len(first) == 3
    assert [doc.source.source_id for doc in first] == [doc.source.source_id for doc in second]
    assert all(doc.source.kind == "fixture" for doc in first)
    assert all(doc.source.url.startswith("https://example.org/") for doc in first)
    assert all("synthetic" in doc.content.lower() for doc in first)
    assert "12 simulated" in first[0].content
    assert await search.search([], ResearchOptions()) == []


@pytest.mark.asyncio
async def test_tavily_deduplicates_urls_and_caps_total_sources():
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["include_raw_content"] == "text"
        assert body["language"] == "en"
        assert request.headers["Authorization"] == "Bearer private-tavily-key"
        rows = [
            {"title": "Source A", "url": "https://example.com/a/#fragment", "content": "snippet", "raw_content": "full source A content"},
            {"title": "Duplicate A", "url": "https://example.com/a", "content": "duplicate snippet"},
            {"title": "Source B", "url": "https://example.com/b", "content": "source B content"},
            {"title": "Source C", "url": "https://example.com/c", "content": "source C content"},
        ]
        return httpx.Response(200, json={"results": rows})

    provider = TavilySearchProvider(Settings(tavily_api_key="private-tavily-key"), httpx.MockTransport(handler))
    docs = await provider.search(["query 1", "query 2"], ResearchOptions(max_sources=2))
    assert len(docs) == 2
    assert len(requests) == 1
    assert docs[0].content == "full source A content"
    assert docs[0].source.url == "https://example.com/a"
    assert docs[0].source.source_id != docs[1].source.source_id
    assert all(doc.source.kind == "web" for doc in docs)


@pytest.mark.asyncio
async def test_tavily_empty_results_query_bounds_and_invalid_urls():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"results": [{"title": "Bad", "url": "javascript:alert(1)", "content": "ignore"}]})

    provider = TavilySearchProvider(Settings(tavily_api_key="key"), httpx.MockTransport(handler))
    assert await provider.search(["query " + str(i) for i in range(20)], ResearchOptions()) == []
    assert len(requests) == 6
    assert await provider.search([], ResearchOptions()) == []
    assert len(requests) == 6


@pytest.mark.asyncio
async def test_tavily_invalid_result_shape_is_not_treated_as_no_results():
    provider = TavilySearchProvider(Settings(tavily_api_key="key"), httpx.MockTransport(lambda _: httpx.Response(200, json={"results": {}})))
    with pytest.raises(IntelligenceError) as error:
        await provider.search(["startup"], ResearchOptions())
    assert error.value.code == "PROVIDER_INVALID_RESPONSE"


@pytest.mark.asyncio
async def test_wikipedia_fetches_introductions_and_preserves_linkage():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.host == "en.wikipedia.org"
        if request.url.params.get("list") == "search":
            return httpx.Response(200, json={"query": {"search": [
                {"pageid": 10, "title": "Startup company"}, {"pageid": 20, "title": "Customer development"},
            ]}})
        assert request.url.params["pageids"] == "10|20"
        assert request.url.params["explaintext"] == "1"
        assert request.url.params["exintro"] == "1"
        return httpx.Response(200, json={"query": {"pages": [
            {"pageid": 10, "title": "Startup company", "extract": "A startup background introduction."},
            {"pageid": 20, "title": "Customer development", "extract": "An introduction to customer discovery."},
            {"pageid": 999, "title": "Injected unrelated source", "extract": "Should not be included."},
        ]}})

    docs = await WikipediaSearchProvider(Settings(), httpx.MockTransport(handler)).search(["startup company"], ResearchOptions(max_sources=2))
    assert len(calls) == 2
    assert len(docs) == 2
    assert docs[0].source.url == "https://en.wikipedia.org/wiki/Startup_company"
    assert all(doc.source.publisher == "Wikipedia" for doc in docs)
    assert all(doc.source.retrieved_at.tzinfo for doc in docs)


@pytest.mark.asyncio
async def test_wikipedia_empty_search_makes_no_extract_request():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"query": {"search": []}})

    docs = await WikipediaSearchProvider(Settings(), httpx.MockTransport(handler)).search(["unlikely query"], ResearchOptions())
    assert docs == []
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_keyless_wikipedia_access_denial_is_not_reported_as_bad_credentials():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(403, text="Blocked private network diagnostic")

    with pytest.raises(IntelligenceError) as error:
        await WikipediaSearchProvider(Settings(), httpx.MockTransport(handler)).search(["startup"], ResearchOptions())
    assert error.value.code == "PROVIDER_ACCESS_DENIED"
    assert "network and provider access" in error.value.message
    assert "credentials" not in error.value.message
    assert "private network diagnostic" not in error.value.message
    assert error.value.retryable is False
    assert len(calls) == 1
