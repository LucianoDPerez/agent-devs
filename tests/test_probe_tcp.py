"""Tests de probe_tcp (diagnóstico TCP para servicios que no hablan HTTP).

E2E: tests con ECONNREFUSED a PostgreSQL y el analyzer sin forma de verificar
si el puerto escucha (probe_http habla HTTP; no hay shell para docker ps).
"""

import socket

from tools.runtime_probe import probe_tcp


def _open_port():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    return srv, srv.getsockname()[1]


def test_puerto_abierto():
    srv, port = _open_port()
    try:
        out = probe_tcp.invoke({"host": "127.0.0.1", "port": port})
    finally:
        srv.close()
    assert "ABIERTO" in out


def test_puerto_cerrado():
    srv, port = _open_port()
    srv.close()
    out = probe_tcp.invoke({"host": "127.0.0.1", "port": port})
    assert "CERRADO" in out


def test_host_no_permitido():
    out = probe_tcp.invoke({"host": "example.com", "port": 5432})
    assert "SOLO acepta" in out


def test_puerto_invalido():
    assert "inválido" in probe_tcp.invoke({"host": "127.0.0.1", "port": 99999})


def test_probe_tcp_en_pools_diagnostico():
    from tools import get_tools

    for role in ("analyzer", "executor", "reviewer"):
        assert "probe_tcp" in {t.name for t in get_tools(role)}
