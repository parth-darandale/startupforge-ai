"""Start local prototype services independently of a terminal/tool process tree."""
import base64
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import uuid

import httpx

BACKEND = Path(__file__).resolve().parents[1]


def spawn_background(command, name, environment=None):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise ValueError("Service name must contain only letters, numbers, underscores or hyphens.")
    if not command or any(not isinstance(argument, str) for argument in command):
        raise ValueError("Service command must be a nonempty list of strings.")
    logs = BACKEND / ".runtime"
    logs.mkdir(exist_ok=True)
    if os.name == "nt":
        return _spawn_windows_service(command, name, environment, logs)
    with (logs / f"{name}-stdout.log").open("ab") as out, (logs / f"{name}-stderr.log").open("ab") as err:
        process = subprocess.Popen(command, cwd=BACKEND, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=out, stderr=err, start_new_session=True)
    print(f"Started {name} (PID {process.pid}).", flush=True)
    return process.pid


def _spawn_windows_service(command, name, environment, logs):
    """Ask the Windows WMI provider to own the persistent worker process.

    CREATE_BREAKAWAY_FROM_JOB is insufficient when the app tears down an entire
    command process tree. WMI creates the worker outside that tree; the worker
    then owns the hidden service and its logs. No scheduled task, startup entry
    or machine/user environment setting is created.
    """
    launch_id = uuid.uuid4().hex
    manifest = logs / f"service-launch-{name}-{launch_id}.json"
    state_path = logs / f"{name}-service.json"
    worker = BACKEND / "scripts" / "service_worker.py"
    # Carry task overrides only. The worker inherits its Windows user process
    # environment and the API loads backend/.env normally. Ollama constraints
    # are always explicit even if they already happen to match this caller.
    overrides = {
        key: value for key, value in (environment or {}).items()
        if key.startswith("OLLAMA_") or os.environ.get(key) != value
    }
    manifest.write_text(json.dumps({
        "launch_id": launch_id, "name": name, "command": command,
        "cwd": str(BACKEND), "environment_overrides": overrides,
    }), encoding="utf-8")
    # Python's Windows argument quoting keeps spaces/backslashes literal. Only
    # base64 values enter PowerShell source, so command text is never evaluated.
    worker_command = subprocess.list2cmdline([sys.executable, str(worker), str(manifest)])
    encoded_command = base64.b64encode(worker_command.encode("utf-8")).decode("ascii")
    encoded_cwd = base64.b64encode(str(BACKEND).encode("utf-8")).decode("ascii")
    script = f"""$ErrorActionPreference = 'Stop'
$startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{{ ShowWindow = [uint16]0 }}
$command = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_command}'))
$directory = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_cwd}'))
$result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{{ CommandLine = $command; CurrentDirectory = $directory; ProcessStartupInformation = $startup }}
$result | Select-Object ProcessId, ReturnValue | ConvertTo-Json -Compress
"""
    encoded_script = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    try:
        result = subprocess.run(
            [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded_script],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW, timeout=20, check=False,
        )
        if result.returncode:
            raise RuntimeError("Windows WMI could not launch the local service worker.")
        try:
            created = json.loads(result.stdout.strip().lstrip("\ufeff"))
        except (ValueError, TypeError):
            raise RuntimeError("Windows WMI returned an invalid service launch response.") from None
        if created.get("ReturnValue") != 0 or not created.get("ProcessId"):
            raise RuntimeError(f"Windows WMI rejected the service launch (code {created.get('ReturnValue')}).")
        wmi_pid = int(created["ProcessId"])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = {}
            if state.get("launch_id") == launch_id:
                if state.get("status") == "running" and state.get("service_pid"):
                    service_pid = int(state["service_pid"])
                    print(f"Started {name} (PID {service_pid}; worker PID {state['worker_pid']}; WMI PID {wmi_pid}).", flush=True)
                    return service_pid
                if state.get("status") in {"failed", "exited"}:
                    raise RuntimeError(f"The {name} service worker failed; inspect backend/.runtime/{name}-stderr.log.")
            time.sleep(0.1)
        raise RuntimeError(f"The {name} service worker did not confirm startup; inspect backend/.runtime logs.")
    finally:
        # The worker normally removes its manifest immediately after reading.
        # A failed WMI call must not leave process overrides behind either.
        manifest.unlink(missing_ok=True)


def ready(address):
    try:
        response = httpx.get(address, timeout=2, trust_env=False)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError):
        return None


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def wait_until(address, *, timeout=30, service_name="Service", log_name=None):
    deadline = time.monotonic() + timeout
    while not ready(address):
        if time.monotonic() > deadline:
            logs = f"backend/.runtime/{log_name}-stderr.log" if log_name else "backend/.runtime logs"
            raise RuntimeError(
                f"{service_name} did not become ready within {timeout:g} seconds at {address}; "
                f"inspect {logs}, then launch StartupForge again."
            )
        time.sleep(0.5)


def start():
    python = str(BACKEND / ".venv" / "Scripts" / "python.exe")
    runtime = BACKEND / ".runtime" / "ollama-v0.40.1" / "ollama.exe"
    if not runtime.is_file() or not Path(python).is_file():
        raise RuntimeError("Install the local environment and portable Ollama first; see README.md.")
    ollama = ready("http://127.0.0.1:11434/api/version")
    if port_open(11434) and (not isinstance(ollama, dict) or not isinstance(ollama.get("version"), str)):
        raise RuntimeError("Port 11434 is occupied by another or unresponsive service; preserve it and inspect the listener.")
    if not ollama:
        environment = {**os.environ, "OLLAMA_HOST": "127.0.0.1:11434",
            "OLLAMA_MODELS": str(BACKEND / "models" / "ollama"), "OLLAMA_NUM_PARALLEL": "1",
            "OLLAMA_MAX_LOADED_MODELS": "1", "OLLAMA_CONTEXT_LENGTH": "4096", "OLLAMA_NO_CLOUD": "1",
            "OLLAMA_FLASH_ATTENTION": "1", "OLLAMA_KV_CACHE_TYPE": "q8_0"}
        spawn_background([str(runtime), "serve"], "ollama", environment)
        # A cold GPU discovery pass can take more than 30 seconds on laptops.
        wait_until("http://127.0.0.1:11434/api/version", timeout=90, service_name="Ollama", log_name="ollama")
    health = ready("http://127.0.0.1:8000/api/v1/health")
    required_modules = {"intelligence", "visual_studio", "reports", "workspace"}
    if port_open(8000) and (not isinstance(health, dict) or not required_modules.issubset(set(health.get("modules", [])))):
        raise RuntimeError("Port 8000 is serving another application or an incomplete StartupForge build; preserve it and choose another port.")
    if not health:
        spawn_background([python, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"], "api")
        wait_until("http://127.0.0.1:8000/api/v1/health", timeout=45, service_name="StartupForge API", log_name="api")
    print("Open http://127.0.0.1:8000/", flush=True)


if __name__ == "__main__":
    start()
