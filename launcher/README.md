# StartupForge Windows launcher

Double-click **`StartupForge.exe` in the repository root** after completing setup. It checks the workspace, starts or reuses the local Ollama and FastAPI services, waits for the API and compiled frontend, and opens `http://127.0.0.1:8000/` in your default browser. It displays progress and actionable errors without opening terminal windows.

Keep the executable beside the member folders in the repository root, corresponding to the original workspace's `implementation` folder. Run the commands below from that root. This is a workspace launcher, not a standalone distribution: it uses a separately installed Python environment, frontend build and portable Ollama runtime. It supports `member1/backend` + `member1/frontend`, and the older `backend` + `frontend` layout. It prefers `member1/backend` when that directory exists.

For a fresh checkout, follow the [repository setup instructions](../README.md), then download the pinned runtime:

```powershell
.\member1\backend\.venv\Scripts\python.exe member1\backend\scripts\download_model_assets.py runtime
```

The launcher requires this runtime even in demo mode. Model weights are optional for the demo and must be installed separately for live local AI. To run the demo without downloading Ollama, use the direct Uvicorn command in the repository README instead.

## Rebuild

Windows 10/11 with .NET Framework 4.x is required. The installed Windows C# compiler builds this small executable; no Visual Studio or additional Python packages are required.

```powershell
.\launcher\Build-Launcher.ps1 -RunChecks
```

The executable is compiled as a GUI application and is unsigned. The source is `launcher/StartupForgeLauncher.cs`.

## Diagnostics and automated checks

```powershell
# Validate files without starting services or opening a browser:
.\StartupForge.exe --check

# Start/reuse services and verify readiness, without a browser or GUI:
.\StartupForge.exe --headless

# Write the diagnostic result to an explicit path in this checkout:
.\StartupForge.exe --check --workspace "$PWD" --report "$PWD\launcher\.runtime\check.json"
```

For shell commands that need the exit code, use `Start-Process -Wait -PassThru` because this is a Windows GUI executable. Exit code `0` means success, `1` means failure, and `2` means cancellation in the interactive launcher. `--check` verifies required files and does not claim that their contents or live providers work. `--headless` performs the actual startup and health checks.

Default JSON results are `launcher/.runtime/check-result.json` or `launcher/.runtime/launch-result.json`. Launcher logs are `launcher/.runtime/launcher-YYYYMMDD.log`. Backend service logs are under the chosen backend's `.runtime/` directory. Logs contain startup diagnostics, not the contents of `.env`.

The launcher rejects an occupied app or Ollama port if the responding service has an unexpected health payload. It never stops the listener. The existing Python launcher creates persistent services through Windows WMI, so closing the browser or launcher does not stop them. There is no scheduled task or Windows autostart entry.

Use **Shutdown** at the top right of the app to stop the local API and its launcher-owned Ollama service, including model runners. Running jobs are interrupted; saved projects and files remain on disk. The app displays a shutdown screen. Double-click `StartupForge.exe` to start again. The local-only endpoint verifies browser origin and process identities; an unrelated or externally managed Ollama service is preserved.

The workspace Ollama process uses one resident model/request, Flash Attention and a Q8 context cache to reduce memory use with the verified workflow's larger context. These overrides are scoped to the launched process; Windows user/system environment settings stay unchanged. An already-running daemon is reused with its existing settings. Changing these daemon options in `member1/backend/scripts/start_local_services.py` requires restarting the workspace daemon.

To run the launcher's regression checks with the project's Python:

```powershell
.\member1\backend\.venv\Scripts\python.exe -m unittest discover -s launcher\tests -v
# In the older layout, use .\backend\.venv\Scripts\python.exe instead.
```
