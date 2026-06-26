"""
CLI de navdiag.

Dos modos:

1) Rápido (una URL):
    python -m navdiag https://ejemplo.cl --label home

2) Con un guion de acciones en JSON (para "anda a tal página y apreta tal botón"):
    python -m navdiag --script flujo.json

   flujo.json:
   {
     "label": "login",
     "headless": true,
     "steps": [
       {"action": "goto",  "url": "https://ejemplo.cl"},
       {"action": "click", "selector": "text=Iniciar sesión"},
       {"action": "fill",  "selector": "#user", "value": "demo"},
       {"action": "fill",  "selector": "#pass", "value": "1234"},
       {"action": "click", "selector": "button[type=submit]"},
       {"action": "snapshot", "label": "post-login"}
     ]
   }
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .capture import DiagnosticSession


def _run_steps(session: DiagnosticSession, steps: list[dict]) -> None:
    for st in steps:
        action = st.get("action")
        if action == "goto":
            session.goto(st["url"])
        elif action == "click":
            session.click(st["selector"])
        elif action == "fill":
            session.fill(st["selector"], st.get("value", ""))
        elif action == "press":
            session.press(st["selector"], st["key"])
        elif action == "wait":
            session.wait(float(st.get("seconds", 1)))
        elif action == "snapshot":
            session.snapshot(st.get("label", "snapshot"))
        else:
            print(f"[navdiag] acción desconocida: {action!r}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="navdiag", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("url", nargs="?", help="URL a diagnosticar (modo rápido)")
    p.add_argument("--script", help="archivo JSON con label/steps")
    p.add_argument("--label", default="session", help="nombre de la sesión")
    p.add_argument("--out", default="captures", help="carpeta de salida")
    p.add_argument("--headed", action="store_true", help="mostrar el navegador")
    p.add_argument("--settle", type=float, default=2.0,
                   help="segundos extra de espera tras networkidle")
    args = p.parse_args(argv)

    label = args.label
    steps: list[dict] = []
    headless = not args.headed

    if args.script:
        cfg = json.loads(Path(args.script).read_text(encoding="utf-8"))
        label = cfg.get("label", label)
        headless = cfg.get("headless", headless)
        steps = cfg.get("steps", [])
    elif args.url:
        steps = [{"action": "goto", "url": args.url}]
    else:
        p.error("debes pasar una URL o --script")

    with DiagnosticSession(out_dir=args.out, label=label, headless=headless,
                           settle_seconds=args.settle) as session:
        _run_steps(session, steps)
        run_dir = session.run_dir

    print(f"[navdiag] expediente guardado en: {run_dir}")
    print(f"[navdiag] revisa {run_dir}/AI_SUMMARY.md y endpoints.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
