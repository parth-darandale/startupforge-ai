"""Small explicit-backend search check; no model call or application writes."""
import json
import time
from pathlib import Path

from ddgs import DDGS


if __name__ == "__main__":
    records = []
    for engine in ("brave", "yahoo"):
        started = time.monotonic()
        try:
            results = DDGS(timeout=12).text(
                "India higher education enrolment 4.33 crore AISHE 2021 2022",
                backend=engine, max_results=5, region="in-en",
            )
            row = {"engine": engine, "status": "success", "results": results}
        except Exception as error:
            row = {"engine": engine, "status": "failed", "error_type": type(error).__name__}
        row["elapsed_seconds"] = round(time.monotonic() - started, 2)
        records.append(row)
        print(json.dumps(row), flush=True)
    destination = Path(__file__).resolve().parents[3] / "integration" / "web_trials" / "search_probe.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(records, indent=2), encoding="utf-8")
