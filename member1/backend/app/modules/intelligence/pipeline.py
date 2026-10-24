"""Independent Member 1 service: research, retrieval, analysis, critique.

The service returns a ContextDraft and never writes another module's project
database. Providers are injected, so fixtures and real providers share the same
contract. Live failures are surfaced; they never switch to demo mode.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .contracts import (
    AnalysisReport, AnalysisResult, ContextDraft, CriticIssue, CritiqueReport,
    EvidenceItem, ExecutionInfo, IdeaSpec, ResearchDocument, ResearchOptions,
)
from .demo import DEMO_WARNING, DemoLLM
from .errors import IntelligenceError
from .grounding import (
    ExtractedIdea, comparable_claim_text, ground_idea, quotation_issue, quoted_claim_text,
)
from .settings import Settings


ProgressCallback = Callable[[str, int], Awaitable[None]]


class QueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    queries: list[str] = Field(min_length=1, max_length=3)

    @field_validator("queries")
    @classmethod
    def focused_queries(cls, value: list[str]) -> list[str]:
        if any(not query.strip() or len(query) > 250 for query in value):
            raise ValueError("Search queries must contain 1 to 250 characters")
        return list(dict.fromkeys(query.strip() for query in value))


def _errors(error: ValidationError) -> list[dict[str, Any]]:
    # Avoid echoing submitted ideas or external pages through error responses.
    return [{"path": ".".join(map(str, item["loc"])), "type": item["type"], "message": item["msg"]}
            for item in error.errors()]


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


_WRITTEN_QUANTITY = re.compile(
    r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion)"
    r"(?:[\s-]+(?:\w+)){0,3}[\s-]+(?:percent|million|billion|thousand|crore|lakh|dollars?|rupees?|"
    r"sales|customers|users|respondents|participants|students|households)\b", re.IGNORECASE,
)


def _has_numerical_assertion(text: str) -> bool:
    # A classification badge cannot validate a paraphrased statistic or forecast.
    # This deliberately conservative check covers digit-bearing dates/quantities
    # and common written quantities; it is not a general semantic verifier.
    return bool(re.search(r"\d", text) or _WRITTEN_QUANTITY.search(text))


class IntelligenceService:
    def __init__(self, settings: Settings, llm: Any, search: Any, retriever: Any):
        if settings.app_mode not in {"demo", "live"}:
            raise IntelligenceError("INVALID_CONFIGURATION", "APP_MODE must be demo or live.", 500)
        self.settings = settings
        self.llm = llm
        self.search = search
        self.retriever = retriever

    async def _generate(
        self, task: str, payload: dict[str, Any], model: type[BaseModel],
        supplied_values: dict[str, Any] | None = None,
    ) -> Any:
        try:
            result = await self.llm.generate_json(task, payload, model.model_json_schema())
            if supplied_values and isinstance(result, dict):
                result = {**result, **supplied_values}
            return model.model_validate(result)
        except IntelligenceError:
            raise
        except ValidationError as error:
            raise IntelligenceError(
                "LLM_INVALID_RESPONSE", f"The language model returned an invalid {task} response.",
                details={"task": task, "validation_errors": _errors(error)},
            ) from error
        except (TimeoutError, asyncio.TimeoutError) as error:
            raise IntelligenceError("PROVIDER_TIMEOUT", "The language model request timed out.",
                                    retryable=True, details={"task": task}) from error
        except Exception as error:
            raise IntelligenceError("LLM_PROVIDER_ERROR", "The language model request failed.",
                                    retryable=True, details={"task": task}) from error

    async def extract(self, idea_text: str) -> IdeaSpec:
        if not isinstance(idea_text, str) or not 10 <= len(idea_text) <= 12000:
            raise IntelligenceError("INVALID_IDEA", "Idea text must contain 10 to 12000 characters.", 422)
        # Preserve the actual submitted input even if the model rewrites, omits,
        # or substitutes original_text. Other fields still require validation.
        if self.settings.app_mode == "demo":
            return await self._generate("extract", {"idea_text": idea_text}, IdeaSpec,
                                        supplied_values={"original_text": idea_text})
        candidate = await self._generate("extract", {"idea_text": idea_text}, ExtractedIdea,
                                         supplied_values={"original_text": idea_text})
        return ground_idea(candidate, idea_text)

    async def analyze(
        self, project_id: str, idea: IdeaSpec, research_options: ResearchOptions,
        progress: ProgressCallback | None = None,
    ) -> AnalysisResult:
        stages: list[str] = []
        warnings: list[str] = [DEMO_WARNING] if self.settings.app_mode == "demo" else []
        if getattr(self.search, "name", self.settings.search_provider) == "duckduckgo":
            warnings.append("DuckDuckGo supplies search snippets, not full publications. Verify every cited statistic, date, geography and methodology against the full publication before relying on it.")
        if getattr(self.search, "name", self.settings.search_provider) == "crossref":
            warnings.append("Crossref supplies deposited scholarly abstracts and metadata, not full publications or verified current market data. Relevance, publication details, statistics, geography and language coverage require verification.")
        if getattr(self.search, "name", self.settings.search_provider) == "web":
            warnings.append("Web research fetches a bounded set of public pages and text-based PDF passages. Source quotations are traceable but publication dates, geography, methodology and commercial relevance require review. Search snippets are not used as evidence.")
            warnings.append("Web retrieval selects at most six relevant passages from the fetched documents for this local model; inspect original sources for full report coverage.")
        if not isinstance(project_id, str) or not project_id.strip() or len(project_id) > 100:
            raise IntelligenceError("INVALID_PROJECT_ID", "Project ID must contain 1 to 100 characters.", 422)
        try:
            idea = IdeaSpec.model_validate(idea)
            research_options = ResearchOptions.model_validate(research_options)
        except ValidationError as error:
            raise IntelligenceError("INVALID_ANALYSIS_INPUT", "Analysis input does not match the shared contract.",
                                    422, details={"validation_errors": _errors(error)}) from error
        if research_options.source_urls and getattr(self.search, "name", self.settings.search_provider) != "web":
            raise IntelligenceError("USE_WEB_PROVIDER", "Supplied report URLs require SEARCH_PROVIDER=web in live mode.", 422)

        async def stage(name: str, percent: int) -> None:
            stages.append(name)
            if progress:
                await progress(name, percent)

        await stage("query_generation", 10)
        plan: QueryPlan = await self._generate("queries", {
            "idea": idea.model_dump(mode="json"),
            "research_options": research_options.model_dump(mode="json"),
            "search_provider": getattr(self.search, "name", self.settings.search_provider),
        }, QueryPlan)

        await stage("research", 25)
        crossref = getattr(self.search, "name", self.settings.search_provider) == "crossref"
        research_queries = plan.queries[:1] if crossref else plan.queries
        gaps: list[str] = []
        documents = await self._research(research_queries, research_options, warnings, gaps, idea)
        live_web = (self.settings.app_mode == "live"
                    and getattr(self.search, "name", self.settings.search_provider) == "web")
        if live_web:
            from .business_evidence import filter_business_documents
            documents, business_warnings, business_gaps = filter_business_documents(documents, idea, research_options)
            warnings.extend(business_warnings)
            gaps.extend(business_gaps)
        if getattr(self.search, "name", self.settings.search_provider) == "crossref":
            filter_topic = getattr(self.search, "filter_for_idea", None)
            if callable(filter_topic):
                original_count = len(documents)
                documents = filter_topic(documents, idea, research_options)
                if len(documents) < original_count:
                    warnings.append("Scholarly abstracts that did not match the reviewed solution's core topic were omitted by conservative lexical filtering. Topic overlap alone does not establish claim support.")
        source_ids: set[str] = set()
        bounded_documents: list[ResearchDocument] = []
        for document in documents:
            if document.source.source_id in source_ids:
                warnings.append("A duplicate research source was omitted.")
                continue
            if self.settings.app_mode == "demo" and document.source.kind != "fixture":
                raise IntelligenceError("INVALID_DEMO_SOURCE", "Demo research must use explicitly marked fixture sources.", 500)
            source_ids.add(document.source.source_id)
            bounded_documents.append(document)
            if len(bounded_documents) >= research_options.max_sources:
                break
        documents = bounded_documents
        sources = [document.source for document in documents]
        if getattr(self.search, "name", self.settings.search_provider) == "duckduckgo":
            gaps.append("Research evidence consists of search snippets; full publications and their market statistics still require verification.")
        if getattr(self.search, "name", self.settings.search_provider) == "crossref":
            warnings.append("Crossref research executes only the primary scholarly topic query from a bounded candidate pool; coverage is limited to this query.")
            if len(plan.queries) > 1:
                warnings.append("Other planned research queries were not searched by Crossref; alternative coverage is incomplete.")
            gaps.append("Research evidence consists of scholarly abstracts and metadata; full publications, current market statistics, regional applicability and requested language coverage remain unverified.")
            gaps.append("Only the primary Crossref query was searched; alternative, regional and counterevidence coverage remains incomplete.")
            if len(sources) < research_options.max_sources:
                warnings.append(f"Crossref returned {len(sources)} of up to {research_options.max_sources} requested sources after conservative topic filtering; missing evidence remains a research gap.")
                gaps.append("The requested source budget was not filled with relevant scholarly abstracts; this does not prove that no relevant publications exist.")
        if not sources:
            gaps.append("No research sources were returned; project-specific conclusions remain unverified.")

        await stage("retrieval", 40)
        # Discovery queries can be broad; passage selection must remain rooted in
        # the reviewed product rather than a demographic-only market query.
        retrieval_queries = [idea.solution] if live_web else research_queries
        if live_web and idea.field_origins.get("problem") == "provided":
            retrieval_queries.append(idea.problem)
        evidence = await self._retrieve(documents, retrieval_queries)
        if live_web:
            from .business_evidence import filter_business_evidence
            evidence, business_warnings, business_gaps = filter_business_evidence(evidence, idea, research_options)
            warnings.extend(business_warnings)
            gaps.extend(business_gaps)
        known_ids: set[str] = set()
        clean_evidence: list[EvidenceItem] = []
        for item in evidence:
            if item.source_id not in source_ids or item.evidence_id in known_ids:
                warnings.append("An evidence item with an unknown source or duplicate ID was omitted.")
                continue
            known_ids.add(item.evidence_id)
            clean_evidence.append(item)
            if len(clean_evidence) >= 12:
                break
        evidence = clean_evidence
        if not evidence:
            gaps.append("No relevant evidence was retrieved; mark substantive conclusions as assumptions or unknowns.")

        payload = {
            "idea": idea.model_dump(mode="json"),
            "research_options": research_options.model_dump(mode="json"),
            "queries": research_queries,
            "sources": [source.model_dump(mode="json") for source in sources],
            "evidence": [item.model_dump(mode="json") for item in evidence],
            "gaps": gaps,
        }
        await stage("analysis", 60)
        initial: AnalysisReport = await self._generate("analyze", payload, AnalysisReport)
        await stage("critic", 75)
        review_payload = {
            "idea": payload["idea"], "initial_analysis": initial.model_dump(mode="json"),
            "evidence": payload["evidence"], "sources": payload["sources"], "gaps": gaps,
            "as_of_date": datetime.now(timezone.utc).date().isoformat(),
        }
        critic: CritiqueReport = await self._generate("critic", review_payload, CritiqueReport)
        critic = self._normalize_critic(critic, initial, known_ids, warnings)
        live_evidence = evidence if self.settings.app_mode == "live" else None
        self._merge_reference_issues(critic, initial, known_ids, live_evidence)
        revisions = 0
        final = initial.model_copy(deep=True)
        if critic.issues:
            await stage("revision", 85)
            final = await self._generate("revise", {
                **review_payload, "critic": critic.model_dump(mode="json"),
            }, AnalysisReport)
            revisions = 1

        await stage("validation", 95)
        final, guard_messages = self._guard_final(final, known_ids, live_evidence, critic, initial)
        if guard_messages:
            warnings.extend(guard_messages)
            final.gaps = _unique(final.gaps + [
                "Unsupported or invalidly cited statements were qualified by the deterministic evidence guard."
            ])
        final.gaps = _unique(final.gaps + gaps)
        if self.settings.app_mode == "live":
            warnings.append("Supported claims are complete literal source quotations. This verifies quotation traceability, not source accuracy, commercial relevance or semantic proof of the startup's market or demand.")
            # Free-form model summaries can silently reintroduce facts removed
            # from claims. Keep the original visible, but make the reviewed
            # summary reflect the actual guarded classifications.
            counts = {kind: sum(claim.kind == kind for section in final.sections for claim in section.claims)
                      for kind in ("supported", "assumption", "unknown")}
            final.summary = (f"Reviewed analysis contains {counts['supported']} traceable source quotations, "
                             f"{counts['assumption']} unvalidated hypotheses and {counts['unknown']} unknown conclusions. "
                             "Read the section claims and remaining gaps. Source quotations do not by themselves validate "
                             "this startup's demand, market size, feasibility or financial outcomes.")
        self._resolve_critic(critic, final, known_ids, revisions, live_evidence)
        warnings = _unique(warnings)
        try:
            context = ContextDraft(
                idea=idea, sources=sources, evidence=evidence,
                initial_analysis=initial, final_analysis=final, critic=critic,
                warnings=warnings,
                execution=ExecutionInfo(
                    mode=self.settings.app_mode,
                    llm_provider=getattr(self.llm, "name", self.settings.llm_provider),
                    search_provider=getattr(self.search, "name", self.settings.search_provider),
                    retrieval_method=getattr(self.retriever, "method", self.settings.retrieval_provider),
                    revision_passes=revisions,
                    model_id=(self.settings.llm_model or None) if self.settings.app_mode == "live" else None,
                ),
            )
        except ValidationError as error:
            raise IntelligenceError("INVALID_CONTEXT", "Analysis could not satisfy the shared context contract.",
                                    details={"validation_errors": _errors(error)}) from error
        await stage("completed", 100)
        return AnalysisResult(
            analysis_id=f"analysis_{uuid4().hex}", project_id=project_id,
            context_draft=context, stages=stages, warnings=warnings,
            model_id=(self.settings.llm_model or None) if self.settings.app_mode == "live" else None,
        )

    async def _research(self, queries: list[str], options: ResearchOptions,
                        warnings: list[str] | None = None, gaps: list[str] | None = None,
                        idea: IdeaSpec | None = None) -> list[ResearchDocument]:
        try:
            detailed_search = getattr(self.search, "search_with_diagnostics", None)
            idea_search = getattr(self.search, "search_for_idea", None)
            if callable(idea_search) and idea is not None:
                batch = await idea_search(queries, options, idea)
            elif callable(detailed_search):
                batch = await detailed_search(queries, options)
            else:
                batch = None
            if batch is not None:
                documents = batch.documents
                if warnings is not None:
                    warnings.extend(batch.warnings)
                if gaps is not None:
                    gaps.extend(batch.gaps)
            else:
                documents = await self.search.search(queries, options)
            return [ResearchDocument.model_validate(item) for item in documents]
        except IntelligenceError:
            raise
        except ValidationError as error:
            raise IntelligenceError("INVALID_SEARCH_RESPONSE", "Search returned invalid research documents.",
                                    details={"validation_errors": _errors(error)}) from error
        except (TimeoutError, asyncio.TimeoutError) as error:
            raise IntelligenceError("PROVIDER_TIMEOUT", "The research provider timed out.", retryable=True) from error
        except Exception as error:
            raise IntelligenceError("SEARCH_PROVIDER_ERROR", "The research provider failed.", retryable=True) from error

    async def _retrieve(self, documents: list[ResearchDocument], queries: list[str]) -> list[EvidenceItem]:
        if not documents:
            return []
        try:
            limit = 6 if getattr(self.search, "name", self.settings.search_provider) == "web" else 12
            items = await self.retriever.retrieve(documents, " ".join(queries), limit=limit)
            return [EvidenceItem.model_validate(item) for item in items]
        except IntelligenceError:
            raise
        except ValidationError as error:
            raise IntelligenceError("INVALID_RETRIEVAL_RESPONSE", "Retrieval returned invalid evidence.",
                                    details={"validation_errors": _errors(error)}) from error
        except Exception as error:
            raise IntelligenceError("RETRIEVAL_ERROR", "Evidence retrieval failed.", retryable=True) from error

    @staticmethod
    def _normalize_critic(
        critic: CritiqueReport, initial: AnalysisReport, known_ids: set[str], warnings: list[str],
    ) -> CritiqueReport:
        critic = critic.model_copy(deep=True)
        claim_ids = {claim.claim_id for section in initial.sections for claim in section.claims}
        seen: set[str] = set()
        for index, issue in enumerate(critic.issues):
            # Small local models sometimes put the referenced claim ID in
            # issue_id while leaving claim_id null. An exact known ID restores
            # traceability without guessing from the issue's prose or lowering
            # its severity. Explicit references and general concerns keep their
            # existing behavior.
            if issue.claim_id is None and issue.issue_id in claim_ids:
                issue.claim_id = issue.issue_id
                warnings.append("A missing critic claim reference was recovered from its exact claim ID.")
            # Only the service may label a concern as a deterministic guard
            # issue. Model-authored labels cannot grant automatic resolution.
            if issue.issue_id in seen or issue.issue_id.startswith(("guard_reference_", "guard_quotation_")):
                issue.issue_id = f"critic_{index}_{uuid4().hex[:8]}"
            seen.add(issue.issue_id)
            if issue.claim_id and issue.claim_id not in claim_ids:
                issue.claim_id = None
                warnings.append("A critic reference to an unknown claim was treated as a general review concern.")
            valid_refs = [ref for ref in issue.evidence_ids if ref in known_ids]
            if len(valid_refs) != len(issue.evidence_ids):
                warnings.append("Unknown evidence references were removed from the critic report.")
            issue.evidence_ids = _unique(valid_refs)
            # The critic runs before revision, so it cannot assert its issue fixed.
            issue.resolution = None
        # Review severity is advisory. Preserve the findings and revise unsafe
        # claims, but represent every non-clean review as gaps rather than a gate.
        if critic.review_status == "blocked":
            critic.unresolved_gaps = _unique(critic.unresolved_gaps + [
                "The review identified items that need validation; see the findings below."
            ])
        if critic.issues or critic.unresolved_gaps or critic.review_status != "ready":
            critic.review_status = "ready_with_gaps"
        else:
            critic.review_status = "ready"
        return critic

    @staticmethod
    def _merge_reference_issues(critic: CritiqueReport, report: AnalysisReport, known_ids: set[str],
                                evidence: list[EvidenceItem] | None = None) -> None:
        by_id = {item.evidence_id: item for item in evidence or []}
        for index, claim in enumerate(claim for section in report.sections for claim in section.claims):
            invalid = any(ref not in known_ids for ref in claim.evidence_ids)
            missing = claim.kind == "supported" and not claim.evidence_ids
            grounding_reason = quotation_issue(claim, by_id) if evidence is not None else None
            if not invalid and not missing and not grounding_reason:
                continue
            existing = any(issue.claim_id == claim.claim_id for issue in critic.issues)
            if existing:
                continue
            critic.issues.append(CriticIssue(
                issue_id=f"guard_reference_{index}" if invalid or missing else f"guard_quotation_{index}",
                severity="high" if claim.kind == "supported" else "medium",
                claim_id=claim.claim_id,
                reason=("Claim references unknown evidence." if invalid else
                        "Supported claim has no evidence references." if missing else grounding_reason),
                evidence_ids=[ref for ref in claim.evidence_ids if ref in known_ids],
            ))
        if critic.issues and critic.review_status == "ready":
            critic.review_status = "ready_with_gaps"

    @staticmethod
    def _guard_final(report: AnalysisReport, known_ids: set[str],
                     evidence: list[EvidenceItem] | None = None, critic: CritiqueReport | None = None,
                     initial: AnalysisReport | None = None) -> tuple[AnalysisReport, list[str]]:
        report = report.model_copy(deep=True)
        messages: list[str] = []
        seen: set[str] = set()
        by_id = {item.evidence_id: item for item in evidence or []}
        original = {claim.claim_id: claim for section in initial.sections for claim in section.claims} if initial else {}
        rejected = {issue.claim_id for issue in critic.issues
                    if issue.claim_id and issue.severity in {"high", "medium"}
                    and not issue.issue_id.startswith(("guard_reference_", "guard_quotation_"))} if critic else set()
        for index, claim in enumerate(claim for section in report.sections for claim in section.claims):
            original_id = claim.claim_id
            if claim.claim_id in seen:
                replacement = f"{claim.claim_id}_{index}"
                while replacement in seen:
                    replacement += "_unique"
                claim.claim_id = replacement
                messages.append("A duplicate final claim ID was assigned a unique ID.")
            seen.add(claim.claim_id)
            invalid = any(ref not in known_ids for ref in claim.evidence_ids)
            missing = claim.kind == "supported" and not claim.evidence_ids
            grounding_reason = quotation_issue(claim, by_id) if evidence is not None else None
            unchanged_rejected = (evidence is not None and original_id in rejected and original_id in original
                                  and comparable_claim_text(claim.text) == comparable_claim_text(original[original_id].text))
            if evidence is not None and (grounding_reason or unchanged_rejected):
                claim.kind = "unknown"
                claim.text = ("This conclusion remains unknown; the critic's concern requires further evidence or a validation test."
                              if unchanged_rejected else
                              "This conclusion remains unknown; the cited material does not directly establish it for the submitted startup.")
                claim.evidence_ids = []
                messages.append("An ungrounded or unchanged critic-rejected final claim was rewritten as an explicit unknown.")
            elif claim.kind == "supported" and (invalid or missing):
                claim.kind = "assumption"
                if not claim.text.lower().startswith("unverified assumption:"):
                    claim.text = "Unverified assumption: " + claim.text
                claim.evidence_ids = []
                messages.append("An unsupported final claim was qualified as an assumption.")
            else:
                claim.evidence_ids = _unique([ref for ref in claim.evidence_ids if ref in known_ids])
                if invalid:
                    messages.append("Unknown evidence references were removed from a final claim.")
                if evidence is not None and claim.kind == "supported":
                    claim.text = quoted_claim_text(claim.text)
                elif evidence is not None and claim.evidence_ids:
                    claim.evidence_ids = []
                    messages.append("Background citations were removed from an unvalidated hypothesis or unknown conclusion.")
            # The revision can relabel or paraphrase a rejected statistic rather
            # than correcting it. Numerical prose must pass the complete literal
            # quotation guard as supported; otherwise keep only an explicit gap.
            # `None` identifies demo/reference-only validation, so fixture output
            # retains its existing behavior.
            if evidence is not None and claim.kind != "supported" and _has_numerical_assertion(claim.text):
                claim.kind = "unknown"
                claim.text = ("This numerical conclusion remains unknown; inspect the quoted source figure cards and evidence "
                              "and validate their applicability to the submitted startup.")
                claim.evidence_ids = []
                messages.append("A numerical assertion in an unvalidated final claim was replaced by an explicit unknown; inspect the quoted source figures and evidence.")
        if messages:
            report.summary = "Analysis includes qualified assumptions or gaps; inspect individual claims and evidence before using conclusions."
        return report, _unique(messages)

    @staticmethod
    def _resolve_critic(critic: CritiqueReport, final: AnalysisReport, known_ids: set[str], revisions: int,
                        evidence: list[EvidenceItem] | None = None) -> None:
        claims = {claim.claim_id: claim for section in final.sections for claim in section.claims}
        by_id = {item.evidence_id: item for item in evidence or []}
        for issue in critic.issues:
            if issue.claim_id is None:
                continue
            claim = claims.get(issue.claim_id)
            if claim is None:
                issue.resolution = "The referenced claim was removed from the final analysis."
            elif claim.kind in {"assumption", "unknown"}:
                issue.resolution = f"The final claim is explicitly classified as {claim.kind}; it is not presented as an established fact."
            elif issue.issue_id.startswith("guard_reference_") and claim.evidence_ids and all(ref in known_ids for ref in claim.evidence_ids):
                issue.resolution = "The final claim references known evidence; reference validity was restored."
            elif (issue.issue_id.startswith("guard_quotation_") and evidence is not None
                  and quotation_issue(claim, by_id) is None):
                issue.resolution = ("Literal quotation traceability was restored in the final claim; "
                                    "this does not establish source accuracy or business applicability.")
        unresolved = [issue for issue in critic.issues if issue.resolution is None]
        critic.unresolved_gaps = _unique(critic.unresolved_gaps + final.gaps + [issue.reason for issue in unresolved])
        if unresolved or critic.unresolved_gaps:
            critic.review_status = "ready_with_gaps"
        else:
            critic.review_status = "ready"
        prefix = "One revision pass completed." if revisions else "No model revision was required."
        critic.revision_summary = prefix + " " + (critic.revision_summary or "Final reference validation completed.")


def build_service(settings: Settings) -> IntelligenceService:
    """The final app can mount this service without project-workspace imports."""
    from .providers import make_language_model, make_search_provider
    from .retrieval import make_retriever

    llm = DemoLLM(delay=settings.demo_delay) if settings.app_mode == "demo" else make_language_model(settings)
    return IntelligenceService(settings, llm, make_search_provider(settings), make_retriever(settings))
