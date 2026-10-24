"""Delayed helper for the app's local Shutdown action."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.local_shutdown import run_shutdown

if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Expected one shutdown plan.")
    run_shutdown(sys.argv[1])
