"""
navdiag.driver — driver interactivo de iteración rápida (v2).

Se conecta UNA sola vez a un navegador persistente (CDP) y escucha comandos por
FIFO. Cada acción ejecuta algo y devuelve al instante: screenshot del viewport
activo + info compacta (URL, título, clickeables, inputs, endpoints disparados
con su post_data/respuesta). Pensado para el bucle:

    click ──▶ screenshot + info ──▶ (analizar) ──▶ click ──▶ ...

Mejoras v2 respecto a v1:
  - Sigue PESTAÑAS/POPUPS nuevos (target=_blank, window.open) y opera sobre la
    pestaña activa. Comandos `tabs` y `switch`.
  - Acciones y probe atraviesan IFRAMES (busca el selector en todos los frames).
  - Captura BODIES de XHR/fetch/documento: método, post_data, status y cuerpo
    de respuesta (preview + archivo en captures/live/resources/).
  - TIMELINE persistente: cada acción se anexa a captures/live/timeline.jsonl y
    su screenshot se copia a captures/live/steps/NNN_accion.png.
  - Más acciones: back, forward, reload, hover, select, scroll, eval, dump.
  - `dump` escribe un expediente completo (manifest/network/endpoints/dom).

Uso:
    python -m navdiag.browser_server --port 9222
    python -m navdiag.driver --cdp http://localhost:9222
    python -m navdiag.act goto https://ejemplo.cl
    python -m navdiag.act click "text=Entrar"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


LIVE = Path("captures/live")
REQ = LIVE / "req.fifo"
RESP = LIVE / "resp.fifo"
SHOT = LIVE / "shot.png"
INFO = LIVE / "info.json"
STEPS = LIVE / "steps"
RES = LIVE / "resources"
TIMELINE = LIVE / "timeline.jsonl"

_TEXT_HINTS = ("text/", "json", "javascript", "xml", "ecmascript", "csv", "graphql",
               "x-www-form-urlencoded")

# JS que extrae elementos visibles útiles de UN frame.
_PROBE_JS = r"""() => {
  const vis = el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const clean = t => (t || '').trim().replace(/\s+/g, ' ').slice(0, 60);
  const sel = el => {
    if (el.id) return '#' + CSS.escape(el.id);
    if (el.name) return `${el.tagName.toLowerCase()}[name=${JSON.stringify(el.name)}]`;
    const t = clean(el.innerText || el.value || '');
    if (t && el.tagName !== 'INPUT') return `${el.tagName.toLowerCase()}:has-text(${JSON.stringify(t)})`;
    return el.tagName.toLowerCase();
  };
  const clickables = [];
  document.querySelectorAll('a,button,input[type=submit],input[type=button],[role=button],[onclick]').forEach(el => {
    if (!vis(el)) return;
    clickables.push({ tag: el.tagName, text: clean(el.innerText || el.value),
                      href: (el.getAttribute && el.getAttribute('href')) || '',
                      selector: sel(el) });
  });
  const inputs = [];
  document.querySelectorAll('input,select,textarea').forEach(el => {
    if (!vis(el) || el.type === 'hidden') return;
    inputs.push({ type: el.type || el.tagName.toLowerCase(), id: el.id || '',
                  name: el.name || '', placeholder: el.placeholder || '',
                  selector: sel(el) });
  });
  return { url: location.href, title: document.title,
           clickables: clickables.slice(0, 40), inputs: inputs.slice(0, 30) };
}"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _textual(ct: str) -> bool:
    ct = (ct or "").lower()
    return any(h in ct for h in _TEXT_HINTS)


def _ext(url: str, ct: str) -> str:
    e = Path(urlparse(url).path).suffix
    if e and len(e) <= 6:
        return e
    return mimetypes.guess_extension((ct or "").split(";")[0].strip()) or ".bin"


def _mkfifo(path: Path) -> None:
    if path.exists():
        path.unlink()
    os.mkfifo(path)


class Driver:
    def __init__(self, cdp: str, settle_ms: int, max_body: int = 2_000_000) -> None:
        self.cdp = cdp
        self.settle_ms = settle_ms
        self.max_body = max_body
        self._net: list[dict] = []          # registros de red (xhr/fetch/no-GET)
        self._mark = 0                      # marca para "endpoints nuevos"
        self._seen: dict[str, str] = {}     # hash -> archivo (dedupe bodies)
        self._step = 0

    # ---- arranque -------------------------------------------------------- #

    def start(self) -> None:
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(self.cdp)
        self.ctx = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
        self.pages = list(self.ctx.pages) or [self.ctx.new_page()]
        self.page = self.pages[-1]
        # eventos a nivel de CONTEXTO: capturan todas las pestañas y frames
        self.ctx.on("page", self._on_page)
        self.ctx.on("requestfinished", self._on_finished)
        self.ctx.on("requestfailed", self._on_failed)

    def _on_page(self, page) -> None:
        # nueva pestaña/popup -> pasa a ser la activa
        self.pages.append(page)
        self.page = page
        try:
            page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass

    # ---- captura de red -------------------------------------------------- #

    def _interesting(self, request) -> bool:
        return request.resource_type in ("xhr", "fetch", "websocket", "eventsource") \
            or request.method != "GET"

    def _on_finished(self, request) -> None:
        if not self._interesting(request):
            return
        rec = {"ts": _now(), "method": request.method, "url": request.url,
               "type": request.resource_type, "status": None,
               "post_data": None, "content_type": None,
               "response_preview": None, "response_file": None}
        try:
            rec["post_data"] = request.post_data
        except Exception:
            pass
        try:
            resp = request.response()
        except Exception:
            resp = None
        if resp is not None:
            try:
                rec["status"] = resp.status
            except Exception:
                pass
            try:
                rec["content_type"] = resp.headers.get("content-type", "")
            except Exception:
                pass
            self._grab_body(rec, resp)
        self._net.append(rec)

    def _on_failed(self, request) -> None:
        if not self._interesting(request):
            return
        try:
            err = request.failure
        except Exception:
            err = "failed"
        self._net.append({"ts": _now(), "method": request.method, "url": request.url,
                          "type": request.resource_type, "status": None,
                          "failed": err, "post_data": None})

    def _grab_body(self, rec: dict, resp) -> None:
        try:
            body = resp.body()
        except Exception:
            return
        if not body:
            return
        if _textual(rec.get("content_type") or ""):
            try:
                rec["response_preview"] = body.decode("utf-8", "replace")[:1500]
            except Exception:
                pass
        if len(body) <= self.max_body:
            h = hashlib.sha1(body).hexdigest()[:12]
            if h not in self._seen:
                name = f"{h}{_ext(rec['url'], rec.get('content_type') or '')}"
                try:
                    (RES / name).write_bytes(body)
                    self._seen[h] = name
                except Exception:
                    return
            rec["response_file"] = f"resources/{self._seen.get(h, '')}"

    # ---- resolución de selector a través de frames ----------------------- #

    def _locator(self, selector: str):
        """Busca el selector en todos los frames de la pestaña activa."""
        for fr in self.page.frames:
            try:
                loc = fr.locator(selector)
                if loc.count() > 0:
                    return loc.first
            except Exception:
                continue
        return self.page.locator(selector).first

    def _settle(self) -> None:
        # deja que un posible popup se registre como pestaña activa
        try:
            self.page.wait_for_timeout(250)
        except Exception:
            pass
        try:
            self.page.wait_for_load_state("networkidle", timeout=self.settle_ms)
        except Exception:
            self.page.wait_for_timeout(min(self.settle_ms, 1200))

    # ---- acciones -------------------------------------------------------- #

    def handle(self, cmd: dict) -> dict:
        action = cmd.get("action")
        arg = cmd.get("arg")
        value = cmd.get("value")
        try:
            if action == "goto":
                self.page.goto(arg, wait_until="domcontentloaded", timeout=60_000)
                self._settle()
            elif action == "click":
                self._locator(arg).click(timeout=15_000)
                self._settle()
            elif action == "fill":
                self._locator(arg).fill(value or "", timeout=15_000)
            elif action == "press":
                self._locator(arg).press(value or "Enter", timeout=15_000)
                self._settle()
            elif action == "hover":
                self._locator(arg).hover(timeout=15_000)
                self.page.wait_for_timeout(400)
            elif action == "select":
                self._locator(arg).select_option(value)
                self._settle()
            elif action == "scroll":
                self.page.mouse.wheel(0, int(value or arg or 800))
                self.page.wait_for_timeout(400)
            elif action == "wait":
                self.page.wait_for_timeout(int(float(arg or 1) * 1000))
            elif action == "back":
                self.page.go_back(timeout=30_000)
                self._settle()
            elif action == "forward":
                self.page.go_forward(timeout=30_000)
                self._settle()
            elif action == "reload":
                self.page.reload(timeout=60_000)
                self._settle()
            elif action == "tabs":
                return {"ok": True, "tabs": self._tabs(), **self._capture(action)}
            elif action == "switch":
                self._switch(int(arg))
            elif action == "eval":
                val = self.page.evaluate(arg)
                return {"ok": True, "eval": val, **self._capture(action)}
            elif action == "dump":
                path = self._dump(arg or "dump")
                return {"ok": True, "dumped": path, **self._capture(action)}
            elif action == "info":
                pass
            else:
                return {"ok": False, "error": f"acción desconocida: {action}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e), **self._capture(action)}
        return {"ok": True, **self._capture(action)}

    def _tabs(self) -> list[dict]:
        out = []
        for i, p in enumerate(self.pages):
            try:
                out.append({"index": i, "active": p is self.page,
                            "url": p.url, "title": p.title()})
            except Exception:
                out.append({"index": i, "active": p is self.page, "closed": True})
        return out

    def _switch(self, index: int) -> None:
        self.pages = [p for p in self.pages if not p.is_closed()]
        if 0 <= index < len(self.pages):
            self.page = self.pages[index]
            self.page.bring_to_front()

    # ---- captura --------------------------------------------------------- #

    def _probe(self) -> dict:
        main = {"url": "", "title": "", "clickables": [], "inputs": []}
        for i, fr in enumerate(self.page.frames):
            try:
                r = fr.evaluate(_PROBE_JS)
            except Exception:
                continue
            if i == 0:
                main["url"], main["title"] = r.get("url", ""), r.get("title", "")
            for c in r.get("clickables", []):
                c["frame"] = i
                main["clickables"].append(c)
            for inp in r.get("inputs", []):
                inp["frame"] = i
                main["inputs"].append(inp)
        main["clickables"] = main["clickables"][:60]
        main["inputs"] = main["inputs"][:40]
        return main

    def _capture(self, action: str) -> dict:
        try:
            self.page.screenshot(path=str(SHOT), full_page=False)
        except Exception:
            pass
        probe = self._probe()
        new = self._net[self._mark:]
        self._mark = len(self._net)
        probe["new_endpoints"] = new[-25:]
        INFO.write_text(json.dumps(probe, ensure_ascii=False, indent=2), encoding="utf-8")

        # timeline persistente + copia del screenshot por paso
        self._step += 1
        step_png = STEPS / f"{self._step:03d}_{action}.png"
        try:
            if SHOT.exists():
                shutil.copy(SHOT, step_png)
        except Exception:
            pass
        entry = {"step": self._step, "ts": _now(), "action": action,
                 "url": probe.get("url"), "title": probe.get("title"),
                 "screenshot": str(step_png),
                 "new_endpoints": [{"method": e["method"], "url": e["url"].split("?")[0],
                                    "status": e.get("status")} for e in new[-25:]]}
        try:
            with open(TIMELINE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

        # resumen corto (respuesta inmediata)
        ep = [{"method": e["method"], "url": e["url"].split("?")[0],
               "type": e.get("type"), "status": e.get("status"),
               "post_data": (e.get("post_data") or "")[:160] or None,
               "response_file": e.get("response_file")} for e in new[-10:]]
        return {"url": probe.get("url"), "title": probe.get("title"),
                "clickables": len(probe["clickables"]), "inputs": len(probe["inputs"]),
                "tabs_open": len(self.pages), "new_endpoints": ep,
                "shot": str(SHOT), "info": str(INFO)}

    def _dump(self, label: str) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        d = LIVE.parent / f"dump-{label}_{ts}"
        d.mkdir(parents=True, exist_ok=True)
        try:
            self.page.screenshot(path=str(d / "fullpage.png"), full_page=True)
        except Exception:
            pass
        try:
            (d / "dom.html").write_text(self.page.content(), encoding="utf-8")
        except Exception:
            pass
        (d / "network.json").write_text(
            json.dumps(self._net, ensure_ascii=False, indent=2), encoding="utf-8")
        # endpoints deduplicados
        seen: dict[tuple, dict] = {}
        for r in self._net:
            k = (r["method"], r["url"].split("?")[0])
            if k not in seen:
                seen[k] = {"method": r["method"], "url": r["url"].split("?")[0],
                           "status": r.get("status"), "type": r.get("type"),
                           "post_data": r.get("post_data"),
                           "response_file": r.get("response_file"),
                           "response_preview": r.get("response_preview")}
        (d / "endpoints.json").write_text(
            json.dumps(list(seen.values()), ensure_ascii=False, indent=2), encoding="utf-8")
        (d / "cookies.json").write_text(
            json.dumps(self._cookies(), ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            storage = self.page.evaluate(
                "() => ({local: {...localStorage}, session: {...sessionStorage}})")
            (d / "storage.json").write_text(
                json.dumps(storage, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        return str(d)

    def _cookies(self) -> list:
        """Cookies de toda la sesión. En modo CDP, ctx.cookies() suele venir
        vacío, así que se piden vía CDP Network.getAllCookies (incluye httpOnly
        y de todos los dominios: SSO, keycloak, etc.)."""
        try:
            sess = self.ctx.new_cdp_session(self.page)
            cks = sess.send("Network.getAllCookies").get("cookies", [])
            if cks:
                return cks
        except Exception:
            pass
        try:
            return self.ctx.cookies()
        except Exception:
            return []

    # ---- loop FIFO ------------------------------------------------------- #

    def serve(self) -> None:
        import select
        _mkfifo(REQ)
        _mkfifo(RESP)
        # O_RDWR: el propio proceso mantiene un escritor, así open() no bloquea y
        # el loop ocioso es un 'tick' por select (como un sleep) -> sobrevive en
        # background en vez de quedar bloqueado en open(fifo) y ser reapeado.
        req_fd = os.open(REQ, os.O_RDWR | os.O_NONBLOCK)
        print(f"READY driver cdp={self.cdp} fifo={REQ}", flush=True)
        buf = ""
        while True:
            ready, _, _ = select.select([req_fd], [], [], 1.0)
            if not ready:
                continue  # tick ocioso
            try:
                chunk = os.read(req_fd, 65536).decode("utf-8", "replace")
            except BlockingIOError:
                continue
            if not chunk:
                continue
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                if not line.strip():
                    continue
                try:
                    cmd = json.loads(line)
                except Exception as e:  # noqa: BLE001
                    self._reply({"ok": False, "error": f"json inválido: {e}"})
                    continue
                if cmd.get("action") == "quit":
                    self._reply({"ok": True, "bye": True})
                    os.close(req_fd)
                    return
                self._reply(self.handle(cmd))

    def _reply(self, obj: dict) -> None:
        with open(RESP, "w") as w:
            w.write(json.dumps(obj, ensure_ascii=False) + "\n")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="navdiag.driver")
    p.add_argument("--cdp", default="http://localhost:9222")
    p.add_argument("--settle", type=int, default=2500, help="ms de espera tras cada acción")
    args = p.parse_args(argv)

    for d in (LIVE, STEPS, RES):
        d.mkdir(parents=True, exist_ok=True)
    drv = Driver(args.cdp, args.settle)
    drv.start()
    drv.serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
