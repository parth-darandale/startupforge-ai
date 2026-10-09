"""Verify actual publication downloads without model-generated figures."""
import asyncio
import json
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.modules.intelligence.contracts import ResearchOptions
from app.modules.intelligence.figures import extract_figures
from app.modules.intelligence.retrieval import KeywordRetriever
from app.modules.intelligence.settings import Settings
from app.modules.intelligence.web_research import WebResearchProvider


async def main():
    urls = [
        "https://www.pib.gov.in/PressReleasePage.aspx?PRID=1999713&lang=2&reg=48",
        "https://www.education.gov.in/sites/upload_files/mhrd/files/PIB1999713.pdf",
    ]
    provider = WebResearchProvider(Settings(web_request_timeout=30))
    started = time.monotonic()
    batch = await provider.search_with_diagnostics(
        ["India higher education student enrolment AISHE"], ResearchOptions(max_sources=2, source_urls=urls)
    )
    evidence = await KeywordRetriever().retrieve(batch.documents, "India higher education student enrolment AISHE", limit=4)
    figures = extract_figures(evidence, [document.source for document in batch.documents])
    report = {
        "status": "completed" if batch.documents else "no_sources",
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "note": "Supplied official URLs, no search or model call. Historical AISHE 2021-22 statistics exercise fetching/quotation only; no current market validation.",
        "documents": [document.model_dump(mode="json") for document in batch.documents],
        "evidence": [item.model_dump(mode="json") for item in evidence],
        "figures": [item.model_dump(mode="json") for item in figures],
        "warnings": batch.warnings, "gaps": batch.gaps,
    }
    destination = BACKEND.parent.parent / "integration" / "web_trials" / "full_page_probe.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"status": report["status"], "elapsed_seconds": report["elapsed_seconds"],
        "sources": len(batch.documents), "evidence": len(evidence), "figures": len(figures),
        "values": [figure.display_value for figure in figures[:8]], "warnings": batch.warnings}, indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
