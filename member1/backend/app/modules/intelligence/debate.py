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
from .settings import Settings


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


class DebateService:
    def __init__(self, settings: Settings, repository: IntelligenceRepository, llm: Any):
        self.settings = settings
        self.repository = repository
        self.llm = llm

    def create_session(self, request: DebateRequest) -> DebateSession:
        result = self.repository.get_analysis(request.analysis_id)
        if result.project_id != request.project_id:
            raise IntelligenceError("ANALYSIS_PROJECT_MISMATCH", "The analysis belongs to another project.", 409)
        if result.context_draft.critic.review_status == "blocked":
            raise IntelligenceError("CONTEXT_BLOCKED", "Resolve the blocked evidence review before starting a debate.", 422)
        session = DebateSession(debate_id=f"d_{uuid.uuid4().hex}", project_id=request.project_id,
                                analysis_id=request.analysis_id, mode=request.mode,
                                user_stance=request.user_stance, max_rounds=request.max_rounds)
        self.repository.save_debate(session)
        return session

    async def prepare(self, session: DebateSession, progress: Progress) -> DebateSession:
        self.repository.claim_debate(session.debate_id)
        try:
            if session.mode == "agent_vs_agent":
                for round_number in range(1, session.max_rounds + 1):
                    for stance in ("for", "against"):
                        done = (round_number - 1) * 2 + (stance == "against")
                        await progress(f"debating_round_{round_number}_{stance}", 10 + int(75 * done / (session.max_rounds * 2)))
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
        except BaseException:
            session.status = "failed"
            self._save(session)
            raise
        finally:
            self.repository.release_debate(session.debate_id)

    async def reply(self, debate_id: str, message: str, progress: Progress) -> DebateSession:
        self.repository.claim_debate(debate_id)
        original = None
        try:
            original = self.repository.get_debate(debate_id)
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
                       "history": _model_history(session.turns),
                       "evidence": [e.model_dump(mode="json") for e in result.context_draft.evidence],
                       "figures": _model_figures(result.figures),
                       "analysis": result.context_draft.final_analysis.model_dump(mode="json")}
            try:
                response = TurnResponse.model_validate(await self.llm.generate_json(
                    "debate_turn", payload, TurnResponse.model_json_schema()))
            except ValidationError:
                raise IntelligenceError("DEBATE_INVALID_RESPONSE", "The debate agent returned an invalid argument.") from None
        evidence_by_id = {item.evidence_id: item for item in result.context_draft.evidence}
        figure_by_id = {figure.figure_id: figure for figure in result.figures}
        if any(ref not in evidence_by_id for ref in response.evidence_ids) or any(ref not in figure_by_id for ref in response.figure_ids):
            raise IntelligenceError("DEBATE_INVALID_RESPONSE", "The debate agent cited evidence that does not exist.")
        valid_evidence = list(dict.fromkeys(ref for ref in response.evidence_ids if ref in evidence_by_id))
        if any(figure_by_id[ref].evidence_id not in valid_evidence for ref in response.figure_ids):
            raise IntelligenceError("DEBATE_INVALID_RESPONSE", "A debate figure is missing its evidence citation.")
        selected_figures = [figure for figure in result.figures
                            if figure.figure_id in response.figure_ids and figure.evidence_id in valid_evidence]
        message = self._guard_numbers(response.message)
        return DebateTurn(turn_id=f"t_{uuid.uuid4().hex}", round_number=number,
                          speaker="pro" if stance == "for" else "skeptic", stance=stance,
                          message=message, evidence_ids=valid_evidence,
                          source_ids=list(dict.fromkeys(evidence_by_id[eid].source_id for eid in valid_evidence)),
                          figures=selected_figures)

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
        message = reply + positions[min(max(number - 1, 0), 2)]
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
                   "history": _model_history(session.turns),
                   "evidence": [e.model_dump(mode="json") for e in result.context_draft.evidence],
                   "figures": _model_figures(result.figures)}
        try:
            summary = SummaryResponse.model_validate(await self.llm.generate_json(
                "debate_summary", payload, SummaryResponse.model_json_schema()))
        except ValidationError:
            raise IntelligenceError("DEBATE_INVALID_RESPONSE", "The moderator returned an invalid summary.") from None
        return self._guard_numbers(summary.summary)
