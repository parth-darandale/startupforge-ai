"""Debate behaviour tests using real fixture evidence and controlled providers."""

import asyncio
import copy
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
import pytest_asyncio

from app.harness.store import SQLiteStore
from app.modules.intelligence.contracts import AnalysisResult, DebateRequest, ResearchOptions
from app.modules.intelligence.debate import DebateService
from app.modules.intelligence.errors import IntelligenceError
from app.modules.intelligence.figures import extract_figures
from app.modules.intelligence.pipeline import build_service
from app.modules.intelligence.settings import Settings


async def no_progress(stage, percent):
    pass


class ControlledModel:
    """Pause exactly at a provider call, without timing-dependent sleeps."""

    def __init__(self, evidence_id):
        self.response = {
            "message": "The customer problem is testable, but willingness to pay remains an unverified assumption.",
            "evidence_ids": [evidence_id], "figure_ids": [],
        }
        self.summary = {"summary": "Demand is unresolved; interview intended users and compare existing alternatives."}
        self.failure = None
        self.wait = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = []

    def pause_next_turn(self):
        self.wait = True
        self.started.clear()
        self.release.clear()

    async def generate_json(self, task, payload, schema):
        self.calls.append((task, copy.deepcopy(payload)))
        if task == "debate_turn":
            self.started.set()
            if self.wait:
                await self.release.wait()
            if self.failure:
                raise self.failure
            return copy.deepcopy(self.response)
        if task == "debate_summary":
            return copy.deepcopy(self.summary)
        raise AssertionError(f"Unexpected debate provider task: {task}")


@pytest_asyncio.fixture
async def prepared_analysis(tmp_path):
    settings = Settings(app_mode="demo", demo_delay=0)
    store = SQLiteStore(str(tmp_path / "debate.sqlite3"))
    submitted = "We help student founders research and validate startup ideas with sourced evidence."
    project = store.create_project("Debate tests", submitted)
    service = build_service(settings)
    idea = await service.extract(submitted)
    result = await service.analyze(project["project_id"], idea, ResearchOptions())
    figures = extract_figures(result.context_draft.evidence, result.context_draft.sources)
    result.figures = figures
    result.context_draft.figures = figures
    result = AnalysisResult.model_validate(result.model_dump())
    store.save_analysis(result)
    return store, project, result


def live_debate(prepared_analysis, max_rounds=2, mode="user_vs_agent"):
    store, project, result = prepared_analysis
    model = ControlledModel(result.context_draft.evidence[0].evidence_id)
    debate = DebateService(Settings(app_mode="live", demo_delay=0), store, model)
    session = debate.create_session(DebateRequest(
        project_id=project["project_id"], analysis_id=result.analysis_id,
        mode=mode, max_rounds=max_rounds,
    ))
    return store, result, model, debate, session


@pytest.mark.asyncio
async def test_opening_argument_excludes_concurrent_reply(prepared_analysis):
    store, _, model, debate, session = live_debate(prepared_analysis)
    model.pause_next_turn()
    opening = asyncio.create_task(debate.prepare(session, no_progress))
    try:
        await asyncio.wait_for(model.started.wait(), timeout=2)
        with pytest.raises(IntelligenceError) as raised:
            await debate.reply(session.debate_id, "The target users have this problem.", no_progress)
        assert raised.value.code == "DEBATE_BUSY"
        assert store.get_debate(session.debate_id).turns == []
    finally:
        model.release.set()
        await asyncio.wait_for(opening, timeout=2)
    assert len(store.get_debate(session.debate_id).turns) == 1
    # The opening lock is released; the rejected concurrent request consumed no round.
    reply = await debate.reply(session.debate_id, "The target users have this problem.", no_progress)
    assert len(reply.turns) == 3
    assert sum(turn.speaker == "user" for turn in reply.turns) == 1
    assert reply.turns[-1].round_number == 1


@pytest.mark.asyncio
async def test_failed_live_reply_rolls_back_and_can_be_retried(prepared_analysis):
    store, _, model, debate, session = live_debate(prepared_analysis)
    await debate.prepare(session, no_progress)
    original = store.get_debate(session.debate_id).model_dump(mode="json")
    model.failure = IntelligenceError("PROVIDER_TIMEOUT", "Controlled provider failure.", retryable=True)
    with pytest.raises(IntelligenceError) as raised:
        await debate.reply(session.debate_id, "I propose a focused customer pilot.", no_progress)
    assert raised.value.code == "PROVIDER_TIMEOUT"
    assert store.get_debate(session.debate_id).model_dump(mode="json") == original
    model.failure = None
    retried = await debate.reply(session.debate_id, "I propose a focused customer pilot.", no_progress)
    assert retried.turns[-1].round_number == 1
    assert sum(turn.speaker == "user" for turn in retried.turns) == 1


@pytest.mark.asyncio
async def test_pending_user_reply_is_not_persisted_before_agent_finishes(prepared_analysis):
    store, _, model, debate, session = live_debate(prepared_analysis)
    await debate.prepare(session, no_progress)
    original = store.get_debate(session.debate_id).model_dump(mode="json")
    model.pause_next_turn()
    task = asyncio.create_task(debate.reply(session.debate_id, "A pilot could test demand.", no_progress))
    try:
        await asyncio.wait_for(model.started.wait(), timeout=2)
        assert store.get_debate(session.debate_id).model_dump(mode="json") == original
        assert not any(turn.speaker == "user" for turn in store.get_debate(session.debate_id).turns)
    finally:
        model.release.set()
        updated = await asyncio.wait_for(task, timeout=2)
    assert len(updated.turns) == 3
    persisted = store.get_debate(session.debate_id)
    assert persisted.turns[-2].speaker == "user"
    assert persisted.turns[-1].speaker == "skeptic"


@pytest.mark.asyncio
async def test_cancelled_reply_preserves_transcript_and_releases_lock(prepared_analysis):
    store, _, model, debate, session = live_debate(prepared_analysis)
    await debate.prepare(session, no_progress)
    original = store.get_debate(session.debate_id).model_dump(mode="json")
    model.pause_next_turn()
    task = asyncio.create_task(debate.reply(session.debate_id, "A pilot could test demand.", no_progress))
    await asyncio.wait_for(model.started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.get_debate(session.debate_id).model_dump(mode="json") == original
    store.claim_debate(session.debate_id)
    store.release_debate(session.debate_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_reference", ["unknown_evidence", "unknown_figure", "figure_without_evidence"])
async def test_invalid_citations_are_rejected_and_transcript_unchanged(prepared_analysis, bad_reference):
    store, result, model, debate, session = live_debate(prepared_analysis)
    await debate.prepare(session, no_progress)
    original = store.get_debate(session.debate_id).model_dump(mode="json")
    if bad_reference == "unknown_evidence":
        model.response["evidence_ids"] = ["fabricated_evidence"]
    elif bad_reference == "unknown_figure":
        model.response["figure_ids"] = ["fabricated_figure"]
    else:
        assert result.figures, "Fixture must include a clearly synthetic quoted quantity"
        model.response["figure_ids"] = [result.figures[0].figure_id]
        model.response["evidence_ids"] = []
    with pytest.raises(IntelligenceError) as raised:
        await debate.reply(session.debate_id, "I propose a focused customer pilot.", no_progress)
    assert raised.value.code == "DEBATE_INVALID_RESPONSE"
    assert store.get_debate(session.debate_id).model_dump(mode="json") == original
    store.claim_debate(session.debate_id)
    store.release_debate(session.debate_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_message", ["", "   "])
async def test_empty_agent_response_is_typed_invalid(prepared_analysis, empty_message):
    store, _, model, debate, session = live_debate(prepared_analysis)
    await debate.prepare(session, no_progress)
    original = store.get_debate(session.debate_id).model_dump(mode="json")
    model.response["message"] = empty_message
    with pytest.raises(IntelligenceError) as raised:
        await debate.reply(session.debate_id, "I propose a focused customer pilot.", no_progress)
    assert raised.value.code == "DEBATE_INVALID_RESPONSE"
    assert store.get_debate(session.debate_id).model_dump(mode="json") == original


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_summary", ["", "   "])
async def test_empty_moderator_response_rolls_back_entire_final_round(prepared_analysis, empty_summary):
    store, _, model, debate, session = live_debate(prepared_analysis, max_rounds=1)
    await debate.prepare(session, no_progress)
    original = store.get_debate(session.debate_id).model_dump(mode="json")
    model.summary = {"summary": empty_summary}
    with pytest.raises(IntelligenceError) as raised:
        await debate.reply(session.debate_id, "A customer pilot is the next step.", no_progress)
    assert raised.value.code == "DEBATE_INVALID_RESPONSE"
    assert store.get_debate(session.debate_id).model_dump(mode="json") == original
    model.summary = {"summary": "Test demand before treating this proposal as a validated business."}
    final = await debate.reply(session.debate_id, "A customer pilot is the next step.", no_progress)
    assert final.status == "completed"
    assert sum(turn.speaker == "user" for turn in final.turns) == 1
    assert final.summary == model.summary["summary"]


@pytest.mark.parametrize("message", [
    "The startup can make 100000 sales every month.",
    "There are ten million potential customers.",
    "Demand rises by ninety percent.",
    "Demand rises by fifteen percent.",
    "The startup can charge a hundred-dollar price.",
    "The market will be $5,000.",
    "Users would pay INR 5000 for the service.",
])
def test_quantities_are_omitted_from_prose_and_replaced_with_figure_notice(message):
    guarded = DebateService._guard_numbers(message)
    assert guarded == "Numerical claims are shown only in the quoted source figure cards below."


def test_numeric_prose_filter_preserves_nonnumeric_caveats():
    result = DebateService._guard_numbers(
        "The startup can make 100000 sales every month. This forecast is unverified; conduct a customer pilot."
    )
    assert "100000" not in result
    assert "This forecast is unverified; conduct a customer pilot." in result


@pytest.mark.asyncio
async def test_verbatim_valid_figures_remain_attached_when_numeric_prose_is_removed(prepared_analysis):
    _, result, model, debate, session = live_debate(prepared_analysis)
    assert result.figures
    figure = result.figures[0]
    model.response = {"message": f"The quoted source mentions {figure.display_value}. Demand remains unverified.",
                      "evidence_ids": [figure.evidence_id], "figure_ids": [figure.figure_id]}
    opening = await debate.prepare(session, no_progress)
    turn = opening.turns[0]
    assert figure.display_value not in turn.message
    assert "Demand remains unverified." in turn.message
    assert turn.figures == [figure]
    assert turn.source_ids == [figure.source_id]
    assert turn.evidence_ids == [figure.evidence_id]


@pytest.mark.asyncio
async def test_agent_debate_has_bounded_opposing_turns_and_summary(prepared_analysis):
    _, _, model, debate, session = live_debate(prepared_analysis, max_rounds=2, mode="agent_vs_agent")
    completed = await debate.prepare(session, no_progress)
    assert completed.status == "completed"
    assert [turn.speaker for turn in completed.turns] == ["pro", "skeptic", "pro", "skeptic"]
    assert [turn.round_number for turn in completed.turns] == [1, 1, 2, 2]
    assert completed.summary
    assert [task for task, _ in model.calls].count("debate_turn") == 4
    assert [task for task, _ in model.calls].count("debate_summary") == 1
    with pytest.raises(IntelligenceError) as raised:
        await debate.reply(session.debate_id, "I want to add an argument.", no_progress)
    assert raised.value.code == "WATCH_ONLY_DEBATE"


@pytest.mark.asyncio
async def test_repository_lookup_failure_after_claim_still_releases_lock(prepared_analysis, monkeypatch):
    store, _, _, debate, session = live_debate(prepared_analysis)
    await debate.prepare(session, no_progress)
    original_lookup = store.get_debate

    def fail_lookup(debate_id):
        raise IntelligenceError("CONTROLLED_REPOSITORY_ERROR", "The repository could not load this session.")

    monkeypatch.setattr(store, "get_debate", fail_lookup)
    with pytest.raises(IntelligenceError) as raised:
        await debate.reply(session.debate_id, "Try a customer pilot.", no_progress)
    assert raised.value.code == "CONTROLLED_REPOSITORY_ERROR"
    monkeypatch.setattr(store, "get_debate", original_lookup)
    store.claim_debate(session.debate_id)
    store.release_debate(session.debate_id)


@pytest.mark.asyncio
async def test_debate_claim_is_exclusive_across_repository_instances(prepared_analysis):
    store, _, _, _, session = live_debate(prepared_analysis)
    second_store = SQLiteStore(str(store.path))
    ready = threading.Barrier(2)

    def attempt(repository):
        ready.wait(timeout=2)
        try:
            repository.claim_debate(session.debate_id)
            return "claimed"
        except IntelligenceError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [store, second_store]))
    assert sorted(results) == ["DEBATE_BUSY", "claimed"]
    store.release_debate(session.debate_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["user_vs_agent", "agent_vs_agent"])
async def test_model_debate_payload_deduplicates_metadata_and_preserves_full_content_and_lineage(prepared_analysis, mode):
    store, result, model, debate, session = live_debate(prepared_analysis, max_rounds=2, mode=mode)
    original_analysis = result.model_dump(mode="json")
    figure = result.figures[0]
    full_agent_message = ("A focused pilot could test the stated customer problem, while willingness to pay remains uncertain. " * 20).strip()
    model.response = {"message": full_agent_message, "evidence_ids": [figure.evidence_id],
                      "figure_ids": [figure.figure_id]}
    if mode == "user_vs_agent":
        await debate.prepare(session, no_progress)
        full_user_message = ("My argument proposes a voluntary campus pilot with student feedback: गुंतवणूक. " * 30).strip()
        await debate.reply(session.debate_id, full_user_message, no_progress)
        completed = await debate.reply(session.debate_id, full_user_message + " Preserve this final sentence.", no_progress)
        assert completed.turns[1].message == full_user_message
        assert completed.turns[3].message.endswith("Preserve this final sentence.")
    else:
        completed = await debate.prepare(session, no_progress)
    assert completed.status == "completed"
    assert any(task == "debate_summary" for task, _ in model.calls)
    assert [len(payload["history"]) for _, payload in model.calls] == (
        [0, 2, 4, 5] if mode == "user_vs_agent" else [0, 1, 2, 3, 4]
    )
    for task, payload in model.calls:
        assert task in {"debate_turn", "debate_summary"}
        # Entire source evidence remains available, with exact original excerpts.
        assert payload["evidence"] == original_analysis["context_draft"]["evidence"]
        assert payload["figures"] == [f.model_dump(mode="json", exclude={"excerpt"}) for f in result.figures]
        for compact_figure in payload["figures"]:
            assert "excerpt" not in compact_figure
            original_figure = next(f for f in result.figures if f.figure_id == compact_figure["figure_id"])
            original_evidence = next(e for e in result.context_draft.evidence if e.evidence_id == compact_figure["evidence_id"])
            assert original_figure.excerpt in original_evidence.excerpt
            assert compact_figure["source_id"] == original_evidence.source_id
            assert compact_figure["display_value"] == original_figure.display_value
            assert compact_figure["qualification"] == original_figure.qualification
        for compact_turn, original_turn in zip(payload["history"], completed.turns):
            assert set(compact_turn) == {"round_number", "speaker", "stance", "message", "evidence_ids", "figure_ids"}
            assert compact_turn["message"] == original_turn.message
            assert compact_turn["round_number"] == original_turn.round_number
            assert compact_turn["speaker"] == original_turn.speaker
            assert compact_turn["stance"] == original_turn.stance
            assert compact_turn["evidence_ids"] == original_turn.evidence_ids
            assert compact_turn["figure_ids"] == [f.figure_id for f in original_turn.figures]
        if task == "debate_summary":
            assert len(payload["history"]) == len(completed.turns)
    # Compaction affects only provider inputs; stored/public cards retain quotes.
    assert store.get_analysis(result.analysis_id).model_dump(mode="json") == original_analysis
    stored = store.get_debate(session.debate_id)
    for turn in stored.turns:
        if turn.speaker != "user":
            assert turn.message == full_agent_message
            assert turn.figures == [figure]
            assert turn.figures[0].excerpt == figure.excerpt
            assert turn.source_ids == [figure.source_id]
