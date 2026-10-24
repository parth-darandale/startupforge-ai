"""Debate restoration retains its seed jobs after lengthy interactive sessions."""
import httpx
import pytest

from app.main import create_app
from app.modules.intelligence.settings import Settings


@pytest.mark.asyncio
async def test_history_filters_find_seed_jobs_beyond_a_hundred_replies(tmp_path):
    app = create_app(Settings(database_path=str(tmp_path / "history.sqlite3"), demo_delay=0))
    store = app.state.store
    project = store.create_project("Long debate", "A startup idea with a complete research and debate history.")
    project_id = project["project_id"]
    analysis, _ = store.reserve_job("analyze_idea", project_id, "analysis", {})
    debate, _ = store.reserve_job("create_debate", project_id, "debate", {})
    replies = [store.reserve_job("debate_turn", project_id, f"reply-{number}", {}, "d_long")[0]
               for number in range(120)]
    other, _ = store.reserve_job("debate_turn", project_id, "other-session", {}, "d_other")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        path = f"/api/v1/projects/{project_id}/jobs"
        assert len((await client.get(path)).json()["jobs"]) == 100
        analyses = (await client.get(path, params={"operation": "analyze_idea"})).json()["jobs"]
        debates = (await client.get(path, params={"operation": "create_debate"})).json()["jobs"]
        turns = (await client.get(path, params={"operation": "debate_turn", "debate_id": "d_long"})).json()["jobs"]
    assert [job["job_id"] for job in analyses] == [analysis.job_id]
    assert [job["job_id"] for job in debates] == [debate.job_id]
    assert len(turns) == 100
    assert turns[0]["job_id"] == replies[-1].job_id
    assert other.job_id not in {job["job_id"] for job in turns}


@pytest.mark.asyncio
async def test_debate_history_filter_is_project_scoped_and_requires_turn_operation(tmp_path):
    app = create_app(Settings(database_path=str(tmp_path / "scope.sqlite3"), demo_delay=0))
    store = app.state.store
    first = store.create_project("First", "The first startup idea for a separate workspace.")
    second = store.create_project("Second", "The second startup idea for a separate workspace.")
    store.reserve_job("debate_turn", second["project_id"], "reply", {}, "d_shared")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        path = f"/api/v1/projects/{first['project_id']}/jobs"
        assert (await client.get(path, params={"operation": "debate_turn", "debate_id": "d_shared"})).json()["jobs"] == []
        invalid = await client.get(path, params={"operation": "analyze_idea", "debate_id": "d_shared"})
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "INVALID_JOB_FILTER"
