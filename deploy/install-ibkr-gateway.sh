#!/usr/bin/env bash
# Install the IBKR Client Portal Gateway on this server and run it as a service on port 5000.
# Run it on the server from the project folder, after deploy/install.sh:
#
#   bash deploy/install-ibkr-gateway.sh
#
# The Gateway only accepts connections from this machine. Log in from your computer
# through the SSH tunnel (deploy/tunnel.sh) at https://127.0.0.1:5000 . IBKR ends the
# session at least once a day, so log in again before you use the screen.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GW_DIR="${GW_DIR:-$HOME/ibkr-gateway}"
URL="https://download2.interactivebrokers.com/portal/clientportal.gw.zip"
SERVICE="ibkr-gateway"
RUN_USER="$(id -un)"

say() { printf '\n== %s\n' "$*"; }

install_pkg() {
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -qq && sudo apt-get install -y -qq "$1"
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y -q "$2"
  else
    echo "$1 을(를) 설치한 뒤 다시 실행하세요."
    exit 1
  fi
}

if ! sudo -n true 2>/dev/null && ! { [ -t 0 ] && sudo -v; }; then
  echo "sudo가 필요해요 (Java 설치와 서비스 등록)."
  exit 1
fi

say "Java와 unzip 확인"
command -v java >/dev/null 2>&1 || install_pkg openjdk-17-jre-headless java-17-amazon-corretto-headless
command -v unzip >/dev/null 2>&1 || install_pkg unzip unzip
java -version 2>&1 | head -1

say "Gateway 내려받기 ($GW_DIR)"
mkdir -p "$GW_DIR"
curl -fsSL -o "$GW_DIR/clientportal.gw.zip" "$URL"
# Keep the Gateway certificate made by deploy/install-https.sh (Caddy and the app trust it).
if [ -f "$GW_DIR/root/gateway-cert.pem" ]; then
  (cd "$GW_DIR" && unzip -o -q clientportal.gw.zip -x root/vertx.jks)
else
  (cd "$GW_DIR" && unzip -o -q clientportal.gw.zip)
fi

say "이 서버 안에서 오는 연결만 받게 설정"
if [ -x "$APP_DIR/.venv/bin/python" ]; then
  "$APP_DIR/.venv/bin/python" - "$GW_DIR/root/conf.yaml" <<'PY' || echo "conf.yaml을 고치지 못했어요. 보안 그룹에서 5000번 포트를 열지 않았는지 꼭 확인하세요."
import sys

import yaml

path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    conf = yaml.safe_load(f)
ips = conf.get("ips") or {}
ips["allow"] = ["127.0.0.1"]
conf["ips"] = ips
with open(path, "w", encoding="utf-8") as f:
    yaml.safe_dump(conf, f, sort_keys=False)
print("허용 IP를 127.0.0.1 하나로 줄였어요. SSH 터널로 오는 연결도 127.0.0.1이에요.")
PY
else
  echo "먼저 bash deploy/install.sh 를 실행하세요."
  exit 1
fi

say "로그아웃 스크립트"
cat > "$GW_DIR/logout.sh" <<'LOGOUT'
#!/usr/bin/env bash
# Log the Gateway out of IBKR (used when the service stops). Verifies the Gateway's
# own certificate when deploy/install-https.sh has made one.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CERT="$DIR/root/gateway-cert.pem"
if [ -f "$CERT" ]; then TLS=(--cacert "$CERT"); else TLS=(-k); fi
curl -s --max-time 5 "${TLS[@]}" -X POST -d "" https://localhost:5000/v1/api/logout >/dev/null || true
LOGOUT
chmod 755 "$GW_DIR/logout.sh"

say "서비스 등록 ($SERVICE)"
sudo tee "/etc/systemd/system/$SERVICE.service" >/dev/null <<UNIT
[Unit]
Description=IBKR Client Portal Gateway (port 5000, local connections only)
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$GW_DIR
ExecStart=/bin/bash $GW_DIR/bin/run.sh root/conf.yaml
# Log out of IBKR before stopping. A session cut off without logging out can block
# the next login for a while (IBKR allows one session per username).
ExecStop=/bin/bash $GW_DIR/logout.sh
Restart=on-failure
RestartSec=10
# Keep the Java gateway from crowding out other services on the server.
Environment=JAVA_TOOL_OPTIONS=-Xmx384m
MemoryMax=640M
Nice=5
OOMScoreAdjust=300

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE" >/dev/null 2>&1
sudo systemctl restart "$SERVICE"
sleep 5
if sudo systemctl is-active --quiet "$SERVICE"; then
  echo "켜졌어요."
else
  sudo journalctl -u "$SERVICE" -n 40 --no-pager -o cat || true
  exit 1
fi

cat <<NEXT

다음 단계
  1. 내 컴퓨터에서 터널 열기: bash deploy/tunnel.sh <ssh 호스트>
  2. https://127.0.0.1:5000 에서 IBKR 로그인 (자체 서명 인증서 경고가 떠요. 계속 진행하세요)
  3. $APP_DIR/.env 에 IBKR_ACCOUNT_ID=DU... 를 넣고, config.yaml의 broker.type을 ibkr로 바꾼 뒤
     sudo systemctl restart portfolio-ui
NEXT
