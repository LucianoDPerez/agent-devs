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

    def test_java_gradle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "build.gradle", "plugins { id 'java' }\n")
            assert _detect_stack(root) == "java"

    def test_java_kotlin_dsl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "build.gradle.kts", "plugins { java }\n")
            assert _detect_stack(root) == "java"

    def test_java_maven(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "pom.xml", "<project/>")
            assert _detect_stack(root) == "java"

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
            _write(root / "go.mod", "module example.com/foo\n")
            assert _resolve_command(root, "lint") == ["go", "vet", "./..."]
            assert _resolve_command(root, "test") == ["go", "test", "./..."]
            assert _resolve_command(root, "build") == ["go", "build", "./..."]

    def test_java_gradle_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "build.gradle", "plugins { id 'java' }\n")
            _write(root / "gradlew", "#!/bin/sh\n")
            wrapper = str(root / "gradlew")
            assert _resolve_command(root, "lint") == [wrapper, "compileJava", "--console=plain"]
            assert _resolve_command(root, "test") == [wrapper, "test", "--console=plain"]
            assert _resolve_command(root, "build") == [wrapper, "build", "-x", "test", "--console=plain"]

    def test_java_gradle_no_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "build.gradle", "plugins { id 'java' }\n")
            assert _resolve_command(root, "lint") == ["gradle", "compileJava", "--console=plain"]

    def test_java_maven_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "pom.xml", "<project/>")
            assert _resolve_command(root, "lint") == ["mvn", "compile", "-q"]
            assert _resolve_command(root, "test") == ["mvn", "test", "-q"]
            assert _resolve_command(root, "build") == ["mvn", "package", "-DskipTests", "-q"]


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
            # Auto-install (deps faltantes) agrega install + re-run; con el
            # mock todo "falla" y devuelve string SIEMPRE (nunca raise).
            assert isinstance(result, str)

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


def test_run_install_reinstalls_when_workspaces_missing_symlinks(tmp_path):
    """Workspaces declarados sin symlinks en node_modules → install stale."""
    from tools.verify import _needs_node_install
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "root", "private": True,
        "workspaces": ["backend", "frontend"],
    }))
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / ".package-lock.json").write_text("{}")
    assert _needs_node_install(tmp_path) is True


def test_run_install_ok_when_workspace_symlinks_exist(tmp_path):
    """Workspace con symlink en node_modules → install OK."""
    from tools.verify import _needs_node_install
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "root", "private": True,
        "workspaces": ["backend"],
    }))
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "backend").mkdir()  # symlink materializado
    (tmp_path / "node_modules" / ".package-lock.json").write_text("{}")
    assert _needs_node_install(tmp_path) is False


def test_workspace_symlink_detected_by_package_name(tmp_path):
    """El symlink del workspace se crea con el NOMBRE del paquete, no la carpeta."""
    from tools.verify import _needs_node_install
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "root", "private": True, "workspaces": ["backend"],
    }))
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "package.json").write_text(json.dumps({"name": "medica-backend"}))
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / ".package-lock.json").write_text("{}")
    # Sin symlink del paquete → stale → hay que instalar
    assert _needs_node_install(tmp_path) is True
    (tmp_path / "node_modules" / "medica-backend").mkdir()
    # Symlink materializado (con el nombre del paquete) → install OK
    assert _needs_node_install(tmp_path) is False


def test_needs_install_when_declared_dep_missing(tmp_path):
    """Dep declarada en package.json pero ausente de node_modules → stale."""
    import json

    from tools.verify import _needs_node_install
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "root", "private": True,
        "devDependencies": {"jest": "^29.7.0"},
    }))
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / ".package-lock.json").write_text("{}")
    assert _needs_node_install(tmp_path) is True
    (tmp_path / "node_modules" / "jest").mkdir()
    assert _needs_node_install(tmp_path) is False


def test_needs_install_scoped_dep(tmp_path):
    """Dep scoped (@types/jest) resuelve contra node_modules/@types/jest."""
    import json

    from tools.verify import _needs_node_install
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "root", "private": True,
        "devDependencies": {"@types/jest": "^29.5.1"},
    }))
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / ".package-lock.json").write_text("{}")
    assert _needs_node_install(tmp_path) is True
    (tmp_path / "node_modules" / "@types").mkdir()
    (tmp_path / "node_modules" / "@types" / "jest").mkdir()
    assert _needs_node_install(tmp_path) is False


def test_run_tests_auto_installs_when_deps_missing(tmp_path):
    """Verify falla por deps faltantes → auto-install + re-run (E2E real: los
    LLM chicos ignoran el hint de run_install)."""
    import json
    from unittest.mock import patch

    from tools import verify as v
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "t", "private": True,
        "scripts": {"test": "jest"},
        "devDependencies": {"jest": "^29.7.0"},
    }))
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / ".package-lock.json").write_text("{}")
    # Primera corrida falla (jest no instalado); install "lo instala"; re-run pasa.
    calls = {"n": 0}

    def fake_run_command(path, args, timeout=180):
        calls["n"] += 1
        if calls["n"] == 1:
            return "[FAILED] exit=127\n$ npm run test\nsh: jest: command not found"
        return "[PASSED] exit=0\n$ npm run test\n2 passed"

    with patch.object(v, "_run_command", side_effect=fake_run_command), \
         patch.object(v, "_run_node_install", return_value="[PASSED] npm install ok"):
        result = v.run_tests.invoke({"path": str(tmp_path)})
    assert "npm install" in result
    assert "Re-run" in result
    assert calls["n"] == 2


def test_run_npm_script_runs_declared_script(tmp_path):
    import json
    from unittest.mock import patch

    from tools import verify as v
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "t", "private": True, "scripts": {"db:generate": "prisma generate"},
    }))
    with patch.object(v, "_run_command", return_value="[PASSED] exit=0\n$ npm run db:generate\nok") as m:
        result = v.run_npm_script.invoke({"path": str(tmp_path), "script": "db:generate"})
    assert "PASSED" in result
    m.assert_called_once()


def test_run_npm_script_rejects_undeclared_script(tmp_path):
    import json

    from tools import verify as v
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "t", "private": True, "scripts": {"dev": "vite"},
    }))
    result = v.run_npm_script.invoke({"path": str(tmp_path), "script": "rm -rf /"})
    assert "No 'rm -rf /' script" in result
    assert "dev" in result  # lista los disponibles
