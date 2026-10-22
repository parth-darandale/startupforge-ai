"""Compatibility import; Member 3 owns the shared local job runner."""
import sys
from pathlib import Path

_implementation_root = Path(__file__).resolve().parents[4]
_member3_backend = _implementation_root / "member3" / "backend"
if str(_member3_backend) not in sys.path:
    sys.path.insert(0, str(_member3_backend))

from startupforge_member3.core.jobs import LocalJobRunner

__all__ = ["LocalJobRunner"]
