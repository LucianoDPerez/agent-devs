"""Tests para inspect_env: solo archivos de ejemplo, NUNCA .env real."""

import tempfile
from pathlib import Path

from tools.env import inspect_env


def _create_repo(files: dict[str, str]) -> str:
    tmp = tempfile.mkdtemp()
    for name, content in files.items():
        p = Path(tmp) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return tmp


def test_example_keys_listadas():
    repo = _create_repo({
        ".env.example": (
            "DATABASE_URL=postgresql://localhost:5432/app\n"
            "PORT=3000\n"
            "JWT_SECRET=changeme\n"
            "# comentario ignorado\n"
        ),
    })
    result = inspect_env.invoke({"path": repo})
    assert "DATABASE_URL=postgresql://localhost:5432/app" in result
    assert "PORT=3000" in result
    assert "comentario" not in result


def test_env_real_nunca_se_lee():
    """El secreto del .env real NO puede aparecer en la salida."""
    secret = "SK-SUPER-SECRETO-1234567890"
    repo = _create_repo({
        ".env": f"API_KEY={secret}\n",
        ".env.example": "API_KEY=\n",
    })
    result = inspect_env.invoke({"path": repo})
    assert secret not in result
    assert "API_KEY" in result  # viene del example


def test_monorepo_encuentra_examples_anidados():
    repo = _create_repo({
        "backend/.env.example": "DB_HOST=localhost\n",
        "frontend/.env.example": "VITE_API_URL=http://localhost:8080\n",
    })
    result = inspect_env.invoke({"path": repo})
    assert "backend/.env.example" in result
    assert "frontend/.env.example" in result


def test_valores_largos_truncados():
    long_value = "x" * 120
    repo = _create_repo({
        ".env.template": f"SOME_VAR={long_value}\n",
    })
    result = inspect_env.invoke({"path": repo})
    assert long_value not in result
    assert "SOME_VAR=x" in result
