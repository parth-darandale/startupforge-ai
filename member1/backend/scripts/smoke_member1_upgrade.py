"""Real local AI + automatic news research + immutable teammate handoff check."""
import json
import re
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.modules.intelligence.contracts import Claim, EvidenceItem
from app.modules.intelligence.grounding import quotation_issue

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "integration" / "web_trials" / "member1_upgrade_workflow.json"
IDEA = (
    "SunQuote is a rooftop solar quote comparison service for small factory owners in India. "
    "It would collect installer quotations and help compare costs. "
    "Start with a pilot in one industrial area and test demand before charging customers."
)


def main():
    report = {"status": "running", "steps": [], "original_idea": IDEA,
              "scope": "Real Qwen extraction and automatic news discovery/full-page fetching. "
                       "No supplied URLs, synthetic data or calculated market size. "
                       "Trade publication claims remain publisher assertions requiring review."}
    started = time.monotonic()

    def checkpoint():
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    checkpoint()
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=30) as client:
        def get(path):
            response = client.get(path)
            response.raise_for_status()
            return response.json()

        def job(path, body):
            step_started = time.monotonic()
            response = client.post(path, json=body, headers={"Idempotency-Key": uuid.uuid4().hex})
            response.raise_for_status()
            receipt = response.json()
            report["active_receipt"] = receipt
            checkpoint()
            previous = None
            while time.monotonic() - step_started < 600:
                current = get(receipt["status_url"])
                if current["stage"] != previous:
                    print(path, current["stage"], flush=True)
                    previous = current["stage"]
                if current["status"] in {"succeeded", "failed"}:
                    report["steps"].append({"endpoint": path, "job": current,
                                            "elapsed_seconds": round(time.monotonic() - step_started, 2)})
                    checkpoint()
                    if current["status"] != "succeeded":
                        raise RuntimeError(current["error"]["code"] + ": " + current["error"]["message"])
                    return get(current["result_url"]) if current.get("result_url") else current["result"]
                time.sleep(1)
            raise TimeoutError("Live job exceeded its bounded check deadline.")

        try:
            report["health"] = get("/api/v1/health")
            assert report["health"]["mode"] == "live"
            assert report["health"]["providers"]["discovery"] == "bing_news"
            response = client.post("/api/v1/projects", json={"display_name": "SunQuote upgraded workflow", "idea_text": IDEA})
            response.raise_for_status()
            project = response.json()
            report["project"] = project
            idea = job("/api/v1/intelligence/extractions", {"project_id": project["project_id"], "idea_text": IDEA})
            report["extracted_idea"] = idea
            assert idea["original_text"] == IDEA
            assert "rooftop solar" in idea["solution"].lower(), "Extraction lost the core product topic."
            assert idea["project_name"] == "SunQuote"
            assert any("small factory owners" in item for item in idea["target_users"])
            assert all(item in IDEA for item in idea["target_users"])
            assert any("pilot" in item.lower() for item in idea["constraints"])
            assert any("before charging" in item.lower() for item in idea["constraints"])
            checkpoint()
            analysis = job("/api/v1/intelligence/analyses", {"project_id": project["project_id"], "idea": idea,
                          "research_options": {"max_sources": 2, "region": "India", "source_urls": []}})
            report["analysis"] = analysis
            draft = analysis["context_draft"]
            assert draft["sources"] and draft["evidence"], "Automatic run did not produce usable full-page evidence."
            evidence = {item["evidence_id"]: item for item in draft["evidence"]}
            typed_evidence = {key: EvidenceItem.model_validate(item) for key, item in evidence.items()}
            for section in draft["final_analysis"]["sections"]:
                for claim in section["claims"]:
                    if claim["kind"] == "supported":
                        assert quotation_issue(Claim.model_validate(claim), typed_evidence) is None
                    else:
                        assert not re.search(r"\d", claim["text"]), "Unvalidated numerical assertion survived review."
            for figure in draft["figures"]:
                cited = evidence[figure["evidence_id"]]
                assert figure["source_id"] == cited["source_id"]
                assert figure["excerpt"] in cited["excerpt"] and figure["display_value"] in figure["excerpt"]
            report["integration_modules"] = get("/api/v1/integration/modules")
            if draft["critic"]["review_status"] == "blocked":
                report["status"] = "completed_with_blocked_review"
            else:
                response = client.post(f"/api/v1/projects/{project['project_id']}/contexts",
                                       json={"expected_latest_version": 0, "context": draft})
                response.raise_for_status()
                saved = response.json()
                report["saved_context"] = saved
                report["handoff"] = get(f"/api/v1/projects/{project['project_id']}/contexts/{saved['context_version']}/handoff")
                assert report["handoff"]["context"] == saved
                response = client.post("/api/v1/studio/pitch-decks", headers={"Idempotency-Key": uuid.uuid4().hex},
                                       json={"context": saved, "options": {"template_id": "not-installed", "slide_count": 8, "tone": "clear"}})
                report["pending_member2_response"] = {"status_code": response.status_code, "body": response.json()}
                assert response.status_code == 503 and response.json()["error"]["code"] == "MODULE_NOT_AVAILABLE"
                response = client.post("/api/v1/reports/generations", headers={"Idempotency-Key": uuid.uuid4().hex},
                                       json={"context": saved, "options": {"template_id": "not-installed", "formats": ["docx"]}})
                report["pending_member3_response"] = {"status_code": response.status_code, "body": response.json()}
                assert response.status_code == 503 and response.json()["error"]["code"] == "MODULE_NOT_AVAILABLE"
                debate = job("/api/v1/intelligence/debates", {
                    "project_id": project["project_id"], "analysis_id": analysis["analysis_id"],
                    "mode": "user_vs_agent", "user_stance": "against", "max_rounds": 1,
                })
                report["live_debate_opening"] = debate
                assert debate["turns"] and debate["turns"][0]["stance"] == "for"
                completed_debate = job(f"/api/v1/intelligence/debates/{debate['debate_id']}/turns", {
                    "message": "Industry installation totals do not show that factory owners need another comparison service. Test the pilot against their existing quotation process before charging."
                })
                report["live_debate_completed"] = completed_debate
                assert completed_debate["status"] == "completed" and completed_debate["summary"]
                assert [turn["speaker"] for turn in completed_debate["turns"]] == ["pro", "user", "pro"]
                report["status"] = "completed"
            report["counts"] = {"sources": len(draft["sources"]), "evidence": len(draft["evidence"]), "figures": len(draft["figures"])}
        except Exception as error:
            report["status"] = "failed"
            report["error"] = str(error)
            raise
        finally:
            report["elapsed_seconds"] = round(time.monotonic() - started, 2)
            checkpoint()
            print(json.dumps({key: report.get(key) for key in ("status", "counts", "elapsed_seconds", "error")}), flush=True)


if __name__ == "__main__":
    main()
