"""Check unrelated public sources once, recording only typed fetch outcomes."""
import asyncio
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from app.modules.intelligence.errors import IntelligenceError
from app.modules.intelligence.web_fetch import PublicPageFetcher


async def main():
    records = []
    for url in (
        "https://ourworldindata.org/global-education",
        "https://nces.ed.gov/programs/coe/indicator/cha",
        "https://www.worldbank.org/en/topic/education/overview",
    ):
        try:
            document = await PublicPageFetcher(timeout=20).fetch(url)
            row = {"url": url, "status": "success", "document": document.model_dump(mode="json")}
        except IntelligenceError as error:
            row = {"url": url, "status": "failed", "code": error.code, "message": error.message}
        records.append(row)
        print(json.dumps({key: value for key, value in row.items() if key != "document"}), flush=True)
    out = BACKEND.parent.parent / "integration" / "web_trials" / "public_access_probe.json"
    out.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
