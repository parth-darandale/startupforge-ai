"""Own one explicitly launched local service outside the caller's process tree.

The launcher creates this hidden process through Win32_Process.Create. A unique
temporary launch manifest carries process environment overrides without exposing them in
the command line or persisting it in diagnostics. This worker performs no restart
or autostart: it exists only for the service explicitly requested by the launcher.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import traceback

BACKEND = Path(__file__).resolve().parents[1]
RUNTIME = BACKEND / ".runtime"


def _record(path, state):
    temporary = path.with_name(f"{path.stem}-{state['launch_id']}.tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    # Windows readers (including the startup poll) can briefly hold the
    # destination without delete sharing. Keep the old complete record visible
    # and retry the atomic replacement; do not lose ownership of a started
    # service because its status file was momentarily open.
    for attempt in range(40):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 39:
                raise
            time.sleep(0.05)


def run(manifest_path):
    manifest = Path(manifest_path).resolve()
    if manifest.parent != RUNTIME.resolve() or not re.fullmatch(r"service-launch-[A-Za-z0-9_-]+-[0-9a-f]{32}\.json", manifest.name):
        raise ValueError("The worker only accepts a task-owned launch manifest in backend/.runtime.")
    try:
        configuration = json.loads(manifest.read_text(encoding="utf-8"))
    finally:
        manifest.unlink(missing_ok=True)
    name = configuration.get("name", "")
    launch_id = configuration.get("launch_id", "")
    command = configuration.get("command")
    overrides = configuration.get("environment_overrides")
    cwd = Path(configuration.get("cwd", "")).resolve()
    if (not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name)
            or not re.fullmatch(r"[0-9a-f]{32}", launch_id)
            or manifest.name != f"service-launch-{name}-{launch_id}.json"
            or cwd != BACKEND
            or not isinstance(command, list) or not command
            or any(not isinstance(argument, str) for argument in command)
            or not isinstance(overrides, dict)
            or any(not isinstance(key, str) or not isinstance(value, str) for key, value in overrides.items())):
        raise ValueError("The service launch manifest is invalid.")
    state_path = RUNTIME / f"{name}-service.json"
    state = {
        "launch_id": launch_id, "name": name, "worker_pid": os.getpid(),
        "service_pid": None, "status": "starting",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    environment = {**os.environ, **overrides}
    with (RUNTIME / f"{name}-stdout.log").open("ab") as out, (RUNTIME / f"{name}-stderr.log").open("ab") as err:
        try:
            _record(state_path, state)
            process = subprocess.Popen(
                command, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                stdout=out, stderr=err, creationflags=flags,
            )
            state.update(service_pid=process.pid, status="running")
            _record(state_path, state)
            exit_code = process.wait()
            state.update(status="exited", exit_code=exit_code,
                         exited_at=datetime.now(timezone.utc).isoformat())
            _record(state_path, state)
            return exit_code
        except Exception:
            err.write(traceback.format_exc().encode("utf-8", errors="replace"))
            err.flush()
            state.update(status="failed", exited_at=datetime.now(timezone.utc).isoformat())
            _record(state_path, state)
            return 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Pass one task-owned service launch manifest.")
    raise SystemExit(run(sys.argv[1]))
