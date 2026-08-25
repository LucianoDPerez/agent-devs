"""Tests de robustez del análisis ante symlinks rotos y binarios.

node_modules/.bin/ suele tener symlinks temporales de npm (.rimraf-XXX)
que quedan ROTOS si un install se interrumpió (Ctrl+C, disco lleno).
Path.stat() sobre un symlink roto lanza FileNotFoundError y mataba el
análisis del repo entero ([Errno 2] reportado en otra máquina).

Además: NUNCA se analizan binarios, solo archivos de proyecto — en
cualquier ecosistema (Go/Java/PHP/JS/TS/Rust…). La detección es por
contenido (magic bytes + densidad no-texto), no por extensión.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analyzer import _file_tree, _is_binary, build_context, detect_language


@pytest.fixture()
def repo_con_symlink_roto(tmp_path: Path) -> Path:
    """Repo JS con un symlink roto dentro de node_modules/.bin + uno en la raíz."""
    os.makedirs(tmp_path / "node_modules" / ".bin", exist_ok=True)
    (tmp_path / "package.json").write_text('{"name":"x","dependencies":{}}')
    (tmp_path / "README.md").write_text("# repo de prueba")
    (tmp_path / "main.py").write_text("x = 1\n")
    # Symlinks rotos (destino inexistente) — típicos de un npm install cortado.
    os.symlink("/no/existe/rimraf", tmp_path / "node_modules" / ".bin" / ".rimraf-abc123")
    os.symlink("/no/existe/otro", tmp_path / "rotolink")
    return tmp_path


def test_build_context_tolera_symlink_roto(repo_con_symlink_roto):
    ctx = build_context(str(repo_con_symlink_roto))
    assert "main.py" in ctx
    assert "rimraf" not in ctx


def test_detect_language_tolera_symlink_roto(repo_con_symlink_roto):
    assert detect_language(str(repo_con_symlink_roto)) == "javascript"


def test_file_tree_tolera_symlink_roto(repo_con_symlink_roto):
    tree = _file_tree(repo_con_symlink_roto)
    assert "main.py" in tree
    assert "rimraf" not in tree


# Binarios de distintos ecosistemas: se detectan por CONTENIDO (magic bytes
# o densidad no-texto), nunca se listan en el análisis.
_BINARIOS = {
    "App.class": b"\xca\xfe\xba\xbe\x00\x00\x00\x34",   # java
    "app.jar": b"PK\x03\x04binarycontent",               # java/zip
    "x.pyc": b"\x00\x00\x00\x00f\r\r\n",                # python bytecode
    "lib.so": b"\x7fELF\x02\x01\x01\x00",               # linux
    "img.png": b"\x89PNG\r\n\x1a\n\x00\x00",            # imagen
    "app.exe": b"MZ\x90\x00",                           # windows
    "db.sqlite": b"SQLite format 3\x00",                # db
    "fuente.ttf": b"\x00\x01\x00\x00",                  # fuente
    "sin_ext": b"\x00\x01\x02\xff raw",                 # binario sin extensión
}

# Código fuente real de varios lenguajes: JAMÁS binario.
_CODIGO = [
    "main.go", "Main.java", "index.php", "app.ts", "app.js",
    "main.py", "lib.rs", "app.cs", "Gemfile", "mod.rs",
]


def test_detecta_binarios_por_contenido(tmp_path):
    for name, content in _BINARIOS.items():
        p = tmp_path / name
        p.write_bytes(content)
        assert _is_binary(p), f"{name} debería ser binario"


def test_codigo_fuente_nunca_es_binario(tmp_path):
    for name in _CODIGO:
        p = tmp_path / name
        p.write_text(f"// {name}\nfn main() {{}}\n")
        assert not _is_binary(p), f"{name} NO debería ser binario"


def test_codigo_legitimo_no_binario(tmp_path):
    """Minificado/JSON/YAML con acentos: texto válido, nunca binario."""
    casos = {
        "app.min.js": b"var a=function(){return 'x'};" * 50,
        "style.css": b"body { color: #fff; }",
        "data.json": '{"nome":"José","ñandú":true}'.encode(),
        "config.yaml": b"puerto: 8080\n",
        "README.md": "# Título\n\ntexto ñoño".encode(),
    }
    for name, content in casos.items():
        p = tmp_path / name
        p.write_bytes(content)
        assert not _is_binary(p), f"{name} NO debería ser binario"


def test_build_context_no_lista_binarios(tmp_path):
    for name, content in _BINARIOS.items():
        (tmp_path / name).write_bytes(content)
    for name in _CODIGO:
        (tmp_path / name).write_text(f"// {name}\n")
    ctx = build_context(str(tmp_path))
    for bin_name in _BINARIOS:
        assert bin_name not in ctx, f"{bin_name} no debería aparecer"
    assert any(c in ctx for c in _CODIGO), "el código fuente sí debe aparecer"
