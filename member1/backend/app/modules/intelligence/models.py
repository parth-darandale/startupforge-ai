"""Allowlisted, per-job model choices; never change the shared configuration."""
from dataclasses import replace

import httpx

from .errors import IntelligenceError
from .providers import _provider_url, _request_json
from .settings import Settings


QWEN_MODEL = "qwen3:4b-instruct-2507-q4_K_M"
FIN_MODEL = "startupforge-fin-r1"
LOCAL_MODELS = (
    {"model_id": QWEN_MODEL, "label": "Qwen", "description": "General research and debate.", "experimental": False},
    {"model_id": FIN_MODEL, "label": "Fin-R1", "description": "Compare finance-oriented responses.", "experimental": True},
)


class ModelSelection:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport

    def validate(self, model_id: str | None) -> None:
        if model_id is None:
            return
        allowed = ({item["model_id"] for item in LOCAL_MODELS} if self.settings.llm_provider == "ollama"
                   and self.settings.app_mode == "live" else set())
        if self.settings.app_mode == "live" and self.settings.llm_model:
            allowed.add(self.settings.llm_model)
        if model_id not in allowed:
            raise IntelligenceError("MODEL_NOT_ALLOWED", "Choose a model listed in the app's model selector.", 422)

    async def _installed_models(self) -> set[str]:
        url = _provider_url(self.settings.ollama_base_url, "/api/tags")
        async with httpx.AsyncClient(timeout=5, transport=self.transport, follow_redirects=False) as client:
            data = await _request_json(client, "Ollama", "GET", url)
        models = data.get("models")
        if not isinstance(models, list):
            raise IntelligenceError("PROVIDER_INVALID_RESPONSE", "Ollama returned an invalid model list.", 502)
        result = set()
        for item in models:
            if isinstance(item, dict):
                for value in (item.get("name"), item.get("model")):
                    if isinstance(value, str):
                        result.add(value)
                        if value.endswith(":latest"):
                            result.add(value[:-7])
        return result

    async def catalog(self) -> dict:
        if self.settings.app_mode == "demo":
            return {"mode": "demo", "default_model_id": None, "models": []}
        if self.settings.llm_provider != "ollama":
            model = self.settings.llm_model
            choices = [{"model_id": model, "label": model, "description": "Configured model service.",
                        "experimental": False, "available": True, "unavailable_reason": None}] if model else []
            return {"mode": "live", "default_model_id": model or None, "models": choices}
        reason = "Not installed in the local model service."
        try:
            installed = await self._installed_models()
        except IntelligenceError:
            installed = set()
            reason = "The local model service is unavailable."
        choices = [{**item, "available": item["model_id"] in installed,
                    "unavailable_reason": None if item["model_id"] in installed else reason}
                   for item in LOCAL_MODELS]
        return {"mode": "live", "default_model_id": self.settings.llm_model or None, "models": choices}

    async def resolve(self, model_id: str | None) -> Settings:
        self.validate(model_id)
        if model_id is None:
            return self.settings
        if self.settings.llm_provider == "ollama":
            installed = await self._installed_models()
            if model_id not in installed:
                raise IntelligenceError("MODEL_NOT_AVAILABLE", "The selected model is not installed in the local model service.",
                                        503, retryable=True, details={"model_id": model_id})
        return replace(self.settings, llm_model=model_id)
