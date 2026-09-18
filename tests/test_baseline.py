"""Tests del baseline flaco de tests (heredados vs nuevos, sin batería extra).

El cierre del turno N guarda el SET de fallos observados; el turno N+1
distingue heredados (ya fallaban) de nuevos cosechando salidas ya corridas.
"""

from tools.verify import extract_failing_tests

_VITEST_SAMPLE = """
 RUN  v3.2.4 /repo
 ✓ tests/unit/a.test.ts (3 tests) 12ms
 ❯ tests/integration/users-table.test.ts (8 tests | 5 failed) 90ms
 FAIL  tests/integration/users-table.test.ts > users table (DDL real) > allows multiple
 FAIL  tests/integration/users-table.test.ts > users table (DDL real) > enforces unique
 FAIL  tests/integration/segment-repository-port.test.ts > repo > conecta
⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯[3/28]⎯
"""


def test_extract_vitest_dedup():
    assert extract_failing_tests(_VITEST_SAMPLE) == [
        "tests/integration/users-table.test.ts",
        "tests/integration/segment-repository-port.test.ts",
    ]


def test_extract_pytest():
    sample = "FAILED tests/test_x.py::test_a - assert\nERROR tests/test_y.py\nok\n"
    assert extract_failing_tests(sample) == [
        "tests/test_x.py::test_a",
        "tests/test_y.py",
    ]


def test_extract_go():
    sample = "ok  github.com/x/a 0.1s\n--- FAIL: TestFoo (0.00s)\nFAIL\tgithub.com/x/b 0.2s\n"
    out = extract_failing_tests(sample)
    assert "TestFoo" in out
    assert "github.com/x/b" in out
    assert len(out) == 2


def test_extract_vacio_y_tope():
    assert extract_failing_tests("") == []
    assert extract_failing_tests(None) == []
    assert extract_failing_tests("[PASSED] todo verde") == []
    big = "".join(f"FAIL  tests/t{i}.test.ts > x\n" for i in range(100))
    assert len(extract_failing_tests(big)) == 30


def test_split_reds():
    from orchestration.session import _split_reds

    cur = ["a.test.ts", "b.test.ts::test_x", "c.test.ts"]
    base = ["a.test.ts", "b.test.ts::test_y"]
    heredados, nuevos = _split_reds(cur, base)
    assert heredados == ["a.test.ts", "b.test.ts::test_x"]
    assert nuevos == ["c.test.ts"]
    assert _split_reds(cur, None) is None
    assert _split_reds(cur, []) == ([], cur)


def test_format_attribution():
    from orchestration.session import _format_reds_attribution

    out = _format_reds_attribution(
        ["a/b.test.ts"], ["c/d.test.ts", "e/f.test.ts", "g/h.test.ts"]
    )
    assert "Heredados: 1" in out
    assert "Nuevos: 3" in out
    assert "b.test.ts" in out
    assert "(+1 más)" in out
