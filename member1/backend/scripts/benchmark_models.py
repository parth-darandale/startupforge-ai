"""Run a small, repeatable local-model trial; never use the demo provider.

The saved JSON includes raw model text before application repair/number guards.
Automated checks are deliberately narrow, not a business-accuracy score.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import httpx
from pydantic import BaseModel, ValidationError

from app.modules.intelligence.contracts import AnalysisReport, CritiqueReport, IdeaSpec
from app.modules.intelligence.debate import TurnResponse
from app.modules.intelligence.prompts import build_messages


IDEA_TEXT = (
    "Build CampusLoop, a campus marketplace for students to buy and sell used "
    "textbooks. Existing campus message boards are a free alternative. Start "
    "with one college and validate demand before charging a commission."
)
IDEA = IdeaSpec(
    original_text=IDEA_TEXT, project_name="CampusLoop", domain="Student marketplace",
    problem="Students need a convenient way to exchange used textbooks.",
    solution="A campus marketplace for used textbooks.", target_users=["College students"],
    assumptions=["Students will prefer a dedicated marketplace."],
    constraints=["Pilot at one college", "Validate demand before charging commission"],
    field_origins={"project_name": "provided", "problem": "inferred", "solution": "provided",
                   "target_users": "provided", "domain": "inferred"},
).model_dump(mode="json")
EVIDENCE = [
    {"evidence_id": "ev_interviews", "source_id": "src_interviews", "excerpt":
     "SYNTHETIC BENCHMARK FIXTURE: In a fictional pilot, 7 of 12 interviewed "
     "students expressed interest in exchanging used books. These interviews "
     "did not measure willingness to pay and are not representative market data."},
    {"evidence_id": "ev_alternatives", "source_id": "src_alternatives", "excerpt":
     "SYNTHETIC BENCHMARK FIXTURE: The fictional college already has a free "
     "message board for used-book listings. Its transaction volume and "
     "reliability have not been measured."},
]
SOURCES = [
    {"source_id": "src_interviews", "title": "Fictional pilot interviews", "kind": "fixture",
     "url": "https://example.org/benchmark/interviews"},
    {"source_id": "src_alternatives", "title": "Fictional campus alternatives", "kind": "fixture",
     "url": "https://example.org/benchmark/alternatives"},
]
FIGURES = [{"figure_id": "fig_interest", "display_value": "7 of 12", "metric":
            "Fictional interview interest", "excerpt": EVIDENCE[0]["excerpt"],
            "evidence_id": "ev_interviews", "source_id": "src_interviews",
            "qualification": "Synthetic benchmark; interest is not payment intent or market size."}]
GOOD_ANALYSIS = {
    "summary": "A campus pilot may test convenience; payment intent and differentiation remain unknown.",
    "sections": [{"section_id": "fit", "heading": "Customer fit", "claims": [
        {"claim_id": "interest", "text": "The fictional interviews suggest interest, not willingness to pay.",
         "kind": "supported", "evidence_ids": ["ev_interviews"]},
        {"claim_id": "advantage", "text": "Better listing quality might differentiate the service; this is untested.",
         "kind": "assumption", "evidence_ids": []}]}],
    "gaps": ["Willingness to pay is unknown", "Free alternative reliability is unmeasured"],
}
FLAWED_ANALYSIS = {
    "summary": "The market has proven payment demand and guaranteed profitability.",
    "sections": [{"section_id": "fit", "heading": "Customer fit", "claims": [
        {"claim_id": "bad_payment", "text": "80% of all college students will pay a commission.",
         "kind": "supported", "evidence_ids": ["ev_interviews"]},
        {"claim_id": "bad_profit", "text": "The venture is guaranteed to be profitable.",
         "kind": "supported", "evidence_ids": ["ev_nonexistent"]}]}],
    "gaps": [],
}
KNOWN_CRITIC = {
    "review_status": "ready_with_gaps", "issues": [
        {"issue_id": "issue_payment", "severity": "high", "claim_id": "bad_payment",
         "reason": "Interest in a tiny fictional sample does not establish willingness to pay or the asserted percentage.",
         "evidence_ids": ["ev_interviews"]},
        {"issue_id": "issue_profit", "severity": "high", "claim_id": "bad_profit",
         "reason": "The cited evidence does not exist and profitability is untested.", "evidence_ids": []}],
    "revision_summary": "Remove invented figures, invalid citations and guaranteed success.",
    "unresolved_gaps": ["Payment intent", "Costs and revenue", "Differentiation from the free board"],
}


@dataclass(frozen=True)
class TrialCase:
    case_id: str
    task: str
    payload: dict[str, Any]
    contract: type[BaseModel]
    expected_terms: tuple[str, ...] = ()


def make_cases(quick: bool = False) -> list[TrialCase]:
    """Fixed inputs keep later calls independent of preceding model outputs."""
    common = {"idea": IDEA, "evidence": EVIDENCE}
    cases = [TrialCase("extraction", "extract", {"idea_text": IDEA_TEXT}, IdeaSpec)]
    if not quick:
        cases.extend([
            TrialCase("sourced_analysis", "analyze", {**common, "sources": SOURCES,
                      "research_options": {"max_sources": 2}, "queries": [],
                      "gaps": ["All supplied evidence is synthetic; no real market validation."]}, AnalysisReport),
            TrialCase("critic_known_errors", "critic", {**common, "initial_analysis": FLAWED_ANALYSIS,
                      "gaps": []}, CritiqueReport),
            TrialCase("revision_known_errors", "revise", {**common, "initial_analysis": FLAWED_ANALYSIS,
                      "critic": KNOWN_CRITIC, "gaps": KNOWN_CRITIC["unresolved_gaps"]}, AnalysisReport),
        ])
    cases.extend([
        TrialCase("debate_for", "debate_turn", {**common, "stance": "for", "round_number": 1,
                  "mode": "user_vs_agent", "figures": FIGURES, "analysis": GOOD_ANALYSIS,
                  "history": [{"speaker": "user", "stance": "against", "message":
                               "A commission will destroy your margins: students can just use the existing free campus message board. Why would they pay?"}]},
                  TurnResponse, ("commission", "margin", "free", "pay", "board")),
        TrialCase("debate_against", "debate_turn", {**common, "stance": "against", "round_number": 1,
                  "mode": "user_vs_agent", "figures": FIGURES, "analysis": GOOD_ANALYSIS,
                  "history": [{"speaker": "user", "stance": "for", "message":
                               "Our verified listings and campus integration will solve trust, so interested students will definitely pay."}]},
                  TurnResponse, ("verif", "trust", "integrat", "pay", "interest")),
    ])
    if not quick:
        cash = {"evidence_id": "ev_cashbook", "source_id": "src_cashbook", "excerpt":
                "SYNTHETIC ARITHMETIC FIXTURE: Opening cash is INR 5000. Expenses are INR 1200. "
                "Revenue received is INR 800. These are fictional amounts, not market figures."}
        cases.append(TrialCase("financial_arithmetic", "analyze", {
            "idea": {**IDEA, "constraints": ["Calculate closing cash and net cash change from the supplied fictional cashbook."]},
            "sources": [{"source_id": "src_cashbook", "title": "Fictional cashbook", "kind": "fixture",
                         "url": "https://example.org/benchmark/cashbook"}], "evidence": [cash],
            "research_options": {"max_sources": 1}, "queries": [],
            "gaps": ["Show the arithmetic explicitly in a supported claim: opening cash minus expenses plus revenue. "
                     "State closing cash and net cash change. Do not infer commercial viability from it."]}, AnalysisReport))
    return cases


def request_fingerprint(case: TrialCase) -> str:
    request = {"task": case.task, "payload": case.payload,
               "schema": case.contract.model_json_schema()}
    canonical = json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_prompt_snapshot(path: Path) -> None:
    """Freeze prompt text as JSON data, before changing application prompts."""
    snapshot = {
        "snapshot_version": "1.0", "protocol_id": "startupforge_model_trial_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "description": "Frozen original model-trial prompts. Includes complete system rules, JSON schemas and user payloads.",
        "cases": [{"case_id": case.case_id, "task": case.task,
                   "request_fingerprint": request_fingerprint(case),
                   "messages": build_messages(case.task, case.payload, case.contract.model_json_schema())}
                  for case in make_cases(False)],
    }
    checkpoint(path, snapshot)


def load_prompt_snapshot(path: Path, cases: list[TrialCase]) -> tuple[dict[str, list[dict[str, str]]], str]:
    """Read static messages only; reject changes to the benchmark input/schema."""
    raw = path.read_bytes()
    snapshot = json.loads(raw)
    if not isinstance(snapshot, dict) or snapshot.get("snapshot_version") != "1.0":
        raise ValueError("Unsupported prompt snapshot version")
    entries = snapshot.get("cases")
    if not isinstance(entries, list):
        raise ValueError("Prompt snapshot cases must be a list")
    by_id = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("case_id"), str):
            raise ValueError("Invalid prompt snapshot case")
        if entry["case_id"] in by_id:
            raise ValueError("Duplicate prompt snapshot case ID")
        by_id[entry["case_id"]] = entry
    selected = {}
    for case in cases:
        entry = by_id.get(case.case_id)
        if not entry or entry.get("task") != case.task or entry.get("request_fingerprint") != request_fingerprint(case):
            raise ValueError(f"Prompt snapshot input/schema does not match case {case.case_id}")
        messages = entry.get("messages")
        if (not isinstance(messages, list) or len(messages) != 2
                or any(not isinstance(message, dict) or set(message) != {"role", "content"}
                       or not isinstance(message.get("content"), str) for message in messages)
                or [message.get("role") for message in messages] != ["system", "user"]):
            raise ValueError(f"Invalid frozen messages for case {case.case_id}")
        user_data = json.loads(messages[1]["content"])
        if user_data != {"task": case.task, "untrusted_input": case.payload}:
            raise ValueError(f"Frozen user data does not match case {case.case_id}")
        # The original builder ends the system message with the exact schema.
        if not messages[0]["content"].endswith(json.dumps(case.contract.model_json_schema(), ensure_ascii=False)):
            raise ValueError(f"Frozen schema text does not match case {case.case_id}")
        selected[case.case_id] = messages
    return selected, hashlib.sha256(raw).hexdigest()


def _numbers(text: str) -> list[str]:
    return re.findall(r"(?<![\w])[-−]?\d[\d,]*(?:\.\d+)?\s*%?", text)


def inspect_output(case: TrialCase, output: dict[str, Any]) -> dict[str, Any]:
    """Inspect raw model JSON. Do not repair it or assert semantic truth."""
    checks: dict[str, Any] = {}
    try:
        parsed = case.contract.model_validate(output)
    except ValidationError as exc:
        checks["schema_valid"] = False
        checks["schema_errors"] = [{"location": list(item["loc"]), "message": item["msg"]}
                                    for item in exc.errors()]
        return checks
    checks["schema_valid"] = True
    data = parsed.model_dump(mode="json")
    evidence = {item["evidence_id"]: item for item in case.payload.get("evidence", [])}
    figures = {item["figure_id"]: item for item in case.payload.get("figures", [])}
    known = set(evidence)
    refs: list[str] = []
    prose: list[str] = []
    if isinstance(parsed, IdeaSpec):
        checks["original_text_preserved"] = parsed.original_text == IDEA_TEXT
        checks["invented_numeric_mentions"] = sorted(set(_numbers(json.dumps(data))) - set(_numbers(IDEA_TEXT)))
    if isinstance(parsed, AnalysisReport):
        claims = [claim for section in parsed.sections for claim in section.claims]
        checks["supported_uncited_claim_ids"] = [claim.claim_id for claim in claims
                                                 if claim.kind == "supported" and not claim.evidence_ids]
        checks["duplicate_claim_ids"] = sorted({claim.claim_id for claim in claims
                                                if sum(other.claim_id == claim.claim_id for other in claims) > 1})
        refs = [ref for claim in claims for ref in claim.evidence_ids]
        prose = [parsed.summary, *[claim.text for claim in claims], *parsed.gaps]
        allowed_numbers = {number.strip().replace(",", "") for item in evidence.values()
                           for number in _numbers(item["excerpt"])}
        if case.case_id == "financial_arithmetic":
            allowed_numbers.update({"4600", "-400", "400"})
            words = " ".join(prose).replace(",", "")
            checks["closing_cash_4600_present"] = bool(re.search(r"\b4600\b", words))
            checks["net_decrease_400_present"] = bool(re.search(r"(?:[-−]\s*400|(?:decrease|loss|negative|outflow|decline|deficit)[^.!?]{0,50}\b400\b|\b400\b[^.!?]{0,50}(?:decrease|loss|negative|outflow|decline|deficit))", words, re.I))
        checks["numeric_mentions_not_in_fixture_or_expected_calculation"] = sorted({
            number.strip() for text in prose for number in _numbers(text)
            if number.strip().replace(",", "") not in allowed_numbers})
        if case.case_id == "revision_known_errors":
            checks["known_false_claims_still_supported"] = [claim.claim_id for claim in claims
                if claim.kind == "supported" and ("80%" in claim.text or "guarantee" in claim.text.lower()
                                                  or "ev_nonexistent" in claim.evidence_ids)]
    if isinstance(parsed, CritiqueReport):
        refs = [ref for issue in parsed.issues for ref in issue.evidence_ids]
        checks["known_bad_claims_flagged"] = sorted({issue.claim_id for issue in parsed.issues
                                                     if issue.claim_id in {"bad_payment", "bad_profit"}})
        checks["review_status"] = parsed.review_status
    if isinstance(parsed, TurnResponse):
        refs = parsed.evidence_ids
        prose = [parsed.message]
        checks["unknown_figure_ids"] = sorted(set(parsed.figure_ids) - set(figures))
        checks["figure_lineage_errors"] = [ref for ref in parsed.figure_ids if ref in figures
            and (figures[ref]["evidence_id"] not in refs
                 or figures[ref]["source_id"] != evidence[figures[ref]["evidence_id"]]["source_id"])]
        checks["numerical_mentions_forbidden_by_debate_prompt"] = _numbers(parsed.message)
        checks["objection_terms_addressed"] = [term for term in case.expected_terms
                                                if term in parsed.message.casefold()]
        checks["objection_overlap_at_least_two_terms"] = len(checks["objection_terms_addressed"]) >= 2
    if refs or evidence:
        checks["unknown_evidence_ids"] = sorted(set(refs) - known)
    return checks


class RecordingOllama:
    """Same task interface as providers, retaining unmodified text for review."""
    name = "ollama"

    def __init__(self, base_url: str, model: str, *, timeout: float = 180,
                 context_size: int = 4096, num_predict: int = 1024,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.base_url, self.model = base_url.rstrip("/"), model
        self.timeout, self.context_size, self.num_predict = timeout, context_size, num_predict
        self.transport = transport
        self.last_raw_text: str | None = None
        self.last_metrics: dict[str, Any] = {}
        self.capabilities: list[str] | None = None
        self.think: bool | None = None
        self.frozen_messages: list[dict[str, str]] | None = None

    async def inspect_capabilities(self) -> list[str]:
        """Disable optional thinking only where the runtime declares support."""
        async with httpx.AsyncClient(timeout=30, transport=self.transport) as client:
            response = await client.post(self.base_url + "/api/show", json={"model": self.model})
            response.raise_for_status()
            result = response.json()
        capabilities = result.get("capabilities", [])
        if not isinstance(capabilities, list) or any(not isinstance(value, str) for value in capabilities):
            raise ValueError("Ollama returned invalid model capabilities")
        self.capabilities = capabilities
        self.think = False if "thinking" in capabilities else None
        return capabilities

    async def generate_json(self, task: str, payload: dict, schema: dict) -> dict:
        self.last_raw_text, self.last_metrics = None, {}
        messages = self.frozen_messages if self.frozen_messages is not None else build_messages(task, payload, schema)
        self.last_metrics["prompt_characters"] = sum(len(item["content"]) for item in messages)
        body = {"model": self.model, "messages": messages, "format": schema, "stream": False,
                "keep_alive": "5m", "options": {"temperature": 0, "seed": 42,
                "num_ctx": self.context_size, "num_predict": self.num_predict}}
        if self.think is not None:
            body["think"] = self.think
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
            response = await client.post(self.base_url + "/api/chat", json=body)
            response.raise_for_status()
            result = response.json()
        self.last_metrics.update({key: result.get(key) for key in
            ("done", "done_reason", "total_duration", "load_duration", "prompt_eval_count", "eval_count", "eval_duration")})
        self.last_metrics["thinking_text_present"] = bool(result.get("message", {}).get("thinking"))
        prompt_tokens = result.get("prompt_eval_count")
        if isinstance(prompt_tokens, int):
            self.last_metrics["prompt_near_context_capacity"] = prompt_tokens >= max(
                0, self.context_size - self.num_predict)
            self.last_metrics["context_capacity_warning"] = (
                "Prompt token count leaves less than the requested output budget; context pressure or truncation is possible, not proven."
                if self.last_metrics["prompt_near_context_capacity"] else None)
        self.last_raw_text = result.get("message", {}).get("content")
        if not isinstance(self.last_raw_text, str):
            raise ValueError("Ollama did not return message.content text")
        data = json.loads(self.last_raw_text)
        if not isinstance(data, dict):
            raise ValueError("Model output is not a JSON object")
        return data

    async def unload(self) -> None:
        async with httpx.AsyncClient(timeout=30, transport=self.transport) as client:
            response = await client.post(self.base_url + "/api/generate",
                                         json={"model": self.model, "keep_alive": 0})
            response.raise_for_status()


def checkpoint(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


async def run_trials(args: argparse.Namespace) -> dict[str, Any]:
    cases = make_cases(args.quick_smoke)
    frozen_messages = {}
    snapshot_sha256 = None
    if args.prompt_snapshot is not None:
        frozen_messages, snapshot_sha256 = load_prompt_snapshot(args.prompt_snapshot, cases)
    report: dict[str, Any] = {
        "report_version": "1.0", "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "running", "provider": "ollama", "synthetic_fixture": True,
        "protocol": "Fixed independent inputs, sequential models/tasks, temperature 0 and seed 42; no application guards or repair.",
        "limitations": ["These synthetic cases do not measure factual business accuracy, real market knowledge or commercial viability.",
                        "Citation checks validate IDs and figure lineage, not whether prose is semantically supported.",
                        "Numeric checks are heuristic; derived quantities and qualified hypotheses require human review.",
                        "Keyword overlap is a weak responsiveness indicator; manually read each raw response.",
                        "A single deterministic run does not establish repeatability. Latency includes loading where reported."],
        "config": {"context_size": args.context_size, "num_predict": args.num_predict,
                   "task_timeout_seconds": args.timeout, "quick_smoke": args.quick_smoke,
                   "prompt_snapshot": str(args.prompt_snapshot) if args.prompt_snapshot is not None else None,
                   "prompt_snapshot_sha256": snapshot_sha256},
        "cases": [{"case_id": case.case_id, "task": case.task, "payload": case.payload,
                   "output_schema": case.contract.model_json_schema()} for case in cases],
        "models": [],
    }
    checkpoint(args.output, report)
    for model_name in args.models:
        model = RecordingOllama(args.base_url, model_name, timeout=args.timeout,
                                context_size=args.context_size, num_predict=args.num_predict)
        trial: dict[str, Any] = {"model": model_name, "status": "running", "tasks": []}
        report["models"].append(trial)
        checkpoint(args.output, report)
        try:
            try:
                trial["capabilities"] = await model.inspect_capabilities()
                trial["think_setting"] = model.think
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                trial["capability_inspection_error"] = {"type": type(exc).__name__, "message": str(exc)[:500]}
                trial["think_setting"] = None
                trial["capability_warning"] = "Capability inspection failed; the request omits think and retains the runtime default."
            checkpoint(args.output, report)
            for case in cases:
                model.frozen_messages = frozen_messages.get(case.case_id)
                print(f"{model_name}: {case.case_id}", flush=True)
                item: dict[str, Any] = {"case_id": case.case_id, "task": case.task}
                started = time.perf_counter()
                try:
                    output = await asyncio.wait_for(model.generate_json(case.task, case.payload,
                                                     case.contract.model_json_schema()), timeout=args.timeout)
                    item.update({"output": output, "checks": inspect_output(case, output)})
                except (httpx.HTTPError, asyncio.TimeoutError, TimeoutError, ValueError, KeyError, TypeError) as exc:
                    item["error"] = {"type": type(exc).__name__, "message": str(exc)[:500]}
                finally:
                    item.update({"elapsed_seconds": round(time.perf_counter() - started, 3),
                                 "raw_text": model.last_raw_text, "ollama_metrics": model.last_metrics})
                    if model.last_metrics.get("done_reason") == "length":
                        item["output_truncated"] = True
                    trial["tasks"].append(item)
                    checkpoint(args.output, report)
            trial["status"] = "completed"
        finally:
            try:
                await model.unload()
                trial["unloaded"] = True
            except (httpx.HTTPError, ValueError):
                trial["unloaded"] = False
                trial["unload_warning"] = "Unload failed. Verify memory before loading another model."
            checkpoint(args.output, report)
        if not trial["unloaded"]:
            report["status"] = "stopped_unload_failed"
            checkpoint(args.output, report)
            return report
    report["status"] = "completed"
    checkpoint(args.output, report)
    return report


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--models", nargs="+", required=True)
    cli.add_argument("--base-url", default="http://127.0.0.1:11434")
    cli.add_argument("--output", type=Path, default=BACKEND.parent.parent / "integration" / "model_trials" / "local_models.json")
    cli.add_argument("--quick-smoke", action="store_true", help="Extraction and opposing debate replies only (3 calls/model).")
    cli.add_argument("--prompt-snapshot", type=Path, help="Use frozen v1 prompt messages; validates identical case payloads and schemas.")
    cli.add_argument("--timeout", type=float, default=180)
    cli.add_argument("--context-size", type=int, default=4096)
    cli.add_argument("--num-predict", type=int, default=1024)
    return cli


def main() -> int:
    args = parser().parse_args()
    if args.timeout <= 0 or args.context_size < 1024 or args.num_predict < 64:
        raise SystemExit("Use a positive timeout, context-size >=1024 and num-predict >=64.")
    try:
        report = asyncio.run(run_trials(args))
    except KeyboardInterrupt:
        print("Interrupted; per-task checkpoints remain in the output file.", flush=True)
        return 130
    print(f"Saved {report['status']} trial to {args.output}", flush=True)
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
