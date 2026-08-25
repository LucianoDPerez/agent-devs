"""Utilidades de normalización de texto para comparar entrada del usuario.

El usuario escribe en español con acentos arbitrarios: "sí"/"si", "SÍ", "dale",
"revisá"/"revisa". Comparar contra listas literales duplica cada variante
("si" Y "sí") y es frágil: basta que falte una forma para romper el match.

normalize() baja a minúsculas y quita los acentos (descomposición NFD +
eliminación de combining marks), dejando UNA forma canónica contra la que
comparar. Sin dependencias externas: unicodedata es stdlib.
"""

from __future__ import annotations

import unicodedata


def normalize(text: str) -> str:
    """Minúsculas + sin acentos + espacios colapsados.

    "SÍ" -> "si" · "Revisá" -> "revisa" · "  dale " -> "dale"
    """
    nfkd = unicodedata.normalize("NFKD", text or "")
    ascii_ = "".join(c for c in nfkd if not unicodedata.combining(c))
    return " ".join(ascii_.lower().split())
