"""Citation regeneration and interrupted-debate recovery retain validated work."""

import asyncio
import copy
import json

import httpx
import pytest
import pytest_asyncio

from app.harness.store import SQLiteStore
from app.main import create_app
from app.modules.intelligence.contracts import AnalysisResult, DebateRequest, DebateSession, ResearchOptions
from app.modules.intelligence.debate import DebateService
from app.modules.intelligence.errors import IntelligenceError
from app.modules.intelligence.figures import extract_figures
from app.modules.intelligence.pipeline import build_service
from app.modules.intelligence.settings import Settings


async def no_progress(stage, percent):
    pass


async def seed_analysis(store):
    text = "We help student founders validate startup ideas using sourced research and customer interviews."
    project = store.create_project("Recovery tests", text)
    service = build_service(Settings(app_mode="demo", demo_delay=0))
    idea = await service.extract(text)
    result = await service.analyze(project["project_id"], idea, ResearchOptions())
    result.figures = extract_figures(result.context_draft.evidence, result.context_draft.sources)
    result.context_draft.figures = result.figures
    result = AnalysisResult.model_validate(result.model_dump())
    store.save_analysis(result)
    return project, result


class SequenceModel:
    """Return controlled attempts and capture what was persisted before each one."""

    def __init__(self, store, result):
        self.store = store
        self.valid = {"message": "Demand is unverified; compare the proposal with existing alternatives.",
                      "evidence_ids": [result.context_draft.evidence[0].evidence_id], "figure_ids": []}
        self.actions = []
        self.summary_actions = []
        self.calls = []
        self.persisted = []
        self.debate_id = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.pause = False

    async def generate_json(self, task, payload, schema):
        self.calls.append({"task": task, "payload": copy.deepcopy(payload), "schema": copy.deepcopy(schema)})
        if self.debate_id:
            self.persisted.append(self.store.get_debate(self.debate_id).model_dump(mode="json"))
        if task == "debate_turn":
            self.started.set()
            if self.pause:
                await self.release.wait()
            value = self.actions.pop(0) if self.actions else self.valid
        elif task == "debate_summary":
            value = self.summary_actions.pop(0) if self.summary_actions else {
                "summary": "Demand remains unresolved. Interview intended users before expanding."}
        else:
            raise AssertionError(task)
        if isinstance(value, BaseException):
            raise value
        return copy.deepcopy(value)


@pytest_asyncio.fixture
async def recovery(tmp_path):
    store = SQLiteStore(str(tmp_path / "recovery.sqlite3"))
    project, result = await seed_analysis(store)
    model = SequenceModel(store, result)
    debate = DebateService(Settings(app_mode="live", demo_delay=0), store, model)
    return store, project, result, model, debate


def session_for(recovery, mode="user_vs_agent", rounds=2):
    store, project, result, model, debate = recovery
    session = debate.create_session(DebateRequest(
        project_id=project["project_id"], analysis_id=result.analysis_id,
        mode=mode, max_rounds=rounds))
    model.debate_id = session.debate_id
    return session


def turn_calls(model):
    return [call for call in model.calls if call["task"] == "debate_turn"]


def invalid_references(model, field="evidence_ids"):
    response = copy.deepcopy(model.valid)
    response[field] = ["fabricated_reference"]
    return response


@pytest.mark.asyncio
async def test_live_turn_schema_limits_references_to_the_current_evidence_package(recovery):
    _, _, result, model, debate = recovery
    session = session_for(recovery)
    await debate.prepare(session, no_progress)
    schema = turn_calls(model)[0]["schema"]
    evidence = schema["properties"]["evidence_ids"]
    figures = schema["properties"]["figure_ids"]
    assert set(evidence["items"]["enum"]) == {item.evidence_id for item in result.context_draft.evidence}
    assert set(figures["items"]["enum"]) == {item.figure_id for item in result.figures}
    # The public response contract must not accidentally retain this analysis's IDs.
    from app.modules.intelligence.debate import TurnResponse
    assert "enum" not in TurnResponse.model_json_schema()["properties"]["evidence_ids"]["items"]


@pytest.mark.asyncio
async def test_empty_evidence_package_requires_empty_reference_arrays_without_empty_enum(recovery, monkeypatch):
    store, _, result, model, debate = recovery
    session = session_for(recovery)
    empty = result.model_copy(deep=True)
    empty.context_draft.evidence = []
    empty.context_draft.figures = []
    empty.figures = []
    monkeypatch.setattr(store, "get_analysis", lambda analysis_id: empty)
    model.valid["evidence_ids"] = []
    opening = await debate.prepare(session, no_progress)
    for field in ("evidence_ids", "figure_ids"):
        field_schema = turn_calls(model)[0]["schema"]["properties"][field]
        assert field_schema["maxItems"] == 0
        assert field_schema.get("items", {}).get("enum") != []
    assert opening.turns[0].evidence_ids == []
    assert opening.turns[0].figures == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["evidence_ids", "figure_ids"])
async def test_invalid_first_citation_regenerates_without_persisting_the_bad_turn(recovery, field):
    store, _, result, model, debate = recovery
    session = session_for(recovery)
    await debate.prepare(session, no_progress)
    before = store.get_debate(session.debate_id).model_dump(mode="json")
    model.calls.clear()
    model.persisted.clear()
    model.actions = [invalid_references(model, field), model.valid]
    updated = await debate.reply(session.debate_id, "Test willingness to pay with intended users.", no_progress)
    assert len(turn_calls(model)) == 2
    assert all(snapshot == before for snapshot in model.persisted)
    assert len(updated.turns) == len(before["turns"]) + 2
    assert updated.turns[-1].evidence_ids == [result.context_draft.evidence[0].evidence_id]
    assert "fabricated_reference" not in updated.model_dump_json()
    # The corrective request provides the allowed references, not a guessed replacement.
    repair = json.dumps(turn_calls(model)[1]["payload"])
    assert result.context_draft.evidence[0].evidence_id in repair
    assert turn_calls(model)[0]["payload"] != turn_calls(model)[1]["payload"]


@pytest.mark.asyncio
async def test_repeated_invalid_citations_exhaust_bounded_attempts_and_roll_back_reply(recovery):
    store, _, _, model, debate = recovery
    session = session_for(recovery)
    await debate.prepare(session, no_progress)
    before = store.get_debate(session.debate_id).model_dump(mode="json")
    model.calls.clear()
    model.persisted.clear()
    model.actions = [invalid_references(model)] * 10
    with pytest.raises(IntelligenceError) as raised:
        await debate.reply(session.debate_id, "A pilot should test actual customer behaviour.", no_progress)
    assert raised.value.code == "DEBATE_INVALID_RESPONSE"
    assert 2 <= len(turn_calls(model)) <= 3
    assert store.get_debate(session.debate_id).model_dump(mode="json") == before
    assert all(snapshot == before for snapshot in model.persisted)
    store.claim_debate(session.debate_id)
    store.release_debate(session.debate_id)


@pytest.mark.asyncio
async def test_figure_without_its_evidence_is_regenerated_instead_of_silently_repaired(recovery):
    store, _, result, model, debate = recovery
    session = session_for(recovery)
    await debate.prepare(session, no_progress)
    before = store.get_debate(session.debate_id).model_dump(mode="json")
    figure = result.figures[0]
    bad = {"message": "This source gives background, but demand remains unverified.",
           "evidence_ids": [], "figure_ids": [figure.figure_id]}
    good = {**bad, "evidence_ids": [figure.evidence_id]}
    model.actions = [bad, good]
    model.calls.clear()
    model.persisted.clear()
    updated = await debate.reply(session.debate_id, "What does the cited figure establish?", no_progress)
    assert len(turn_calls(model)) == 2
    assert all(snapshot == before for snapshot in model.persisted)
    assert updated.turns[-1].figures == [figure]
    assert updated.turns[-1].evidence_ids == [figure.evidence_id]
    assert updated.turns[-1].source_ids == [figure.source_id]


@pytest.mark.asyncio
async def test_last_correction_regenerates_qualitative_argument_with_no_figure_selection(recovery):
    store, _, result, model, debate = recovery
    session = session_for(recovery)
    await debate.prepare(session, no_progress)
    before = store.get_debate(session.debate_id).model_dump(mode="json")
    figure = result.figures[0]
    bad = {"message": "The selected statistic needs validation.", "evidence_ids": [],
           "figure_ids": [figure.figure_id]}
    corrected = {"message": "Customer demand remains unverified. A pilot should test actual commitment.",
                 "evidence_ids": [], "figure_ids": []}
    model.actions = [bad, bad, corrected]
    model.calls.clear()
    model.persisted.clear()
    updated = await debate.reply(session.debate_id, "What do the figures establish?", no_progress)
    assert len(turn_calls(model)) == 3
    assert turn_calls(model)[-1]["schema"]["properties"]["figure_ids"]["maxItems"] == 0
    assert "qualitative" in turn_calls(model)[-1]["payload"]["validation_feedback"]["instruction"]
    assert updated.turns[-1].message == corrected["message"]
    assert updated.turns[-1].figures == []
    assert updated.turns[-1].evidence_ids == []
    assert all(snapshot == before for snapshot in model.persisted)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["PROVIDER_TIMEOUT", "MODEL_CONTEXT_LIMIT"])
async def test_provider_failures_are_not_misclassified_as_correctable_citations(recovery, code):
    store, _, _, model, debate = recovery
    session = session_for(recovery)
    await debate.prepare(session, no_progress)
    before = store.get_debate(session.debate_id).model_dump(mode="json")
    model.calls.clear()
    model.actions = [IntelligenceError(code, "Controlled model failure.", retryable=True)]
    with pytest.raises(IntelligenceError) as raised:
        await debate.reply(session.debate_id, "Test the customer problem first.", no_progress)
    assert raised.value.code == code
    assert len(turn_calls(model)) == 1
    assert store.get_debate(session.debate_id).model_dump(mode="json") == before


@pytest.mark.asyncio
@pytest.mark.parametrize("validated_turns", [0, 1])
async def test_resume_continues_auto_debate_from_zero_or_half_pair_preserving_turn_ids(recovery, validated_turns):
    store, _, _, model, debate = recovery
    session = session_for(recovery, mode="agent_vs_agent", rounds=2)
    model.actions = [copy.deepcopy(model.valid) for _ in range(validated_turns)]
    model.actions += [IntelligenceError("PROVIDER_TIMEOUT", "Controlled interruption.")]
    with pytest.raises(IntelligenceError):
        await debate.prepare(session, no_progress)
    failed = store.get_debate(session.debate_id)
    assert failed.status == "failed"
    assert len(failed.turns) == validated_turns
    original = [turn.model_dump(mode="json") for turn in failed.turns]
    model.calls.clear()
    resumed = await debate.resume(session.debate_id, no_progress)
    assert resumed.debate_id == failed.debate_id
    assert resumed.status == "completed"
    assert [turn.model_dump(mode="json") for turn in resumed.turns[:validated_turns]] == original
    assert [(turn.round_number, turn.speaker) for turn in resumed.turns] == [
        (1, "pro"), (1, "skeptic"), (2, "pro"), (2, "skeptic")]
    assert len(turn_calls(model)) == 4 - validated_turns
    assert len({turn.turn_id for turn in resumed.turns}) == 4
    assert store.get_debate(session.debate_id) == resumed


@pytest.mark.asyncio
async def test_summary_failure_resume_reuses_every_finished_auto_turn(recovery):
    store, _, _, model, debate = recovery
    session = session_for(recovery, mode="agent_vs_agent", rounds=2)
    model.summary_actions = [IntelligenceError("PROVIDER_TIMEOUT", "Controlled moderator interruption.")]
    with pytest.raises(IntelligenceError):
        await debate.prepare(session, no_progress)
    failed = store.get_debate(session.debate_id)
    assert failed.status == "failed"
    original = [turn.model_dump(mode="json") for turn in failed.turns]
    assert len(original) == 4
    model.calls.clear()
    resumed = await debate.resume(session.debate_id, no_progress)
    assert resumed.status == "completed"
    assert [turn.model_dump(mode="json") for turn in resumed.turns] == original
    assert [call["task"] for call in model.calls] == ["debate_summary"]


@pytest.mark.asyncio
async def test_completed_debate_cannot_be_resumed_or_changed(recovery):
    store, _, _, model, debate = recovery
    session = session_for(recovery, mode="agent_vs_agent", rounds=1)
    await debate.prepare(session, no_progress)
    before = store.get_debate(session.debate_id).model_dump(mode="json")
    model.calls.clear()
    with pytest.raises(IntelligenceError) as raised:
        await debate.resume(session.debate_id, no_progress)
    assert raised.value.status_code == 409
    assert store.get_debate(session.debate_id).model_dump(mode="json") == before
    assert model.calls == []


@pytest.mark.asyncio
async def test_failed_user_opening_can_resume_without_spending_a_user_round(recovery):
    store, _, _, model, debate = recovery
    session = session_for(recovery, rounds=10)
    model.actions = [invalid_references(model)] * 3
    with pytest.raises(IntelligenceError):
        await debate.prepare(session, no_progress)
    assert store.get_debate(session.debate_id).turns == []
    model.actions = []
    resumed = await debate.resume(session.debate_id, no_progress)
    assert resumed.status == "active"
    assert len(resumed.turns) == 1
    assert resumed.turns[0].round_number == 0
    assert resumed.max_rounds == 10


@pytest.mark.asyncio
async def test_resume_refuses_an_invalid_saved_turn_order(recovery):
    store, _, _, model, debate = recovery
    session = session_for(recovery, mode="agent_vs_agent", rounds=1)
    session.turns = [await debate._agent_turn(session, "against", 1)]
    session.status = "failed"
    store.save_debate(session)
    before = store.get_debate(session.debate_id).model_dump(mode="json")
    model.calls.clear()
    with pytest.raises(IntelligenceError) as raised:
        await debate.resume(session.debate_id, no_progress)
    assert raised.value.code == "DEBATE_TRANSCRIPT_INVALID"
    assert store.get_debate(session.debate_id).model_dump(mode="json") == before
    assert not model.calls


@pytest.mark.asyncio
async def test_process_restart_exposes_interrupted_automatic_debate_for_resume(recovery):
    store, project, _, _, debate = recovery
    session = session_for(recovery, mode="agent_vs_agent", rounds=2)
    session.turns = [await debate._agent_turn(session, "for", 1)]
    store.save_debate(session)
    job, _ = store.reserve_job("resume_debate", project["project_id"], "interrupted", {"debate_id": session.debate_id})
    store.update_job(job.job_id, status="running", result_url=f"/api/v1/intelligence/debates/{session.debate_id}")
    store.claim_debate(session.debate_id)
    store.recover_interrupted_jobs()
    recovered = store.get_debate(session.debate_id)
    assert recovered.status == "failed"
    assert recovered.turns == session.turns
    assert store.get_job(job.job_id).status == "failed"
    resumed = await debate.resume(session.debate_id, no_progress)
    assert resumed.status == "completed"
    assert resumed.turns[0] == session.turns[0]


@pytest.mark.asyncio
async def test_competing_resume_keeps_the_running_session_active_and_lock_owned(recovery):
    store, _, _, model, debate = recovery
    session = session_for(recovery, mode="agent_vs_agent", rounds=1)
    session.status = "failed"
    store.save_debate(session)
    model.pause = True
    pending = asyncio.create_task(debate.resume(session.debate_id, no_progress))
    try:
        await asyncio.wait_for(model.started.wait(), timeout=2)
        before = store.get_debate(session.debate_id).model_dump(mode="json")
        assert before["status"] == "active"
        with pytest.raises(IntelligenceError) as raised:
            await debate.resume(session.debate_id, no_progress)
        assert raised.value.code == "DEBATE_BUSY"
        assert store.get_debate(session.debate_id).model_dump(mode="json") == before
        with pytest.raises(IntelligenceError) as busy:
            store.claim_debate(session.debate_id)
        assert busy.value.code == "DEBATE_BUSY"
    finally:
        model.release.set()
        await asyncio.wait_for(pending, timeout=2)
    assert store.get_debate(session.debate_id).status == "completed"


@pytest.mark.asyncio
async def test_resume_api_returns_idempotent_job_and_existing_transcript_url(tmp_path):
    app = create_app(Settings(app_mode="demo", database_path=str(tmp_path / "api.sqlite3"), demo_delay=0))
    async with app.router.lifespan_context(app):
        store = app.state.store
        project, result = await seed_analysis(store)
        failed = DebateSession(debate_id="d_api_resume", project_id=project["project_id"],
                               analysis_id=result.analysis_id, mode="agent_vs_agent",
                               user_stance="for", max_rounds=1, status="failed")
        store.save_debate(failed)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            path = f"/api/v1/intelligence/debates/{failed.debate_id}/resume"
            first = await client.post(path, headers={"Idempotency-Key": "resume-once"})
            assert first.status_code == 202, first.text
            replay = await client.post(path, headers={"Idempotency-Key": "resume-once"})
            assert replay.status_code == 202, replay.text
            assert replay.json()["job_id"] == first.json()["job_id"]
            for _ in range(100):
                job = (await client.get(first.json()["status_url"])).json()
                if job["status"] in {"succeeded", "failed"}:
                    break
                await asyncio.sleep(0.01)
            assert job["status"] == "succeeded", job
            assert job["result_url"] == f"/api/v1/intelligence/debates/{failed.debate_id}"
            resumed = (await client.get(job["result_url"])).json()
            assert resumed["debate_id"] == failed.debate_id
            assert resumed["status"] == "completed"
            assert len(resumed["turns"]) == 2
