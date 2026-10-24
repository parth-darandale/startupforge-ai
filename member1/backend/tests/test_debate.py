"""Debate behaviour tests using real fixture evidence and controlled providers."""

import asyncio
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
import pytest_asyncio
from pydantic import ValidationError

from app.harness.store import SQLiteStore
from app.modules.intelligence.contracts import AnalysisResult, DebateRequest, DebateSession, DebateTurn, ResearchOptions
from app.modules.intelligence.debate import (DebateService, HISTORY_BYTE_BUDGET,
    RECENT_VERBATIM_TURNS, SummaryResponse, TurnResponse)
from app.modules.intelligence.errors import IntelligenceError
from app.modules.intelligence.figures import extract_figures
from app.modules.intelligence.pipeline import build_service
from app.modules.intelligence.prompts import build_messages
from app.modules.intelligence.providers import OLLAMA_CONTEXT_SAFETY_TOKENS
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


@pytest.mark.parametrize("mode", ["user_vs_agent", "agent_vs_agent"])
@pytest.mark.parametrize("rounds", [1, 10, 25, 100])
def test_request_and_persisted_session_accept_numeric_rounds(mode, rounds):
    request = DebateRequest(project_id="p_rounds", analysis_id="a_rounds", mode=mode, max_rounds=rounds)
    session = DebateSession(debate_id="d_rounds", **request.model_dump())
    assert request.max_rounds == session.max_rounds == rounds
    assert DebateSession.model_validate_json(session.model_dump_json()).max_rounds == rounds


@pytest.mark.parametrize("bad_rounds", [0, -1, 101, 3.5, 10.0, "10", True, None])
def test_request_and_session_reject_invalid_round_count_without_coercion(bad_rounds):
    fields = {"project_id": "p_rounds", "analysis_id": "a_rounds", "mode": "agent_vs_agent",
              "max_rounds": bad_rounds}
    with pytest.raises(ValidationError):
        DebateRequest(**fields)
    with pytest.raises(ValidationError):
        DebateSession(debate_id="d_rounds", user_stance="for", **fields)


def test_debate_defaults_to_ten_rounds():
    assert DebateRequest(project_id="p_rounds", analysis_id="a_rounds", mode="agent_vs_agent").max_rounds == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("rounds", [10, 25, 100])
async def test_long_automatic_debate_completes_every_requested_round_and_summary(prepared_analysis, rounds):
    store, result, model, debate, session = live_debate(prepared_analysis, max_rounds=rounds, mode="agent_vs_agent")
    figure = result.figures[0]
    model.response["evidence_ids"] = [figure.evidence_id]
    model.response["figure_ids"] = [figure.figure_id]
    progress_updates = []

    async def progress(stage, percent):
        progress_updates.append((stage, percent))

    completed = await debate.prepare(session, progress)
    assert completed.status == "completed"
    assert completed.max_rounds == rounds
    assert len(completed.turns) == rounds * 2
    assert [turn.round_number for turn in completed.turns] == [number for number in range(1, rounds + 1) for _ in range(2)]
    assert [turn.speaker for turn in completed.turns] == ["pro", "skeptic"] * rounds
    assert all(turn.figures == [figure] for turn in completed.turns)
    assert [task for task, _ in model.calls] == ["debate_turn"] * (rounds * 2) + ["debate_summary"]
    final_payload = model.calls[-1][1]
    assert final_payload["history"][-1]["round_number"] == rounds
    assert len(final_payload["history"]) + final_payload.get("earlier_history", {}).get("turn_count", 0) == rounds * 2
    assert completed.summary == model.summary["summary"]
    assert len(progress_updates) == rounds * 2 + 1
    assert progress_updates[-2][0] == f"debating_round_{rounds}_against"
    assert progress_updates[-1][0] == "moderating"
    assert all(first[1] <= second[1] for first, second in zip(progress_updates, progress_updates[1:]))
    assert store.get_debate(session.debate_id) == completed


@pytest.mark.asyncio
@pytest.mark.parametrize("rounds", [10, 25])
async def test_long_interactive_debate_waits_for_all_user_rounds_then_summarizes(prepared_analysis, rounds):
    store, _, model, debate, session = live_debate(prepared_analysis, max_rounds=rounds)
    opening = await debate.prepare(session, no_progress)
    assert opening.status == "active"
    assert opening.turns[0].round_number == 0
    for number in range(1, rounds + 1):
        updated = await debate.reply(session.debate_id, f"My pilot argument for round {number}.", no_progress)
        assert len(updated.turns) == 1 + number * 2
        assert updated.turns[-2].speaker == "user"
        assert updated.turns[-2].message == f"My pilot argument for round {number}."
        assert updated.turns[-1].round_number == number
        if number < rounds:
            assert updated.status == "active"
            assert updated.summary is None
            assert not any(task == "debate_summary" for task, _ in model.calls)
    assert updated.status == "completed"
    assert updated.summary == model.summary["summary"]
    assert [task for task, _ in model.calls].count("debate_turn") == rounds + 1
    assert [task for task, _ in model.calls].count("debate_summary") == 1
    assert len(model.calls[-1][1]["history"]) == 1 + rounds * 2
    assert store.get_debate(session.debate_id) == updated
    with pytest.raises(IntelligenceError) as raised:
        await debate.reply(session.debate_id, "An extra argument after the final round.", no_progress)
    assert raised.value.code == "DEBATE_FINISHED"


@pytest.mark.asyncio
async def test_hundred_round_model_inputs_use_labelled_bounded_excerpts_without_altering_transcript(prepared_analysis):
    store, result, model, debate, session = live_debate(prepared_analysis, max_rounds=100, mode="agent_vs_agent")
    debate.settings = replace(debate.settings, llm_provider="ollama", ollama_num_ctx=16384)
    figure = result.figures[0]
    full_message = ("A focused pilot could test the customer problem while willingness to pay remains uncertain. " * 7).strip()
    model.response = {"message": full_message, "evidence_ids": [figure.evidence_id], "figure_ids": [figure.figure_id]}
    completed = await debate.prepare(session, no_progress)
    assert completed.status == "completed"
    assert len(completed.turns) == 200
    assert completed.history_compacted is True
    assert "saved transcript keeps every full argument" in completed.history_note
    assert all(turn.message == full_message for turn in completed.turns)
    assert all(turn.figures == [figure] for turn in completed.turns)
    assert store.get_debate(session.debate_id) == completed

    compacted_payloads = 0
    for task, payload in model.calls:
        assert payload["evidence"] == result.context_draft.model_dump(mode="json")["evidence"]
        if "earlier_history" not in payload:
            continue
        compacted_payloads += 1
        earlier = payload["earlier_history"]
        assert earlier["format"] == "exact_prefix_table"
        assert len(payload["history"]) == RECENT_VERBATIM_TURNS
        assert "navigation aids, never independent evidence" in payload["history_note"]
        rows = [dict(zip(earlier["columns"], row)) for row in earlier["rows"]]
        assert len(rows) == earlier["turn_count"]
        for compact_turn, original_turn in zip(rows, completed.turns):
            assert compact_turn["round_number"] == original_turn.round_number
            assert compact_turn["speaker"] == original_turn.speaker
            assert compact_turn["stance"] == original_turn.stance
            assert original_turn.message.startswith(compact_turn["message_prefix"])
            assert compact_turn["message_prefix"]
            assert compact_turn["message_complete"] == (compact_turn["message_prefix"] == original_turn.message)
            assert [earlier["evidence_ids"][index] for index in compact_turn["evidence_indices"]] == original_turn.evidence_ids
            assert [earlier["figure_ids"][index] for index in compact_turn["figure_indices"]] == [figure.figure_id]
        for recent_turn, original_turn in zip(payload["history"], completed.turns[len(rows):]):
            assert recent_turn["message"] == original_turn.message
            assert recent_turn["evidence_ids"] == original_turn.evidence_ids
        response_schema = (TurnResponse if task == "debate_turn" else SummaryResponse).model_json_schema()
        prompt_size = sum(len(message["content"].encode("utf-8")) for message in build_messages(task, payload, response_schema))
        remaining_tokens = (debate.settings.ollama_num_ctx - debate.settings.ollama_num_predict
                            - OLLAMA_CONTEXT_SAFETY_TOKENS)
        assert prompt_size <= remaining_tokens * debate.settings.ollama_context_bytes_per_token - 512
        baseline = {key: value for key, value in payload.items() if key not in {"history", "history_note", "earlier_history"}}
        baseline["history"] = []
        baseline_size = sum(len(message["content"].encode("utf-8")) for message in build_messages(task, baseline, response_schema))
        assert prompt_size - baseline_size <= HISTORY_BYTE_BUDGET
    assert compacted_payloads > 1
    summary_payload = model.calls[-1][1]
    assert summary_payload["earlier_history"]["turn_count"] + len(summary_payload["history"]) == 200


@pytest.mark.asyncio
async def test_context_too_small_even_for_sources_and_recent_turns_fails_without_consuming_round(prepared_analysis):
    store, _, model, debate, session = live_debate(prepared_analysis, max_rounds=25)
    await debate.prepare(session, no_progress)
    for _ in range(4):
        await debate.reply(session.debate_id, "Test the pilot before scaling.", no_progress)
    original = store.get_debate(session.debate_id).model_dump(mode="json")
    debate.settings = replace(debate.settings, llm_provider="ollama", ollama_num_ctx=512, ollama_num_predict=256)
    with pytest.raises(IntelligenceError) as raised:
        await debate.reply(session.debate_id, "Keep all previous objections available.", no_progress)
    assert raised.value.code == "MODEL_CONTEXT_LIMIT"
    assert store.get_debate(session.debate_id).model_dump(mode="json") == original
    assert [task for task, _ in model.calls].count("debate_turn") == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("turn_count", [5, 6, 7, 8])
async def test_long_early_history_compacts_when_needed_before_ninth_turn(prepared_analysis, turn_count):
    store, result, model, debate, session = live_debate(prepared_analysis, max_rounds=25)
    debate.settings = replace(debate.settings, llm_provider="ollama", ollama_num_ctx=16384)
    figure = result.figures[0]
    long_argument = ("The pilot proposal preserves this objection and its qualification for review. " * 80)[:5500]
    session.turns = [DebateTurn(
        turn_id=f"t_long_{index}", round_number=(index + 1) // 2,
        speaker="user" if index % 2 else "skeptic", stance="for" if index % 2 else "against",
        message=long_argument, evidence_ids=[figure.evidence_id],
        source_ids=[figure.source_id], figures=[figure],
    ) for index in range(turn_count)]
    store.save_debate(session)
    original_turns = [turn.model_dump(mode="json") for turn in session.turns]

    await debate._agent_turn(session, "against", 5)
    payload = model.calls[-1][1]
    assert session.history_compacted is True
    assert len(payload["history"]) == 4
    assert payload["earlier_history"]["turn_count"] == turn_count - 4
    assert all(turn["message"] == long_argument for turn in payload["history"])
    assert payload["evidence"] == result.context_draft.model_dump(mode="json")["evidence"]
    prompt_size = sum(len(message["content"].encode("utf-8"))
                      for message in build_messages("debate_turn", payload, TurnResponse.model_json_schema()))
    token_budget = (debate.settings.ollama_num_ctx - debate.settings.ollama_num_predict
                    - OLLAMA_CONTEXT_SAFETY_TOKENS)
    assert prompt_size <= token_budget * debate.settings.ollama_context_bytes_per_token - 512
    assert [turn.model_dump(mode="json") for turn in session.turns] == original_turns
    assert [turn.model_dump(mode="json") for turn in store.get_debate(session.debate_id).turns] == original_turns
