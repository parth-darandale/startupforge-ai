"""Exercise the compiled Windows entry point, without real services or a browser."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


WORKSPACE = Path(__file__).resolve().parents[2]
EXECUTABLE = WORKSPACE / "StartupForge.exe"


@unittest.skipUnless(EXECUTABLE.is_file(), "Build StartupForge.exe first")
class LauncherTests(unittest.TestCase):
    def run_check(self, workspace):
        report = workspace / "diagnostics" / "check.json"
        result = subprocess.run(
            [str(EXECUTABLE), "--check", "--workspace", str(workspace), "--report", str(report)],
            check=False, timeout=15, capture_output=True,
        )
        return result.returncode, json.loads(report.read_text(encoding="utf-8"))

    @staticmethod
    def create_layout(workspace, nested):
        member = workspace / "member1" if nested else workspace
        backend = member / "backend"
        for relative in (
            ".venv/Scripts/python.exe", "scripts/start_local_services.py", "scripts/service_worker.py",
            ".runtime/ollama-v0.40.1/ollama.exe",
        ):
            path = backend / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        frontend = member / "frontend/dist/index.html"
        frontend.parent.mkdir(parents=True, exist_ok=True)
        frontend.write_text("<!doctype html><title>fixture</title>", encoding="utf-8")
        return backend

    def test_missing_environment_is_reported_without_starting_services(self):
        with tempfile.TemporaryDirectory(prefix="StartupForge test ") as directory:
            code, report = self.run_check(Path(directory))
            self.assertEqual(code, 1)
            self.assertEqual(report["status"], "check_failed")
            self.assertIn("python_environment", report["missing"])
            self.assertIn("frontend_build", report["missing"])

    def test_legacy_layout_with_spaces(self):
        with tempfile.TemporaryDirectory(prefix="StartupForge legacy ") as directory:
            root = Path(directory)
            backend = self.create_layout(root, False)
            code, report = self.run_check(root)
            self.assertEqual(code, 0)
            self.assertEqual(Path(report["backend_directory"]), backend)
            self.assertTrue(all(report["checks"].values()))

    def test_member1_layout(self):
        with tempfile.TemporaryDirectory(prefix="StartupForge member ") as directory:
            root = Path(directory)
            backend = self.create_layout(root, True)
            code, report = self.run_check(root)
            self.assertEqual(code, 0)
            self.assertEqual(Path(report["backend_directory"]), backend)

    def test_incomplete_member_layout_does_not_silently_use_old_backend(self):
        with tempfile.TemporaryDirectory(prefix="StartupForge both ") as directory:
            root = Path(directory)
            self.create_layout(root, False)
            (root / "member1/backend").mkdir(parents=True)
            code, report = self.run_check(root)
            self.assertEqual(code, 1)
            self.assertEqual(Path(report["backend_directory"]), root / "member1/backend")

    def test_unknown_option_returns_failure_without_dialog(self):
        result = subprocess.run([str(EXECUTABLE), "--check", "--unexpected"], check=False, timeout=15)
        self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main()
