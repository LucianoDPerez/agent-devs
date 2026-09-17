"""Gate de verificación en commit + comando /verify (híbido de contexto).

Decisión: verificar por checkpoint (no por archivo) + batería obligatoria
al commitear + /verify manual. Nada roto llega a git.
"""

import tools.verify as _verify_mod
from display.commands import interpret_slash
from orchestration.session import run_commit_verification


class _Fake:
    def __init__(self, text):
        self.text = text

    def invoke(self, args):
        return self.text


def test_bateria_todo_verde(monkeypatch):
    monkeypatch.setattr(_verify_mod, "run_lint", _Fake("[PASSED] exit=0\n$ tsc"))
    monkeypatch.setattr(_verify_mod, "run_tests", _Fake("[PASSED] exit=0\n$ pytest"))
    monkeypatch.setattr(_verify_mod, "run_build", _Fake("[PASSED] exit=0\n$ build"))
    passed, report = run_commit_verification("/tmp")
    assert passed is True
    assert "✅ lint" in report and "✅ tests" in report and "✅ build" in report


def test_bateria_un_rojo_falla(monkeypatch):
    monkeypatch.setattr(_verify_mod, "run_lint", _Fake("[PASSED] exit=0"))
    monkeypatch.setattr(_verify_mod, "run_tests", _Fake("[FAILED] exit=1\n$ pytest\nF test_x"))
    monkeypatch.setattr(_verify_mod, "run_build", _Fake("[PASSED] exit=0"))
    passed, report = run_commit_verification("/tmp")
    assert passed is False
    assert "❌ tests" in report


def test_capture_guarda_cola_de_rojos(monkeypatch):
    """El detalle del fallo no se descarta: capture['details'] lo nombra."""
    monkeypatch.setattr(_verify_mod, "run_lint", _Fake("[PASSED] exit=0"))
    monkeypatch.setattr(
        _verify_mod, "run_tests",
        _Fake("[FAILED] exit=1\n$ pytest\nF sample_fail_test.py::test_rojo"),
    )
    monkeypatch.setattr(_verify_mod, "run_build", _Fake("[PASSED] exit=0"))
    captured: dict = {}
    passed, _ = run_commit_verification("/tmp", capture=captured)
    assert passed is False
    assert set(captured.get("details", {})) == {"tests"}
    assert "sample_fail_test" in captured["details"]["tests"]


def test_capture_vacio_en_verde(monkeypatch):
    monkeypatch.setattr(_verify_mod, "run_lint", _Fake("[PASSED] exit=0"))
    monkeypatch.setattr(_verify_mod, "run_tests", _Fake("[PASSED] exit=0"))
    monkeypatch.setattr(_verify_mod, "run_build", _Fake("[PASSED] exit=0"))
    captured: dict = {}
    passed, _ = run_commit_verification("/tmp", capture=captured)
    assert passed is True
    assert captured.get("details", {}) == {}


def test_capture_e2e_nombra_archivo_que_falla(tmp_path):
    """E2E real (repo python mínimo con pytest que falla): la cola guardada
    nombra el archivo — es lo que el turno siguiente ataca directo."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = \"x\"\n", encoding="utf-8")
    (tmp_path / "sample_fail_test.py").write_text(
        "def test_rojo():\n    assert False\n", encoding="utf-8",
    )
    captured: dict = {}
    passed, report = run_commit_verification(str(tmp_path), capture=captured)
    assert passed is False
    assert "❌ tests" in report
    assert "sample_fail_test" in captured.get("details", {}).get("tests", "")


def test_bateria_tool_que_explota_cuenta_como_rojo(monkeypatch):
    class Boom:
        def invoke(self, args):
            raise RuntimeError("boom")

    monkeypatch.setattr(_verify_mod, "run_lint", Boom())
    monkeypatch.setattr(_verify_mod, "run_tests", _Fake("[PASSED] x"))
    monkeypatch.setattr(_verify_mod, "run_build", _Fake("[PASSED] x"))
    passed, _ = run_commit_verification("/tmp")
    assert passed is False


def test_bateria_sin_linter_cuenta_como_rojo(monkeypatch):
    # Mensaje "no hay linter" NO es [PASSED] → no valida como verde.
    monkeypatch.setattr(_verify_mod, "run_lint", _Fake("No hay linter configurado"))
    monkeypatch.setattr(_verify_mod, "run_tests", _Fake("[PASSED] x"))
    monkeypatch.setattr(_verify_mod, "run_build", _Fake("[PASSED] x"))
    passed, _ = run_commit_verification("/tmp")
    assert passed is False


def test_interpret_verify():
    assert interpret_slash("/verify") == ("run", ("/verify", ""))
    # Único con ese prefijo → corre directo
    assert interpret_slash("/ver") == ("run", ("/verify", ""))


def test_prompt_execute_checkpoint_no_por_archivo():
    from core.roles import Role, load_prompt

    prompt = load_prompt(Role.EXECUTE)
    assert "por checkpoint, no por archivo" in prompt
    assert "Batería COMPLETA" in prompt
    assert "después de CADA subtarea" not in prompt


def test_commit_gate_reusa_trio_verde_sin_ejecutar(monkeypatch):
    """Si el turno ya dejó lint+tests+build en verde, el gate NO re-ejecuta."""
    import tools.verify as _v

    called = {"n": 0}

    class _FakeCount:
        def __init__(self, text):
            self.text = text

        def invoke(self, args):
            called["n"] += 1
            return self.text

    monkeypatch.setattr(_v, "run_lint", _FakeCount("[PASSED] x"))
    monkeypatch.setattr(_v, "run_tests", _FakeCount("[PASSED] x"))
    monkeypatch.setattr(_v, "run_build", _FakeCount("[PASSED] x"))
    passed, report = run_commit_verification(
        "/tmp", reuse={"run_lint": True, "run_tests": True, "run_build": True}
    )
    assert passed is True
    assert "reusa" in report
    assert called["n"] == 0


def test_commit_gate_reusa_run_verify_verde(monkeypatch):
    import tools.verify as _v

    called = {"n": 0}

    class _FakeCount:
        def __init__(self, text):
            self.text = text

        def invoke(self, args):
            called["n"] += 1
            return self.text

    monkeypatch.setattr(_v, "run_lint", _FakeCount("[PASSED] x"))
    monkeypatch.setattr(_v, "run_tests", _FakeCount("[PASSED] x"))
    monkeypatch.setattr(_v, "run_build", _FakeCount("[PASSED] x"))
    passed, _ = run_commit_verification("/tmp", reuse={"run_verify": True})
    assert passed is True
    assert called["n"] == 0


def test_commit_gate_no_reusa_si_hay_rojo(monkeypatch):
    """Con un rojo en el turno, el gate SÍ re-ejecuta para confirmar."""
    import tools.verify as _v

    monkeypatch.setattr(_v, "run_lint", _Fake("[PASSED] x"))
    monkeypatch.setattr(_v, "run_tests", _Fake("[PASSED] x"))
    monkeypatch.setattr(_v, "run_build", _Fake("[PASSED] x"))
    passed, _ = run_commit_verification(
        "/tmp", reuse={"run_lint": True, "run_tests": False, "run_build": True}
    )
    assert passed is True  # re-ejecutó y dio verde
