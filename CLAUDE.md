# portfolio-manager

Jae Hyeok's personal app that keeps an IBKR account at target ETF weights (core/satellite portfolio, LSE USD lines). README.md (Korean) is the user guide; read it before changing behaviour.

## Layout

- `rebalancer/planner.py`: monthly buys (most underweight first, at most `max_buy_orders`) and rebalances
- `rebalancer/jobs.py`, `execute.py`: order jobs; every IBKR warning waits for a human answer
- `rebalancer/brokers/`: `mock` (example files) and `ibkr` (Client Portal Gateway on https://127.0.0.1:5000)
- `rebalancer/server.py` + `static/`: FastAPI web screen on 127.0.0.1 (Host check, per-run session token, CSP). From outside it is reached through Caddy (HTTPS) with a password (`webauth.py`)
- `rebalancer/assistant.py`, `llm.py`, `chatgpt_auth.py`: chat that edits the draft only; default provider is the user's ChatGPT plan via Sign in with ChatGPT
- `config.yaml` (rules, fees, broker, assistant), `portfolio.yaml` (targets; the web screen saves here and backs up to `history/`)
- `deploy/`: `push.sh` and `tunnel.sh` run on the PC; `install.sh`, `install-ibkr-gateway.sh` and `install-https.sh` run on the server

## Commands

- Tests: `.venv/bin/python -m pytest -q` (create the venv with `python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"`)
- Dry-run plan with the mock account: `python -m rebalancer plan`
- Web screen locally: `python -m rebalancer ui`

## Rules that must keep holding

- Orders go out only when the user presses the button in the plan dialog and confirms. The chat has no order tool and must never get one.
- Every IBKR order warning needs an explicit human answer. Never auto-confirm.
- The app never asks for or stores the IBKR username or password. Login happens on the Gateway page.
- Secrets stay out of git: `.env` (IBKR_ACCOUNT_ID, API keys) and `chatgpt-auth.json` (mode 600). Never print them.
- ChatGPT plan use goes through this app's own sign-in. Never reuse tokens from Codex or other apps.
- Never disable TLS verification. The Gateway on 127.0.0.1 has its own certificate (made by install-https.sh); Caddy and the app verify against it (`broker.ibkr.ca_file`).
- Anything reachable from outside goes through the password login. The Gateway's paths (`/sso/*`, `/v1/api/*`, ...) are only passed on after Caddy's `forward_auth` to `/auth/check`. Ports 8765 and 5000 stay closed to the internet.

## UI copy and replies

- Korean, polite 존댓말 (~해요), short plain sentences. No em dashes, no decorative emoji.
- Avoid Korean particles right after a variable value; write `SYM: ...` or use parentheses.

## Deployment

- Server: EC2 `openclaw-singapore` (`i-0d80fbe5857aa1141`, ap-southeast-1, Ubuntu 22.04, user `ubuntu`), Elastic IP 54.255.220.60. The user's key is `~/.ssh/openclaw-sg-key.pem` on the PC. The AWS CLI on the PC is configured for this account.
- Web screen: https://54.255.220.60 (Caddy, Let's Encrypt 6-day IP certificate, `default_sni` set because browsers send no SNI for IPs). Security group `sg-080a2968d0cd3c9e9` allows 22, 80, 443 (and an unrelated 3000).
- The Hermes agent runs on the same server. Leave `~/.hermes` and `hermes-gateway.service` alone. About 1.6 GiB RAM is free, so our services are capped (portfolio-ui 256M, ibkr-gateway 640M, caddy 192M) and yield first under memory pressure.
- Update code from the PC repo folder: `SSH_KEY=~/.ssh/openclaw-sg-key.pem bash deploy/push.sh ubuntu@54.255.220.60`. Re-running updates code only; the server's `config.yaml`, `portfolio.yaml`, `.env`, `chatgpt-auth.json`, `web-auth.json` and `history/` are kept.
- ChatGPT sign-in from the public address: the return address stays http://127.0.0.1:8765/auth/callback (loopback, as OpenAI requires); the user pastes it into the chat panel (`/api/chatgpt/callback-url`).

## Status (2026-10-20)

- Running on the server: `portfolio-ui`, `ibkr-gateway`, `caddy`. Broker is `ibkr` on the live account (`IBKR_ACCOUNT_ID` in the server's `.env`). A paper account exists but was not usable yet.
- The web password is not set yet: until the user runs `set-password` on the server, https://54.255.220.60 refuses visitors.
- The Gateway was restarted for its new certificate, so IBKR needs a fresh login (the banner's 'IBKR 로그인').
- IBKR's first Gateway login failed with `sso/validate` Access Denied; it worked after the user turned off Read-Only Access (Settings > Trading Platform).
- conids are filled in the server's `portfolio.yaml` (LSEETF USD lines): VWRA 375855577, AGGU 297482848, IGLN 86656182, SMH 458512500, ZAPP 880582689, WDGF 868938564. Avoid the NASDAQ listings of SMH and WDGF.
- Account cash is SGD only, so planning stops ("USD 현금 정보를 찾을 수 없어요") until the user converts SGD to USD in IBKR.
- IBKR Pro with Tiered pricing (config default). LSE UK (L1) market data subscribed. UK trading permission status unconfirmed.
- ChatGPT is signed in (done earlier through the tunnel). Monthly contributions of about USD 3,000 start in January 2027.
