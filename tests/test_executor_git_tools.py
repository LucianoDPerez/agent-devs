"""EXECUTE debe poder VER el estado de git antes de commitear.

E2E real: 'hacer commit de los archivos modificados' sin git_status/changed_files
→ el modelo intentó leer .git/HEAD con read_file y quemó el presupuesto.
"""
from core.roles import Role, tools_for_role


def test_execute_tiene_git_read_tools():
    names = {t.name for t in tools_for_role(Role.EXECUTE)}
    assert {"git_status", "changed_files", "current_branch", "git_log"} <= names


def test_execute_tiene_git_write_tools():
    names = {t.name for t in tools_for_role(Role.EXECUTE)}
    assert {"stage_files", "create_commit", "push", "git_restore"} <= names
