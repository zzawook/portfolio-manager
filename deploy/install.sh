#!/usr/bin/env bash
# Install or update portfolio-manager on a Linux server (e.g. EC2) and run the web
# screen as a systemd service. Run it from the project folder on the server:
#
#   bash deploy/install.sh
#
# The web screen listens on 127.0.0.1 only. Open it from your computer through an
# SSH tunnel (deploy/tunnel.sh). Nothing is opened to the internet.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${PORT:-8765}"
SERVICE="portfolio-ui"
RUN_USER="$(id -un)"
cd "$APP_DIR"

say() { printf '\n== %s\n' "$*"; }

can_sudo() {
  sudo -n true 2>/dev/null && return 0
  [ -t 0 ] && sudo -v
}

find_python() {
  PY=""
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
      PY="$c"
      return 0
    fi
  done
  return 1
}

say "Python 확인"
if ! find_python && can_sudo; then
  echo "Python 3.10 이상이 없어서 설치해요."
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -qq && sudo apt-get install -y -qq python3 python3-venv
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y -q python3.11
  fi
  find_python || true
fi
if [ -z "$PY" ]; then
  echo "Python 3.10 이상이 필요해요."
  echo "  Ubuntu:            sudo apt-get install -y python3 python3-venv"
  echo "  Amazon Linux 2023: sudo dnf install -y python3.11"
  exit 1
fi
"$PY" --version 2>&1

say "가상환경과 패키지 설치"
if [ ! -x .venv/bin/python ]; then
  if ! "$PY" -m venv .venv 2>/dev/null; then
    rm -rf .venv
    # Ubuntu ships venv separately (python3-venv, or python3.X-venv for a non-default version).
    if command -v apt-get >/dev/null 2>&1 && can_sudo; then
      echo "venv 패키지를 설치해요."
      VENV_PKG="$("$PY" -c 'import sys; print(f"python3.{sys.version_info.minor}-venv")')"
      sudo apt-get update -qq || true
      sudo apt-get install -y -qq python3-venv || true
      sudo apt-get install -y -qq "$VENV_PKG" || true
    fi
    if ! "$PY" -m venv .venv; then
      rm -rf .venv
      echo "가상환경을 만들 수 없어요. Ubuntu라면 sudo apt-get install -y python3-venv 를 실행한 뒤 다시 해 주세요."
      exit 1
    fi
  fi
fi
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -e .
.venv/bin/python -m rebalancer check >/dev/null
echo "설정 확인 끝"

say ".env 준비"
if [ ! -f .env ]; then
  (
    umask 077
    cat > .env <<'ENV'
# 비밀 값은 이 파일에만 두세요. git에 안 올라가요.
# IBKR 계좌번호 (모의계좌는 DU로 시작해요)
IBKR_ACCOUNT_ID=
# 채팅을 ChatGPT 구독 대신 API 키로 쓸 때만 채워요
# OPENAI_API_KEY=
# ANTHROPIC_API_KEY=
ENV
  )
  echo "만들었어요: $APP_DIR/.env"
else
  echo "있는 파일을 그대로 둬요: $APP_DIR/.env"
fi
chmod 600 .env

say "서비스 등록 ($SERVICE, 127.0.0.1:$PORT)"
if ! can_sudo; then
  echo "sudo를 쓸 수 없어서 서비스로 등록하지 못했어요. 직접 켜려면:"
  echo "  cd $APP_DIR && .venv/bin/python -m rebalancer ui --no-browser --port $PORT"
  exit 0
fi
sudo tee "/etc/systemd/system/$SERVICE.service" >/dev/null <<UNIT
[Unit]
Description=portfolio-manager web screen (127.0.0.1:$PORT)
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python -m rebalancer -c $APP_DIR/config.yaml ui --no-browser --port $PORT
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
# Share the server politely with other services (e.g. an agent already running there):
# the app uses about 60 MB, is capped at 256 MB, runs at lower CPU priority, and is
# the one the kernel stops first if memory runs out.
MemoryMax=256M
Nice=5
OOMScoreAdjust=300

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE" >/dev/null 2>&1
sudo systemctl restart "$SERVICE"
sleep 3
if sudo systemctl is-active --quiet "$SERVICE"; then
  echo "켜졌어요. 서버가 다시 켜져도 자동으로 실행돼요."
  sudo journalctl -u "$SERVICE" -n 6 --no-pager -o cat || true
else
  echo "서비스가 켜지지 않았어요. 로그:"
  sudo journalctl -u "$SERVICE" -n 40 --no-pager -o cat || true
  exit 1
fi

cat <<NEXT

다음 단계
  1. 내 컴퓨터에서 터널 열기:   bash deploy/tunnel.sh <ssh 호스트>
  2. 브라우저로 열기:           http://127.0.0.1:$PORT
  3. 채팅 창에서 'ChatGPT로 로그인'
  IBKR를 연결할 때는 bash deploy/install-ibkr-gateway.sh 를 먼저 실행하세요.
NEXT
