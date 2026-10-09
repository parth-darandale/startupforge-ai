"""Exercise live full-page research and context integration through the API."""
import json
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "integration" / "web_trials" / "live_workflow.json"


def main():
    report = {"status": "running", "steps": [],
              "scope": "Real Qwen calls and supplied public page URLs; search discovery is not credited by this check. Broad education background is not Indian textbook market validation."}
    def checkpoint():
        OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=30) as client:
        report["health"] = client.get("/api/v1/health").json()
        assert report["health"]["providers"]["search"] == "web"
        old = json.loads((ROOT / "integration" / "model_trials" / "live_workflow.json").read_text(encoding="utf-8"))
        idea = old["reviewed_idea"]
        response = client.post("/api/v1/projects", json={"display_name": "CampusLoop full-page research check", "idea_text": idea["original_text"]})
        response.raise_for_status()
        project = response.json()
        report["project"] = project
        checkpoint()
        response = client.post("/api/v1/intelligence/analyses", headers={"Idempotency-Key": uuid.uuid4().hex}, json={
            "project_id": project["project_id"], "idea": idea,
            "research_options": {"max_sources": 2, "region": "India", "source_urls": [
                "https://ourworldindata.org/global-education", "https://www.worldbank.org/en/topic/education/overview",
            ]},
        })
        response.raise_for_status()
        receipt = response.json()
        report["receipt"] = receipt
        checkpoint()
        started = time.monotonic()
        previous = None
        while time.monotonic() - started < 600:
            job = client.get(receipt["status_url"]).json()
            if job.get("stage") != previous:
                print("research", job.get("stage"), flush=True)
                previous = job.get("stage")
            if job["status"] in {"succeeded", "failed"}:
                report["job"] = job
                if job["status"] == "failed":
                    report["status"] = "failed"
                    checkpoint()
                    raise RuntimeError(job["error"]["code"] + ": " + job["error"]["message"])
                break
            time.sleep(1)
        else:
            raise TimeoutError("Live web research exceeded deadline")
        analysis = client.get(job["result_url"]).json()
        report["analysis"] = analysis
        response = client.post(f"/api/v1/projects/{project['project_id']}/contexts", json={"expected_latest_version": 0, "context": analysis["context_draft"]})
        response.raise_for_status()
        report["saved_context"] = response.json()
        assert report["saved_context"]["figures"] == analysis["figures"]
        report["status"] = "completed"
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        checkpoint()
        print(json.dumps({"status": report["status"], "sources": len(analysis["context_draft"]["sources"]),
            "evidence": len(analysis["context_draft"]["evidence"]), "figures": len(analysis["figures"]),
            "critic": analysis["context_draft"]["critic"]["review_status"],
            "context_version": report["saved_context"]["context_version"],
            "elapsed_seconds": report["elapsed_seconds"]}), flush=True)


if __name__ == "__main__":
    main()
