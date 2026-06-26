"""
Navegador persistente para navdiag.

Lanza un Chromium VISIBLE con un puerto CDP fijo y lo deja vivo. Los pasos de
diagnóstico se conectan a él (DiagnosticSession(cdp_endpoint=...)), actúan,
capturan y se desconectan SIN cerrarlo. Así el navegador no se cierra entre
pasos: guardar info -> click -> análisis -> repetir.

    python -m navdiag.browser_server            # puerto 9222, visible
    python -m navdiag.browser_server --port 9333 --headless

Para detenerlo: matar el proceso (Ctrl-C o kill).
"""
from __future__ import annotations

import argparse
import time

from playwright.sync_api import sync_playwright


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="navdiag.browser_server")
    p.add_argument("--port", type=int, default=9222)
    p.add_argument("--headless", action="store_true")
    p.add_argument("--width", type=int, default=1366)
    p.add_argument("--height", type=int, default=900)
    args = p.parse_args(argv)

    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=args.headless,
        args=[f"--remote-debugging-port={args.port}", "--start-maximized"],
    )
    ctx = browser.new_context(
        viewport={"width": args.width, "height": args.height},
        ignore_https_errors=True,
    )
    page = ctx.new_page()
    page.goto("about:blank")
    # señal lista (la lee quien lanzó el server)
    print(f"READY cdp=http://localhost:{args.port}", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            browser.close()
            pw.stop()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
