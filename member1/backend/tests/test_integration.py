"""Contract tests use fake generators; runtime never substitutes these adapters."""
import asyncio
import copy
import json
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from app.integration.contracts import ContextReference, GenerationResult, TemplateDescriptor
from app.integration.interfaces import IntegrationAdapters
from app.integration.router import context_digest
from app.main import create_app
from app.modules.intelligence.contracts import ContextDraft, ProjectContext
from app.modules.intelligence.errors import IntelligenceError
from app.modules.intelligence.settings import Settings

FIXTURE = Path(__file__).resolve().parents[3] / "integration" / "fixtures" / "project_context.demo.json"


class FakeGenerator:
    def __init__(self, kinds, *, invalid=None, started=None, barrier=None):
        self.kinds = kinds
        self.invalid = invalid
        self.calls = []
        self.started = started
        self.barrier = barrier

    def templates(self, kind=None):
        return [TemplateDescriptor(template_id="test-" + current, name="Contract-test template", kind=current,
                                   supported_formats=[{"pitch_deck": "pptx", "poster": "txt", "report": "docx"}[current]],
                                   supported_options={"slide_count": {"minimum": 3, "maximum": 12}})
                for current in self.kinds if kind is None or current == kind]

    def validate_options(self, kind, options):
        if kind == "pitch_deck" and not 3 <= options["slide_count"] <= 12:
            raise IntelligenceError("INVALID_TEMPLATE_OPTIONS", "This template accepts 3 to 12 slides.", 422)

    async def generate(self, kind, context, options, progress):
        self.calls.append((kind, context.model_dump(mode="json"), options))
        if self.started is not None:
            self.started.append(kind)
            if len(self.started) == 2:
                self.barrier.set()
            await self.barrier.wait()
        await progress("rendering_" + kind, 70)
        value = {"kind": kind, "context_ref": ContextReference.from_context(context).model_dump(),
                 "artifact_ids": ["test-artifact-" + kind], "file_ids": ["test-file-" + kind]}
        if self.invalid == "wrong_version":
            value["context_ref"]["context_version"] += 1
        elif self.invalid == "wrong_hash":
            value["context_ref"]["context_hash"] = "0" * 64
        elif self.invalid == "wrong_kind":
            value["kind"] = "report"
        elif self.invalid == "empty_ids":
            value["file_ids"] = []
        elif self.invalid == "mutate":
            context.idea.problem = "The adapter illegally edited this fact"
        elif self.invalid == "failure":
            raise IntelligenceError("GENERATION_FAILED", "A test renderer failed.", 502)
        elif self.invalid == "mutated_model":
            result = GenerationResult.model_validate(value)
            result.file_ids = []
            return result
        return value


@pytest_asyncio.fixture
async def factory(tmp_path):
    clients = []

    async def make(adapters=None):
        app = create_app(Settings(database_path=str(tmp_path / f"test-{len(clients)}.sqlite3"), demo_delay=0),
                         integration_adapters=adapters)
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        clients.append((client, lifespan))
        return app, client

    yield make
    for client, lifespan in clients:
        await client.aclose()
        await lifespan.__aexit__(None, None, None)


def saved_context(app, *, blocked=False):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    draft = ContextDraft.model_validate({key: value for key, value in data.items()
                                       if key not in {"project_id", "context_version", "created_at", "content_hash"}})
    if blocked:
        draft.critic.review_status = "blocked"
    project = app.state.store.create_project("Connector contract test", draft.idea.original_text)
    return app.state.store.commit_context(project["project_id"], 0, draft).model_dump(mode="json")


def body(context, kind="pitch_deck"):
    options = {"template_id": "test-" + kind}
    if kind == "pitch_deck":
        options.update(slide_count=8, tone="professional")
    elif kind == "poster":
        options.update(audience="potential customers")
    else:
        options.update(formats=["docx"])
    return {"context": context, "options": options}


ROUTES = {"pitch_deck": "/api/v1/studio/pitch-decks", "poster": "/api/v1/studio/posters",
          "report": "/api/v1/reports/generations"}


async def completed(client, receipt):
    for _ in range(100):
        job = (await client.get(receipt["status_url"])).json()
        if job["status"] in {"succeeded", "failed"}:
            return job
        await asyncio.sleep(0.001)
    pytest.fail("Test generation job did not finish")


async def test_missing_modules_are_explicit_and_create_no_jobs(factory):
    app, client = await factory()
    context = saved_context(app)
    status = (await client.get("/api/v1/integration/modules")).json()
    assert [item["member"] for item in status["modules"]] == [2, 3]
    assert not any(item["available"] for item in status["modules"])
    for route in ("/api/v1/studio/templates", "/api/v1/reports/templates"):
        response = await client.get(route)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "MODULE_NOT_AVAILABLE"
    for kind, route in ROUTES.items():
        response = await client.post(route, json=body(context, kind),
                                     headers={"Idempotency-Key": kind, "X-Request-ID": "integration-test"})
        assert response.status_code == 503, response.text
        error = response.json()["error"]
        assert error["code"] == "MODULE_NOT_AVAILABLE"
        assert error["request_id"] == "integration-test"
        assert error["details"]["required_adapter"] in {"VisualStudioAdapter", "ReportsAdapter"}
    assert app.state.store.list_jobs(context["project_id"]) == []


async def test_handoff_preserves_entire_saved_version_and_eligibility(factory):
    app, client = await factory()
    context = saved_context(app, blocked=True)
    response = await client.get(f'/api/v1/projects/{context["project_id"]}/contexts/1/handoff')
    assert response.status_code == 200, response.text
    handoff = response.json()
    assert handoff["context"] == context
    assert not handoff["generation_eligible"]
    assert handoff["context_ref"]["context_hash"] == context["content_hash"]
    assert len(handoff["destinations"]) == 2


async def test_template_filter_and_published_option_validation(factory):
    generator = FakeGenerator(["pitch_deck", "poster"])
    app, client = await factory(IntegrationAdapters(visual_studio=generator))
    templates = (await client.get("/api/v1/studio/templates?kind=poster")).json()["templates"]
    assert [item["kind"] for item in templates] == ["poster"]
    assert (await client.get("/api/v1/studio/templates?kind=report")).status_code == 422
    context = saved_context(app)
    value = body(context)
    value["options"]["slide_count"] = 25
    response = await client.post(ROUTES["pitch_deck"], json=value, headers={"Idempotency-Key": "bad-option"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_TEMPLATE_OPTIONS"
    value["options"]["template_id"] = "invented-template"
    response = await client.post(ROUTES["pitch_deck"], json=value, headers={"Idempotency-Key": "bad-template"})
    assert response.json()["error"]["code"] == "TEMPLATE_NOT_FOUND"
    assert generator.calls == []


async def test_all_generators_pin_old_context_and_exact_result_provenance(factory):
    studio, reports = FakeGenerator(["pitch_deck", "poster"]), FakeGenerator(["report"])
    app, client = await factory(IntegrationAdapters(studio, reports))
    context = saved_context(app)
    newer = ContextDraft.model_validate({key: value for key, value in context.items()
                                        if key not in {"project_id", "context_version", "created_at", "content_hash"}})
    newer.warnings.append("Newer version must not replace requested v1")
    app.state.store.commit_context(context["project_id"], 1, newer)
    for kind, route in ROUTES.items():
        response = await client.post(route, json=body(context, kind), headers={"Idempotency-Key": kind})
        assert response.status_code == 202, response.text
        job = await completed(client, response.json())
        assert job["status"] == "succeeded", job
        assert job["context_ref"] == job["result"]["context_ref"]
        assert job["context_ref"] == {"project_id": context["project_id"], "context_version": 1,
                                      "context_hash": context["content_hash"]}
        assert job["result"]["kind"] == kind
        assert job["result"]["artifact_ids"] and job["result"]["file_ids"]
    assert len(studio.calls) == 2 and len(reports.calls) == 1
    assert all(call[1] == context for call in studio.calls + reports.calls)


async def test_output_jobs_can_develop_and_execute_independently(factory):
    started, barrier = [], asyncio.Event()
    studio = FakeGenerator(["pitch_deck", "poster"], started=started, barrier=barrier)
    reports = FakeGenerator(["report"], started=started, barrier=barrier)
    app, client = await factory(IntegrationAdapters(studio, reports))
    context = saved_context(app)
    receipts = []
    for kind in ("pitch_deck", "report"):
        response = await client.post(ROUTES[kind], json=body(context, kind), headers={"Idempotency-Key": kind})
        receipts.append(response.json())
    jobs = await asyncio.wait_for(asyncio.gather(*(completed(client, item) for item in receipts)), 2)
    assert started == ["pitch_deck", "report"]
    assert all(job["status"] == "succeeded" for job in jobs)


async def test_generation_idempotency_replays_and_conflicts(factory):
    generator = FakeGenerator(["pitch_deck", "poster"])
    app, client = await factory(IntegrationAdapters(visual_studio=generator))
    value = body(saved_context(app))
    headers = {"Idempotency-Key": "same-key"}
    first = await client.post(ROUTES["pitch_deck"], json=value, headers=headers)
    replay = await client.post(ROUTES["pitch_deck"], json=value, headers=headers)
    assert first.status_code == replay.status_code == 202
    assert first.json() == replay.json()
    changed = copy.deepcopy(value)
    changed["options"]["tone"] = "conversational"
    conflict = await client.post(ROUTES["pitch_deck"], json=changed, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert (await completed(client, first.json()))["status"] == "succeeded"
    assert len(generator.calls) == 1


@pytest.mark.parametrize("change,expected", [("payload", "CONTEXT_HASH_MISMATCH"),
                                          ("rehashed", "CONTEXT_SNAPSHOT_MISMATCH"),
                                          ("timestamp", "CONTEXT_SNAPSHOT_MISMATCH"),
                                          ("version", "CONTEXT_NOT_FOUND"),
                                          ("project", "PROJECT_NOT_FOUND")])
async def test_changed_context_snapshots_are_rejected(factory, change, expected):
    generator = FakeGenerator(["pitch_deck"])
    app, client = await factory(IntegrationAdapters(visual_studio=generator))
    context = saved_context(app)
    if change in {"payload", "rehashed"}:
        context["warnings"].append("Forged addition")
        if change == "rehashed":
            context["content_hash"] = context_digest(ProjectContext.model_validate(context))
    elif change == "timestamp":
        context["created_at"] = "2000-01-01T00:00:00Z"
    elif change == "version":
        context["context_version"] += 1
    else:
        context["project_id"] = "nonexistent-project"
    response = await client.post(ROUTES["pitch_deck"], json=body(context), headers={"Idempotency-Key": "forged"})
    assert response.json()["error"]["code"] == expected, response.text
    assert generator.calls == []


async def test_blocked_context_generation_is_rejected(factory):
    app, client = await factory(IntegrationAdapters(visual_studio=FakeGenerator(["pitch_deck"])))
    context = saved_context(app, blocked=True)
    response = await client.post(ROUTES["pitch_deck"], json=body(context), headers={"Idempotency-Key": "blocked"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONTEXT_BLOCKED"
    assert app.state.store.list_jobs(context["project_id"]) == []


@pytest.mark.parametrize("invalid", ["wrong_version", "wrong_hash", "wrong_kind", "empty_ids", "mutate", "failure", "mutated_model"])
async def test_invalid_or_failed_generators_never_report_success(factory, invalid):
    app, client = await factory(IntegrationAdapters(visual_studio=FakeGenerator(["pitch_deck"], invalid=invalid)))
    context = saved_context(app)
    response = await client.post(ROUTES["pitch_deck"], json=body(context), headers={"Idempotency-Key": invalid})
    assert response.status_code == 202
    job = await completed(client, response.json())
    assert job["status"] == "failed"
    assert job["error"]["code"] == ("GENERATION_FAILED" if invalid == "failure" else "ADAPTER_RESULT_INVALID")
    assert job["result"] is None
    assert app.state.store.get_context(context["project_id"], 1).model_dump(mode="json") == context


@pytest.mark.parametrize("invalid", ["duplicate", "wrong_kind", "mutated_model"])
async def test_invalid_template_adapters_return_typed_errors(factory, invalid):
    generator = FakeGenerator(["pitch_deck"])
    template = generator.templates("pitch_deck")[0]
    if invalid == "duplicate":
        values = [template, template]
    elif invalid == "wrong_kind":
        values = [template.model_copy(update={"kind": "report"})]
    else:
        template.supported_formats = []
        values = [template]
    generator.templates = lambda kind=None: values
    _, client = await factory(IntegrationAdapters(visual_studio=generator))
    response = await client.get("/api/v1/studio/templates?kind=pitch_deck")
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "ADAPTER_RESULT_INVALID"


async def test_missing_key_bad_shape_and_unsupported_formats(factory):
    app, client = await factory(IntegrationAdapters(reports=FakeGenerator(["report"])))
    value = body(saved_context(app), "report")
    response = await client.post(ROUTES["report"], json=value)
    assert response.status_code == 422
    value["options"]["formats"] = ["pdf"]
    response = await client.post(ROUTES["report"], json=value, headers={"Idempotency-Key": "pdf"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_FORMAT"
    assert response.json()["error"]["details"]["supported_formats"] == ["docx"]
    value["options"]["formats"] = ["docx", "docx"]
    response = await client.post(ROUTES["report"], json=value, headers={"Idempotency-Key": "duplicate"})
    assert response.status_code == 422
