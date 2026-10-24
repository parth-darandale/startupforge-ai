"""Stop this local app and only its launcher-owned model services."""
import base64
from datetime import datetime
import ipaddress
import json
import logging
import ntpath
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit

from fastapi import Request

from .modules.intelligence.errors import IntelligenceError


def validate_local_request(request: Request) -> None:
    def loopback(value):
        try:
            return ipaddress.ip_address(value).is_loopback
        except ValueError:
            return value == "localhost"

    if (not request.client or not loopback(request.client.host)
            or not request.url.hostname or not loopback(request.url.hostname)
            or request.headers.get("X-StartupForge-Action") != "shutdown"):
        raise IntelligenceError("LOCAL_ACTION_ONLY", "Shutdown is available only from the local StartupForge app.", 403)
    origin = request.headers.get("origin")
    if origin:
        try:
            parsed = urlsplit(origin)
            allowed = (parsed.scheme == request.url.scheme
                       and parsed.hostname == request.url.hostname
                       and (parsed.port or 80) == (request.url.port or 80)
                       and not parsed.username and not parsed.password
                       and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment)
            # Vite development uses the existing explicit local CORS allowlist.
            allowed = allowed or origin in {"http://localhost:5173", "http://127.0.0.1:5173"}
        except ValueError:
            allowed = False
        if not allowed:
            raise IntelligenceError("LOCAL_ACTION_ONLY", "Open Shutdown from the local StartupForge app.", 403)
    if request.headers.get("sec-fetch-site", "none") not in {"same-origin", "same-site", "none"}:
        raise IntelligenceError("LOCAL_ACTION_ONLY", "A different website cannot shut down StartupForge.", 403)


def _path(value: str) -> str:
    return ntpath.normcase(ntpath.normpath(value or ""))


def _powershell() -> str:
    return str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" /
               "WindowsPowerShell" / "v1.0" / "powershell.exe")


def process_snapshot() -> dict[int, dict]:
    # The script is constant: no request data is evaluated as PowerShell.
    script = """Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine,@{Name='Created';Expression={$_.CreationDate.ToUniversalTime().ToString('o')}} | ConvertTo-Json -Compress"""
    result = subprocess.run([_powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
                            creationflags=subprocess.CREATE_NO_WINDOW, check=True)
    values = json.loads(result.stdout.lstrip("\ufeff"))
    if not isinstance(values, list):
        values = [values]
    return {int(item["ProcessId"]): item for item in values if isinstance(item, dict)}


def same_process(expected: dict, actual: dict | None) -> bool:
    return bool(actual and expected["ProcessId"] == actual["ProcessId"]
                and expected.get("Created") and expected["Created"] == actual.get("Created")
                and _path(expected["ExecutablePath"]) == _path(actual.get("ExecutablePath")))


def _owned_worker_service(backend: Path, processes: dict[int, dict],
                          name: str, executable: Path) -> dict | None:
    try:
        state = json.loads((backend / ".runtime" / f"{name}-service.json").read_text(encoding="utf-8"))
        if state.get("status") != "running" or state.get("name") != name:
            return None
        service = processes.get(int(state["service_pid"]))
        worker = processes.get(int(state["worker_pid"]))
        if not service or not worker or service["ParentProcessId"] != worker["ProcessId"]:
            return None
        worker_pythons = {_path(str(backend / ".venv" / "Scripts" / "python.exe")),
                          _path(getattr(sys, "_base_executable", sys.executable))}
        if (_path(service.get("ExecutablePath")) != _path(str(executable))
                or _path(worker.get("ExecutablePath")) not in worker_pythons
                or _path(str(backend / "scripts" / "service_worker.py")) not in _path(worker.get("CommandLine"))):
            return None
        started = datetime.fromisoformat(state["started_at"])
        # CIM uses seven fractional digits and Z, which Python 3.10's ISO
        # parser does not accept. Keep the raw identity for exact revalidation.
        created_text = re.sub(r"(\.\d{6})\d+", r"\1", service["Created"]).replace("Z", "+00:00")
        created = datetime.fromisoformat(created_text)
        # The worker writes started_at immediately before spawning the service.
        if not -1 <= (created - started).total_seconds() <= 30:
            return None
        return service
    except (OSError, ValueError, KeyError, TypeError):
        return None


def owned_model_process(backend: Path, processes: dict[int, dict]) -> dict | None:
    return _owned_worker_service(backend, processes, "ollama",
                                 backend / ".runtime" / "ollama-v0.40.1" / "ollama.exe")


def owned_autopresent_process(backend: Path, processes: dict[int, dict]) -> dict | None:
    service = _owned_worker_service(
        backend, processes, "autopresent",
        backend / ".runtime" / "autopresent" / "llama.cpp-b11838" / "llama-server.exe")
    if not service:
        return None
    command = _path(service.get("CommandLine"))
    expected_arguments = (
        str(backend / "models" / "autopresent" / "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"),
        str(backend / "models" / "autopresent" / "autopresent-detailed-f16.gguf"),
        "startupforge-autopresent",
    )
    if not all(_path(argument) in command for argument in expected_arguments):
        return None
    return service


class LocalShutdownController:
    def __init__(self, backend: Path):
        self.backend = backend

    def prepare(self) -> dict:
        if os.name != "nt":
            raise IntelligenceError("SHUTDOWN_UNAVAILABLE", "This shutdown control requires the Windows local installation.", 503)
        try:
            processes = process_snapshot()
            api = processes.get(os.getpid())
            # Windows venv python.exe is a launcher; the running interpreter can
            # be the exact base executable while sys.executable keeps the venv path.
            python_paths = {_path(sys.executable), _path(getattr(sys, "_base_executable", sys.executable))}
            if not api or not api.get("Created") or _path(api.get("ExecutablePath")) not in python_paths:
                raise ValueError("Unable to verify this API process")
            return {"api": api, "ollama": owned_model_process(self.backend, processes),
                    "autopresent": owned_autopresent_process(self.backend, processes)}
        except (OSError, ValueError, subprocess.SubprocessError):
            raise IntelligenceError("SHUTDOWN_UNAVAILABLE", "The local services could not be verified. Try Shutdown again.", 503) from None

    def start(self, plan: dict) -> None:
        encoded = base64.b64encode(json.dumps(plan).encode("utf-8")).decode("ascii")
        runtime = self.backend / ".runtime"
        runtime.mkdir(exist_ok=True)
        try:
            with (runtime / "shutdown.log").open("ab") as log:
                subprocess.Popen([sys.executable, str(self.backend / "scripts" / "stop_local_services.py"), encoded],
                                 cwd=self.backend, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
                                 close_fds=True)
        except OSError:
            raise IntelligenceError("SHUTDOWN_UNAVAILABLE", "Shutdown could not start. Please try again.", 503) from None


def stop_verified_processes(plan: dict) -> None:
    processes = process_snapshot()
    api = plan["api"]
    if not same_process(api, processes.get(api["ProcessId"])):
        return  # Never act on a plan from an earlier API instance.
    taskkill = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe")
    for service_name in ("ollama", "autopresent"):
        model = plan.get(service_name)
        if model and same_process(model, processes.get(model["ProcessId"])):
            try:
                subprocess.run([taskkill, "/PID", str(model["ProcessId"]), "/T", "/F"],
                               timeout=15, creationflags=subprocess.CREATE_NO_WINDOW, check=False)
            except (OSError, subprocess.SubprocessError):
                logging.exception("The local %s service could not be stopped; continuing app shutdown.", service_name)
    current = process_snapshot().get(api["ProcessId"])
    if same_process(api, current):
        # No /T: this helper is a child of the API and must finish normally.
        subprocess.run([taskkill, "/PID", str(api["ProcessId"]), "/F"],
                       timeout=15, creationflags=subprocess.CREATE_NO_WINDOW, check=True)


def run_shutdown(encoded_plan: str) -> None:
    plan = json.loads(base64.b64decode(encoded_plan).decode("utf-8"))
    time.sleep(1.5)  # Let the browser receive the shutdown acknowledgement.
    stop_verified_processes(plan)
