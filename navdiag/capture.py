"""
navdiag.capture
===============

Toolkit de diagnóstico de navegación basado en Playwright.

Idea central
------------
Abrir una página, ejecutar una secuencia de acciones ("anda a tal página y
apreta tal botón") y guardar localmente TODO lo que pasó: requests/responses
(GET, POST, ...), endpoints/XHR, HTML renderizado, CSS, JS, cookies,
localStorage, consola y screenshots. Cada artefacto queda con timestamp.

El objetivo es dejar un "expediente" estructurado y fácil de leer por una IA
para acelerar el scraping o la auto-reparación de un scrapper.

Uso típico
----------
    from navdiag import DiagnosticSession

    with DiagnosticSession(out_dir="captures", label="login-flow") as s:
        s.goto("https://ejemplo.cl")
        s.click("text=Iniciar sesión")
        s.fill("#user", "demo")
        s.click("button[type=submit]")
        s.snapshot("post-login")     # snapshot manual extra
    # -> captures/login-flow_<timestamp>/manifest.json + endpoints.json + ...
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, Page, Response, Request


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

# content-types que tratamos como texto (se guardan como archivo y además se
# deja un snippet inline en el índice para que la IA lo lea sin abrir archivos).
_TEXT_HINTS = (
    "text/",
    "application/json",
    "application/javascript",
    "application/xml",
    "application/x-www-form-urlencoded",
    "+json",
    "+xml",
    "javascript",
    "ecmascript",
    "csv",
    "graphql",
)

_API_PATH_HINT = re.compile(
    r"(/api/|/v\d+/|/graphql|/rest/|\.json($|\?)|/rpc/|/ajax/|/gql)", re.I
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_textual(content_type: str) -> bool:
    ct = (content_type or "").lower()
    return any(h in ct for h in _TEXT_HINTS)


def _ext_for(url: str, content_type: str) -> str:
    ext = Path(urlparse(url).path).suffix
    if ext and len(ext) <= 6:
        return ext
    guessed = mimetypes.guess_extension((content_type or "").split(";")[0].strip())
    return guessed or ".bin"


def _slug(text: str, maxlen: int = 40) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", text).strip("-")
    return (s[:maxlen] or "x").lower()


def _short_hash(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Modelos de datos
# --------------------------------------------------------------------------- #

@dataclass
class CapturedRequest:
    """Una transacción HTTP completa observada durante la navegación."""
    index: int
    step: str
    started_at: str
    method: str
    url: str
    resource_type: str
    request_headers: dict = field(default_factory=dict)
    post_data: Optional[str] = None
    status: Optional[int] = None
    status_text: Optional[str] = None
    response_headers: dict = field(default_factory=dict)
    content_type: Optional[str] = None
    from_cache: bool = False
    failed: Optional[str] = None
    duration_ms: Optional[float] = None
    # rutas relativas a los cuerpos guardados en disco (si aplica)
    response_body_file: Optional[str] = None
    response_body_size: Optional[int] = None
    # snippet textual inline (recortado) para lectura rápida por IA
    response_preview: Optional[str] = None
    is_endpoint: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StepRecord:
    index: int
    label: str
    action: str
    detail: str
    timestamp: str
    url: Optional[str] = None
    title: Optional[str] = None
    screenshot: Optional[str] = None
    dom_file: Optional[str] = None
    console_count: int = 0
    error: Optional[str] = None


# --------------------------------------------------------------------------- #
# Sesión de diagnóstico
# --------------------------------------------------------------------------- #

class DiagnosticSession:
    """
    Maneja un navegador Playwright y graba un expediente de diagnóstico.

    Parámetros
    ----------
    out_dir : carpeta raíz donde se crean los expedientes.
    label : nombre lógico de esta sesión (prefijo de la carpeta).
    headless : correr el navegador sin ventana (default True).
    settle_seconds : segundos extra de espera tras 'networkidle' para dejar que
        scripts perezosos disparen sus requests (default 2.0).
    networkidle_timeout : ms máximos esperando red inactiva (default 15000).
    save_response_bodies : guardar cuerpos de respuesta a disco (default True).
    max_body_bytes : tope por cuerpo guardado para no llenar el disco.
    viewport : (ancho, alto) del viewport.
    user_agent : UA opcional.
    extra_http_headers : headers extra (auth, etc.).
    cdp_endpoint : si se entrega (ej. "http://localhost:9222"), en vez de lanzar
        un navegador nuevo se CONECTA a uno ya abierto vía CDP y reutiliza su
        pestaña/cookies. Ideal para un navegador persistente que NO se cierra
        entre pasos (loop: guardar info -> click -> análisis -> ...).
    keep_browser_open : al terminar, no cerrar el navegador (solo desconectar).
        Por defecto True cuando se usa cdp_endpoint.
    reuse_page : al conectar por CDP, reutilizar la pestaña existente en vez de
        abrir una nueva (default True).
    """

    def __init__(
        self,
        out_dir: str = "captures",
        label: str = "session",
        *,
        headless: bool = True,
        settle_seconds: float = 2.0,
        networkidle_timeout: int = 15_000,
        save_response_bodies: bool = True,
        max_body_bytes: int = 3_000_000,
        preview_chars: int = 2_000,
        viewport: tuple[int, int] = (1366, 900),
        user_agent: Optional[str] = None,
        extra_http_headers: Optional[dict] = None,
        cdp_endpoint: Optional[str] = None,
        keep_browser_open: Optional[bool] = None,
        reuse_page: bool = True,
    ) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self.run_dir = Path(out_dir) / f"{_slug(label)}_{ts}"
        self.label = label
        self.headless = headless
        self.settle_seconds = settle_seconds
        self.networkidle_timeout = networkidle_timeout
        self.save_response_bodies = save_response_bodies
        self.max_body_bytes = max_body_bytes
        self.preview_chars = preview_chars
        self.viewport = viewport
        self.user_agent = user_agent
        self.extra_http_headers = extra_http_headers or {}
        self.cdp_endpoint = cdp_endpoint
        self.reuse_page = reuse_page
        self.keep_browser_open = (
            keep_browser_open if keep_browser_open is not None
            else bool(cdp_endpoint)
        )

        # estado interno
        self._requests: list[CapturedRequest] = []
        self._req_by_obj: dict[Any, CapturedRequest] = {}
        self._steps: list[StepRecord] = []
        self._console: list[dict] = []
        self._page_errors: list[dict] = []
        self._current_step = "init"
        self._req_counter = 0
        self._seen_bodies: dict[str, str] = {}  # hash -> filename (dedupe)
        self._started_monotonic = 0.0

        # carpetas
        self.resources_dir = self.run_dir / "resources"
        self.steps_dir = self.run_dir / "steps"
        self.dom_dir = self.run_dir / "dom"

        self._pw = None
        self._browser = None
        self._context = None
        self.page: Optional[Page] = None

    # ---- ciclo de vida ---------------------------------------------------- #

    def __enter__(self) -> "DiagnosticSession":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.finish()

    def start(self) -> "DiagnosticSession":
        for d in (self.resources_dir, self.steps_dir, self.dom_dir):
            d.mkdir(parents=True, exist_ok=True)

        self._started_monotonic = time.monotonic()
        self._pw = sync_playwright().start()

        if self.cdp_endpoint:
            # conectarse a un navegador ya abierto (persistente) y reutilizarlo
            self._browser = self._pw.chromium.connect_over_cdp(self.cdp_endpoint)
            self._context = (self._browser.contexts[0]
                             if self._browser.contexts
                             else self._browser.new_context(ignore_https_errors=True))
            if self.extra_http_headers:
                self._context.set_extra_http_headers(self.extra_http_headers)
            pages = self._context.pages
            self.page = (pages[0] if (self.reuse_page and pages)
                         else self._context.new_page())
        else:
            self._browser = self._pw.chromium.launch(headless=self.headless)
            ctx_kwargs: dict[str, Any] = {
                "viewport": {"width": self.viewport[0], "height": self.viewport[1]},
                "ignore_https_errors": True,
            }
            if self.user_agent:
                ctx_kwargs["user_agent"] = self.user_agent
            self._context = self._browser.new_context(**ctx_kwargs)
            if self.extra_http_headers:
                self._context.set_extra_http_headers(self.extra_http_headers)
            self.page = self._context.new_page()

        self._wire_listeners(self.page)
        return self

    def finish(self) -> Path:
        """Escribe el manifiesto y desconecta. Devuelve run_dir.

        Si keep_browser_open es True (modo CDP), NO cierra el navegador: solo
        retira los listeners y suelta la conexión, dejando la ventana viva para
        el siguiente paso.
        """
        try:
            self._write_outputs()
        finally:
            try:
                if self.keep_browser_open:
                    # soltar solo nuestros listeners; dejar el navegador vivo
                    self._unwire_listeners(self.page)
                else:
                    if self._context:
                        self._context.close()
                    if self._browser:
                        self._browser.close()
                if self._pw:
                    self._pw.stop()
            except Exception:
                pass
        return self.run_dir

    # ---- listeners de red / consola --------------------------------------- #

    def _wire_listeners(self, page: Page) -> None:
        page.on("request", self._on_request)
        page.on("requestfinished", self._on_request_finished)
        page.on("requestfailed", self._on_request_failed)
        page.on("console", self._on_console)
        page.on("pageerror", self._on_page_error)

    def _unwire_listeners(self, page: Optional[Page]) -> None:
        if page is None:
            return
        for ev, fn in (
            ("request", self._on_request),
            ("requestfinished", self._on_request_finished),
            ("requestfailed", self._on_request_failed),
            ("console", self._on_console),
            ("pageerror", self._on_page_error),
        ):
            try:
                page.remove_listener(ev, fn)
            except Exception:
                pass

    def _on_request(self, request: Request) -> None:
        self._req_counter += 1
        rec = CapturedRequest(
            index=self._req_counter,
            step=self._current_step,
            started_at=_now_iso(),
            method=request.method,
            url=request.url,
            resource_type=request.resource_type,
        )
        try:
            rec.request_headers = request.headers
        except Exception:
            pass
        try:
            rec.post_data = request.post_data
        except Exception:
            rec.post_data = None
        rec.is_endpoint = self._looks_like_endpoint(request.resource_type,
                                                     request.method, request.url,
                                                     rec.post_data)
        self._req_by_obj[request] = rec
        self._requests.append(rec)

    def _on_request_finished(self, request: Request) -> None:
        rec = self._req_by_obj.get(request)
        if rec is None:
            return
        try:
            response = request.response()
        except Exception:
            response = None
        if response is not None:
            self._fill_response(rec, response)
        try:
            timing = request.timing
            if timing and timing.get("responseEnd", -1) >= 0:
                rec.duration_ms = round(timing["responseEnd"], 1)
        except Exception:
            pass

    def _on_request_failed(self, request: Request) -> None:
        rec = self._req_by_obj.get(request)
        if rec is None:
            return
        try:
            rec.failed = request.failure or "failed"
        except Exception:
            rec.failed = "failed"

    def _fill_response(self, rec: CapturedRequest, response: Response) -> None:
        try:
            rec.status = response.status
            rec.status_text = response.status_text
        except Exception:
            pass
        try:
            rec.response_headers = response.all_headers()
        except Exception:
            try:
                rec.response_headers = response.headers
            except Exception:
                rec.response_headers = {}
        rec.content_type = rec.response_headers.get("content-type", "")
        try:
            rec.from_cache = bool(response.from_service_worker)
        except Exception:
            pass

        if not self.save_response_bodies:
            return
        try:
            body = response.body()
        except Exception:
            return
        if not body:
            return
        rec.response_body_size = len(body)
        textual = _is_textual(rec.content_type or "")
        if textual:
            try:
                text = body.decode("utf-8", errors="replace")
                rec.response_preview = text[: self.preview_chars]
            except Exception:
                text = None
        # guardar a disco (con dedupe por hash) si no excede el tope
        if len(body) <= self.max_body_bytes:
            rec.response_body_file = self._save_resource(rec.url,
                                                         rec.content_type or "",
                                                         body)

    # ---- consola / errores ------------------------------------------------ #

    def _on_console(self, msg) -> None:
        try:
            entry = {
                "ts": _now_iso(),
                "step": self._current_step,
                "type": msg.type,
                "text": msg.text,
            }
            loc = msg.location
            if loc:
                entry["location"] = loc
            self._console.append(entry)
        except Exception:
            pass

    def _on_page_error(self, err) -> None:
        self._page_errors.append({
            "ts": _now_iso(),
            "step": self._current_step,
            "error": str(err),
        })

    # ---- clasificación de endpoints --------------------------------------- #

    _STATIC_TYPES = frozenset({"font", "image", "stylesheet", "media"})

    @classmethod
    def _looks_like_endpoint(cls, resource_type: str, method: str, url: str,
                             post_data: Optional[str]) -> bool:
        if resource_type in ("xhr", "fetch", "websocket", "eventsource"):
            return True
        if method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
            return True
        # recursos estáticos en GET nunca son "endpoints", aunque la URL tenga
        # un segmento versionado (/v37/ de un CDN, etc.)
        if resource_type in cls._STATIC_TYPES:
            return False
        if _API_PATH_HINT.search(url or ""):
            return True
        if post_data:
            return True
        return False

    # ---- guardado de recursos --------------------------------------------- #

    def _save_resource(self, url: str, content_type: str, body: bytes) -> str:
        h = _short_hash(body)
        if h in self._seen_bodies:
            return self._seen_bodies[h]
        ext = _ext_for(url, content_type)
        name = f"{h}{ext}"
        path = self.resources_dir / name
        try:
            path.write_bytes(body)
        except Exception:
            return ""
        rel = str(path.relative_to(self.run_dir))
        self._seen_bodies[h] = rel
        return rel

    # ---- API pública de acciones ----------------------------------------- #

    def goto(self, url: str, *, snapshot: bool = True, wait: bool = True) -> "DiagnosticSession":
        """Navega a una URL y (por defecto) toma snapshot tras carga completa."""
        self._begin_step("goto", url)
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            if wait:
                self._wait_settled()
            if snapshot:
                self._snapshot(label=self._current_step, action="goto", detail=url)
        except Exception as e:  # noqa: BLE001
            self._record_step_error("goto", url, str(e))
        return self

    def click(self, selector: str, *, snapshot: bool = True, wait: bool = True) -> "DiagnosticSession":
        """Hace click en un selector (texto, css, role=...) y captura el efecto."""
        self._begin_step("click", selector)
        try:
            self.page.click(selector, timeout=15_000)
            if wait:
                self._wait_settled()
            if snapshot:
                self._snapshot(label=self._current_step, action="click", detail=selector)
        except Exception as e:  # noqa: BLE001
            self._record_step_error("click", selector, str(e))
        return self

    def fill(self, selector: str, value: str, *, snapshot: bool = False) -> "DiagnosticSession":
        """Escribe en un input. Por defecto NO toma snapshot (suele ser paso intermedio)."""
        self._begin_step("fill", f"{selector} = {value!r}")
        try:
            self.page.fill(selector, value, timeout=15_000)
            if snapshot:
                self._snapshot(label=self._current_step, action="fill", detail=selector)
        except Exception as e:  # noqa: BLE001
            self._record_step_error("fill", selector, str(e))
        return self

    def press(self, selector: str, key: str, *, snapshot: bool = True, wait: bool = True) -> "DiagnosticSession":
        """Presiona una tecla sobre un selector (ej. 'Enter')."""
        self._begin_step("press", f"{selector} -> {key}")
        try:
            self.page.press(selector, key, timeout=15_000)
            if wait:
                self._wait_settled()
            if snapshot:
                self._snapshot(label=self._current_step, action="press", detail=f"{selector} {key}")
        except Exception as e:  # noqa: BLE001
            self._record_step_error("press", selector, str(e))
        return self

    def wait(self, seconds: float) -> "DiagnosticSession":
        """Espera fija (segundos)."""
        self.page.wait_for_timeout(int(seconds * 1000))
        return self

    def snapshot(self, label: str) -> "DiagnosticSession":
        """Fuerza un snapshot manual con una etiqueta arbitraria."""
        self._begin_step("snapshot", label)
        self._snapshot(label=label, action="snapshot", detail=label)
        return self

    def run_js(self, expression: str) -> Any:
        """Ejecuta JS en la página y devuelve el resultado (útil para inspección)."""
        return self.page.evaluate(expression)

    # ---- internos de pasos ------------------------------------------------ #

    def _begin_step(self, action: str, detail: str) -> None:
        idx = len(self._steps) + 1
        self._current_step = f"{idx:02d}_{action}_{_slug(detail, 24)}"

    def _wait_settled(self) -> None:
        try:
            self.page.wait_for_load_state("networkidle", timeout=self.networkidle_timeout)
        except Exception:
            pass  # algunas páginas nunca quedan idle; seguimos igual
        if self.settle_seconds > 0:
            self.page.wait_for_timeout(int(self.settle_seconds * 1000))

    def _snapshot(self, *, label: str, action: str, detail: str) -> None:
        idx = len(self._steps) + 1
        step_slug = f"{idx:02d}_{_slug(label, 30)}"
        rec = StepRecord(
            index=idx, label=label, action=action, detail=detail,
            timestamp=_now_iso(),
        )
        try:
            rec.url = self.page.url
            rec.title = self.page.title()
        except Exception:
            pass

        # screenshot
        shot = self.steps_dir / f"{step_slug}.png"
        try:
            self.page.screenshot(path=str(shot), full_page=True)
            rec.screenshot = str(shot.relative_to(self.run_dir))
        except Exception as e:  # noqa: BLE001
            rec.error = f"screenshot: {e}"

        # DOM renderizado
        dom = self.dom_dir / f"{step_slug}.html"
        try:
            dom.write_text(self.page.content(), encoding="utf-8")
            rec.dom_file = str(dom.relative_to(self.run_dir))
        except Exception as e:  # noqa: BLE001
            rec.error = (rec.error or "") + f" dom: {e}"

        rec.console_count = sum(1 for c in self._console if c["step"] == self._current_step)
        self._steps.append(rec)

    def _record_step_error(self, action: str, detail: str, msg: str) -> None:
        idx = len(self._steps) + 1
        self._steps.append(StepRecord(
            index=idx, label=self._current_step, action=action, detail=detail,
            timestamp=_now_iso(), error=msg,
        ))

    # ---- escritura de salida --------------------------------------------- #

    def _collect_state(self) -> dict:
        state: dict[str, Any] = {"cookies": [], "local_storage": {}, "session_storage": {}}
        try:
            state["cookies"] = self._context.cookies()
        except Exception:
            pass
        try:
            state["local_storage"] = self.page.evaluate(
                "() => Object.fromEntries(Object.entries(window.localStorage))")
        except Exception:
            pass
        try:
            state["session_storage"] = self.page.evaluate(
                "() => Object.fromEntries(Object.entries(window.sessionStorage))")
        except Exception:
            pass
        return state

    def _endpoints_summary(self) -> list[dict]:
        """Lista deduplicada de endpoints (lo más valioso para IA)."""
        seen: dict[tuple, dict] = {}
        for r in self._requests:
            if not r.is_endpoint:
                continue
            base = r.url.split("?")[0]
            key = (r.method.upper(), base)
            if key in seen:
                seen[key]["count"] += 1
                continue
            seen[key] = {
                "method": r.method.upper(),
                "url": base,
                "full_url_sample": r.url,
                "resource_type": r.resource_type,
                "status": r.status,
                "content_type": r.content_type,
                "request_headers": r.request_headers,
                "post_data": r.post_data,
                "response_body_file": r.response_body_file,
                "response_preview": r.response_preview,
                "step": r.step,
                "count": 1,
            }
        return list(seen.values())

    def _write_outputs(self) -> None:
        state = self._collect_state()
        elapsed = round(time.monotonic() - self._started_monotonic, 2)
        endpoints = self._endpoints_summary()

        # contadores por tipo de recurso
        by_type: dict[str, int] = {}
        for r in self._requests:
            by_type[r.resource_type] = by_type.get(r.resource_type, 0) + 1

        manifest = {
            "label": self.label,
            "created_at": _now_iso(),
            "duration_seconds": elapsed,
            "run_dir": str(self.run_dir),
            "config": {
                "headless": self.headless,
                "settle_seconds": self.settle_seconds,
                "networkidle_timeout_ms": self.networkidle_timeout,
            },
            "summary": {
                "steps": len(self._steps),
                "requests_total": len(self._requests),
                "requests_by_type": by_type,
                "endpoints": len(endpoints),
                "console_messages": len(self._console),
                "page_errors": len(self._page_errors),
            },
            "steps": [asdict(s) for s in self._steps],
            "final_state": {
                "cookies": state["cookies"],
                "local_storage": state["local_storage"],
                "session_storage": state["session_storage"],
            },
        }

        (self.run_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        (self.run_dir / "network.json").write_text(
            json.dumps([r.to_dict() for r in self._requests], indent=2, ensure_ascii=False),
            encoding="utf-8")
        (self.run_dir / "endpoints.json").write_text(
            json.dumps(endpoints, indent=2, ensure_ascii=False), encoding="utf-8")
        (self.run_dir / "console.json").write_text(
            json.dumps({"console": self._console, "page_errors": self._page_errors},
                       indent=2, ensure_ascii=False), encoding="utf-8")
        (self.run_dir / "AI_SUMMARY.md").write_text(
            self._render_ai_summary(manifest, endpoints), encoding="utf-8")

    def _render_ai_summary(self, manifest: dict, endpoints: list[dict]) -> str:
        s = manifest["summary"]
        lines = [
            f"# Diagnóstico de navegación — {self.label}",
            "",
            f"- Creado: {manifest['created_at']}",
            f"- Duración: {manifest['duration_seconds']} s",
            f"- Pasos: {s['steps']} | Requests: {s['requests_total']} | "
            f"Endpoints: {s['endpoints']} | Errores JS: {s['page_errors']}",
            "",
            "## Estructura del expediente",
            "- `manifest.json` — resumen, pasos, estado final (cookies/localStorage).",
            "- `endpoints.json` — endpoints/XHR deduplicados (lo más útil para reparar el scrapper).",
            "- `network.json` — TODAS las transacciones HTTP con headers/body/timing.",
            "- `console.json` — mensajes de consola y errores de página.",
            "- `resources/` — cuerpos de respuesta (HTML/CSS/JS/JSON/imagenes) por hash.",
            "- `dom/` — HTML renderizado por paso.",
            "- `steps/` — screenshots full-page por paso.",
            "",
            "## Pasos",
        ]
        for st in manifest["steps"]:
            mark = "❌" if st.get("error") else "✅"
            lines.append(
                f"{mark} `{st['index']:02d}` **{st['action']}** {st['detail']} "
                f"→ {st.get('title') or ''} ({st.get('url') or ''})"
            )
            if st.get("error"):
                lines.append(f"    - error: {st['error']}")
        lines += ["", "## Endpoints detectados"]
        if not endpoints:
            lines.append("_(no se detectaron endpoints XHR/fetch/POST)_")
        for e in endpoints:
            lines.append(
                f"- `{e['method']} {e['url']}` → {e['status']} "
                f"[{e['resource_type']}] x{e['count']}"
            )
            if e.get("post_data"):
                pd = e["post_data"][:200]
                lines.append(f"    - body: `{pd}`")
        lines.append("")
        return "\n".join(lines)
