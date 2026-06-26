"""
navdiag.act — cliente liviano del driver interactivo (v2).

Manda UN comando al driver (por FIFO) y espera su respuesta inmediata
(screenshot + info). No abre navegador ni reconecta: retorno casi instantáneo.

    python -m navdiag.act goto https://ejemplo.cl
    python -m navdiag.act click "text=Entrar"
    python -m navdiag.act fill "#user" demo
    python -m navdiag.act select "#region" "RM"
    python -m navdiag.act hover "text=Menú"
    python -m navdiag.act back
    python -m navdiag.act reload
    python -m navdiag.act tabs                 # lista pestañas abiertas
    python -m navdiag.act switch 1             # cambia a la pestaña 1
    python -m navdiag.act eval "document.title"
    python -m navdiag.act dump login           # expediente completo
    python -m navdiag.act info
    python -m navdiag.act quit                 # detiene el driver (no el navegador)

Salida: resumen JSON. Screenshot en captures/live/shot.png, info completa en
captures/live/info.json, timeline en captures/live/timeline.jsonl.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

LIVE = Path("captures/live")
REQ = LIVE / "req.fifo"
RESP = LIVE / "resp.fifo"
LOCK = LIVE / "act.lock"


def _send(cmd: dict) -> dict:
    # lock simple para que dos 'act' no se pisen en la FIFO
    try:
        import fcntl
        LOCK.touch(exist_ok=True)
        lockf = open(LOCK, "w")
        fcntl.flock(lockf, fcntl.LOCK_EX)
    except Exception:
        lockf = None
    try:
        with open(REQ, "w") as w:
            w.write(json.dumps(cmd, ensure_ascii=False) + "\n")
        with open(RESP, "r") as r:
            line = r.readline()
    finally:
        if lockf:
            lockf.close()
    return json.loads(line)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("uso: act <goto|click|fill|press|hover|select|scroll|wait|back|"
              "forward|reload|tabs|switch|eval|dump|info|quit> [arg] [value]",
              file=sys.stderr)
        return 2
    cmd: dict = {"action": argv[0]}
    if len(argv) >= 2:
        cmd["arg"] = argv[1]
    if len(argv) >= 3:
        cmd["value"] = argv[2]

    if not REQ.exists():
        print("ERROR: el driver no está corriendo (no existe la FIFO). "
              "Inicia: python -m navdiag.driver", file=sys.stderr)
        return 1

    try:
        result = _send(cmd)
    except Exception as e:  # noqa: BLE001
        print("ERROR de comunicación con el driver:", e, file=sys.stderr)
        return 1

    ok = result.get("ok")
    print(("✅" if ok else "❌"), cmd["action"], cmd.get("arg", ""))
    if result.get("error"):
        print("   error:", result["error"])
    if "eval" in result:
        print("   eval:", json.dumps(result["eval"], ensure_ascii=False))
    if result.get("dumped"):
        print("   expediente:", result["dumped"])
    print("   url:", result.get("url"))
    print("   title:", result.get("title"))
    print(f"   clickables: {result.get('clickables')} | inputs: {result.get('inputs')} "
          f"| pestañas: {result.get('tabs_open')}")
    for t in result.get("tabs", []) or []:
        mark = "→" if t.get("active") else " "
        print(f"   {mark} [{t['index']}] {t.get('title','')[:40]} | {t.get('url','')}")
    for e in result.get("new_endpoints", []) or []:
        line = f"   ↪ {e.get('method')} {e.get('url')} [{e.get('type')}] {e.get('status')}"
        if e.get("post_data"):
            line += f"  body={e['post_data']!r}"
        if e.get("response_file"):
            line += f"  resp={e['response_file']}"
        print(line)
    print("   shot:", result.get("shot"), "| info:", result.get("info"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
