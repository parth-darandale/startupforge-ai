"""Deterministic teaching mode; it does not research or call a language model.

The initial report deliberately contains ONE ungrounded supported claim. The
critic and revision make the evidence-checking workflow visible without keys.
Every actual retrieved source in this mode is a synthetic fixture.
"""

import asyncio
import copy
import re
from typing import Any

from .contracts import AnalysisReport, CritiqueReport, IdeaSpec


DEMO_WARNING = (
    "DEMO MODE: deterministic educational output using synthetic fixture sources; "
    "this is not live research or a validated assessment of the startup."
)


class DemoLLM:
    name = "demo"

    def __init__(self, delay: float = 0.0):
        self.delay = max(0.0, delay)

    async def generate_json(self, task: str, payload: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
        if self.delay:
            await asyncio.sleep(self.delay)
        handlers = {
            "extract": self._extract,
            "queries": self._queries,
            "analyze": self._analyze,
            "critic": self._critic,
            "revise": self._revise,
        }
        if task not in handlers:
            raise ValueError(f"Unknown demo task: {task}")
        return handlers[task](payload)

    @staticmethod
    def _extract(payload: dict[str, Any]) -> dict[str, Any]:
        text = payload["idea_text"]
        words = re.findall(r"[\w'-]+", text)
        normalized = text.lower()
        domains = [
            ("Education", ("student", "school", "college", "learning", "education")),
            ("Agriculture", ("farm", "agricultur", "crop")),
            ("Healthcare", ("health", "patient", "medical")),
            ("Sustainability", ("waste", "recycl", "carbon")),
            ("Food services", ("food", "meal", "restaurant")),
            ("Business productivity", ("startup", "business", "workflow", "pitch")),
        ]
        domain = next((label for label, keys in domains if any(k in normalized for k in keys)), "Unspecified domain")
        users = [group for group in ("students", "farmers", "patients", "founders", "teachers", "businesses")
                 if re.search(r"\b" + group + r"\b", normalized)]
        return IdeaSpec(
            original_text=text,
            project_name=" ".join(words[:6])[:150] or "Startup concept",
            problem="Demo extraction: the specific customer problem needs confirmation from the submitted concept.",
            solution=text.strip()[:2900],
            target_users=users,
            domain=domain,
            assumptions=["Customer demand and willingness to adopt require validation."],
            constraints=[],
            field_origins={
                "project_name": "inferred", "problem": "unknown", "solution": "provided",
                "target_users": "provided" if users else "unknown",
                "domain": "unknown" if domain == "Unspecified domain" else "inferred",
                "assumptions": "inferred", "constraints": "unknown",
            },
        ).model_dump(mode="json")

    @staticmethod
    def _queries(payload: dict[str, Any]) -> dict[str, Any]:
        domain = payload["idea"]["domain"]
        return {"queries": [
            f"{domain} product validation customer discovery market statistics sources",
            f"{domain} competitor research alternatives comparison",
            f"{domain} technical feasibility prototype pilot validation limitations counterevidence",
        ]}

    @staticmethod
    def _analyze(payload: dict[str, Any]) -> dict[str, Any]:
        evidence = payload["evidence"]
        sections: list[dict[str, Any]] = []
        topics = [
            ("customer_validation", "Customer and problem validation", {
                "customer discovery": 6, "customer validation": 5, "product validation": 4,
                "interview": 3, "target users": 2, "potential users": 2,
            }),
            ("competitors", "Competitors and alternatives", {
                "competitor": 4, "comparison": 3, "alternatives": 3,
                "feature": 2, "price": 2, "pricing": 2,
            }),
            ("feasibility", "Feasibility and prototype", {
                "prototype scope": 6, "development feasibility": 6,
                "implementation": 3, "integration": 3, "pilot": 2, "prototype": 2,
            }),
        ]
        for section_id, heading, keywords in topics:
            candidates = []
            for item in evidence:
                # Retrieval order is a relevance ranking, not a section mapping.
                # Select one brief process sentence about this topic and preserve
                # its actual evidence reference. Quantities appear in Figures.
                sentences = re.split(r"(?<=[.!?])\s+|\n+", item["excerpt"])
                for sentence in sentences:
                    sentence = sentence.strip()
                    if not sentence or re.search(r"\d", sentence):
                        continue
                    normalized = sentence.lower()
                    relevance = sum(weight for keyword, weight in keywords.items() if keyword in normalized)
                    if not relevance:
                        continue
                    process = 2 if re.search(r"\b(record|records|ask|asks|test|tests|compare|check|checks|needs|evaluate|evaluates|depends|identifies)\b", normalized) else 0
                    candidates.append((relevance + process, sentence, item))
            if candidates:
                _, sentence, item = max(candidates, key=lambda candidate: candidate[0])
                if len(sentence) > 250:
                    sentence = sentence[:247].rsplit(" ", 1)[0] + "..."
                claim = {
                    "claim_id": f"claim_{section_id}",
                    "text": "DEMO teaching guidance: " + sentence,
                    "kind": "supported",
                    "evidence_ids": [item["evidence_id"]],
                }
            else:
                claim = {
                    "claim_id": f"claim_{section_id}",
                    "text": "No retrieved evidence establishes this aspect of the submitted startup idea.",
                    "kind": "unknown", "evidence_ids": [],
                }
            sections.append({"section_id": section_id, "heading": heading, "claims": [claim]})
        sections.append({
            "section_id": "risks", "heading": "Risks and assumptions",
            "claims": [{
                "claim_id": "claim_adoption_assumption",
                "text": "Potential users would be willing to adopt and pay for the proposed solution.",
                "kind": "supported", "evidence_ids": [],
            }],
        })
        return AnalysisReport(
            summary="DEMO: example validation guidance with an intentionally unsupported adoption assumption for the critic to detect.",
            sections=sections,
            gaps=list(payload.get("gaps", [])) + [
                "Actual competitors, customer demand, pricing, and project feasibility require live research and user validation."
            ],
        ).model_dump(mode="json")

    @staticmethod
    def _critic(payload: dict[str, Any]) -> dict[str, Any]:
        known = {item["evidence_id"] for item in payload["evidence"]}
        issues = []
        for section in payload["initial_analysis"]["sections"]:
            for claim in section["claims"]:
                invalid = any(ref not in known for ref in claim["evidence_ids"])
                if claim["kind"] == "supported" and (not claim["evidence_ids"] or invalid):
                    issues.append({
                        "issue_id": f"critic_{claim['claim_id']}", "severity": "high",
                        "claim_id": claim["claim_id"],
                        "reason": "No valid retrieved evidence supports this asserted conclusion; qualify it as an assumption.",
                        "evidence_ids": [ref for ref in claim["evidence_ids"] if ref in known],
                        "resolution": None,
                    })
        return CritiqueReport(
            review_status="ready_with_gaps",
            issues=issues,
            revision_summary="Qualify the unvalidated adoption assumption and preserve the evidence gaps.",
            unresolved_gaps=list(payload.get("gaps", [])) + ["Demo fixtures cannot validate this startup's market or demand."],
        ).model_dump(mode="json")

    @staticmethod
    def _revise(payload: dict[str, Any]) -> dict[str, Any]:
        revised = copy.deepcopy(payload["initial_analysis"])
        known = {item["evidence_id"] for item in payload["evidence"]}
        issue_claims = {issue["claim_id"] for issue in payload["critic"]["issues"] if issue["claim_id"]}
        for section in revised["sections"]:
            for claim in section["claims"]:
                invalid = any(ref not in known for ref in claim["evidence_ids"])
                if claim["kind"] == "supported" and (not claim["evidence_ids"] or invalid or claim["claim_id"] in issue_claims):
                    claim["kind"] = "assumption"
                    claim["text"] = "Unverified assumption: " + claim["text"]
                    claim["evidence_ids"] = []
        revised["summary"] = "DEMO: synthetic guidance is cited; startup demand and feasibility remain unverified assumptions or gaps."
        revised["gaps"] = list(dict.fromkeys(revised["gaps"] + payload.get("gaps", [])))
        return revised


# Compatibility with adapters that use the longer descriptive class name.
DemoLanguageModel = DemoLLM
