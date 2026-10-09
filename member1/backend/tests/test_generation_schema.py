import copy
import json

import httpx
import pytest

from app.modules.intelligence.contracts import IdeaSpec
from app.modules.intelligence.grounding import ExtractedIdea
from app.modules.intelligence.providers import (
    OllamaLanguageModel, OpenAICompatibleLanguageModel, _generation_schema,
)
from app.modules.intelligence.settings import Settings


ORIGIN_FIELDS = {"project_name", "problem", "solution", "target_users", "domain", "assumptions", "constraints"}


def test_private_extraction_schema_requires_every_field_and_fixed_provenance_without_mutating_public_schema():
    public = IdeaSpec.model_json_schema()
    original = copy.deepcopy(public)
    prepared = _generation_schema("extract", public)
    assert prepared is not public
    assert public == original == IdeaSpec.model_json_schema()
    assert set(prepared["required"]) == set(public["properties"])
    origins = prepared["properties"]["field_origins"]
    assert set(origins["properties"]) == set(origins["required"]) == ORIGIN_FIELDS
    assert origins["additionalProperties"] is False
    assert "default" not in origins
    assert all(field["enum"] == ["provided", "inferred", "unknown"] for field in origins["properties"].values())
    assert "field_origins" not in public["required"]
    idea = IdeaSpec(original_text="A proposed student service", project_name="Student service",
                    problem="Unknown problem", solution="A proposed service", domain="Unknown domain")
    assert idea.constraints == []
    assert idea.field_origins == {}  # Existing public client defaults remain valid.


def test_live_extraction_grammar_requires_proof_map_and_every_quote_list():
    public = copy.deepcopy(IdeaSpec.model_json_schema())
    schema = _generation_schema("extract", ExtractedIdea.model_json_schema())
    assert set(schema["required"]) == {*public["properties"], "source_quotes"}
    quote_schema = schema["$defs"]["ExtractionQuotes"]
    assert set(quote_schema["required"]) == set(quote_schema["properties"]) == ORIGIN_FIELDS
    assert quote_schema["additionalProperties"] is False
    origins = schema["properties"]["field_origins"]
    assert set(origins["required"]) == set(origins["properties"]) == ORIGIN_FIELDS
    assert origins["additionalProperties"] is False
    assert IdeaSpec.model_json_schema() == public
    # A legacy response is still accepted for conservative grounding rather
    # than trusting its unquoted values or exposing the internal map publicly.
    legacy = IdeaSpec(original_text="A proposed service for factory owners", project_name="Service",
                      problem="Unknown", solution="A proposed service", domain="Unknown")
    assert ExtractedIdea(**legacy.model_dump()).source_quotes.target_users == []


@pytest.mark.parametrize("task,schema", [
    ("extract", {"type": "object", "properties": {"project_name": {"type": "string"}}}),
    ("analyze", IdeaSpec.model_json_schema()),
    ("extract", {"type": "object", "properties": {key: {} for key in {"original_text", "field_origins", *ORIGIN_FIELDS}}}),
])
def test_unrelated_task_or_partial_mock_schema_is_not_strengthened(task, schema):
    assert _generation_schema(task, schema) == schema


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name", ["ollama", "openai_compatible"])
@pytest.mark.parametrize("schema_type", [IdeaSpec, ExtractedIdea])
async def test_live_adapters_use_the_same_prepared_extraction_schema_in_prompt_and_format(provider_name, schema_type):
    original = schema_type.model_json_schema()
    prepared = _generation_schema("extract", original)
    calls = []

    def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        assert json.dumps(prepared, ensure_ascii=False) in body["messages"][0]["content"]
        if provider_name == "ollama":
            assert body["format"] == prepared
            return httpx.Response(200, json={"message": {"content": '{"answer":"ready"}'}, "done": True})
        assert body["response_format"]["json_schema"]["schema"] == prepared
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"answer":"ready"}'}, "finish_reason": "stop"}]})

    settings = Settings(app_mode="live", llm_provider=provider_name, llm_api_key="private-test-key", llm_model="local-test")
    provider_type = OllamaLanguageModel if provider_name == "ollama" else OpenAICompatibleLanguageModel
    assert await provider_type(settings, httpx.MockTransport(handler)).generate_json("extract", {}, original) == {"answer": "ready"}
    assert original == schema_type.model_json_schema()
    assert len(calls) == 1
