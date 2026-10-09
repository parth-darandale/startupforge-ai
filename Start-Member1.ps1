$ErrorActionPreference = 'Stop'
$backendDirectory = Join-Path $PSScriptRoot 'member1\backend'
$pythonExecutable = Join-Path $backendDirectory '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $pythonExecutable)) { throw 'Install the Python environment first; see README.md.' }
& $pythonExecutable (Join-Path $backendDirectory 'scripts\start_local_services.py')
if ($LASTEXITCODE -ne 0) { throw 'The services did not start; inspect backend/.runtime logs.' }
