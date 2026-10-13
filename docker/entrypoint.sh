#!/bin/sh
set -eu
umask 077
cd /app

run_command() {
    if [ "${1:-service}" = "service" ]; then
        if [ "$#" -gt 0 ]; then shift; fi
        python -m auto_location.service "$@"
    else
        python /app/main.py "$@"
    fi
}

if [ "${AUTO_LOCATION_GUI:-0}" = "1" ]; then
    export DISPLAY=:99
    Xvfb "$DISPLAY" -screen 0 1440x900x24 -nolisten tcp &
    xvfb_pid=$!
    cleanup() {
        kill "${vnc_pid:-}" "${web_pid:-}" "${wm_pid:-}" "$xvfb_pid" 2>/dev/null || true
    }
    trap cleanup EXIT
    trap 'exit 143' TERM
    trap 'exit 130' INT
    attempt=0
    until xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; do
        attempt=$((attempt + 1))
        if [ "$attempt" -ge 30 ]; then echo "Xvfb failed to start" >&2; exit 1; fi
        sleep 1
    done
    fluxbox >/tmp/fluxbox.log 2>&1 &
    wm_pid=$!
    vnc_mode=""
    if [ "${1:-service}" = "service" ]; then vnc_mode="-viewonly"; fi
    x11vnc $vnc_mode -display "$DISPLAY" -localhost -nopw -forever -shared -noclipboard -rfbport 5900 >/tmp/x11vnc.log 2>&1 &
    vnc_pid=$!
    websockify --web=/usr/share/novnc 0.0.0.0:6080 127.0.0.1:5900 >/tmp/novnc.log 2>&1 &
    web_pid=$!
    echo "Browser display ready; service dashboard provides monitoring and account setup."
    run_command "$@"
elif [ "${1:-service}" = "service" ]; then
    if [ "$#" -gt 0 ]; then shift; fi
    exec python -m auto_location.service "$@"
else
    exec python /app/main.py "$@"
fi
