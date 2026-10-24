"""Local model leases keep Ollama inference out of AutoPresent's GPU session."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path

import httpx
import pytest

import app.modules.intelligence.autopresent_runtime as runtime_module
from app.modules.intelligence.autopresent_runtime import AutoPresentRuntime
from app.modules.intelligence.providers import OllamaLanguageModel
from app.modules.intelligence.settings import Settings


@pytest.mark.asyncio
async def test_ollama_instances_wait_through_autopresent_shutdown(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "member2" / "backend"))
    monkeypatch.setattr(runtime_module, "BACKEND_ROOT", tmp_path)
    manifest = tmp_path / "models/autopresent/manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"assets": {"base": {"path": "base.gguf"}},
                                    "converted_adapter": {"path": "adapter.gguf"}}))
    for relative in ("base.gguf", "adapter.gguf"):
        (tmp_path / relative).touch()
    runtime = AutoPresentRuntime(Settings(llm_model="qwen-one"))
    assert not runtime.installed  # The manifest alone must not expose the option.
    executable = tmp_path / ".runtime/autopresent/llama.cpp-b11838/llama-server.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    assert runtime.installed

    first_entered, release_first = asyncio.Event(), asyncio.Event()
    deck_entered, release_deck = asyncio.Event(), asyncio.Event()
    stop_entered, release_stop = asyncio.Event(), asyncio.Event()
    second_entered = asyncio.Event()
    events = []

    async def request(request):
        if request.url.path == "/api/generate":
            events.append("unload")
            return httpx.Response(200, json={"done": True})
        model = json.loads(request.content)["model"]
        events.append(model)
        if model == "qwen-one":
            first_entered.set()
            await release_first.wait()
        else:
            second_entered.set()
        return httpx.Response(200, json={"done": True, "message": {"content": "{}"}})

    transport = httpx.MockTransport(request)
    original_client = httpx.AsyncClient

    def mock_client(**kwargs):
        kwargs.setdefault("transport", transport)
        return original_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)

    async def command(action):
        events.append(action)
        if action == "stop":
            stop_entered.set()
            await release_stop.wait()

    monkeypatch.setattr(runtime, "_command", command)

    async def deck():
        async with runtime.session():
            deck_entered.set()
            await release_deck.wait()

    schema = {"type": "object", "properties": {}}
    first = asyncio.create_task(OllamaLanguageModel(runtime.settings, transport).generate_json("extract", {}, schema))
    await asyncio.wait_for(first_entered.wait(), 2)
    slides = asyncio.create_task(deck())
    await asyncio.sleep(0)  # Queue AutoPresent before the next Ollama instance.
    second = asyncio.create_task(OllamaLanguageModel(replace(runtime.settings, llm_model="fin-r1"), transport).generate_json("extract", {}, schema))
    await asyncio.sleep(0)
    assert not deck_entered.is_set() and not second_entered.is_set()
    release_first.set()
    await asyncio.wait_for(deck_entered.wait(), 2)
    assert not second_entered.is_set()
    release_deck.set()
    await asyncio.wait_for(stop_entered.wait(), 2)
    assert not second_entered.is_set()
    release_stop.set()
    await asyncio.wait_for(asyncio.gather(first, slides, second), 2)
    assert events.index("qwen-one") < events.index("unload") < events.index("start") < events.index("stop") < events.index("fin-r1")
