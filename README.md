# navdiag

Toolkit de diagnóstico de navegación **cuyo output está pensado para que lo lea
un modelo**, no una persona.

Abres una página, ejecutas una secuencia de acciones (*"anda a tal página y
aprieta tal botón"*) y navdiag guarda todo lo que pasó por debajo en un
expediente local estructurado: requests y responses con sus payloads,
endpoints/XHR, HTML renderizado, CSS, JS, cookies, localStorage, consola y
screenshots, todo con timestamp.

## Por qué existe

Cuando un scrapper se rompe, el trabajo caro no es escribir el código nuevo: es
volver a descubrir qué endpoints usa el sitio hoy, qué payload esperan y qué
cambió en el DOM. navdiag hace ese descubrimiento una vez y lo deja en
`endpoints.json` —método, URL, status, headers, `post_data` y preview de la
respuesta por cada llamada— más un `AI_SUMMARY.md` ya redactado.

Con eso, un agente puede regenerar el scrapper sin abrir un navegador.

## Instalación

```bash
pip install -r requirements.txt
playwright install chromium      # si aún no tienes el navegador
```

## Uso como librería

```python
from navdiag import DiagnosticSession

with DiagnosticSession(out_dir="captures", label="login") as s:
    s.goto("https://ejemplo.cl")
    s.click("text=Iniciar sesión")
    s.fill("#user", "demo")
    s.fill("#pass", "1234")
    s.click("button[type=submit]")
    s.snapshot("post-login")        # snapshot manual extra
# -> captures/login_<timestamp>/
```

Cada `goto`/`click`/`press` espera carga completa (`networkidle` + un settle
configurable, porque una página suele tardar varios segundos) y toma un
snapshot automático.

### Acciones disponibles
| método | qué hace |
|--------|----------|
| `goto(url)` | navega y captura |
| `click(selector)` | click (`text=…`, css, `role=…`) y captura |
| `fill(selector, value)` | escribe en un input (paso intermedio, sin snapshot) |
| `press(selector, key)` | presiona una tecla (ej. `Enter`) |
| `wait(seconds)` | espera fija |
| `snapshot(label)` | fuerza un snapshot manual |
| `run_js(expr)` | evalúa JS y devuelve el resultado |

## Uso por CLI

Modo rápido (una URL):
```bash
python -m navdiag https://ejemplo.cl --label home
```

Modo guion (para flujos con botones/formularios):
```bash
python -m navdiag --script flujo.json
```
```json
{
  "label": "login",
  "headless": true,
  "steps": [
    {"action": "goto",  "url": "https://ejemplo.cl"},
    {"action": "click", "selector": "text=Iniciar sesión"},
    {"action": "fill",  "selector": "#user", "value": "demo"},
    {"action": "click", "selector": "button[type=submit]"},
    {"action": "snapshot", "label": "post-login"}
  ]
}
```

Opciones útiles: `--headed` (ver el navegador), `--settle 3` (más espera por
carga), `--out carpeta`.

## Modo iteración rápida (navegador persistente)

Para depurar/explorar un flujo paso a paso **sin cerrar el navegador** entre
acciones (click → screenshot + info → analizar → repetir):

```bash
scripts/up.sh                      # levanta navegador visible + driver
# (HEADLESS=1 scripts/up.sh para sin ventana)

python -m navdiag.act goto https://ejemplo.cl
python -m navdiag.act click "text=Entrar"
python -m navdiag.act fill "#user" demo
python -m navdiag.act tabs          # lista pestañas (sigue popups/target=_blank)
python -m navdiag.act switch 1
python -m navdiag.act eval "document.title"
python -m navdiag.act dump login    # expediente completo en captures/dump-*
python -m navdiag.act quit          # detiene el driver (NO el navegador)
```

Cada acción responde al instante con:
- `captures/live/shot.png` — screenshot del viewport activo.
- `captures/live/info.json` — clickeables, inputs y endpoints disparados (con
  **post_data** y **body de respuesta** guardado en `captures/live/resources/`).
- `captures/live/timeline.jsonl` + `captures/live/steps/NNN_*.png` — timeline
  persistente de toda la sesión interactiva.

Acciones: `goto, click, fill, press, hover, select, scroll, wait, back,
forward, reload, tabs, switch, eval, dump, info, quit`. Los clicks/inputs
buscan el selector **en todos los iframes** de la pestaña activa.

Arquitectura: `browser_server` (Chromium con puerto CDP) ← `driver` (se conecta
1 vez, escucha por FIFO) ← `act` (cliente liviano por acción). Ambos servicios
se lanzan con `setsid` para no ser reapeados.

## Qué genera cada expediente

```
captures/<label>_<timestamp>/
├── AI_SUMMARY.md      # resumen legible: pasos, endpoints, estructura
├── manifest.json      # resumen + pasos + estado final (cookies/localStorage)
├── endpoints.json     # endpoints/XHR deduplicados  ← lo más útil para reparar
├── network.json       # TODAS las transacciones HTTP (headers, body, timing)
├── console.json       # mensajes de consola y errores JS
├── resources/         # cuerpos de respuesta (HTML/CSS/JS/JSON/img) por hash
├── dom/               # HTML renderizado por paso
└── steps/             # screenshots full-page por paso
```

`endpoints.json` incluye, por cada endpoint detectado: método, URL, status,
content-type, headers de request, **post_data** (payload) y un preview de la
respuesta. Es lo que normalmente necesita una IA para regenerar las llamadas
de un scrapper sin tener que abrir un navegador.

## Ejemplo

```bash
python examples/example_basic.py
```

## Arquitectura del modo interactivo

```
browser_server  (Chromium con puerto CDP, proceso propio)
      ▲
      │ CDP
      │
   driver       (se conecta UNA vez, escucha por FIFO con select())
      ▲
      │ req.fifo / resp.fifo
      │
    act         (cliente liviano, un proceso por acción)
```

La separación existe para que el navegador **no se cierre entre acciones**: la
sesión, las cookies y el estado de la página sobreviven mientras tú analizas el
screenshot y decides el paso siguiente. `browser_server` y `driver` se lanzan
con `setsid` para quedar en su propia sesión y que no los mate el reaper de
procesos del entorno.

## Límites conocidos

- Requiere Chromium vía Playwright; no soporta Firefox ni WebKit en el modo
  interactivo.
- Los expedientes guardan **cookies y localStorage en claro**: si diagnosticas
  un flujo con login, el directorio `captures/` contiene credenciales de sesión.
  Está en `.gitignore` por eso — no lo subas.
- `networkidle` no alcanza en páginas con polling permanente; usar `--settle`.

## Licencia

MIT
