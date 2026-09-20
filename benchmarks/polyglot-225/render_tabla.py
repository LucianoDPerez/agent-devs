#!/usr/bin/env python3
"""Renderiza la tabla comparativa 4B vs 35B como PNG para difundir.

Uso:
    .venv/bin/python benchmarks/polyglot-225/render_tabla.py
    -> benchmarks/polyglot-225/tabla-19-25.png
"""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BANK = Path(__file__).resolve().parent
W, ROW_H, PAD = 1200, 44, 40
GREEN, RED, INK, MUTED, BG, HEADER_BG = (
    (22, 163, 74), (220, 38, 38), (17, 24, 39), (107, 114, 128),
    (255, 255, 255), (17, 24, 39),
)


def _font(size: int, bold: bool = False):
    for name in ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
                 "/System/Library/Fonts/Helvetica.ttc"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def main() -> None:
    tasks = json.load(open(BANK / "tasks25.json"))["tasks"]
    s4 = {r["id"]: r for r in map(json.loads, open(BANK / "results/summary.jsonl"))}
    s35 = {r["id"]: r for r in map(json.loads, open(BANK / "results_35b/summary.jsonl"))}
    rows = [(t["id"], t["lang"], t["exercise"],
             s4[t["id"]]["passed"], s35[t["id"]]["passed"]) for t in tasks]
    n4 = sum(1 for r in rows if r[3])
    n35 = sum(1 for r in rows if r[4])

    H = PAD * 2 + 110 + 52 + len(rows) * ROW_H + 60
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    f_title, f_sub, f_row, f_head = _font(44, True), _font(24), _font(23), _font(22, True)
    y = PAD
    d.text((PAD, y), "AgentDevs · Aider Polyglot 25 (10%)", fill=INK, font=f_title)
    y += 62
    d.text((PAD, y), f"Spark 4B: {n4}/25  ·  Qwen3.6-35B: {n35}/25  ·  mismo harness, mismos pins",
           fill=MUTED, font=f_sub)
    y += 48
    cols = (PAD, 180, 560, 860)
    d.rectangle([0, y - 8, W, y + ROW_H - 8], fill=HEADER_BG)
    for x, t in zip(cols, ["ID", "Lenguaje / ejercicio", "Spark 4B", "Qwen 35B"]):
        d.text((x, y), t, fill=(255, 255, 255), font=f_head)
    y += ROW_H
    for i, (tid, lang, ex, p4, p35) in enumerate(rows):
        if i % 2:
            d.rectangle([0, y - 6, W, y + ROW_H - 6], fill=(243, 244, 246))
        d.text((cols[0], y), tid, fill=INK, font=f_row)
        d.text((cols[1], y), f"{lang} · {ex}", fill=INK, font=f_row)
        for j, ok in enumerate((p4, p35)):
            d.text((cols[2 + j], y), "PASS" if ok else "FAIL",
                   fill=GREEN if ok else RED, font=f_row)
        y += ROW_H
    d.text((PAD, y + 8), "github.com/LucianoDPerez/agent-devs · benchmarks/polyglot-225/",
           fill=MUTED, font=f_sub)
    out = BANK / "tabla-19-25.png"
    img.save(out)
    print(f"OK {out} ({W}x{H})")


if __name__ == "__main__":
    main()
