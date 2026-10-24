"""Lazy lifetime for the installed, separate AutoPresent slide model."""
import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
import sys

import httpx

from .errors import IntelligenceError
from .local_model_lock import local_model_lock
from .models import LOCAL_MODELS
from .settings import BACKEND_ROOT, Settings

LOGGER = logging.getLogger(__name__)


class AutoPresentRuntime:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def installed(self) -> bool:
        manifest = BACKEND_ROOT / "models" / "autopresent" / "manifest.json"
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not data:
                return False
            paths = (
                data.get("assets", {}).get("base", {}).get("path", "models/autopresent/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"),
                data.get("converted_adapter", {}).get("path", "models/autopresent/autopresent-detailed-f16.gguf"),
                ".runtime/autopresent/llama.cpp-b11838/llama-server.exe",
            )
            root = BACKEND_ROOT.resolve()
            return all(isinstance(value, str) and value and
                       (path := (root / value).resolve()).is_relative_to(root) and path.is_file()
                       for value in paths)
        except (OSError, ValueError, TypeError, AttributeError):
            return False

    async def _command(self, action: str) -> None:
        script = BACKEND_ROOT / "scripts" / "setup_autopresent.py"
        kwargs = {"creationflags": 0x08000000} if os.name == "nt" else {}
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(script), action, cwd=BACKEND_ROOT,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kwargs)
        try:
            out, err = await asyncio.wait_for(process.communicate(), timeout=240)
        except (TimeoutError, asyncio.CancelledError):
            process.kill()
            await process.wait()
            raise
        if process.returncode:
            LOGGER.error("AutoPresent runtime %s failed: %s", action, (out + err).decode("utf-8", "replace")[-3000:])
            raise IntelligenceError("AUTOPRESENT_UNAVAILABLE",
                                    "AutoPresent could not start. Check the local AutoPresent runtime logs.", 503)

    @asynccontextmanager
    async def session(self):
        # Keep the lease through unloading, the complete deck and shutdown.
        # Direct unload HTTP requests below deliberately bypass the Ollama
        # provider, which takes this same non-reentrant lease for inference.
        async with local_model_lock():
            if not self.installed:
                raise IntelligenceError("AUTOPRESENT_NOT_INSTALLED",
                                        "Install AutoPresent with scripts/setup_autopresent.py setup first.", 503)
            try:
                # The laptop shares GPU memory between Ollama and the slide model.
                # Unload only this app's configured text models before starting it.
                async with httpx.AsyncClient(timeout=30, trust_env=False) as http:
                    models = [self.settings.llm_model, self.settings.output_llm_model,
                              *(item["model_id"] for item in LOCAL_MODELS)]
                    for model in dict.fromkeys(filter(None, models)):
                        try:
                            response = await http.post(self.settings.ollama_base_url + "/api/generate",
                                                       json={"model": model, "keep_alive": 0})
                            response.raise_for_status()
                        except httpx.HTTPError:
                            LOGGER.info("Could not unload Ollama model %s before AutoPresent.", model)
                await self._command("start")
                from startupforge_member2.autopresent import AutoPresentClient
                yield AutoPresentClient(self.settings.autopresent_base_url, self.settings.autopresent_model,
                                        timeout=self.settings.autopresent_timeout, max_tokens=3072)
            finally:
                # Leave GPU memory available for the next research/report request.
                try:
                    await asyncio.shield(self._command("stop"))
                except Exception:
                    LOGGER.exception("Could not stop the owned AutoPresent runtime.")
