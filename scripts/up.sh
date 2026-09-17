#!/usr/bin/env bash
# Levanta el navegador persistente + driver de navdiag de forma robusta.
#
#   scripts/up.sh            # navegador visible (usa DISPLAY) + driver
#   HEADLESS=1 scripts/up.sh # navegador sin ventana
#
# Detalle importante: ambos procesos se lanzan con `setsid` para que queden en
# su propia sesión y NO los mate el reaper de procesos del entorno. El driver,
# además, usa un loop select() en la FIFO (no se bloquea en open()).
set -u
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PORT="${PORT:-9222}"
LOGDIR="${LOGDIR:-/tmp/navdiag}"
mkdir -p "$LOGDIR" captures/live
export PYTHONPATH="$ROOT"
export DISPLAY="${DISPLAY:-:0}"

HEADLESS_FLAG=""
[ "${HEADLESS:-0}" = "1" ] && HEADLESS_FLAG="--headless"

# 1) navegador persistente (solo si el puerto CDP no responde aún)
if ! curl -s "http://localhost:$PORT/json/version" >/dev/null 2>&1; then
  setsid bash -c "exec python -m navdiag.browser_server --port $PORT $HEADLESS_FLAG" \
    > "$LOGDIR/browser.log" 2>&1 < /dev/null &
  echo "browser_server lanzado (puerto $PORT)"
  for i in $(seq 1 60); do
    curl -s "http://localhost:$PORT/json/version" >/dev/null 2>&1 && break; sleep 0.2
  done
else
  echo "browser_server ya estaba arriba (puerto $PORT)"
fi

# 2) driver (reinicia si había uno)
pkill -f "[n]avdiag.driver" 2>/dev/null || true
rm -f captures/live/req.fifo captures/live/resp.fifo
setsid bash -c "exec python -m navdiag.driver --cdp http://localhost:$PORT --settle ${SETTLE:-2000}" \
  > "$LOGDIR/driver.log" 2>&1 < /dev/null &
echo "driver lanzado"
for i in $(seq 1 60); do
  [ -p captures/live/req.fifo ] && grep -q READY "$LOGDIR/driver.log" 2>/dev/null && { echo "READY"; break; }
  sleep 0.2
done
echo "Logs: $LOGDIR/{browser,driver}.log"
echo "Probar:  python -m navdiag.act goto https://ejemplo.cl"
exit 0
