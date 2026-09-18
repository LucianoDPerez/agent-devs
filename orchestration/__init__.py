"""Paquete de orquestación (roles, sesión, presupuestos, dedupe).

Blindaje de imports: al importar CUALQUIER submódulo (`import
orchestration.session` desde cualquier CWD), el repo del harness va PRIMERO
en sys.path para que un CWD con módulos genéricos (config/, tools/) no haga
shadowing de los nuestros. Ver main.py (mismo guard para el entrypoint).
"""

import sys as _sys
from pathlib import Path as _Path

_root = str(_Path(__file__).resolve().parent.parent)
if _root not in _sys.path:
    _sys.path.insert(0, _root)

del _sys, _Path, _root
