"""Bounded real news discovery/fetch checks, without any model or app writes.

Run from member1/backend. Records live results for two different business topics;
this verifies sourcing mechanics, not the truth of publishers' market claims.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BACKEND_ROOT))

from app.modules.intelligence.business_evidence import filter_business_evidence
from app.modules.intelligence.contracts import IdeaSpec, ResearchOptions
from app.modules.intelligence.figures import extract_figures
from app.modules.intelligence.retrieval import KeywordRetriever
from app.modules.intelligence.settings import Settings
from app.modules.intelligence.web_research import WebResearchProvider


CASES = [
    ("rooftop_solar", "A marketplace to compare rooftop solar installation quotes for small factory owners in India.",
     "Compare rooftop solar installation quotes for factory owners.", "Energy", ["rooftop solar", "rooftop solar market pricing", "rooftop solar manufacturers challenges"]),
    ("grocery_delivery", "A grocery delivery service for households in Indian cities, assessing demand, costs and competition.",
     "Grocery delivery service for households in cities.", "Retail", ["grocery delivery", "grocery delivery market pricing", "grocery delivery companies competition"]),
]


async def main():
    settings = Settings(app_mode="live", search_provider="web", web_discovery_provider="bing_news", web_request_timeout=20)
    provider = WebResearchProvider(settings)
    records = []
    destination = ROOT / "integration" / "web_trials" / "automatic_news_research.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    for name, original, solution, domain, queries in CASES:
        structured = IdeaSpec(original_text=original, project_name=name, problem="Unverified customer need and market assumptions.",
                              solution=solution, domain=domain, field_origins={"solution": "provided"})
        options = ResearchOptions(max_sources=2, region="India")
        started = time.monotonic()
        batch = await provider.search_for_idea(queries, options, structured)
        evidence = await KeywordRetriever().retrieve(batch.documents, structured.solution, limit=6)
        evidence, warnings, gaps = filter_business_evidence(evidence, structured, options)
        figures = extract_figures(evidence, [document.source for document in batch.documents], idea=structured, options=options)
        record = {"case": name, "scope": "Automatic public news RSS discovery followed by full-page public fetch. No supplied source URLs, fixture sources or language model calls. Publisher claims are not independently verified.",
                  "idea": structured.model_dump(mode="json"), "queries": queries, "options": options.model_dump(mode="json"),
                  "documents": [document.model_dump(mode="json") for document in batch.documents],
                  "evidence": [item.model_dump(mode="json") for item in evidence],
                  "figures": [figure.model_dump(mode="json") for figure in figures],
                  "warnings": batch.warnings + warnings, "gaps": batch.gaps + gaps,
                  "elapsed_seconds": round(time.monotonic() - started, 2)}
        records.append(record)
        destination.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({"case": name, "sources": len(batch.documents), "evidence": len(evidence), "figures": len(figures),
                          "elapsed_seconds": record["elapsed_seconds"], "warnings": record["warnings"]}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
