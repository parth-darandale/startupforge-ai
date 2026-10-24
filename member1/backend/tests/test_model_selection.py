"""Request-scoped model choices stay traceable without mutating shared defaults."""

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from app.integration.router import context_digest
from app.main import create_app
from app.modules.intelligence.contracts import AnalysisResult, ContextDraft, ProjectContext
from app.modules.intelligence.demo import DemoLLM
from app.modules.intelligence.errors import IntelligenceError
from app.modules.intelligence.providers import DemoSearchProvider
from app.modules.intelligence.settings import Settings


QWEN = "qwen3:4b-instruct-2507-q4_K_M"
FIN_R1 = "startupforge-fin-r1"
FIXTURE = Path(__file__).resolve().parents[3] / "integration" / "fixtures" / "project_context.demo.json"
SNAPSHOT_FIELDS = {"project_id", "context_version", "created_at", "content_hash"}


def fixture_draft():
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return ContextDraft.model_validate({key: item for key, item in value.items() if key not in SNAPSHOT_FIELDS})


def seed_analysis(app):
    draft = fixture_draft()
    draft.critic.review_status = "ready_with_gaps"
    project = app.state.store.create_project("Model selection", draft.idea.original_text)
    result = AnalysisResult(analysis_id=f"analysis_{uuid4().hex}", project_id=project["project_id"],
                            context_draft=draft, figures=draft.figures, stages=[])
    app.state.store.save_analysis(result)
    return project, result


@pytest_asyncio.fixture
async def app_factory(tmp_path):
    opened = []

    async def make(settings=None, *, database_path=None):
        index = len(opened)
        config = replace(settings or Settings(demo_delay=0),
                         database_path=str(database_path or tmp_path / f"model-{index}.sqlite3"))
        app = create_app(config, artifact_storage_root=tmp_path / f"files-{index}")
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        opened.append((client, lifespan))
        return app, client

    yield make
    for client, lifespan in reversed(opened):
        await client.aclose()
        await lifespan.__aexit__(None, None, None)


async def completed(client, response):
    assert response.status_code == 202, response.text
    for _ in range(300):
        job = (await client.get(response.json()["status_url"])).json()
        if job["status"] in {"succeeded", "failed"}:
            return job
        await asyncio.sleep(0.005)
    pytest.fail("The controlled model-selection job did not finish")


def analysis_body(project, result, model_id=None):
    body = {"project_id": project["project_id"], "idea": result.context_draft.idea.model_dump(mode="json")}
    if model_id is not None:
        body["model_id"] = model_id
    return body


def debate_body(project, result, model_id=None, *, mode="agent_vs_agent"):
    body = {"project_id": project["project_id"], "analysis_id": result.analysis_id,
            "mode": mode, "max_rounds": 1}
    if model_id is not None:
        body["model_id"] = model_id
    return body


def operation_body(operation, project, result, model_id):
    if operation == "extractions":
        return {"project_id": project["project_id"], "idea_text": result.context_draft.idea.original_text,
                "model_id": model_id}
    return (analysis_body if operation == "analyses" else debate_body)(project, result, model_id)


async def test_demo_default_requests_remain_compatible_and_do_not_claim_a_real_model(app_factory):
    app, client = await app_factory()
    project, result = seed_analysis(app)
    response = await client.post("/api/v1/intelligence/analyses", json=analysis_body(project, result),
                                 headers={"Idempotency-Key": "legacy-demo"})
    job = await completed(client, response)
    assert job["status"] == "succeeded", job
    analysis = (await client.get(job["result_url"])).json()
    assert analysis["context_draft"]["execution"]["mode"] == "demo"
    assert analysis.get("model_id") is None
    assert analysis["context_draft"]["execution"].get("model_id") is None
    debate = await completed(client, await client.post("/api/v1/intelligence/debates",
        json=debate_body(project, result), headers={"Idempotency-Key": "legacy-demo-debate"}))
    assert debate["status"] == "succeeded", debate
    transcript = (await client.get(debate["result_url"])).json()
    assert transcript["status"] == "completed"
    assert transcript.get("model_id") is None


def test_legacy_context_payload_keeps_its_original_hash():
    """Adding provenance must not invalidate already saved research snapshots."""
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["execution"].pop("model_id", None)
    draft = {key: value for key, value in raw.items() if key not in SNAPSHOT_FIELDS}
    raw["content_hash"] = hashlib.sha256(json.dumps(
        draft, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    parsed = ProjectContext.model_validate(raw)
    assert parsed.execution.model_id is None
    assert context_digest(parsed) == raw["content_hash"]


def test_selected_model_is_part_of_context_integrity_hash():
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["execution"]["model_id"] = QWEN
    qwen = ProjectContext.model_validate(raw)
    finance = qwen.model_copy(deep=True)
    finance.execution.model_id = FIN_R1
    assert context_digest(qwen) != context_digest(finance)


async def test_demo_with_retained_live_model_name_does_not_claim_real_inference(app_factory):
    app, client = await app_factory(Settings(app_mode="demo", llm_model=QWEN, demo_delay=0))
    project, result = seed_analysis(app)
    job = await completed(client, await client.post("/api/v1/intelligence/debates",
        json=debate_body(project, result), headers={"Idempotency-Key": "demo-retained-name"}))
    assert job["status"] == "succeeded"
    transcript = (await client.get(job["result_url"])).json()
    assert transcript["model_id"] is None
    assert (await client.get("/api/v1/intelligence/models")).json()["models"] == []


def live_settings(model_id=QWEN):
    return Settings(app_mode="live", llm_provider="ollama", llm_model=model_id,
                    ollama_num_ctx=16384, search_provider="wikipedia", demo_delay=0)


def installed_transport(*model_ids):
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{"name": value, "model": value} for value in model_ids]})
    return httpx.MockTransport(handler)


class ControlledModel:
    """Exercise the real pipeline and debate while keeping tests network-free."""

    name = "controlled-local-model"

    def __init__(self, settings, calls, *, barrier=None, fail_turns=None):
        self.settings = settings
        self.calls = calls
        self.barrier = barrier
        self.fail_turns = fail_turns if fail_turns is not None else []
        self.demo = DemoLLM(delay=0)

    async def generate_json(self, task, payload, schema):
        self.calls.append((self.settings.llm_model, task))
        if task == "queries" and self.barrier:
            self.barrier[0].add(self.settings.llm_model)
            if len(self.barrier[0]) == 2:
                self.barrier[1].set()
            await self.barrier[1].wait()
        if task == "debate_turn":
            if self.fail_turns:
                raise self.fail_turns.pop(0)
            return {"message": "Customer adoption remains an assumption to test in a focused pilot.",
                    "evidence_ids": [], "figure_ids": []}
        if task == "debate_summary":
            return {"summary": "Interview intended customers and compare their existing alternatives."}
        return await self.demo.generate_json(task, payload, schema)


def configure_controlled_models(app, monkeypatch, calls, *, installed=(QWEN, FIN_R1), barrier=None,
                                fail_turns=None):
    from app.modules.intelligence import router
    from app.modules.intelligence.models import ModelSelection

    dependencies = app.state.intelligence
    settings = dependencies.settings
    dependencies.model_selection = ModelSelection(settings, installed_transport(*installed))
    dependencies.service.search = DemoSearchProvider()
    default = ControlledModel(settings, calls, barrier=barrier, fail_turns=fail_turns)
    dependencies.service.llm = default
    dependencies.debates.llm = default
    monkeypatch.setattr(router, "make_language_model", lambda selected: ControlledModel(
        selected, calls, barrier=barrier, fail_turns=fail_turns))


@pytest.mark.asyncio
async def test_catalog_lists_only_supported_models_and_installed_availability():
    from app.modules.intelligence.models import ModelSelection

    selection = ModelSelection(live_settings(), installed_transport(QWEN, "unrelated-model:latest"))
    catalog = await selection.catalog()
    assert catalog["mode"] == "live"
    assert catalog["default_model_id"] == QWEN
    by_id = {item["model_id"]: item for item in catalog["models"]}
    assert set(by_id) == {QWEN, FIN_R1}
    assert by_id[QWEN]["available"] is True
    assert by_id[QWEN]["experimental"] is False
    assert by_id[FIN_R1]["available"] is False
    assert by_id[FIN_R1]["experimental"] is True
    assert by_id[FIN_R1]["unavailable_reason"]
    assert all(item["label"] and item["description"] for item in by_id.values())


@pytest.mark.asyncio
async def test_ollama_latest_tag_alias_is_recognized_for_the_imported_finance_model():
    from app.modules.intelligence.models import ModelSelection

    selection = ModelSelection(live_settings(), installed_transport(FIN_R1 + ":latest"))
    catalog = await selection.catalog()
    finance = next(item for item in catalog["models"] if item["model_id"] == FIN_R1)
    assert finance["available"] is True
    assert (await selection.resolve(FIN_R1)).llm_model == FIN_R1


@pytest.mark.asyncio
async def test_catalog_in_demo_has_no_real_models_available_or_network_access():
    from app.modules.intelligence.models import ModelSelection

    def forbidden_network(request):
        pytest.fail("Demo model catalog must not contact Ollama")

    catalog = await ModelSelection(Settings(demo_delay=0), httpx.MockTransport(forbidden_network)).catalog()
    assert catalog["mode"] == "demo"
    assert catalog["default_model_id"] is None
    assert catalog["models"] == []


@pytest.mark.asyncio
async def test_catalog_handles_offline_ollama_without_exposing_internal_diagnostics():
    from app.modules.intelligence.models import ModelSelection

    def offline(request):
        raise httpx.ConnectError("private-provider-diagnostic", request=request)

    catalog = await ModelSelection(live_settings(), httpx.MockTransport(offline)).catalog()
    assert len(catalog["models"]) == 2
    assert not any(item["available"] for item in catalog["models"])
    assert "private-provider-diagnostic" not in json.dumps(catalog)


@pytest.mark.asyncio
async def test_resolve_uses_immutable_request_settings_and_never_falls_back():
    from app.modules.intelligence.models import ModelSelection

    config = live_settings()
    selection = ModelSelection(config, installed_transport(QWEN, FIN_R1))
    qwen, finance = await asyncio.gather(selection.resolve(QWEN), selection.resolve(FIN_R1))
    assert qwen.llm_model == QWEN
    assert finance.llm_model == FIN_R1
    assert config.llm_model == QWEN
    assert finance is not config
    assert (await selection.resolve(None)) is config

    missing = ModelSelection(config, installed_transport(QWEN))
    with pytest.raises(IntelligenceError) as error:
        await missing.resolve(FIN_R1)
    assert error.value.code == "MODEL_NOT_AVAILABLE"
    assert error.value.status_code == 503
    assert config.llm_model == QWEN


@pytest.mark.asyncio
async def test_unknown_model_is_rejected_without_provider_access():
    from app.modules.intelligence.models import ModelSelection

    def forbidden_network(request):
        pytest.fail("An arbitrary model must be rejected before provider access")

    selection = ModelSelection(live_settings(), httpx.MockTransport(forbidden_network))
    with pytest.raises(IntelligenceError) as error:
        await selection.resolve("unrelated-model:latest")
    assert error.value.code == "MODEL_NOT_ALLOWED"
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_catalog_api_exposes_supported_model_choices(app_factory):
    from app.modules.intelligence.models import ModelSelection

    app, client = await app_factory(live_settings())
    app.state.intelligence.model_selection = ModelSelection(live_settings(), installed_transport(QWEN, FIN_R1))
    response = await client.get("/api/v1/intelligence/models")
    assert response.status_code == 200, response.text
    catalog = response.json()
    assert catalog["default_model_id"] == QWEN
    assert {item["model_id"] for item in catalog["models"] if item["available"]} == {QWEN, FIN_R1}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["extractions", "analyses", "debates"])
async def test_api_unknown_model_is_rejected_before_reserving_a_job(app_factory, operation):
    app, client = await app_factory()
    project, result = seed_analysis(app)
    body = operation_body(operation, project, result, "arbitrary-model")
    before = app.state.store.list_jobs(project["project_id"])
    response = await client.post(f"/api/v1/intelligence/{operation}", json=body,
                                 headers={"Idempotency-Key": f"invalid-{operation}"})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "MODEL_NOT_ALLOWED"
    assert app.state.store.list_jobs(project["project_id"]) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["extractions", "analyses", "debates"])
async def test_missing_selected_model_fails_explicitly_without_using_default(app_factory, monkeypatch, operation):
    app, client = await app_factory(live_settings())
    calls = []
    configure_controlled_models(app, monkeypatch, calls, installed=(QWEN,))
    project, result = seed_analysis(app)
    body = operation_body(operation, project, result, FIN_R1)
    job = await completed(client, await client.post(f"/api/v1/intelligence/{operation}", json=body,
                                                   headers={"Idempotency-Key": f"missing-{operation}"}))
    assert job["status"] == "failed", job
    assert job["error"]["code"] == "MODEL_NOT_AVAILABLE"
    assert calls == []


@pytest.mark.asyncio
async def test_parallel_analysis_models_stay_isolated_and_record_provenance(app_factory, monkeypatch):
    app, client = await app_factory(live_settings())
    calls = []
    barrier = (set(), asyncio.Event())
    configure_controlled_models(app, monkeypatch, calls, barrier=barrier)
    dependencies = app.state.intelligence
    default_service = dependencies.service
    default_model = default_service.llm
    default_settings = dependencies.settings
    project, result = seed_analysis(app)

    async def run(model_id):
        receipt = await client.post("/api/v1/intelligence/analyses", json=analysis_body(project, result, model_id),
                                    headers={"Idempotency-Key": model_id})
        job = await completed(client, receipt)
        assert job["status"] == "succeeded", job
        return (await client.get(job["result_url"])).json()

    qwen, finance = await asyncio.wait_for(asyncio.gather(run(QWEN), run(FIN_R1)), timeout=10)
    assert barrier[0] == {QWEN, FIN_R1}
    for value, expected in ((qwen, QWEN), (finance, FIN_R1)):
        assert value["model_id"] == expected
        assert value["context_draft"]["execution"]["model_id"] == expected
        assert (await client.get(f"/api/v1/intelligence/analyses/{value['analysis_id']}")).json() == value
    assert dependencies.settings is default_settings
    assert dependencies.settings.llm_model == QWEN
    assert dependencies.service is default_service
    assert dependencies.service.llm is default_model
    assert default_service.settings.llm_model == QWEN
    assert {model for model, task in calls if task == "queries"} == {QWEN, FIN_R1}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["extractions", "analyses", "debates"])
async def test_same_request_key_with_a_different_model_conflicts(app_factory, monkeypatch, operation):
    app, client = await app_factory(live_settings())
    configure_controlled_models(app, monkeypatch, [])
    project, result = seed_analysis(app)
    endpoint = f"/api/v1/intelligence/{operation}"
    headers = {"Idempotency-Key": f"one-model-{operation}"}
    first = await client.post(endpoint, json=operation_body(operation, project, result, QWEN), headers=headers)
    assert first.status_code == 202, first.text
    replay = await client.post(endpoint, json=operation_body(operation, project, result, QWEN), headers=headers)
    assert replay.status_code == 202, replay.text
    assert replay.json()["job_id"] == first.json()["job_id"]
    changed = await client.post(endpoint, json=operation_body(operation, project, result, FIN_R1), headers=headers)
    assert changed.status_code == 409, changed.text
    assert changed.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert (await completed(client, first))["status"] == "succeeded"


@pytest.mark.asyncio
async def test_debate_reply_remains_on_saved_model_after_default_changes(app_factory, monkeypatch):
    app, client = await app_factory(live_settings(QWEN))
    calls = []
    configure_controlled_models(app, monkeypatch, calls)
    project, result = seed_analysis(app)
    created = await completed(client, await client.post("/api/v1/intelligence/debates",
        json=debate_body(project, result, QWEN, mode="user_vs_agent"),
        headers={"Idempotency-Key": "pinned-user"}))
    assert created["status"] == "succeeded", created
    original = (await client.get(created["result_url"])).json()
    assert original["model_id"] == QWEN

    restarted, connection = await app_factory(live_settings(FIN_R1), database_path=app.state.intelligence.settings.database_path)
    calls.clear()
    configure_controlled_models(restarted, monkeypatch, calls)
    replied = await completed(connection, await connection.post(created["result_url"] + "/turns",
        json={"message": "A small pilot could test whether customers return."},
        headers={"Idempotency-Key": "pinned-reply"}))
    assert replied["status"] == "succeeded", replied
    final = (await connection.get(replied["result_url"])).json()
    assert final["model_id"] == QWEN
    assert final["turns"][0] == original["turns"][0]
    assert final["status"] == "completed"
    assert calls and {model for model, _ in calls} == {QWEN}
    assert restarted.state.intelligence.settings.llm_model == FIN_R1


@pytest.mark.asyncio
async def test_debate_resume_remains_on_saved_model_after_default_changes(app_factory, monkeypatch):
    app, client = await app_factory(live_settings(QWEN))
    calls = []
    failure = [IntelligenceError("PROVIDER_TIMEOUT", "Controlled interruption.", retryable=True)]
    configure_controlled_models(app, monkeypatch, calls, fail_turns=failure)
    project, result = seed_analysis(app)
    interrupted = await completed(client, await client.post("/api/v1/intelligence/debates",
        json=debate_body(project, result, QWEN), headers={"Idempotency-Key": "pinned-auto"}))
    assert interrupted["status"] == "failed", interrupted
    failed = (await client.get(interrupted["result_url"])).json()
    assert failed["status"] == "failed"
    assert failed["model_id"] == QWEN

    restarted, connection = await app_factory(live_settings(FIN_R1), database_path=app.state.intelligence.settings.database_path)
    calls.clear()
    configure_controlled_models(restarted, monkeypatch, calls)
    resumed = await completed(connection, await connection.post(interrupted["result_url"] + "/resume",
                                                               headers={"Idempotency-Key": "pinned-resume"}))
    assert resumed["status"] == "succeeded", resumed
    final = (await connection.get(resumed["result_url"])).json()
    assert final["model_id"] == QWEN
    assert final["status"] == "completed"
    assert len(final["turns"]) == 2
    assert calls and {model for model, _ in calls} == {QWEN}
    assert restarted.state.intelligence.settings.llm_model == FIN_R1


@pytest.mark.asyncio
async def test_direct_debate_service_refuses_a_session_pinned_to_a_different_model(app_factory, monkeypatch):
    from app.modules.intelligence.contracts import DebateRequest

    app, _ = await app_factory(live_settings(QWEN))
    calls = []
    configure_controlled_models(app, monkeypatch, calls)
    project, result = seed_analysis(app)
    with pytest.raises(IntelligenceError) as error:
        app.state.intelligence.debates.create_session(DebateRequest.model_validate(
            debate_body(project, result, FIN_R1)))
    assert error.value.code == "DEBATE_MODEL_MISMATCH"
    assert error.value.status_code == 409
    assert calls == []


@pytest.mark.asyncio
async def test_legacy_live_session_without_model_provenance_can_still_reply(app_factory, monkeypatch):
    app, client = await app_factory(live_settings())
    calls = []
    configure_controlled_models(app, monkeypatch, calls)
    project, result = seed_analysis(app)
    created = await completed(client, await client.post("/api/v1/intelligence/debates",
        json=debate_body(project, result, QWEN, mode="user_vs_agent"),
        headers={"Idempotency-Key": "legacy-live"}))
    assert created["status"] == "succeeded", created
    original = (await client.get(created["result_url"])).json()
    session = app.state.store.get_debate(original["debate_id"])
    session.model_id = None
    app.state.store.save_debate(session)
    replied = await completed(client, await client.post(created["result_url"] + "/turns",
        json={"message": "We should interview intended customers before expanding."},
        headers={"Idempotency-Key": "legacy-reply"}))
    assert replied["status"] == "succeeded", replied
    final = (await client.get(replied["result_url"])).json()
    assert final["status"] == "completed"
    assert final["turns"][0] == original["turns"][0]


@pytest.mark.asyncio
async def test_unavailable_pinned_model_preserves_interrupted_session_for_later_resume(app_factory, monkeypatch):
    app, client = await app_factory(live_settings(QWEN))
    calls = []
    configure_controlled_models(app, monkeypatch, calls,
        fail_turns=[IntelligenceError("PROVIDER_TIMEOUT", "Controlled interruption.", retryable=True)])
    project, result = seed_analysis(app)
    interrupted = await completed(client, await client.post("/api/v1/intelligence/debates",
        json=debate_body(project, result, QWEN), headers={"Idempotency-Key": "missing-pinned-start"}))
    assert interrupted["status"] == "failed", interrupted
    before = (await client.get(interrupted["result_url"])).json()
    calls.clear()
    configure_controlled_models(app, monkeypatch, calls, installed=(FIN_R1,))
    resumed = await completed(client, await client.post(interrupted["result_url"] + "/resume",
        headers={"Idempotency-Key": "missing-pinned-resume"}))
    assert resumed["status"] == "failed", resumed
    assert resumed["error"]["code"] == "MODEL_NOT_AVAILABLE"
    assert calls == []
    assert (await client.get(interrupted["result_url"])).json() == before
