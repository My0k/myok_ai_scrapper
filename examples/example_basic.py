"""
Ejemplo: "anda a tal página y apreta tal botón" + diagnóstico completo.

    python examples/example_basic.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from navdiag import DiagnosticSession

with DiagnosticSession(out_dir="captures", label="demo-quotes", headless=True) as s:
    s.goto("https://quotes.toscrape.com/")
    s.click("text=Login")            # navega al form de login
    s.fill("#username", "demo")
    s.fill("#password", "demo")
    s.click("input[type=submit]")    # dispara el POST (queda en endpoints.json)
    s.snapshot("post-submit")

print("Listo. Revisa la carpeta captures/ -> AI_SUMMARY.md, endpoints.json, network.json")
