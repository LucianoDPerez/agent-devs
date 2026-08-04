"""Tests para verify tools (lint, tests, build)."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from tools.verify import (
    _detect_stack,
    _resolve_command,
    run_build,
    run_lint,
    run_tests,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class TestDetectStack:
    def test_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "package.json", "{}")
            assert _detect_stack(root) == "node"

    def test_python_pyproject(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "pyproject.toml", "[project]\nname = 'x'\n")
            assert _detect_stack(root) == "python"

    def test_go(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "go.mod", "module example.com/foo\n\ngo 1.22\n")
            assert _detect_stack(root) == "go"

    def test_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            assert _detect_stack(Path(tmp)) is None


class TestResolveCommand:
    def test_node_lint_npm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(
                root / "package.json",
                json.dumps({"scripts": {"lint": "eslint ."}}),
            )
            cmd = _resolve_command(root, "lint")
            assert cmd == ["npm", "run", "lint"]

    def test_node_lint_pnpm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(
                root / "package.json",
                json.dumps({"scripts": {"lint": "eslint ."}}),
            )
            _write(root / "pnpm-lock.yaml", "")
            cmd = _resolve_command(root, "lint")
            assert cmd == ["pnpm", "run", "lint"]

    def test_node_missing_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "package.json", "{}")
            cmd = _resolve_command(root, "lint")
            assert isinstance(cmd, str)
            assert "No 'lint' script" in cmd

    def test_python_lint_ruff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "pyproject.toml", "[tool.ruff]\nline-length = 88\n")
            cmd = _resolve_command(root, "lint")
            assert cmd == ["ruff", "check", "."]

    def test_python_lint_no_ruff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "requirements.txt", "requests\n")
            cmd = _resolve_command(root, "lint")
            assert isinstance(cmd, str)
            assert "ruff" in cmd.lower()

    def test_python_test(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "pyproject.toml", "[project]\nname = 'x'\n")
            cmd = _resolve_command(root, "test")
            assert cmd == ["pytest"]

    def test_python_build_with_build_system(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(
                root / "pyproject.toml",
                "[build-system]\nrequires = ['setuptools']\n",
            )
            cmd = _resolve_command(root, "build")
            assert cmd == ["python", "-m", "build"]

    def test_go_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "go.mod", "module example.com/foo\n\ngo 1.22\n")
            assert _resolve_command(root, "lint") == ["go", "vet", "./..."]
            assert _resolve_command(root, "test") == ["go", "test", "./..."]
            assert _resolve_command(root, "build") == ["go", "build", "./..."]


class TestRunVerifyTools:
    def test_invalid_path(self):
        result = run_lint.invoke({"path": "/nonexistent/project"})
        assert "does not exist" in result

    def test_nonzero_exit_returns_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(
                root / "package.json",
                json.dumps({"scripts": {"lint": "exit 1"}}),
            )

            with patch("tools.verify._run_command") as mock_run:
                mock_run.return_value = "[FAILED] exit=1\n$ npm run lint\n────\nlint failed"
                result = run_lint.invoke({"path": tmp})

            assert "FAILED" in result
            mock_run.assert_called_once()

    def test_unknown_stack_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_tests.invoke({"path": tmp})
            assert "Could not detect project type" in result

    @patch("tools.verify._run_command")
    def test_run_tests_invokes_pytest(self, mock_run):
        mock_run.return_value = "[PASSED] exit=0\n$ pytest\n────\nok"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "pyproject.toml", "[project]\nname = 'x'\n")
            result = run_tests.invoke({"path": tmp})

        assert "PASSED" in result
        mock_run.assert_called_once_with(tmp, ["pytest"])

    @patch("tools.verify._run_command")
    def test_run_build_invokes_npm(self, mock_run):
        mock_run.return_value = "[PASSED] exit=0\n$ npm run build\n────\nok"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(
                root / "package.json",
                json.dumps({"scripts": {"build": "tsc"}}),
            )
            result = run_build.invoke({"path": tmp})

        assert "PASSED" in result
        mock_run.assert_called_once_with(tmp, ["npm", "run", "build"])
