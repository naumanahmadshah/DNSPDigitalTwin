#!/usr/bin/env bash
# =============================================================================
# Phone demo: the field app on the audience's phones, live with the deployed app.
#
#   APP_URL=https://<app>.databricksapps.com DATABRICKS_CONFIG_PROFILE=<cli-profile> tools/field_demo.sh
#   (optional: PORT=8787, QR=/tmp/field-demo-qr.png)
#
# Starts the field relay (tools/field_relay.py) on this laptop, opens a Cloudflare quick tunnel to it (no Cloudflare account needed),
# prints the public field URL and a QR code (shown from this laptop; the deployed app has no phone demo option), and keeps
# the tunnel open until Ctrl+C. Only the field app and its API pass through, using your own Databricks login (profile DATABRICKS_CONFIG_PROFILE), so
# phones and the big screen work on the same live day. The tunnel URL changes every run.
# Needs: databricks CLI (signed in), python3, cloudflared and qrencode (brew install cloudflared qrencode).
# =============================================================================
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${PORT:-8787}"
APP="${APP_URL:-}"
PROFILE="${DATABRICKS_CONFIG_PROFILE:-}"
QR="${QR:-/tmp/field-demo-qr.png}"
export DATABRICKS_AUTH_STORAGE=plaintext
if [ -z "$APP" ] || [ -z "$PROFILE" ]; then
  echo "Usage: APP_URL=https://<app>.databricksapps.com DATABRICKS_CONFIG_PROFILE=<cli-profile> tools/field_demo.sh"
  echo "  (optional: PORT=8787, QR=/tmp/field-demo-qr.png)"
  exit 1
fi

for t in databricks python3 cloudflared qrencode curl; do
  command -v "$t" >/dev/null || { echo "$t is not installed (brew install cloudflared qrencode for the last two)."; exit 1; }
done

RELAY_LOG="$(mktemp)"; CF_LOG="$(mktemp)"
RELAY_PID=""; CF_PID=""
cleanup() {
  echo; echo "Stopping the tunnel and the relay..."
  [ -n "$CF_PID" ] && kill "$CF_PID" 2>/dev/null
  [ -n "$RELAY_PID" ] && kill "$RELAY_PID" 2>/dev/null
}
trap cleanup EXIT INT TERM

# 1) the relay, checked through the field API
python3 "$ROOT/tools/field_relay.py" --port "$PORT" --profile "$PROFILE" --app "$APP" >"$RELAY_LOG" 2>&1 &
RELAY_PID=$!
for i in $(seq 1 30); do curl -s -m3 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/field/meta" | grep -q 200 && break; sleep 1; done
if ! curl -s -m5 "http://127.0.0.1:$PORT/api/field/meta" | grep -q regions; then
  echo "The relay could not reach the field app. Relay log:"; tail -20 "$RELAY_LOG"
  echo "Signed out? Run: databricks auth login -p $PROFILE"; exit 1
fi
echo "Relay healthy on http://127.0.0.1:$PORT/field"

# 2) the public quick tunnel
# HTTP/2 over TCP 443: many office and venue networks block the QUIC (UDP 7844) cloudflared would try first
cloudflared tunnel --protocol http2 --url "http://127.0.0.1:$PORT" --no-autoupdate >"$CF_LOG" 2>&1 &
CF_PID=$!
echo "Opening the public tunnel (a few seconds)..."
URL=""
for i in $(seq 1 40); do
  URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$CF_LOG" | head -1)"
  [ -n "$URL" ] && break
  sleep 1
done
[ -z "$URL" ] && { echo "No tunnel URL. cloudflared log:"; tail -20 "$CF_LOG"; exit 1; }
for i in $(seq 1 45); do curl -s -m5 "$URL/api/field/meta" | grep -q regions && break; sleep 2; done   # the URL comes before the route is live
curl -s -m5 "$URL/api/field/meta" | grep -q regions || { echo "The tunnel is not reachable yet. cloudflared log:"; tail -20 "$CF_LOG"; exit 1; }
FIELD_URL="$URL/field"

# 3) the URL and QR on screen, and saved
qrencode -o "$QR" -s 12 -m 2 "$FIELD_URL"
echo
echo "==================================================================="
echo "  AUDIENCE URL:  $FIELD_URL"
echo "==================================================================="
qrencode -t ANSIUTF8 "$FIELD_URL"
echo "QR image saved: $QR"
echo
echo "Say to the room: scan this and the Field Service app opens on your phone."
echo "  iPhone (Safari):  Share, then Add to Home Screen (opens full screen)"
echo "  Android (Chrome): Install app prompt, or menu, then Install app"
echo
echo "Leave this running during the demo. Ctrl+C stops the tunnel and the relay."
wait "$CF_PID"
