"""Conservative live extraction and source-quotation checks.

These checks establish literal traceability, not publication reliability or
semantic entailment. They never turn a nearby citation into proof of a startup
claim. Public context contracts remain unchanged.
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .contracts import Claim, EvidenceItem, IdeaSpec


InputQuote = Annotated[str, StringConstraints(min_length=1, max_length=3000)]
IDEA_FIELDS = ("project_name", "problem", "solution", "target_users",
               "domain", "assumptions", "constraints")
_ANAPHORIC_SOLUTION_START = re.compile(r"^\s*[\"\u201c']?(?:it|they|this|that|these|those)\b", re.I)


def _require_quote_fields(schema: dict) -> None:
    # Defaults remain valid when reading an older provider response, but the
    # generation grammar must not let a model omit the proof map's entries.
    schema["required"] = list(schema["properties"])


def _require_extraction_fields(schema: dict) -> None:
    """Strengthen only the private LLM schema; public IdeaSpec stays unchanged."""
    schema["required"] = list(schema["properties"])
    schema["properties"]["field_origins"] = {
        "type": "object", "title": "Field Origins",
        "properties": {field: {"type": "string", "enum": ["provided", "inferred", "unknown"]}
                       for field in IDEA_FIELDS},
        "required": list(IDEA_FIELDS), "additionalProperties": False,
    }


class ExtractionQuotes(BaseModel):
    """Internal proof map; every nonempty entry must be an exact input span."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=_require_quote_fields)
    project_name: list[InputQuote] = Field(default_factory=list, max_length=1)
    problem: list[InputQuote] = Field(default_factory=list, max_length=3)
    solution: list[InputQuote] = Field(default_factory=list, max_length=3)
    target_users: list[InputQuote] = Field(default_factory=list, max_length=20)
    domain: list[InputQuote] = Field(default_factory=list, max_length=1)
    assumptions: list[InputQuote] = Field(default_factory=list, max_length=30)
    constraints: list[InputQuote] = Field(default_factory=list, max_length=30)


class ExtractedIdea(IdeaSpec):
    model_config = ConfigDict(extra="forbid", json_schema_extra=_require_extraction_fields)
    # A missing proof map is handled conservatively rather than silently trusting
    # a provider's old IdeaSpec-only response. It does not change the public API.
    source_quotes: ExtractionQuotes = Field(default_factory=ExtractionQuotes)


def ground_idea(candidate: ExtractedIdea, submitted: str) -> IdeaSpec:
    """Reconstruct provided facts from input spans, not adjacent paraphrases.

    A model can return an authentic quote while adding nonexistent features to
    its field value. Using the validated quotes themselves avoids that failure.
    Proposed branding and a domain category may remain explicitly inferred.
    A solution quote starting with a referring pronoun can omit the product
    definition. Retain the bounded original input in that case instead of
    guessing its antecedent; this input-preserving fallback still needs review.
    Omitted or misclassified input still needs review; quotes are not a semantic
    verifier of field placement.
    """
    quotes: dict[str, list[str]] = {}
    for field, values in candidate.source_quotes.model_dump().items():
        quotes[field] = list(dict.fromkeys(value for value in values if value in submitted))

    values: dict = {
        "original_text": submitted,
        "project_name": "Startup concept",
        "problem": "The specific customer problem is not explicitly established; clarify it before research.",
        "solution": submitted[:3000],
        "target_users": [],
        "domain": "Unspecified domain",
        "assumptions": ["Customer demand and willingness to adopt require validation."],
        "constraints": [],
    }
    origins = {field: "unknown" for field in quotes}
    origins["solution"] = "provided"
    origins["assumptions"] = "inferred"
    scalar_limits = {"project_name": 200, "problem": 3000, "solution": 3000, "domain": 200}
    for field, limit in scalar_limits.items():
        joined = "\n".join(quotes[field])
        if field == "solution" and _ANAPHORIC_SOLUTION_START.match(joined):
            continue  # Keep the exact submitted-text fallback already set above.
        if joined and len(joined) <= limit:
            values[field] = joined
            origins[field] = "provided"
        elif field in {"project_name", "domain"} and candidate.field_origins.get(field) == "inferred":
            values[field] = getattr(candidate, field)
            origins[field] = "inferred"
    for field in ("target_users", "assumptions", "constraints"):
        if quotes[field]:
            values[field] = quotes[field]
            origins[field] = "provided"
    return IdeaSpec(**values, field_origins=origins)


def claim_quotation(text: str) -> str:
    """Accept only a fixed wrapper around a literal, complete quotation."""
    quote = text.strip()
    if quote.startswith("Source excerpt: "):
        quote = quote[len("Source excerpt: "):].strip()
    if len(quote) >= 2 and (quote[0], quote[-1]) in {('"', '"'), ("“", "”")}:
        quote = quote[1:-1]
    return quote


def _complete_span(quote: str, excerpt: str) -> bool:
    """Reject isolated phrases that drop a sentence's population or units.

    This is a conservative quotation boundary rule, not linguistic entailment.
    Paragraph boundaries and sentence punctuation are accepted. Short standalone
    passages are allowed; a statistic fragment inside a sentence is not.
    """
    if not quote or not quote.strip() or len(quote) < 12:
        return False
    start = 0
    while (index := excerpt.find(quote, start)) >= 0:
        before, after = excerpt[:index], excerpt[index + len(quote):]
        left = not before.strip() or before.endswith("\n") or bool(re.search(r"[.!?][\"”']?\s+$", before))
        right = not after.strip() or after.startswith("\n") or (bool(re.search(r"[.!?][\"”']?$", quote)) and bool(re.match(r"\s", after)))
        if left and right:
            return True
        start = index + 1
    return False


def quotation_issue(claim: Claim, evidence: dict[str, EvidenceItem]) -> str | None:
    if claim.kind != "supported":
        return None
    if not claim.evidence_ids:
        return "Supported claim has no evidence references."
    if any(reference not in evidence for reference in claim.evidence_ids):
        return "Claim references unknown evidence."
    quote = claim_quotation(claim.text)
    if not all(_complete_span(quote, evidence[reference].excerpt) for reference in claim.evidence_ids):
        return ("The supported claim is not a complete literal quotation of every cited passage; "
                "its wording, number, units, population or scope remain unverified.")
    return None


def quoted_claim_text(text: str) -> str:
    return "Source excerpt: “" + claim_quotation(text) + "”"


def comparable_claim_text(text: str) -> str:
    """Ignore badge-like wrappers when checking whether revision rewrote a claim."""
    text = text.strip()
    for prefix in ("Unverified assumption:", "Unverified:", "Unknown:", "Source excerpt:"):
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix):].strip()
    return " ".join(text.strip('"“”').split()).casefold()
