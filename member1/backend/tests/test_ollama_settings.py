import json
from dataclasses import replace

import httpx
import pytest

from app.modules.intelligence import settings as settings_module
from app.modules.intelligence.errors import IntelligenceError
from app.modules.intelligence.providers import OllamaLanguageModel
from app.modules.intelligence.settings import Settings


LOCAL = Settings(app_mode="live", llm_provider="ollama", llm_model="local-model")
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}}


@pytest.mark.asyncio
@pytest.mark.parametrize("think", [None, False, True])
async def test_local_generation_has_explicit_bounds_and_optional_thinking(think):
    def handler(request):
        body = json.loads(request.content)
        assert body["options"] == {"num_ctx": 8192, "num_predict": 3072, "temperature": 0.15}
        assert body["keep_alive"] == "0"
        assert body["format"] == SCHEMA
        assert body["stream"] is False
        assert "authorization" not in request.headers
        if think is None:
            assert "think" not in body
        else:
            assert body["think"] is think
        return httpx.Response(200, json={
            "message": {"content": '{"answer":"final answer"}', "thinking": "provider reasoning"},
            "done": True, "done_reason": "stop",
        })

    settings = replace(LOCAL, ollama_num_ctx=8192, ollama_num_predict=3072,
                       ollama_temperature=0.15, ollama_keep_alive="0", ollama_think=think)
    provider = OllamaLanguageModel(settings, httpx.MockTransport(handler))
    assert await provider.generate_json("extract", {}, SCHEMA) == {"answer": "final answer"}


@pytest.mark.asyncio
@pytest.mark.parametrize("completion", [
    {"done": False}, {"done": True, "done_reason": "length"},
])
async def test_truncated_local_generation_fails_even_when_content_is_valid_json(completion):
    data = {"message": {"content": '{"answer":"looks complete"}'}, **completion}
    provider = OllamaLanguageModel(LOCAL, httpx.MockTransport(lambda _: httpx.Response(200, json=data)))
    with pytest.raises(IntelligenceError) as error:
        await provider.generate_json("extract", {}, SCHEMA)
    assert error.value.code == "MODEL_TRUNCATED"


@pytest.mark.parametrize("change", [
    {"ollama_num_ctx": 0}, {"ollama_num_ctx": 131073}, {"ollama_num_ctx": True},
    {"ollama_num_ctx": "8192"}, {"ollama_num_predict": -1}, {"ollama_num_predict": 16385},
    {"ollama_num_predict": 128.5}, {"ollama_temperature": float("nan")},
    {"ollama_temperature": float("inf")}, {"ollama_temperature": 2.1},
    {"ollama_temperature": True}, {"ollama_think": "false"},
    {"ollama_keep_alive": "-1"}, {"ollama_keep_alive": "2h"},
    {"ollama_keep_alive": "300"}, {"ollama_keep_alive": "private-secret"},
    {"ollama_context_bytes_per_token": 0}, {"ollama_context_bytes_per_token": 4.1},
    {"ollama_context_bytes_per_token": float("nan")}, {"ollama_context_bytes_per_token": float("inf")},
    {"ollama_context_bytes_per_token": True},
])
def test_invalid_local_controls_are_typed_and_sanitized(change):
    with pytest.raises(IntelligenceError) as error:
        OllamaLanguageModel(replace(LOCAL, **change))
    assert error.value.code == "PROVIDER_CONFIGURATION"
    assert error.value.status_code == 503
    assert "private-secret" not in error.value.message


@pytest.mark.parametrize("duration", ["0", "500ms", "30s", "5m", "1h", "0.5h"])
def test_local_model_residency_accepts_bounded_durations(duration):
    OllamaLanguageModel(replace(LOCAL, ollama_keep_alive=duration))


@pytest.fixture
def isolated_env(monkeypatch):
    monkeypatch.setattr(settings_module, "load_dotenv", lambda *_args, **_kwargs: None)
    for name in ["OLLAMA_NUM_CTX", "OLLAMA_NUM_PREDICT", "OLLAMA_TEMPERATURE", "OLLAMA_KEEP_ALIVE", "OLLAMA_THINK", "OLLAMA_CONTEXT_BYTES_PER_TOKEN"]:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_local_controls_are_read_from_environment(isolated_env):
    isolated_env.setenv("OLLAMA_NUM_CTX", "8192")
    isolated_env.setenv("OLLAMA_NUM_PREDICT", "3072")
    isolated_env.setenv("OLLAMA_TEMPERATURE", "0.1")
    isolated_env.setenv("OLLAMA_KEEP_ALIVE", "30s")
    isolated_env.setenv("OLLAMA_THINK", "false")
    isolated_env.setenv("OLLAMA_CONTEXT_BYTES_PER_TOKEN", "2.5")
    settings = Settings.from_env()
    assert settings.ollama_num_ctx == 8192
    assert settings.ollama_num_predict == 3072
    assert settings.ollama_temperature == 0.1
    assert settings.ollama_keep_alive == "30s"
    assert settings.ollama_think is False
    assert settings.ollama_context_bytes_per_token == 2.5


@pytest.mark.parametrize("name", ["OLLAMA_NUM_CTX", "OLLAMA_NUM_PREDICT", "OLLAMA_TEMPERATURE", "OLLAMA_CONTEXT_BYTES_PER_TOKEN", "PROVIDER_TIMEOUT", "DEMO_DELAY"])
def test_invalid_numeric_environment_configuration_does_not_leak_value(isolated_env, name):
    isolated_env.setenv(name, "private-secret-invalid-value")
    with pytest.raises(IntelligenceError) as error:
        Settings.from_env()
    assert error.value.code == "PROVIDER_CONFIGURATION"
    assert name in error.value.message
    assert "private-secret" not in error.value.message


def test_invalid_thinking_environment_configuration_fails_explicitly(isolated_env):
    isolated_env.setenv("OLLAMA_THINK", "private-secret-invalid-value")
    with pytest.raises(IntelligenceError) as error:
        Settings.from_env()
    assert error.value.code == "PROVIDER_CONFIGURATION"
    assert "private-secret" not in error.value.message


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"idea_text": "private-idea-" * 1500},
    {"idea_text": "गोपनीय" * 2000},
    {"history": [{"message": "private-debate-" * 1000}]},
    {"evidence": [{"excerpt": "private-evidence-" * 1500}]},
])
async def test_oversized_local_prompts_fail_before_http_without_clipping_any_input(payload):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"message": {"content": "{}"}, "done": True})

    provider = OllamaLanguageModel(LOCAL, httpx.MockTransport(handler))
    with pytest.raises(IntelligenceError) as error:
        await provider.generate_json("extract", payload, SCHEMA)
    assert error.value.code == "MODEL_CONTEXT_LIMIT"
    assert error.value.status_code == 422
    assert error.value.details["estimated"] is True
    assert error.value.details["output_reserved_tokens"] == 2048
    assert error.value.details["context_tokens"] == 4096
    assert not calls
    assert "private-" not in str(error.value.details)
    assert "गोपनीय" not in str(error.value.details)


@pytest.mark.asyncio
async def test_json_schema_itself_consumes_the_prompt_budget_before_http():
    schema = {"type": "object", "description": "schema description " * 1000}
    provider = OllamaLanguageModel(LOCAL, httpx.MockTransport(lambda _: pytest.fail("Oversized schema must not be sent")))
    with pytest.raises(IntelligenceError) as error:
        await provider.generate_json("extract", {}, schema)
    assert error.value.code == "MODEL_CONTEXT_LIMIT"


@pytest.mark.asyncio
async def test_fitting_prompt_preserves_evidence_and_unicode_with_a_larger_context():
    excerpt = "Evidence passage with exact Unicode: ₹ गुंतवणूक. " * 40
    payload = {"evidence": [{"evidence_id": "ev_original", "excerpt": excerpt}]}

    def handler(request):
        body = json.loads(request.content)
        submitted = json.loads(body["messages"][1]["content"])["untrusted_input"]
        assert submitted == payload
        assert body["options"]["num_ctx"] == 8192
        return httpx.Response(200, json={"message": {"content": '{"answer":"safe"}'}, "done": True,
                                        "prompt_eval_count": 1200, "eval_count": 20})

    provider = OllamaLanguageModel(replace(LOCAL, ollama_num_ctx=8192), httpx.MockTransport(handler))
    assert await provider.generate_json("analyze", payload, SCHEMA) == {"answer": "safe"}


@pytest.mark.asyncio
async def test_reported_context_exhaustion_rejects_an_otherwise_valid_response():
    data = {"message": {"content": '{"answer":"looks valid"}'}, "done": True,
            "prompt_eval_count": 3900, "eval_count": 90}
    provider = OllamaLanguageModel(LOCAL, httpx.MockTransport(lambda _: httpx.Response(200, json=data)))
    with pytest.raises(IntelligenceError) as error:
        await provider.generate_json("extract", {}, SCHEMA)
    assert error.value.code == "MODEL_CONTEXT_LIMIT"
    assert error.value.details["estimated"] is False
    assert error.value.details["prompt_tokens"] == 3900


@pytest.mark.asyncio
async def test_extremely_conservative_ratio_fails_with_typed_budget_error():
    provider = OllamaLanguageModel(replace(LOCAL, ollama_context_bytes_per_token=5e-324))
    with pytest.raises(IntelligenceError) as error:
        await provider.generate_json("extract", {}, SCHEMA)
    assert error.value.code == "MODEL_CONTEXT_LIMIT"
