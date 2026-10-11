#!/usr/bin/env python3
"""Render documentation diagrams through Kroki: docs/diagrams/*.puml -> *.svg (+ png)."""
import sys
from pathlib import Path

import httpx

KROKI = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
D = Path(__file__).resolve().parent
for puml in sorted(D.glob("*.puml")):
    src = puml.read_text(encoding="utf-8")
    kind = "c4plantuml" if "!include <C4/" in src else "plantuml"
    for fmt in ("svg", "png"):
        r = httpx.post(f"{KROKI}/{kind}/{fmt}", content=src.encode(), timeout=120)
        if r.status_code != 200:
            print("FAIL", puml.name, r.text[:300]); sys.exit(1)
        puml.with_suffix("." + fmt).write_bytes(r.content)
    print("ok", puml.name)
