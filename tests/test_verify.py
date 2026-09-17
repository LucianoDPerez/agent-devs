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
            assert cmd == ["pytest", "-q"]

    def test_python_build_with_build_system(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(
                root / "pyproject.toml",
                "[build-system]\nrequires = ['setuptools']\n",
            )
            cmd = _resolve_command(root, "build")
            assert cmd == ["python", "-m", "build"]

    def test_python_uv_detection(self):
        """Proyecto con uv.lock → comandos con `uv run`."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(
                root / "pyproject.toml",
                "[project]\nname='x'\n[tool.ruff]\n[build-system]\nrequires=['hatchling']\n",
            )
            _write(root / "uv.lock", "version = 1\n")
            assert _resolve_command(root, "test") == ["uv", "run", "pytest", "-q"]
            assert _resolve_command(root, "build") == ["uv", "build"]

    def test_lint_prefiere_ruff_del_path(self, monkeypatch):
        """ruff del PATH gana a `uv run` (E2E real: `uv run` mutaba uv.lock
        como efecto colateral de verificar)."""
        import shutil

        import tools.verify as _v

        monkeypatch.setattr(shutil, "which", lambda _: "/bin/ruff")
        assert _v._ruff_cmd(Path("/repo"), True) == ["ruff", "check", "."]

    def test_lint_uv_frozen_sin_ruff_en_path(self, monkeypatch):
        import shutil

        import tools.verify as _v

        monkeypatch.setattr(shutil, "which", lambda _: None)
        assert _v._ruff_cmd(Path("/repo"), True) == [
            "uv", "run", "--frozen", "ruff", "check", ".",
        ]
        assert _v._ruff_cmd(Path("/repo"), False) == ["ruff", "check", "."]

    def test_python_uv_install(self):
        """run_install en proyecto uv debe correr `uv sync`, no venv+pip."""
        from tools import verify as v
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "pyproject.toml", "[project]\nname='x'\n")
            _write(root / "uv.lock", "version = 1\n")
            with patch.object(v, "_run_command", return_value="[PASSED] uv sync ok") as m:
                res = v._run_python_install(root)
            assert "uv sync" in res
            m.assert_called_once_with(str(root), ["uv", "sync"])

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
            assert result.startswith("[SKIPPED]")
            assert "sin stack verificable" in result

    @patch("tools.verify._run_command")
    def test_run_tests_invokes_pytest(self, mock_run):
        mock_run.return_value = "[PASSED] exit=0\n$ pytest\n────\nok"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "pyproject.toml", "[project]\nname = 'x'\n")
            result = run_tests.invoke({"path": tmp})

        assert "PASSED" in result
        called_path, called_cmd = mock_run.call_args[0]
        assert Path(called_path).resolve() == Path(tmp).resolve()
        assert called_cmd == ["pytest", "-q"]

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
        called_path, called_cmd = mock_run.call_args[0]
        assert Path(called_path).resolve() == Path(tmp).resolve()
        assert called_cmd == ["npm", "run", "build"]


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


def test_run_npm_script_rejects_non_node_stack(tmp_path):
    """Corrección 4: en un repo Python/Go, run_npm_script debe RECHAZARSE de
    forma firme (excepción GraphBubbleUp que corta el turno y redirige) y no
    devolver un string ignorable. El modelo local ignoraba el string y repetía
    la llamada con 'install'/'uv sync' en demo-spec-kitti (Python),
    quemando el presupuesto sin instalar nada."""
    from orchestration.tool_dedupe import ToolBudgetExceeded
    from tools import verify as v
    (tmp_path / "pyproject.toml").write_text("[project]\nname='t'\n")
    import pytest
    with pytest.raises(ToolBudgetExceeded) as exc:
        v.run_npm_script.invoke({"path": str(tmp_path), "script": "uv sync --all-extras"})
    assert "solo funciona en repos NODE" in str(exc.value)
    assert "PYTHON" in str(exc.value)
    assert "run_install" in str(exc.value)


def test_truncate_keeps_head_and_tail():
    """Outputs largos: head 2k + tail 6k, no head 40k (el error vive al final)."""
    from tools.verify import _truncate
    big = "A" * 5000 + "B" * 5000 + "C" * 5000
    out = _truncate(big)
    assert len(out) < len(big)
    assert out.startswith("A" * 100)
    assert out.rstrip().endswith("C" * 100)
    assert "middle truncated" in out


def test_shorten_passed_drops_long_green_log():
    """PASSED largo (uv build 16k) → resumen corto que sigue empezando en [PASSED]."""
    from tools.verify import _shorten_passed
    long_pass = "[PASSED] exit=0\n$ uv build\n" + "x" * 20000
    short = _shorten_passed(long_pass)
    assert short.startswith("[PASSED]")
    assert len(short) < 1000
    assert "output completo omitido" in short


def test_shorten_passed_keeps_short_and_fail():
    from tools.verify import _shorten_passed
    short = "[PASSED] exit=0\n$ ruff check .\n(no output)"
    assert _shorten_passed(short) == short
    fail = "[FAILED] exit=1\n$ pytest\nF test_x"
    assert _shorten_passed(fail) == fail


def test_run_verify_all_green_single_message(tmp_path):
    """run_verify: 3 checks en UNA tool, un solo mensaje en verde."""
    from unittest.mock import patch

    from tools import verify as v
    (tmp_path / "pyproject.toml").write_text("[project]\nname='t'\n")
    with patch.object(
        v, "_run_verify",
        side_effect=["[PASSED] lint ok", "[PASSED] tests ok", "[PASSED] build ok"],
    ):
        out = v.run_verify.invoke({"path": str(tmp_path)})
    assert out.startswith("[PASSED]")
    assert "lint" in out and "tests" in out and "build" in out
    assert "no repitas la batería" in out


def test_run_verify_one_red_shows_only_failed_tail(tmp_path):
    from unittest.mock import patch

    from tools import verify as v
    (tmp_path / "pyproject.toml").write_text("[project]\nname='t'\n")
    with patch.object(
        v, "_run_verify",
        side_effect=["[PASSED] lint ok", "[FAILED] exit=1\nF test_x", "[PASSED] build ok"],
    ):
        out = v.run_verify.invoke({"path": str(tmp_path)})
    assert out.startswith("[FAILED]")
    assert "❌ test" in out
    assert "F test_x" in out


def test_run_verify_registered_as_verify_tool():
    """run_verify cuenta como verify (budget, cache, pools por rol)."""
    from orchestration.tool_dedupe import VERIFY_TOOL_NAMES
    from tools import EXECUTOR_TOOLS, REVIEWER_TOOLS

    assert "run_verify" in VERIFY_TOOL_NAMES
    assert "run_verify" in [t.name for t in EXECUTOR_TOOLS]
    assert "run_verify" in [t.name for t in REVIEWER_TOOLS]


def test_run_verify_skipped_sin_stack(tmp_path):
    """Docs/infra sin stack: batería [SKIPPED], no [FAILED] (T001: ADR .md
    cerraba en rojo siendo todo correcto)."""
    from tools import verify as v

    out = v.run_verify.invoke({"path": str(tmp_path)})
    assert out.startswith("[SKIPPED]")
    assert "⏭️ lint" in out and "⏭️ test" in out and "⏭️ build" in out


def test_run_verify_mixto_no_esconde_rojos(tmp_path):
    """Un rojo entre skips sigue siendo [FAILED] con detalle."""
    from unittest.mock import patch

    from tools import verify as v
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    with patch.object(v, "_run_command", return_value="[FAILED] exit=1\nF"):
        out = v._run_verify(str(tmp_path), "test")
    assert out.startswith("[FAILED]")


def test_find_stack_root_sub_monorepo(tmp_path):
    """Monorepo: apps/api sin package.json propio → stack de la raíz (T003:
    lint/tests/build en subpath daban [SKIPPED] teniendo stack)."""
    import json

    from tools import verify as v
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"lint": "x"}}))
    sub = tmp_path / "apps" / "api"
    sub.mkdir(parents=True)
    found = v._find_stack_root(str(sub))
    assert found is not None
    assert (found / "package.json").is_file()


def test_find_stack_root_sin_stack(tmp_path):
    from tools import verify as v

    assert v._find_stack_root(str(tmp_path)) is None
    assert v._find_stack_root("/nonexistent/xyz-123") is None


def test_run_lint_subpath_usa_raiz_y_avisa(tmp_path):
    import json
    from unittest.mock import patch

    from tools import verify as v
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"lint": "eslint ."}}))
    sub = tmp_path / "apps" / "api"
    sub.mkdir(parents=True)
    with patch.object(v, "_run_command", return_value="[PASSED] exit=0 ok") as m:
        out = v.run_lint.invoke({"path": str(sub)})
    assert out.startswith("[PASSED]")
    assert "se ejecuta en" in out
    # el comando corrió en la raíz (donde está el package.json)
    assert Path(m.call_args[0][0]).resolve() == tmp_path.resolve()
