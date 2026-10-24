"""Bounded, role-separated debate grounded in a saved analysis evidence package."""
import asyncio
import re
import uuid
from typing import Any

from pydantic import Field, ValidationError

from .contracts import (AnalysisResult, Contract, DebateRequest, DebateSession,
                        DebateTurn, SourcedFigure, utc_now)
from .errors import IntelligenceError
from .interfaces import IntelligenceRepository, Progress
from .prompts import build_messages
from .providers import OLLAMA_CONTEXT_SAFETY_TOKENS
from .settings import Settings


HISTORY_BYTE_BUDGET = 24000
RECENT_VERBATIM_TURNS = 4
TURN_GENERATION_ATTEMPTS = 3
HISTORY_NOTE = (
    "Older arguments are supplied to the model as labelled exact opening excerpts with citation references; "
    "the latest four turns stay complete. The saved transcript keeps every full argument. "
    "Excerpts may omit counterarguments and are navigation aids, never independent evidence."
)


class TurnResponse(Contract):
    message: str = Field(min_length=1, pattern=r"\S")
    evidence_ids: list[str]
    figure_ids: list[str]


class SummaryResponse(Contract):
    summary: str = Field(min_length=1, pattern=r"\S")


def _model_history(turns: list[DebateTurn]) -> list[dict[str, Any]]:
    """Preserve arguments and citations without repeating full figure cards."""
    return [{"round_number": turn.round_number, "speaker": turn.speaker,
             "stance": turn.stance, "message": turn.message,
             "evidence_ids": list(turn.evidence_ids),
             "figure_ids": [figure.figure_id for figure in turn.figures]}
            for turn in turns]


def _model_figures(figures: list[SourcedFigure]) -> list[dict[str, Any]]:
    # Full source excerpts are already present in evidence. The figure's
    # evidence_id/source_id preserve its connection to that exact passage.
    return [figure.model_dump(mode="json", exclude={"excerpt"}) for figure in figures]


def _turn_schema(result: AnalysisResult) -> dict[str, Any]:
    """Constrain generation to this analysis without weakening validation."""
    schema = TurnResponse.model_json_schema()
    for field, ids in (
        ("evidence_ids", [item.evidence_id for item in result.context_draft.evidence]),
        ("figure_ids", [item.figure_id for item in result.figures]),
    ):
        array = schema["properties"][field]
        if ids:
            array["items"] = {"type": "string", "enum": list(dict.fromkeys(ids))}
        else:
            array["maxItems"] = 0
    return schema


class DebateService:
    def __init__(self, settings: Settings, repository: IntelligenceRepository, llm: Any):
        self.settings = settings
        self.repository = repository
        self.llm = llm

    def create_session(self, request: DebateRequest) -> DebateSession:
        result = self.repository.get_analysis(request.analysis_id)
        if result.project_id != request.project_id:
            raise IntelligenceError("ANALYSIS_PROJECT_MISMATCH", "The analysis belongs to another project.", 409)
        session = DebateSession(debate_id=f"d_{uuid.uuid4().hex}", project_id=request.project_id,
                                analysis_id=request.analysis_id, mode=request.mode,
                                user_stance=request.user_stance, max_rounds=request.max_rounds,
                                model_id=(request.model_id or self.settings.llm_model or None)
                                if self.settings.app_mode == "live" else None)
        self._validate_session_model(session)
        self.repository.save_debate(session)
        return session

    async def prepare(self, session: DebateSession, progress: Progress) -> DebateSession:
        self._validate_session_model(session)
        self.repository.claim_debate(session.debate_id)
        try:
            return await self._continue(session, progress)
        except BaseException:
            session.status = "failed"
            self._save(session)
            raise
        finally:
            self.repository.release_debate(session.debate_id)

    async def resume(self, debate_id: str, progress: Progress) -> DebateSession:
        # Claim before loading or changing state so a concurrent attempt cannot
        # mark a running session as failed or replay its already validated turns.
        self.repository.claim_debate(debate_id)
        session = None
        try:
            current = self.repository.get_debate(debate_id)
            self._validate_session_model(current)
            if current.status != "failed":
                raise IntelligenceError("DEBATE_NOT_INTERRUPTED", "Only an interrupted debate can be resumed.", 409)
            self._validate_resume_prefix(current)
            session = current.model_copy(deep=True)
            session.status = "active"
            self._save(session)
            return await self._continue(session, progress)
        except BaseException:
            if session is not None:
                session.status = "failed"
                self._save(session)
            raise
        finally:
            self.repository.release_debate(debate_id)

    def _validate_session_model(self, session: DebateSession) -> None:
        # A session's model is fixed when it is created. Legacy sessions without
        # this field continue on the configured default, while a pinned session
        # must never silently switch models during a reply or recovery.
        if session.model_id and session.model_id != self.settings.llm_model:
            raise IntelligenceError(
                "DEBATE_MODEL_MISMATCH",
                "This debate must continue with the model it was started with.",
                409,
                details={"model_id": session.model_id, "service_model_id": self.settings.llm_model},
            )

    @staticmethod
    def _validate_resume_prefix(session: DebateSession) -> None:
        if session.mode == "user_vs_agent":
            if session.turns:
                raise IntelligenceError("DEBATE_RESUME_UNAVAILABLE", "This user debate already has an opening. Retry its argument instead.", 409)
            return
        if len(session.turns) > session.max_rounds * 2:
            raise IntelligenceError("DEBATE_TRANSCRIPT_INVALID", "The saved turn order cannot be resumed safely.", 409)
        for index, turn in enumerate(session.turns):
            stance = "for" if index % 2 == 0 else "against"
            speaker = "pro" if stance == "for" else "skeptic"
            if turn.round_number != index // 2 + 1 or turn.stance != stance or turn.speaker != speaker:
                raise IntelligenceError("DEBATE_TRANSCRIPT_INVALID", "The saved turn order cannot be resumed safely.", 409)

    async def _continue(self, session: DebateSession, progress: Progress) -> DebateSession:
        if session.mode == "agent_vs_agent":
            for index in range(len(session.turns), session.max_rounds * 2):
                round_number = index // 2 + 1
                stance = "for" if index % 2 == 0 else "against"
                await progress(f"debating_round_{round_number}_{stance}", 10 + int(75 * index / (session.max_rounds * 2)))
                session.turns.append(await self._agent_turn(session, stance, round_number))
                self._save(session)
            await progress("moderating", 93)
            session.summary = await self._summary(session)
            session.status = "completed"
        else:
            await progress("opening_argument", 50)
            opposite = "against" if session.user_stance == "for" else "for"
            session.turns.append(await self._agent_turn(session, opposite, 0))
        self._save(session)
        return session

    async def reply(self, debate_id: str, message: str, progress: Progress) -> DebateSession:
        self.repository.claim_debate(debate_id)
        original = None
        try:
            current = self.repository.get_debate(debate_id)
            self._validate_session_model(current)
            original = current
            session = original.model_copy(deep=True)
            if session.mode != "user_vs_agent":
                raise IntelligenceError("WATCH_ONLY_DEBATE", "Agent debates run automatically. Start a user debate to send arguments.", 409)
            if session.status != "active":
                raise IntelligenceError("DEBATE_FINISHED", "This debate has finished. Start a new debate to continue.", 409)
            number = sum(turn.speaker == "user" for turn in session.turns) + 1
            if number > session.max_rounds:
                raise IntelligenceError("ROUND_LIMIT", "This debate has reached its round limit.", 409)
            session.turns.append(DebateTurn(turn_id=f"t_{uuid.uuid4().hex}", round_number=number,
                                           speaker="user", stance=session.user_stance, message=message))
            await progress("responding", 35)
            opposite = "against" if session.user_stance == "for" else "for"
            session.turns.append(await self._agent_turn(session, opposite, number))
            if number == session.max_rounds:
                await progress("moderating", 85)
                session.summary = await self._summary(session)
                session.status = "completed"
            self._save(session)
            return session
        except BaseException:
            # A failed model call leaves the session exactly as it was, permitting a new attempt.
            if original is not None:
                self.repository.save_debate(original)
            raise
        finally:
            self.repository.release_debate(debate_id)

    def _save(self, session: DebateSession) -> None:
        session.updated_at = utc_now()
        self.repository.save_debate(session)

    def _model_payload(self, session: DebateSession, task: str, payload: dict[str, Any], schema: dict) -> dict[str, Any]:
        """Bound long model history without changing the saved/public transcript.

        Recent exchanges and source evidence are always verbatim. Older turns
        keep their round, role, exact text prefix and all evidence/figure IDs in
        a compact table. There is no generated or inferred summary in this step.
        """
        history = _model_history(session.turns)
        full = {**payload, "history": history}
        if len(history) <= RECENT_VERBATIM_TURNS:
            return full

        def prompt_bytes(value: dict) -> int:
            return sum(len(message["content"].encode("utf-8"))
                       for message in build_messages(task, value, schema))

        base_bytes = prompt_bytes({**payload, "history": []})
        prompt_budget = base_bytes + HISTORY_BYTE_BUDGET
        if self.settings.llm_provider == "ollama":
            available_tokens = (self.settings.ollama_num_ctx - self.settings.ollama_num_predict
                                - OLLAMA_CONTEXT_SAFETY_TOKENS)
            # Match the provider's conservative estimate and leave extra room
            # for message framing. Never raise the configured model context.
            local_budget = int(available_tokens * self.settings.ollama_context_bytes_per_token) - 512
            prompt_budget = min(prompt_budget, local_budget)
        if prompt_bytes(full) <= prompt_budget:
            return full

        earlier, recent = history[:-RECENT_VERBATIM_TURNS], history[-RECENT_VERBATIM_TURNS:]
        evidence_ids = list(dict.fromkeys(ref for turn in earlier for ref in turn["evidence_ids"]))
        figure_ids = list(dict.fromkeys(ref for turn in earlier for ref in turn["figure_ids"]))
        evidence_indices = {ref: index for index, ref in enumerate(evidence_ids)}
        figure_indices = {ref: index for index, ref in enumerate(figure_ids)}

        def compact(excerpt_bytes: int) -> dict:
            rows = []
            for turn in earlier:
                prefix = turn["message"].encode("utf-8")[:excerpt_bytes].decode("utf-8", errors="ignore")
                rows.append([turn["round_number"], turn["speaker"], turn["stance"], prefix,
                             prefix == turn["message"],
                             [evidence_indices[ref] for ref in turn["evidence_ids"]],
                             [figure_indices[ref] for ref in turn["figure_ids"]]])
            return {**payload, "history": recent, "history_note": HISTORY_NOTE,
                    "earlier_history": {
                        "format": "exact_prefix_table", "turn_count": len(earlier),
                        "columns": ["round_number", "speaker", "stance", "message_prefix", "message_complete",
                                    "evidence_indices", "figure_indices"],
                        "evidence_ids": evidence_ids, "figure_ids": figure_ids, "rows": rows}}

        # At least a short exact prefix is retained for every older turn. If
        # citations, sources and recent turns alone are too large, fail clearly
        # instead of silently removing a turn or an evidence passage.
        if prompt_bytes(compact(32)) > prompt_budget:
            raise IntelligenceError(
                "MODEL_CONTEXT_LIMIT",
                "The source evidence and recent debate exchanges exceed this model's context budget even with labelled older excerpts. Use a supported larger context; the saved transcript remains intact.",
                status_code=422, details={"task": task, "history_turns": len(history),
                                         "recent_verbatim_turns": RECENT_VERBATIM_TURNS})
        low, high = 32, 256
        while low < high:
            middle = (low + high + 1) // 2
            if prompt_bytes(compact(middle)) <= prompt_budget:
                low = middle
            else:
                high = middle - 1
        session.history_compacted = True
        session.history_note = HISTORY_NOTE
        return compact(low)

    async def _agent_turn(self, session: DebateSession, stance: str, number: int) -> DebateTurn:
        result = self.repository.get_analysis(session.analysis_id)
        if self.settings.app_mode == "demo":
            await asyncio.sleep(self.settings.demo_delay)
            response = self._demo_turn(session, result, stance, number)
        else:
            if self.llm is None:
                raise IntelligenceError("PROVIDER_CONFIGURATION", "Configure a live language model before debating.", 503)
            payload = {"idea": result.context_draft.idea.model_dump(mode="json"), "stance": stance,
                       "round_number": number, "mode": session.mode,
                       "evidence": [e.model_dump(mode="json") for e in result.context_draft.evidence],
                       "figures": _model_figures(result.figures),
                       "analysis": result.context_draft.final_analysis.model_dump(mode="json")}
            schema = _turn_schema(result)
            feedback = None
            for attempt in range(TURN_GENERATION_ATTEMPTS):
                attempt_payload = dict(payload)
                attempt_schema = schema
                qualitative_correction = attempt == TURN_GENERATION_ATTEMPTS - 1 and feedback is not None
                if feedback:
                    attempt_payload["validation_feedback"] = feedback
                    await asyncio.sleep(0)
                if qualitative_correction:
                    # Regenerate a qualitative argument rather than silently
                    # removing an invalid statistic from the rejected response.
                    attempt_schema = _turn_schema(result)
                    attempt_schema["properties"]["figure_ids"] = {
                        "type": "array", "items": {"type": "string"}, "maxItems": 0}
                    attempt_payload["validation_feedback"] = {
                        **feedback,
                        "instruction": "Regenerate a qualitative argument with figure_ids as an empty array and no numerical claims. Copy only exact valid evidence IDs from the schema when relevant, or return evidence_ids as an empty array and explain the uncertainty. Do not claim a statistic supports this argument.",
                    }
                attempt_payload = self._model_payload(session, "debate_turn", attempt_payload, attempt_schema)
                try:
                    response = TurnResponse.model_validate(await self.llm.generate_json(
                        "debate_turn", attempt_payload, attempt_schema))
                    self._validate_turn_references(response, result)
                    if qualitative_correction and response.figure_ids:
                        raise IntelligenceError("DEBATE_INVALID_RESPONSE", "The qualitative correction must return an empty figure selection.")
                    break
                except (ValidationError, IntelligenceError) as exc:
                    if isinstance(exc, IntelligenceError) and exc.code != "DEBATE_INVALID_RESPONSE":
                        raise
                    feedback = {
                        "error": exc.message if isinstance(exc, IntelligenceError) else "The previous argument did not match TurnResponse.",
                        "details": exc.details if isinstance(exc, IntelligenceError) else {},
                        "instruction": "Regenerate the complete argument using only IDs in the output schema enums. Use empty arrays when no supplied evidence supports it. Figures must include their matching evidence ID. Do not invent, repair by guessing, or cite IDs in prose.",
                    }
                    if attempt == TURN_GENERATION_ATTEMPTS - 1:
                        recovery = ("Resume the debate to retry" if session.mode == "agent_vs_agent"
                                    or number == 0 else "Send your argument again to retry")
                        raise IntelligenceError(
                            "DEBATE_INVALID_RESPONSE", f"The agent could not produce a valid cited argument after correction attempts. {recovery}; validated arguments are preserved.",
                            retryable=True, details={"attempts": TURN_GENERATION_ATTEMPTS,
                                                     "round_number": number, "stance": stance,
                                                     "last_validation": feedback}) from None
        self._validate_turn_references(response, result)
        evidence_by_id = {item.evidence_id: item for item in result.context_draft.evidence}
        figure_by_id = {figure.figure_id: figure for figure in result.figures}
        valid_evidence = list(dict.fromkeys(ref for ref in response.evidence_ids if ref in evidence_by_id))
        selected_figures = [figure for figure in result.figures
                            if figure.figure_id in response.figure_ids and figure.evidence_id in valid_evidence]
        message = self._guard_numbers(response.message)
        return DebateTurn(turn_id=f"t_{uuid.uuid4().hex}", round_number=number,
                          speaker="pro" if stance == "for" else "skeptic", stance=stance,
                          message=message, evidence_ids=valid_evidence,
                          source_ids=list(dict.fromkeys(evidence_by_id[eid].source_id for eid in valid_evidence)),
                          figures=selected_figures)

    @staticmethod
    def _validate_turn_references(response: TurnResponse, result: AnalysisResult) -> None:
        evidence_ids = {item.evidence_id for item in result.context_draft.evidence}
        figure_by_id = {figure.figure_id: figure for figure in result.figures}
        if any(ref not in evidence_ids for ref in response.evidence_ids) or any(ref not in figure_by_id for ref in response.figure_ids):
            raise IntelligenceError("DEBATE_INVALID_RESPONSE", "The argument cited an ID outside the supplied evidence or figures.",
                                    details={"invalid_evidence_ids": [ref for ref in response.evidence_ids if ref not in evidence_ids],
                                             "invalid_figure_ids": [ref for ref in response.figure_ids if ref not in figure_by_id]})
        missing = {ref: figure_by_id[ref].evidence_id for ref in response.figure_ids
                   if figure_by_id[ref].evidence_id not in response.evidence_ids}
        if missing:
            raise IntelligenceError("DEBATE_INVALID_RESPONSE", "A selected figure is missing its matching evidence citation.",
                                    details={"selected_evidence_ids": response.evidence_ids,
                                             "required_figure_evidence": missing})

    @staticmethod
    def _guard_numbers(message: str) -> str:
        # Keep numerical evidence in verbatim, validated figure cards. A matching
        # value alone cannot establish that a model has kept the source's metric.
        written_quantity = re.compile(
            r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
            r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
            r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion)"
            r"(?:[\s-]+(?:\w+)){0,3}[\s-]+(?:percent|million|billion|thousand|crore|lakh|dollars?|rupees?|"
            r"sales|customers|users|respondents|participants|students|households)\b", re.IGNORECASE)
        sentences = re.split(r"(?<=[.!?])\s+", message)
        safe = []
        for sentence in sentences:
            if re.search(r"\d", sentence) or written_quantity.search(sentence):
                safe.append("Numerical claims are shown only in the quoted source figure cards below.")
            else:
                safe.append(sentence)
        return " ".join(safe)

    @staticmethod
    def _demo_turn(session: DebateSession, result: AnalysisResult, stance: str, number: int) -> TurnResponse:
        idea = result.context_draft.idea
        evidence = result.context_draft.evidence
        chosen = evidence[(number + (stance == "against")) % len(evidence)] if evidence else None
        refs = [chosen.evidence_id] if chosen else []
        figures = [f for f in result.figures if f.evidence_id in refs][:1]
        user_turn = next((t for t in reversed(session.turns) if t.speaker == "user"), None)
        previous = session.turns[-1] if session.turns else None
        reply = (f"You argued: “{user_turn.message[:200]}”. " if user_turn else
                 "The previous argument is a useful hypothesis, but it needs a measurable test. " if previous else "")
        if stance == "for":
            positions = [
                f"The case for {idea.project_name} is that the stated problem is specific enough to test with its target users. Start with a small pilot and measure repeat use before expanding.",
                "The objection about uncertain demand is fair. A narrow experiment can distinguish genuine willingness to pay from friendly feedback; that makes the idea worth testing rather than assuming it will succeed.",
                "The strongest version of this idea focuses on one customer segment and one clear outcome. Set a stop condition and compare the proposed solution with what users currently do.",
            ]
        else:
            positions = [
                f"The case against accepting {idea.project_name} yet is the absence of verified demand and competitor evidence. The idea describes a proposed solution; it does not establish willingness to pay.",
                "A pilot can help, but a small or self-selected sample cannot establish market size. We need dated sources, a defined geography, and an explanation of which customers the figures actually describe.",
                "Even if users like the concept, delivery cost and existing alternatives may weaken it. Identify the assumptions that could make the project fail and test those first.",
            ]
        # Demo mode cycles its labelled teaching arguments throughout any
        # requested length instead of repeating only the last one after round 3.
        message = reply + positions[max(number - 1, 0) % len(positions)]
        if chosen:
            message += " The attached evidence passage explains the research exercise and its limitations."
        if figures:
            message += " The attached figure is explicitly synthetic demonstration data."
        message += " This is a scripted demo argument using fixture evidence, not a live AI judgement or a real market conclusion."
        return TurnResponse(message=message, evidence_ids=refs, figure_ids=[f.figure_id for f in figures])

    async def _summary(self, session: DebateSession) -> str:
        result = self.repository.get_analysis(session.analysis_id)
        if self.settings.app_mode == "demo":
            return ("The debate identifies a testable problem, but real demand, willingness to pay, competitors and delivery costs remain unresolved. "
                    "Next: interview the intended users, run a small pilot, and collect dated market evidence with clear geography and methodology. "
                    "The figures and arguments shown in this demo are synthetic teaching material; they do not validate the startup.")
        payload = {"idea": result.context_draft.idea.model_dump(mode="json"),
                   "evidence": [e.model_dump(mode="json") for e in result.context_draft.evidence],
                   "figures": _model_figures(result.figures)}
        schema = SummaryResponse.model_json_schema()
        payload = self._model_payload(session, "debate_summary", payload, schema)
        try:
            summary = SummaryResponse.model_validate(await self.llm.generate_json(
                "debate_summary", payload, schema))
        except ValidationError:
            raise IntelligenceError("DEBATE_INVALID_RESPONSE", "The moderator returned an invalid summary.") from None
        return self._guard_numbers(summary.summary)
