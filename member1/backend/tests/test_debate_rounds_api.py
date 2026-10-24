"""Longer debate contracts through the asynchronous job and transcript API."""

import asyncio

import httpx
import pytest
import pytest_asyncio

from app.main import create_app
from app.modules.intelligence.settings import Settings


@pytest_asyncio.fixture
async def debate_api(tmp_path):
    app = create_app(Settings(app_mode="demo", database_path=str(tmp_path / "rounds.sqlite3"), demo_delay=0))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client


async def finish(client, response):
    assert response.status_code == 202, response.text
    for _ in range(400):
        job = (await client.get(response.json()["status_url"])).json()
        if job["status"] in {"succeeded", "failed"}:
            assert job["status"] == "succeeded", job
            return job
        await asyncio.sleep(0.01)
    pytest.fail("The demo debate job did not complete")


async def research(client):
    text = "Campus Meals connects college students with local kitchens for affordable nutritious meals."
    project = (await client.post("/api/v1/projects", json={"display_name": "Campus Meals", "idea_text": text})).json()
    extracted = await finish(client, await client.post("/api/v1/intelligence/extractions",
        json={"project_id": project["project_id"], "idea_text": text}, headers={"Idempotency-Key": "extract"}))
    job = await finish(client, await client.post("/api/v1/intelligence/analyses",
        json={"project_id": project["project_id"], "idea": extracted["result"]}, headers={"Idempotency-Key": "research"}))
    analysis = (await client.get(job["result_url"])).json()
    return project, analysis


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_rounds", [0, -1, 101, 3.5, 10.0, "10", True])
async def test_invalid_numeric_rounds_are_rejected_by_api(debate_api, bad_rounds):
    response = await debate_api.post("/api/v1/intelligence/debates", json={
        "project_id": "p_unused", "analysis_id": "a_unused", "mode": "agent_vs_agent", "max_rounds": bad_rounds,
    }, headers={"Idempotency-Key": "invalid-rounds"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_automatic_twenty_five_round_debate_persists_complete_transcript(debate_api):
    project, analysis = await research(debate_api)
    started = await finish(debate_api, await debate_api.post("/api/v1/intelligence/debates", json={
        "project_id": project["project_id"], "analysis_id": analysis["analysis_id"],
        "mode": "agent_vs_agent", "max_rounds": 25,
    }, headers={"Idempotency-Key": "watch-25"}))
    response = await debate_api.get(started["result_url"])
    assert response.status_code == 200
    transcript = response.json()
    assert transcript["max_rounds"] == 25
    assert transcript["status"] == "completed"
    assert len(transcript["turns"]) == 50
    assert [turn["round_number"] for turn in transcript["turns"]] == [number for number in range(1, 26) for _ in range(2)]
    assert transcript["summary"]
    assert (await debate_api.get(started["result_url"])).json() == transcript


@pytest.mark.asyncio
async def test_interactive_ten_round_debate_remains_active_until_tenth_reply(debate_api):
    project, analysis = await research(debate_api)
    started = await finish(debate_api, await debate_api.post("/api/v1/intelligence/debates", json={
        "project_id": project["project_id"], "analysis_id": analysis["analysis_id"],
        "mode": "user_vs_agent", "user_stance": "against", "max_rounds": 10,
    }, headers={"Idempotency-Key": "interactive-10"}))
    for number in range(1, 11):
        await finish(debate_api, await debate_api.post(started["result_url"] + "/turns", json={
            "message": f"My pilot objection for round {number}.",
        }, headers={"Idempotency-Key": f"round-{number}"}))
        transcript = (await debate_api.get(started["result_url"])).json()
        assert len(transcript["turns"]) == 1 + number * 2
        assert transcript["status"] == ("completed" if number == 10 else "active")
        assert transcript["turns"][-1]["speaker"] == "pro"
        assert transcript["turns"][-1]["round_number"] == number
    assert transcript["max_rounds"] == 10
    assert transcript["summary"]
    extra = await debate_api.post(started["result_url"] + "/turns", json={"message": "An extra objection."},
                                 headers={"Idempotency-Key": "extra-round"})
    assert extra.status_code == 202
    for _ in range(400):
        job = (await debate_api.get(extra.json()["status_url"])).json()
        if job["status"] == "failed":
            assert job["error"]["code"] == "DEBATE_FINISHED"
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("The extra-round request was not rejected")
    assert (await debate_api.get(started["result_url"])).json() == transcript
