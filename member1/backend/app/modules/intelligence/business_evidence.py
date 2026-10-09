"""Conservative topic selection and scope notes for fetched business evidence.

Lexical relevance is a selection heuristic, never proof that a claim is true.
Audience, brand, geography and generic business words cannot alone qualify a
source. Shared evidence schemas stay unchanged; unknown scope remains explicit.
"""
from __future__ import annotations

import re

from .contracts import EvidenceItem, IdeaSpec, ResearchDocument, ResearchOptions


STOPWORDS = frozenset((
    "a an and are as at be been by can for from has have how in into is it its of on or that the their this to "
    "was were what when where which will with would used use using research evidence paper study market size "
    "statistic data demand customer user business startup feasibility competitor alternative validation current "
    "recent trend student college campus university school online digital platform application app service "
    "help need way proposed marketplace build create enable efficient efficiency reliable secure friendly "
    "problem solution general based indian india pilot test concept product connect connecting connects "
    "provide providing provides accessible affordable affordability direct directly allow allows allowing "
    "offer offering offers improve improving improvements manage managing management new local small owner "
    "target work team people price pricing cost compare comparison quote quotes review reviewed simple "
    "book bookish free low high limited reduce reducing across within system tool tools"
).split())

CONCEPTS = {
    "textbook": frozenset({"textbook", "coursebook", "textbooks", "coursebooks"}),
    "solar": frozenset({"solar", "photovoltaic", "photovoltaics", "pv"}),
    "rooftop": frozenset({"rooftop", "rooftops"}),
    "resale": frozenset({"resale", "resell", "reselling", "buyback", "secondhand", "second-hand", "reuse", "reused"}),
    "agriculture": frozenset({"farmer", "farming", "agriculture", "agricultural", "farm", "crop"}),
    "grocery": frozenset({"grocery", "groceries", "produce"}),
    "restaurant": frozenset({"restaurant", "dining", "eatery"}),
    "battery": frozenset({"battery", "batteries", "storage"}),
    "waste": frozenset({"waste", "recycling", "recycle", "recycled"}),
    "delivery": frozenset({"delivery", "deliveries", "delivering", "logistics"}),
    "textile": frozenset({"textile", "clothing", "apparel", "garment"}),
    "financing": frozenset({"financing", "finance", "loan", "lending", "credit"}),
    "factory": frozenset({"factory", "factories", "industrial", "industry", "manufacturing"}),
}
ANCHOR_CONCEPTS = frozenset({"textbook", "solar", "grocery", "restaurant", "battery", "waste", "textile"})
METADATA_LINE = re.compile(r"^(?:Source (?:page|PDF) title:|Page-declared publication date:|PDF-declared file creation date:|PDF extraction scope:|\[Partial (?:source|report):)", re.I)


def _tokens(text: str) -> set[str]:
    text = re.sub(r"\btext[\s-]+books?\b", "textbook", text.lower())
    text = re.sub(r"\bsecond[\s-]+hand\b", "secondhand", text)
    words = re.findall(r"\b[^\W\d_]{2,}\b", text, re.UNICODE)
    return {word[:-3] + "y" if word.endswith("ies") and len(word) > 4 else
            word[:-1] if word.endswith("s") and not word.endswith("ss") and len(word) > 3 else word
            for word in words}


def _body(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not METADATA_LINE.match(line.strip()))


def _topic_groups(idea: IdeaSpec, options: ResearchOptions) -> list[tuple[str, frozenset[str]]]:
    # Only the reviewed solution establishes the topic. An inferred broad
    # domain or audience in another field cannot swamp the actual commodity.
    # Project names can themselves be commodity phrases ("Solar Rooftop").
    # Removing brand tokens would then erase the actual research topic.
    ignored = _tokens(options.region or "")
    terms = _tokens(idea.solution) - STOPWORDS - ignored
    groups: dict[str, frozenset[str]] = {}
    for term in sorted(terms):
        name = next((key for key, aliases in CONCEPTS.items() if term in aliases), term)
        groups[name] = CONCEPTS.get(name, frozenset({term}))
    return list(groups.items())


def relevant_business_text(text: str, idea: IdeaSpec, options: ResearchOptions) -> bool:
    groups = _topic_groups(idea, options)
    if not groups:
        return False
    terms = _tokens(_body(text))
    matched = {name for name, aliases in groups if aliases & terms}
    required_anchors = {name for name, _ in groups if name in ANCHOR_CONCEPTS}
    if required_anchors and not (matched & required_anchors):
        return False
    # Commodity background can be useful (e.g., textbooks). It remains
    # contextual evidence, never evidence that the proposed model has demand.
    if "textbook" in required_anchors:
        return bool(matched)
    return len(matched) >= min(2, len(groups))


def filter_business_documents(
    documents: list[ResearchDocument], idea: IdeaSpec, options: ResearchOptions,
) -> tuple[list[ResearchDocument], list[str], list[str]]:
    retained = [document for document in documents if document.source.kind == "fixture"
                or relevant_business_text(document.content, idea, options)]
    warnings: list[str] = []
    gaps: list[str] = []
    if len(retained) < len(documents):
        warnings.append("Fetched pages without the reviewed solution's core topic were omitted; audience, region, domain or generic market words alone do not qualify evidence.")
        gaps.append("Some downloaded pages were off-topic and contribute no evidence or statistics. Topic filtering is a conservative lexical heuristic, not semantic validation.")
    if not _topic_groups(idea, options):
        gaps.append("The reviewed solution does not specify a distinctive research topic. Add the actual product, service or commodity before relying on automatic evidence selection.")
    return retained, warnings, gaps


def filter_business_evidence(
    evidence: list[EvidenceItem], idea: IdeaSpec, options: ResearchOptions,
) -> tuple[list[EvidenceItem], list[str], list[str]]:
    retained = [item for item in evidence if relevant_business_text(item.excerpt, idea, options)]
    warnings: list[str] = []
    gaps: list[str] = []
    if len(retained) < len(evidence):
        warnings.append("Retrieved passages that did not mention the solution's core topic were omitted, even when other parts of their source were relevant.")
        gaps.append("Selected passages do not establish exhaustive market, competitor, pricing or customer coverage. Uncovered conclusions must remain assumptions or unknowns.")
    return retained, warnings, gaps


def figure_scope_notes(text: str, source_url: str, options: ResearchOptions | None = None,
                       idea: IdeaSpec | None = None) -> list[str]:
    """Describe only scope explicitly present in a quote, without inferring it."""
    years = list(dict.fromkeys(re.findall(r"\b(?:19|20)\d{2}(?:s)?\b", text)))
    notes = ["Quoted quantity, not a calculated TAM/SAM/SOM or proof of startup demand."]
    notes.append("Years in quote: " + ", ".join(years[:4]) + "." if years else
                 "Measurement period is not stated in the quote.")
    region = options.region.strip() if options and options.region else ""
    countries = [name for name in (
        "India", "United States", "United Kingdom", "China", "Canada", "Australia", "Nigeria", "Nepal", "Chile",
        "Japan", "Germany", "France", "Brazil", "South Africa", "Kenya", "Bangladesh", "Pakistan",
    ) if re.search(r"\b" + re.escape(name) + r"\b", text, re.I)]
    if region and re.search(r"(?<!\w)" + re.escape(region) + r"(?!\w)", text, re.I) and region.casefold() not in {c.casefold() for c in countries}:
        countries.append(region)
    notes.append("Places in quote: " + ", ".join(countries[:4]) + "; check measured population." if countries else
                 "Geography is not stated in the quote.")
    if region and not re.search(r"(?<!\w)" + re.escape(region) + r"(?!\w)", text, re.I):
        notes.append(f"Quote does not establish applicability to requested region ({region[:100]}).")
    if idea and idea.target_users:
        users = ", ".join(idea.target_users[:2])
        if len(users) > 100:
            users = users[:100] + "…"
        notes.append(f"Check quoted population against intended customers: {users}.")
    if re.search(r"/opinion/|/interview|\binterview\b|\bopinion\b", source_url + " " + text, re.I):
        notes.append("Opinion/interview claim; verify primary data.")
    if re.search(r"\b(?:19\d{2}s?|historically|in those days|retrospective)\b", text, re.I):
        notes.append("Historical context; not a current market estimate.")
    if re.search(r"\b(?:projected|forecast|forecasted|expected|may|could|potential|estimate|estimated)\b", text, re.I):
        notes.append("Estimate/prospective claim; not a verified realized outcome.")
    return notes
