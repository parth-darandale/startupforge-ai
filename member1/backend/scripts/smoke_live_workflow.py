"""Exercise real configured providers through the public localhost API."""
import json
import time
import uuid
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "integration" / "model_trials" / "live_workflow.json"
IDEA = "Build CampusLoop, a campus marketplace for college students to buy and sell used textbooks. Existing campus message boards are a free alternative. Start with one college in India and validate demand before charging a commission."


def run():
    report = {"status": "running", "steps": [], "started_at": time.time()}
    def checkpoint():
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=30) as client:
        report["health"] = client.get("/api/v1/health").json()
        assert report["health"]["mode"] == "live", report["health"]
        def create_job(endpoint, body):
            started = time.monotonic()
            response = client.post(endpoint, json=body, headers={"Idempotency-Key": uuid.uuid4().hex})
            response.raise_for_status()
            receipt = response.json()
            deadline = started + 1200
            previous = None
            while time.monotonic() < deadline:
                job = client.get(receipt["status_url"]).json()
                if job.get("stage") != previous:
                    print(endpoint, job.get("stage"), flush=True)
                    previous = job.get("stage")
                if job.get("status") in {"succeeded", "failed"}:
                    report["steps"].append({"endpoint": endpoint, "elapsed_seconds": round(time.monotonic() - started, 3), "job": job})
                    checkpoint()
                    if job["status"] != "succeeded":
                        raise RuntimeError(json.dumps(job["error"]))
                    return client.get(job["result_url"]).json() if job.get("result_url") else job["result"]
                time.sleep(1)
            raise TimeoutError("Job exceeded live smoke deadline")
        try:
            response = client.post("/api/v1/projects", json={"display_name": "CampusLoop live research trial", "idea_text": IDEA})
            response.raise_for_status()
            project = response.json()
            report["project"] = project
            checkpoint()
            idea = create_job("/api/v1/intelligence/extractions", {"project_id": project["project_id"], "idea_text": IDEA})
            report["extracted_idea"] = idea
            # Exercise the intended review/edit stage. Preserve raw extraction
            # separately; these corrections come directly from the original
            # input and are not credited as successful model extraction.
            reviewed = {**idea, "target_users": ["College students in India"],
                        "problem": "College students need a way to buy and sell used textbooks.",
                        "solution": "A campus marketplace for college students to buy and sell used textbooks.",
                        "field_origins": {**idea["field_origins"], "target_users": "provided",
                                          "problem": "inferred", "solution": "provided"}}
            report["reviewed_idea"] = reviewed
            report["review_note"] = "Assistant corrected users/problem/solution from the explicit original input during the review/edit step; raw extractor output remains recorded."
            analysis = create_job("/api/v1/intelligence/analyses", {"project_id": project["project_id"], "idea": reviewed, "research_options": {"max_sources": 3, "region": "India"}})
            report["analysis"] = analysis
            checkpoint()
            body = {"project_id": project["project_id"], "analysis_id": analysis["analysis_id"], "max_rounds": 1}
            debate = create_job("/api/v1/intelligence/debates", {**body, "mode": "user_vs_agent", "user_stance": "against"})
            debate = create_job(f"/api/v1/intelligence/debates/{debate['debate_id']}/turns", {"message": "Students can already use the free campus message board. What specific benefit could justify a commission, and how would we test willingness to pay?"})
            report["user_debate"] = debate
            report["agent_debate"] = create_job("/api/v1/intelligence/debates", {**body, "mode": "agent_vs_agent"})
            response = client.post(f"/api/v1/projects/{project['project_id']}/contexts", json={"expected_latest_version": 0, "context": analysis["context_draft"]})
            response.raise_for_status()
            report["saved_context"] = response.json()
            report["status"] = "completed"
        except Exception as error:
            report["status"] = "failed"
            report["error"] = str(error)
            raise
        finally:
            checkpoint()

if __name__ == "__main__":
    run()
