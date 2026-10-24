"""Local shutdown rejects other sites and never terminates unverified processes."""
import asyncio
import copy
import json

import httpx
import pytest

from app import local_shutdown
from app.main import create_app
from app.modules.intelligence.errors import IntelligenceError
from app.modules.intelligence.settings import Settings


class FakeShutdown:
    def __init__(self):
        self.prepared = 0
        self.started = 0
        self.failure = False

    def prepare(self):
        self.prepared += 1
        if self.failure:
            raise IntelligenceError("SHUTDOWN_UNAVAILABLE", "Try again.", 503)
        return {"verified": True}

    def start(self, plan):
        assert plan == {"verified": True}
        self.started += 1


@pytest.fixture
def setup(tmp_path):
    controller = FakeShutdown()
    app = create_app(Settings(database_path=str(tmp_path / "app.sqlite3"), demo_delay=0),
                     artifact_storage_root=tmp_path / "files", shutdown_controller=controller)
    return app, controller


@pytest.mark.asyncio
@pytest.mark.parametrize("headers,host,client_ip", [
    ({}, "127.0.0.1:8000", "127.0.0.1"),
    ({"X-StartupForge-Action": "shutdown", "Origin": "https://other.example"}, "127.0.0.1:8000", "127.0.0.1"),
    ({"X-StartupForge-Action": "shutdown", "Origin": "null"}, "127.0.0.1:8000", "127.0.0.1"),
    ({"X-StartupForge-Action": "shutdown", "Origin": "http://127.0.0.1:8001"}, "127.0.0.1:8000", "127.0.0.1"),
    ({"X-StartupForge-Action": "shutdown"}, "evil.example", "127.0.0.1"),
    ({"X-StartupForge-Action": "shutdown"}, "127.0.0.1:8000", "192.168.1.10"),
    ({"X-StartupForge-Action": "shutdown", "Sec-Fetch-Site": "cross-site"}, "127.0.0.1:8000", "127.0.0.1"),
])
async def test_shutdown_rejects_untrusted_requests(setup, headers, host, client_ip):
    app, controller = setup
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(client_ip, 1234)),
                                base_url=f"http://{host}") as client:
        response = await client.post("/api/v1/system/shutdown", headers=headers)
    assert response.status_code == 403
    assert controller.prepared == controller.started == 0
    assert not app.state.shutting_down


@pytest.mark.asyncio
async def test_shutdown_cancels_jobs_once_and_rejects_new_work(setup):
    app, controller = setup
    started = asyncio.Event()
    cancelled = asyncio.Event()
    project = app.state.store.create_project("Shutdown", "A test project with enough text.")

    async def work(progress):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    receipt = app.state.runner.submit("test", project["project_id"], "one", {}, work, "request_test")
    await started.wait()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        headers = {"X-StartupForge-Action": "shutdown", "Origin": "http://127.0.0.1:8000"}
        response = await client.post("/api/v1/system/shutdown", headers=headers)
        duplicate = await client.post("/api/v1/system/shutdown", headers=headers)
        new_work = await client.post("/api/v1/projects", json={"display_name": "New", "idea_text": "Another idea."})
    assert response.status_code == duplicate.status_code == 200
    assert response.json() == {"status": "shutting_down"}
    assert cancelled.is_set()
    assert app.state.store.get_job(receipt.job_id).status == "failed"
    assert controller.prepared == controller.started == 1
    assert new_work.status_code == 503


@pytest.mark.asyncio
async def test_shutdown_failure_keeps_app_usable_for_retry(setup):
    app, controller = setup
    controller.failure = True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000") as client:
        response = await client.post("/api/v1/system/shutdown", headers={"X-StartupForge-Action": "shutdown"})
        assert response.status_code == 503
        assert not app.state.shutting_down
        assert (await client.post("/api/v1/projects", json={"display_name": "New", "idea_text": "Another valid idea."})).status_code == 201


@pytest.mark.asyncio
async def test_local_vite_origin_is_explicitly_supported(setup):
    app, controller = setup
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        response = await client.post("/api/v1/system/shutdown", headers={
            "X-StartupForge-Action": "shutdown", "Origin": "http://127.0.0.1:5173", "Sec-Fetch-Site": "same-site"})
    assert response.status_code == 200
    assert controller.started == 1


def model_fixture(tmp_path):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    state = {"status": "running", "name": "ollama", "worker_pid": 20, "service_pid": 30,
             "started_at": "2026-10-23T08:00:00+00:00"}
    (runtime / "ollama-service.json").write_text(json.dumps(state), encoding="utf-8")
    return {
        20: {"ProcessId": 20, "ExecutablePath": str(tmp_path / ".venv" / "Scripts" / "python.exe"),
             "CommandLine": f'python "{tmp_path / "scripts" / "service_worker.py"}" manifest.json'},
        30: {"ProcessId": 30, "ParentProcessId": 20, "ExecutablePath": str(runtime / "ollama-v0.40.1" / "ollama.exe"),
             "Created": "2026-10-23T08:00:00.1234567Z"},
    }


def test_owned_model_requires_matching_workspace_worker_and_launch_time(tmp_path):
    processes = model_fixture(tmp_path)
    assert local_shutdown.owned_model_process(tmp_path, processes) == processes[30]
    for field, value in (("ParentProcessId", 99), ("ExecutablePath", r"C:\Other\ollama.exe"),
                         ("Created", "2026-10-23T09:00:00+00:00")):
        changed = copy.deepcopy(processes)
        changed[30][field] = value
        assert local_shutdown.owned_model_process(tmp_path, changed) is None
    processes[20]["CommandLine"] = "some other application"
    assert local_shutdown.owned_model_process(tmp_path, processes) is None


@pytest.mark.skipif(local_shutdown.os.name != "nt", reason="Windows venv process layout")
def test_windows_venv_base_interpreter_is_verified_for_api_and_worker(tmp_path, monkeypatch):
    processes = model_fixture(tmp_path)
    base_python = r"C:\Python310\python.exe"
    monkeypatch.setattr(local_shutdown.sys, "_base_executable", base_python)
    processes[20]["ExecutablePath"] = base_python
    processes[local_shutdown.os.getpid()] = {"ProcessId": local_shutdown.os.getpid(),
        "ExecutablePath": base_python, "Created": "2026-10-23T08:01:00Z"}
    monkeypatch.setattr(local_shutdown, "process_snapshot", lambda: processes)
    plan = local_shutdown.LocalShutdownController(tmp_path).prepare()
    assert plan["api"] == processes[local_shutdown.os.getpid()]
    assert plan["ollama"] == processes[30]


def test_reused_api_pid_stops_nothing(monkeypatch):
    api = {"ProcessId": 11, "ExecutablePath": "python.exe", "Created": "original"}
    monkeypatch.setattr(local_shutdown, "process_snapshot", lambda: {11: {**api, "Created": "replacement"}})
    calls = []
    monkeypatch.setattr(local_shutdown.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    local_shutdown.stop_verified_processes({"api": api, "ollama": None})
    assert calls == []


def test_shutdown_kills_only_verified_model_tree_then_api_without_tree(monkeypatch):
    api = {"ProcessId": 11, "ExecutablePath": "python.exe", "Created": "original"}
    model = {"ProcessId": 12, "ExecutablePath": "ollama.exe", "Created": "model"}
    monkeypatch.setattr(local_shutdown, "process_snapshot", lambda: {11: api, 12: model})
    monkeypatch.setattr(local_shutdown.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    calls = []
    monkeypatch.setattr(local_shutdown.subprocess, "run", lambda command, **kwargs: calls.append(command))
    local_shutdown.stop_verified_processes({"api": api, "ollama": model})
    assert calls[0][1:] == ["/PID", "12", "/T", "/F"]
    assert calls[1][1:] == ["/PID", "11", "/F"]


def test_reused_model_pid_is_preserved_while_api_stops(monkeypatch):
    api = {"ProcessId": 11, "ExecutablePath": "python.exe", "Created": "original"}
    model = {"ProcessId": 12, "ExecutablePath": "ollama.exe", "Created": "model"}
    monkeypatch.setattr(local_shutdown, "process_snapshot", lambda: {11: api, 12: {**model, "Created": "new"}})
    monkeypatch.setattr(local_shutdown.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    calls = []
    monkeypatch.setattr(local_shutdown.subprocess, "run", lambda command, **kwargs: calls.append(command))
    local_shutdown.stop_verified_processes({"api": api, "ollama": model})
    assert len(calls) == 1
    assert calls[0][1:] == ["/PID", "11", "/F"]


def test_model_stop_failure_does_not_prevent_api_shutdown(monkeypatch):
    api = {"ProcessId": 11, "ExecutablePath": "python.exe", "Created": "original"}
    model = {"ProcessId": 12, "ExecutablePath": "ollama.exe", "Created": "model"}
    monkeypatch.setattr(local_shutdown, "process_snapshot", lambda: {11: api, 12: model})
    monkeypatch.setattr(local_shutdown.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    calls = []

    def stop(command, **kwargs):
        calls.append(command)
        if "/T" in command:
            raise local_shutdown.subprocess.TimeoutExpired(command, 15)

    monkeypatch.setattr(local_shutdown.subprocess, "run", stop)
    local_shutdown.stop_verified_processes({"api": api, "ollama": model})
    assert calls[-1][1:] == ["/PID", "11", "/F"]
