import asyncio

import httpx
import pytest
import pytest_asyncio

from app.main import create_app
from app.modules.intelligence.settings import Settings


IDEA = "A student startup app that helps local shops manage leftover food and connect with nearby customers."


@pytest_asyncio.fixture
async def client(tmp_path):
    app = create_app(Settings(database_path=str(tmp_path / "test.sqlite3"), demo_delay=0))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as connection:
            yield connection


async def project(client):
    response = await client.post("/api/v1/projects", json={"display_name": "FoodLoop", "idea_text": IDEA})
    assert response.status_code == 201, response.text
    return response.json()


async def test_project_draft_fields_are_saved_and_restored(client):
    item = await project(client)
    idea = {
        "original_text": IDEA,
        "project_name": "FoodLoop",
        "problem": "Local shops have leftover food they cannot sell.",
        "solution": "Connect shops with nearby customers to offer leftover food.",
        "target_users": ["Local shops", "Nearby customers"],
        "domain": "Food services",
        "assumptions": ["Customers will buy discounted food."],
        "constraints": ["Begin in one neighborhood."],
        "field_origins": {"problem": "provided", "solution": "provided", "target_users": "provided"},
    }
    saved = await client.patch(f"/api/v1/projects/{item['project_id']}",
                               json={"expected_revision": item["revision"], "draft_idea": idea})
    assert saved.status_code == 200, saved.text
    assert saved.json()["draft_idea"] == idea
    assert saved.json()["revision"] == item["revision"] + 1
    reopened = await client.get(f"/api/v1/projects/{item['project_id']}")
    assert reopened.json()["draft_idea"] == idea


async def test_project_draft_fields_patch_obeys_revision_guard(client):
    item = await project(client)
    saved = await client.patch(f"/api/v1/projects/{item['project_id']}", json={
        "expected_revision": item["revision"],
        "draft_idea": {"original_text": IDEA, "project_name": "FoodLoop", "problem": "Problem",
                       "solution": "Solution", "domain": "Food", "target_users": [],
                       "assumptions": [], "constraints": [], "field_origins": {}},
    })
    assert saved.status_code == 200
    stale = await client.patch(f"/api/v1/projects/{item['project_id']}", json={
        "expected_revision": item["revision"], "display_name": "Stale update",
    })
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "PROJECT_REVISION_CONFLICT"


async def submit(client, url, body, key="request-1"):
    response = await client.post(url, json=body, headers={"Idempotency-Key": key})
    assert response.status_code == 202, response.text
    return response.json()


async def completed(client, receipt):
    for _ in range(200):
        response = await client.get(receipt["status_url"])
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] in {"succeeded", "failed"}:
            return job
        await asyncio.sleep(0.01)
    pytest.fail("The job did not finish")


async def analyzed(client):
    item = await project(client)
    extraction = await completed(client, await submit(client, "/api/v1/intelligence/extractions",
                           {"project_id": item["project_id"], "idea_text": IDEA}, "extract"))
    assert extraction["status"] == "succeeded", extraction
    assert extraction["result"]["original_text"] == IDEA
    analysis_job = await completed(client, await submit(client, "/api/v1/intelligence/analyses",
                        {"project_id": item["project_id"], "idea": extraction["result"]}, "research"))
    assert analysis_job["status"] == "succeeded", analysis_job
    result = (await client.get(analysis_job["result_url"])).json()
    return item, result


async def test_analysis_context_commit_and_immutable_versions(client):
    item, result = await analyzed(client)
    draft = result["context_draft"]
    assert result["figures"] == draft["figures"]
    assert len(result["figures"]) >= 3
    assert draft["execution"]["mode"] == "demo"
    assert draft["critic"]["review_status"] == "ready_with_gaps"
    endpoint = f'/api/v1/projects/{item["project_id"]}/contexts'
    first = await client.post(endpoint, json={"expected_latest_version": 0, "context": draft})
    assert first.status_code == 201, first.text
    assert first.json()["context_version"] == 1
    conflict = await client.post(endpoint, json={"expected_latest_version": 0, "context": draft})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "CONTEXT_VERSION_CONFLICT"
    updated = dict(draft, warnings=draft["warnings"] + ["Second revision"])
    second = await client.post(endpoint, json={"expected_latest_version": 1, "context": updated})
    assert second.status_code == 201
    original = await client.get(endpoint + "/1")
    latest = await client.get(f'/api/v1/projects/{item["project_id"]}/context')
    assert original.json() == first.json()
    assert latest.json() == second.json()
    assert latest.json()["content_hash"] != original.json()["content_hash"]


async def test_idempotent_job_replay_and_changed_payload_conflict(client):
    item = await project(client)
    body = {"project_id": item["project_id"], "idea_text": IDEA}
    one = await submit(client, "/api/v1/intelligence/extractions", body)
    two = await submit(client, "/api/v1/intelligence/extractions", body)
    assert one == two
    changed = await client.post("/api/v1/intelligence/extractions", json=dict(body, idea_text=IDEA + " Changed."),
                                headers={"Idempotency-Key": "request-1"})
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert (await completed(client, one))["status"] == "succeeded"


async def test_two_agent_debate_alternates_and_uses_known_evidence(client):
    item, result = await analyzed(client)
    job = await completed(client, await submit(client, "/api/v1/intelligence/debates",
                {"project_id": item["project_id"], "analysis_id": result["analysis_id"],
                 "mode": "agent_vs_agent", "max_rounds": 2}))
    assert job["status"] == "succeeded", job
    session = (await client.get(job["result_url"])).json()
    assert session["status"] == "completed"
    assert [turn["speaker"] for turn in session["turns"]] == ["pro", "skeptic", "pro", "skeptic"]
    assert [turn["round_number"] for turn in session["turns"]] == [1, 1, 2, 2]
    assert session["summary"]
    evidence = {entry["evidence_id"]: entry for entry in result["context_draft"]["evidence"]}
    for turn in session["turns"]:
        assert all(ref in evidence for ref in turn["evidence_ids"])
        assert set(turn["source_ids"]) == {evidence[ref]["source_id"] for ref in turn["evidence_ids"]}
        assert all(figure["evidence_id"] in turn["evidence_ids"] for figure in turn["figures"])
    bad = await completed(client, await submit(client, job["result_url"] + "/turns", {"message": "An objection"}, "watch-turn"))
    assert bad["status"] == "failed"
    assert bad["error"]["code"] == "WATCH_ONLY_DEBATE"


@pytest.mark.parametrize("stance,agent", [("for", "skeptic"), ("against", "pro")])
async def test_user_debate_opposite_stance_and_round_limit(client, stance, agent):
    item, result = await analyzed(client)
    start = await completed(client, await submit(client, "/api/v1/intelligence/debates",
                {"project_id": item["project_id"], "analysis_id": result["analysis_id"],
                 "mode": "user_vs_agent", "user_stance": stance, "max_rounds": 1}))
    assert start["status"] == "succeeded", start
    initial = (await client.get(start["result_url"])).json()
    assert initial["status"] == "active"
    assert initial["turns"][0]["speaker"] == agent
    turn = await completed(client, await submit(client, start["result_url"] + "/turns",
                               {"message": "We can measure repeated use in a narrow pilot."}, "turn"))
    assert turn["status"] == "succeeded", turn
    final = (await client.get(turn["result_url"])).json()
    assert [entry["speaker"] for entry in final["turns"]] == [agent, "user", agent]
    assert final["status"] == "completed"
    assert final["summary"]
    replay = await completed(client, await submit(client, start["result_url"] + "/turns",
                               {"message": "We can measure repeated use in a narrow pilot."}, "turn"))
    assert replay["job_id"] == turn["job_id"]
    assert (await client.get(turn["result_url"])).json() == final
    extra = await completed(client, await submit(client, start["result_url"] + "/turns",
                               {"message": "One more objection"}, "extra"))
    assert extra["error"]["code"] == "DEBATE_FINISHED"


async def test_cross_project_debate_rejected_and_error_envelope(client):
    _, result = await analyzed(client)
    other = await project(client)
    response = await client.post("/api/v1/intelligence/debates",
                json={"project_id": other["project_id"], "analysis_id": result["analysis_id"], "mode": "agent_vs_agent"},
                headers={"Idempotency-Key": "wrong-project", "X-Request-ID": "trace-123"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ANALYSIS_PROJECT_MISMATCH"
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"] == "trace-123"
    invalid = await client.post("/api/v1/projects", json={"display_name": "X", "idea_text": "short"})
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "input" not in invalid.json()["error"]["details"]["fields"][0]


async def test_live_unconfigured_fails_job_without_demo_fallback(tmp_path):
    app = create_app(Settings(app_mode="live", database_path=str(tmp_path / "live.sqlite3")))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            health = (await client.get("/api/v1/health")).json()
            assert health["mode"] == "live"
            assert not health["capabilities"]["live_configured"]
            item = await project(client)
            job = await completed(client, await submit(client, "/api/v1/intelligence/extractions",
                                       {"project_id": item["project_id"], "idea_text": IDEA}))
            assert job["status"] == "failed"
            assert job["result"] is None
            assert job["error"]["code"] == "PROVIDER_CONFIGURATION"
