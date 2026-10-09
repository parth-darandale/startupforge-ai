"""Extract quoted quantities; extraction does not certify the source's claims."""
import hashlib
import re

from .contracts import EvidenceItem, IdeaSpec, ResearchOptions, Source, SourcedFigure
from .business_evidence import figure_scope_notes, relevant_business_text

NUMBER = r"[+\-−]?\d[\d,]*(?:\.\d+)?"
CURRENCY = r"(?:[$₹€£]|INR\s*|USD\s*|Rs\.?\s*)\s*"
RANGE = r"\s*(?:[–—-]|to\b)\s*"
BETWEEN_RANGE = r"\s+and\s+"
SCALE = r"(?:\s*(?:million|billion|thousand|crore|lakh)\b)?"
UNIT = (
    r"(?:%|percent\b|per\s+cent\b|million\b|billion\b|thousand\b|crore\b|lakh\b"
    r"|(?:GW|MW|kW|kWh|GWh|MWh)\b"
    r"|(?:(?:simulated|surveyed|active|paying|registered)\s+)?"
    r"(?:interview(?:s|\s+records)\b|respondents\b|participants\b|customers\b|users\b|students\b|households\b"
    r"|businesses\b|companies\b|firms\b|installations\b|subscriptions\b|vehicles\b))"
)
QUANTITY = re.compile(
    r"(?<![\w.+\-−–—])(?:between\s+" + CURRENCY + NUMBER + SCALE
    + BETWEEN_RANGE + r"(?:" + CURRENCY + r")?" + NUMBER + SCALE
    + r"|between\s+" + NUMBER + r"(?:\s*" + UNIT + r")?"
    + BETWEEN_RANGE + NUMBER + r"\s*" + UNIT
    + r"|" + CURRENCY + NUMBER + SCALE
    + r"(?:" + RANGE + r"(?:" + CURRENCY + r")?" + NUMBER + SCALE + r")?"
    + r"|" + NUMBER + r"(?:" + RANGE + NUMBER + r")?\s*" + UNIT
    + r"(?:" + RANGE + NUMBER + r"\s*" + UNIT + r")?)", re.IGNORECASE,
)


def _unit_identity(value: str) -> str:
    unit = " ".join(value.lower().split())
    if unit in {"%", "percent", "per cent"}:
        return "percent"
    if unit in {"million", "billion", "thousand", "crore", "lakh"}:
        return "scaled quantity"
    return unit


def _currency_identity(value: str) -> str:
    return {"$": "usd", "usd": "usd", "₹": "inr", "inr": "inr", "rs": "inr"}.get(
        value.strip().lower().rstrip("."), value.strip().lower())


def _valid_quantity(match: re.Match, sentence: str) -> bool:
    """Never merge independent 'and' quantities or retain a range endpoint.

    Units explicitly repeated at both endpoints must agree. A shared trailing
    unit and a shared leading currency are allowed; unlike converting values,
    this preserves only the complete literal range from the publication.
    """
    value = match.group().strip()
    between = value.lower().startswith("between ")
    if between:
        value = re.sub(r"^between\s+", "", value, flags=re.I)
    separator = BETWEEN_RANGE if between else RANGE
    currency = re.fullmatch(
        r"(" + CURRENCY + r")" + NUMBER + SCALE
        + r"(?:" + separator + r"(" + CURRENCY + r")?" + NUMBER + SCALE + r")?",
        value, re.I,
    )
    if currency:
        if currency.group(2) and _currency_identity(currency.group(1)) != _currency_identity(currency.group(2)):
            return False
        # '$10 million–20 percent' is not a dollar range. The broad matcher
        # consumes both numbers so the second endpoint cannot reappear alone.
        if re.match(r"\s*" + UNIT, sentence[match.end():], re.I):
            return False
    else:
        repeated = re.fullmatch(
            NUMBER + r"\s*(" + UNIT + r")" + separator + NUMBER + r"\s*(" + UNIT + r")",
            value, re.I,
        )
        if repeated and _unit_identity(repeated.group(1)) != _unit_identity(repeated.group(2)):
            return False
        if not repeated and not re.fullmatch(
                NUMBER + r"(?:" + separator + NUMBER + r")?\s*" + UNIT, value, re.I):
            return False
    before, after = sentence[:match.start()], sentence[match.end():]
    # An unsupported/malformed range must not turn into a lone endpoint. This
    # conservative check also covers unsupported units such as 'tons'.
    if re.match(RANGE + r"(?:" + CURRENCY + r")?" + NUMBER, after, re.I):
        return False
    if re.search(NUMBER + r"(?:\s+[A-Za-z%]+){0,3}" + RANGE + r"$", before, re.I):
        return False
    if re.search(r"\bbetween\s*$", before, re.I):
        return False
    return True


def _sentences(text: str) -> list[tuple[int, int]]:
    """Keep literal spans so optional preceding context remains an exact quote."""
    boundaries = list(re.finditer(r"(?<=[.!?])\s+|\n+", text))
    spans = []
    start = 0
    for boundary in boundaries:
        # Rs. is an Indian currency abbreviation, not a sentence boundary.
        if re.search(r"\bRs\.$", text[start:boundary.start()], re.I) and re.match(
                NUMBER, text[boundary.end():]):
            continue
        spans.append((start, boundary.start()))
        start = boundary.end()
    spans.append((start, len(text)))
    return spans


def extract_figures(evidence: list[EvidenceItem], sources: list[Source], *,
                    idea: IdeaSpec | None = None, options: ResearchOptions | None = None) -> list[SourcedFigure]:
    by_id = {source.source_id: source for source in sources}
    figures: list[SourcedFigure] = []
    seen: set[tuple[str, str, str]] = set()
    for item in evidence:
        source = by_id.get(item.source_id)
        if not source:
            continue
        spans = _sentences(item.excerpt)
        for start, end in spans:
            sentence = item.excerpt[start:end].strip()
            quoted_context = sentence
            if source.kind != "fixture" and idea is not None:
                scope = options or ResearchOptions()
                if not relevant_business_text(sentence, idea, scope):
                    # Pronouns do not establish their referent. Require the
                    # quantity's own sentence to mention the reviewed topic.
                    continue
            for match in QUANTITY.finditer(sentence):
                if not _valid_quantity(match, sentence):
                    continue
                value = match.group().strip()
                identity = (source.source_id, value.casefold(), quoted_context)
                if identity in seen:
                    continue
                seen.add(identity)
                digest = hashlib.sha256((item.evidence_id + str(match.start()) + sentence).encode()).hexdigest()[:12]
                qualification = ("Synthetic demonstration figure, not real market data."
                                 if source.kind == "fixture" else
                                 "Quoted from this source. Verify its date, geography and methodology before treating it as a market estimate. "
                                 + " ".join(figure_scope_notes(quoted_context, source.url, options, idea)))
                figures.append(SourcedFigure(
                    figure_id=f"fig_{digest}", display_value=value,
                    metric=sentence[:220], excerpt=quoted_context,
                    evidence_id=item.evidence_id, source_id=item.source_id,
                    qualification=qualification,
                ))
    return figures[:24]
