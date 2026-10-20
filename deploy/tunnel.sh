#!/usr/bin/env bash
# Run this on your own computer to reach the web screen and the IBKR Gateway on the server.
#
#   bash deploy/tunnel.sh <ssh 호스트>
#
# Keep it running while you use the screen. The local ports are the same as on the
# server, which the ChatGPT sign-in needs: it comes back to http://127.0.0.1:8765.
set -euo pipefail

HOST="${1:?사용법: bash deploy/tunnel.sh <ssh 호스트>}"
PORT="${PORT:-8765}"

echo "웹 화면:   http://127.0.0.1:$PORT"
echo "IBKR 로그인: https://127.0.0.1:5000"
echo "끄려면 Ctrl+C"
exec ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -L "$PORT:127.0.0.1:$PORT" -L 5000:127.0.0.1:5000 "$HOST"
