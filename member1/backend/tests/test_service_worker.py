"""Status writes must survive the Windows lock observed during real startup."""
import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "service_worker.py"
spec = importlib.util.spec_from_file_location("tested_service_worker", SCRIPT)
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


def test_atomic_status_replace_retries_a_transient_reader_lock(tmp_path, monkeypatch):
    target = tmp_path / "api-service.json"
    target.write_text('{"status":"starting"}', encoding="utf-8")
    real_replace = worker.os.replace
    attempts = []
    sleeps = []

    def locked_replace(source, destination):
        attempts.append(source)
        if len(attempts) < 3:
            assert json.loads(target.read_text())["status"] == "starting"
            raise PermissionError("Windows destination temporarily open")
        real_replace(source, destination)

    monkeypatch.setattr(worker.os, "replace", locked_replace)
    monkeypatch.setattr(worker.time, "sleep", sleeps.append)
    state = {"launch_id": "a" * 32, "status": "running", "service_pid": 123}
    worker._record(target, state)
    assert json.loads(target.read_text()) == state
    assert sleeps == [0.05, 0.05]
    assert not attempts[-1].exists()


def test_permanent_status_write_error_remains_bounded(tmp_path, monkeypatch):
    sleeps = []
    def unavailable(*args):
        raise PermissionError("Persistent permission failure")
    monkeypatch.setattr(worker.os, "replace", unavailable)
    monkeypatch.setattr(worker.time, "sleep", sleeps.append)
    with pytest.raises(PermissionError):
        worker._record(tmp_path / "api-service.json", {"launch_id": "b" * 32, "status": "starting"})
    assert len(sleeps) == 39


def test_non_lock_status_errors_are_not_hidden(tmp_path, monkeypatch):
    def unavailable(*args):
        raise OSError("Disk unavailable")
    monkeypatch.setattr(worker.os, "replace", unavailable)
    monkeypatch.setattr(worker.time, "sleep", lambda _: pytest.fail("Only transient lock errors may retry"))
    with pytest.raises(OSError, match="Disk unavailable"):
        worker._record(tmp_path / "api-service.json", {"launch_id": "c" * 32, "status": "starting"})
