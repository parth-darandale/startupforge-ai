"""Dedicated model client for startup presentation and report writing."""

from __future__ import annotations

from dataclasses import replace
import math
from typing import Any

from .errors import IntelligenceError
from .providers import LanguageModel, make_language_model
from .settings import Settings


class OutputAuthor:
    """Keep export writing settings independent of the research/debate model."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._model: LanguageModel | None = None

    @property
    def enabled(self) -> bool:
        return self.settings.app_mode == "live"

    @property
    def model_id(self) -> str:
        return self.settings.output_llm_model or self.settings.llm_model

    async def generate_json(self, task: str, payload: dict[str, Any], schema: dict[str, Any]) -> dict:
        if not self.enabled:
            raise IntelligenceError(
                "OUTPUT_MODEL_DISABLED", "AI output writing is available in live mode.", status_code=503
            )
        if self._model is None:
            timeout = self.settings.output_timeout
            if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                    or not math.isfinite(timeout) or timeout <= 0):
                raise IntelligenceError(
                    "PROVIDER_CONFIGURATION", "OUTPUT_TIMEOUT must be a positive finite number.", status_code=503
                )
            writing_settings = replace(
                self.settings,
                llm_model=self.model_id,
                provider_timeout=timeout,
                ollama_num_predict=self.settings.output_num_predict,
                ollama_temperature=self.settings.output_temperature,
                ollama_think=(False if self.model_id.lower().startswith("qwen3.5")
                              else self.settings.ollama_think),
            )
            self._model = make_language_model(writing_settings)
        return await self._model.generate_json(task, payload, schema)
