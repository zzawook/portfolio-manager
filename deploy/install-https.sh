#!/usr/bin/env bash
# Open the web screen to the internet over HTTPS at a fixed address, behind the web
# screen's password. Run it on the server from the project folder, after
# deploy/install.sh (and deploy/install-ibkr-gateway.sh if you use IBKR):
#
#   bash deploy/install-https.sh <공인 IP> [도메인]
#
# The screen is served at a host name, by default pm.<IP with dashes>.sslip.io (sslip.io
# answers any such name with the IP inside it, so no domain needs to be bought). The
# IP address itself redirects there. A name is needed because IBKR's login page saves
# its session cookies for a domain it derives from the address bar: for an IP address
# that domain is invalid, the cookies are dropped, and the IBKR login never completes.
#
# What it does
# - Installs Caddy, which gets Let's Encrypt certificates for the name and the IP
#   (the IP's is the 6-day certificate Let's Encrypt issues for IPs; Caddy renews both).
# - Caddy sends everything to the web screen on 127.0.0.1:8765. The IBKR Gateway's
#   login pages (/sso/..., /v1/api/...) go to the Gateway on 127.0.0.1:5000, but only
#   after the web screen confirms the visitor is signed in (forward_auth).
# - Gives the Gateway its own certificate (IBKR's bundled one expired in 2019), so Caddy
#   and the app verify the local connection instead of skipping the check.
# - Sets web.public_host in config.yaml. Until a password is set with
#   `.venv/bin/python -m rebalancer set-password`, the screen refuses visitors from outside.
#
# The security group must allow TCP 80 (certificate checks, redirect) and 443.
set -euo pipefail

IP="${1:?사용법: bash deploy/install-https.sh <공인 IP> [도메인]}"
if ! [[ "$IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "첫 번째 값은 공인 IPv4 주소여야 해요 (예: 54.255.220.60)."
  exit 1
fi
PUBLIC_HOST="${2:-pm.${IP//./-}.sslip.io}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GW_DIR="${GW_DIR:-$HOME/ibkr-gateway}"
PORT="${PORT:-8765}"
cd "$APP_DIR"

say() { printf '\n== %s\n' "$*"; }

if ! sudo -n true 2>/dev/null && ! { [ -t 0 ] && sudo -v; }; then
  echo "sudo가 필요해요 (Caddy 설치와 설정)."
  exit 1
fi

say "Caddy 설치"
if ! command -v caddy >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl gnupg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update -qq
  sudo apt-get install -y -qq caddy
fi
caddy version

GW_PEM=""
if [ -f "$GW_DIR/root/conf.yaml" ]; then
  say "IBKR Gateway 인증서 (127.0.0.1 연결 검증용)"
  GW_PEM="$GW_DIR/root/gateway-cert.pem"
  if [ ! -f "$GW_PEM" ]; then
    (
      cd "$GW_DIR/root"
      [ -f vertx.jks.ibkr-original ] || cp vertx.jks vertx.jks.ibkr-original
      rm -f vertx.jks.new
      keytool -genkeypair -alias localhost -keyalg RSA -keysize 2048 -validity 3650 \
        -dname "CN=localhost, O=portfolio-manager" -ext "SAN=dns:localhost,ip:127.0.0.1" \
        -storetype JKS -keystore vertx.jks.new -storepass mywebapi -keypass mywebapi >/dev/null 2>&1
      keytool -exportcert -rfc -alias localhost -keystore vertx.jks.new -storepass mywebapi > gateway-cert.pem 2>/dev/null
      mv vertx.jks.new vertx.jks
    )
    echo "새 인증서를 만들었어요 (10년). IBKR 원래 파일은 vertx.jks.ibkr-original 로 남겨 뒀어요."
    echo "Gateway를 다시 켜요. IBKR 로그인은 다시 해야 해요."
    sudo systemctl restart ibkr-gateway
    sleep 8
  fi
  sudo install -m 644 "$GW_PEM" /etc/caddy/ibkr-gateway.pem
  if curl -fsS --cacert "$GW_PEM" -o /dev/null https://localhost:5000/ 2>/dev/null; then
    echo "Gateway 인증서 검증 확인했어요."
  else
    echo "Gateway에 인증서 검증으로 연결하지 못했어요. sudo journalctl -u ibkr-gateway 를 확인하세요."
  fi
fi

say "config.yaml 설정 (web.public_host$( [ -n "$GW_PEM" ] && echo ', broker.ibkr.ca_file'))"
BEFORE="$(sha256sum config.yaml)"
.venv/bin/python - "$APP_DIR/config.yaml" "$PUBLIC_HOST" "$GW_PEM" <<'PY'
import re
import sys

path, host, pem = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path, encoding="utf-8").read()

# web.public_host: replace the value, or add the section at the end.
if re.search(r"(?m)^web:\s*$", text):
    if re.search(r"(?m)^  public_host:", text):
        text = re.sub(r"(?m)^(  public_host:).*$", rf"\1 {host}", text)
    else:
        text = re.sub(r"(?m)^web:\s*$", f"web:\n  public_host: {host}", text)
else:
    text = text.rstrip("\n") + f"\n\nweb:\n  public_host: {host}               # 밖에서 접속하는 주소 (https://{host})\n"

# broker.ibkr.ca_file: verify the Gateway with its own certificate.
if pem:
    if re.search(r"(?m)^    ca_file:", text):
        text = re.sub(r"(?m)^(    ca_file:).*$", rf"\1 {pem}", text)
    else:
        text = re.sub(
            r"(?m)^(    verify_ssl:.*)$",
            rf"\1\n    ca_file: {pem}   # Gateway 인증서로 검증 (deploy/install-https.sh)",
            text,
            count=1,
        )
open(path, "w", encoding="utf-8").write(text)
PY
.venv/bin/python -m rebalancer check >/dev/null && echo "설정 확인 끝"

say "Caddy 설정"
if ! getent hosts "$PUBLIC_HOST" | grep -q "^$IP\b"; then
  echo "주의: $PUBLIC_HOST 가 아직 $IP 로 연결되지 않아요. 인증서 발급이 실패할 수 있어요."
fi
HOST_RE="$(printf '%s' "$PUBLIC_HOST" | sed 's/[.]/\\./g')"
TRUST=""
if [ -n "$GW_PEM" ]; then
  TRUST="tls_server_name localhost
				tls_trust_pool file /etc/caddy/ibkr-gateway.pem"
fi
sudo tee /etc/caddy/Caddyfile >/dev/null <<CADDY
# portfolio-manager: HTTPS front door. Written by deploy/install-https.sh.
{
	# Browsers send no server name (SNI) when they open an IP address, so say which
	# certificate to use then.
	default_sni $IP
	# HTTP/1.1 and HTTP/2 only: with HTTP/3 a browser may send cookies as separate
	# headers, and the IBKR Gateway behind Caddy expects them in one.
	servers {
		protocols h1 h2
	}
}

https://$PUBLIC_HOST {
	# The IBKR Gateway's login pages and API: only for people signed in to the web screen.
	@ibkr path /sso/* /css/* /fonts/* /images/* /scripts/* /portal.proxy/* /v1/api/*
	handle @ibkr {
		forward_auth 127.0.0.1:$PORT {
			uri /auth/check
		}
		reverse_proxy https://127.0.0.1:5000 {
			# Present requests the way a browser on https://localhost:5000 would make them
			# (the only setup IBKR documents), drop the visitor's address, and keep the web
			# screen's own login cookie away from IBKR.
			header_up Host localhost:5000
			header_up Origin "^https://$HOST_RE(:443)?$" "https://localhost:5000"
			header_up Referer "^https://$HOST_RE(:443)?/" "https://localhost:5000/"
			header_up -X-Forwarded-For
			header_up -X-Forwarded-Host
			header_up -X-Forwarded-Proto
			header_up -X-Real-IP
			header_up Cookie "pm_session=[^;]*;?\s*" ""
			header_down Location "^https://(localhost|127\.0\.0\.1):5000" "https://{host}"
			header_down Set-Cookie "(?i);\s*domain=localhost" ""
			transport http {
				$TRUST
			}
		}
	}

	handle {
		reverse_proxy 127.0.0.1:$PORT
	}

	header {
		Strict-Transport-Security "max-age=31536000"
		-Server
	}
}

# The bare IP address: move people to the name (IBKR's login needs a name).
https://$IP {
	tls {
		# Let's Encrypt issues IP-address certificates only with the 6-day profile.
		issuer acme {
			profile shortlived
		}
	}
	redir https://$PUBLIC_HOST{uri} 302
}
CADDY
sudo caddy fmt --overwrite /etc/caddy/Caddyfile >/dev/null 2>&1 || true
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1 | tail -2

# Share the server politely, like the other services.
sudo mkdir -p /etc/systemd/system/caddy.service.d
sudo tee /etc/systemd/system/caddy.service.d/limits.conf >/dev/null <<'UNIT'
[Service]
MemoryMax=192M
OOMScoreAdjust=200
UNIT
sudo systemctl daemon-reload
sudo systemctl enable caddy >/dev/null 2>&1
if systemctl is-active --quiet caddy; then sudo systemctl reload caddy; else sudo systemctl restart caddy; fi
if [ "$BEFORE" != "$(sha256sum config.yaml)" ] || ! systemctl is-active --quiet portfolio-ui; then
  sudo systemctl restart portfolio-ui  # signs everyone out of the web screen
fi
sleep 3
for s in caddy portfolio-ui; do printf '%s: %s\n' "$s" "$(systemctl is-active "$s")"; done

if [ -f web-auth.json ]; then
  PW_NOTE="비밀번호는 이미 정해져 있어요."
else
  PW_NOTE="비밀번호를 정하세요: cd $APP_DIR && .venv/bin/python -m rebalancer set-password"
fi
cat <<NEXT

다음 단계
  1. $PW_NOTE
  2. 브라우저: https://$PUBLIC_HOST (인증서는 처음 접속 전후로 몇십 초 안에 발급돼요)
  3. IBKR 세션이 끊기면 화면 위에 뜨는 'IBKR 로그인'을 누르세요.
  인증서 상태: sudo journalctl -u caddy -n 50 --no-pager
NEXT
