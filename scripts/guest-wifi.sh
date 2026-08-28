#!/usr/bin/env bash
# Check whether the guest Wi-Fi is broadcasting and (optionally) show its
# connect-QR on the Inky frame. Run on the Pi HOST (needs nmcli + wlan0):
#
#   bash guest-wifi.sh            # scan + report status only
#   bash guest-wifi.sh --show     # if the guest SSID is up, show the QR on the frame
#
# The guest SSID is read from the service .env (single source of truth); the QR
# itself is rendered by the service from GUEST_WIFI_* — this script only detects
# and triggers.
set -euo pipefail

ENV_FILE="${ENV_FILE:-$HOME/services/inky-frame-dashboard/.env}"
SVC="${SVC:-http://localhost:8080}"

SSID="$(grep -E '^GUEST_WIFI_SSID=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'")"
if [ -z "$SSID" ]; then
    echo "GUEST_WIFI_SSID is not set in $ENV_FILE" >&2
    exit 2
fi

# Force a fresh scan where allowed; fall back to NetworkManager's cached results.
nmcli dev wifi rescan >/dev/null 2>&1 || true
sleep 4

if nmcli -t -f SSID dev wifi list 2>/dev/null | sed 's/\\//g' | grep -Fxq "$SSID"; then
    echo "UP: guest network '$SSID' is broadcasting"
    if [ "${1:-}" = "--show" ]; then
        code="$(curl -s -o /dev/null -w '%{http_code}' \
            -X POST "$SVC/display/dashboard?name=guest_wifi&dither=NONE&wait=false")"
        echo "  -> frame: POST /display/dashboard?name=guest_wifi&dither=NONE -> HTTP $code"
    fi
    exit 0
else
    echo "DOWN: guest network '$SSID' not found in the current scan"
    exit 1
fi
