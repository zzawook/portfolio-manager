# portfolio-manager

Jae Hyeok's personal app that keeps an IBKR account at target ETF weights (core/satellite portfolio, LSE USD lines). README.md (Korean) is the user guide; read it before changing behaviour.

## Layout

- `rebalancer/planner.py`: monthly buys (most underweight first, at most `max_buy_orders`) and rebalances
- `rebalancer/jobs.py`, `execute.py`: order jobs; every IBKR warning waits for a human answer
- `rebalancer/brokers/`: `mock` (example files) and `ibkr` (Client Portal Gateway on https://127.0.0.1:5000)
- `rebalancer/server.py` + `static/`: FastAPI web screen on 127.0.0.1 (Host check, per-run session token, CSP)
- `rebalancer/assistant.py`, `llm.py`, `chatgpt_auth.py`: chat that edits the draft only; default provider is the user's ChatGPT plan via Sign in with ChatGPT
- `config.yaml` (rules, fees, broker, assistant), `portfolio.yaml` (targets; the web screen saves here and backs up to `history/`)
- `deploy/`: `push.sh` and `tunnel.sh` run on the PC, `install.sh` and `install-ibkr-gateway.sh` run on the server

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
- Never disable TLS verification.

## UI copy and replies

- Korean, polite 존댓말 (~해요), short plain sentences. No em dashes, no decorative emoji.
- Avoid Korean particles right after a variable value; write `SYM: ...` or use parentheses.

## Deployment

- Server: EC2 `openclaw-singapore` (ap-southeast-1, Ubuntu, user `ubuntu`). The user supplies the host and key. The public IP changes when the instance is stopped and started.
- The Hermes agent runs on the same server. Leave `~/.hermes` and `hermes-gateway.service` alone. About 1.6 GiB RAM is free, so our services are capped (portfolio-ui 256M, ibkr-gateway 640M) and yield first under memory pressure.
- Install or update from the PC repo folder: `SSH_KEY=<key.pem> bash deploy/push.sh ubuntu@<host>`. Re-running updates code only; the server's `config.yaml`, `portfolio.yaml`, `.env`, `chatgpt-auth.json` and `history/` are kept.
- Use it: `SSH_KEY=<key.pem> bash deploy/tunnel.sh ubuntu@<host>`, then http://127.0.0.1:8765 and 'ChatGPT로 로그인' in the chat panel. Same-port tunnel matters: the sign-in callback is http://127.0.0.1:8765/auth/callback.
- Never open ports in the security group. Everything goes through the SSH tunnel.

## Status (2026-10-20)

- Code is on GitHub `main`. Not yet deployed to the server.
- IBKR Pro with Tiered pricing (the `fees` default in config.yaml is Tiered). UK (LSE) trading permission not granted yet.
- Next: deploy and sign in to ChatGPT; IBKR paper account; `bash deploy/install-ibkr-gateway.sh` on the server; set `IBKR_ACCOUNT_ID` and `broker.type: ibkr`; look up conids in the web screen; LSE UK (L1) market data (GBP 1/month) once UK trading is open. Monthly contributions of about USD 3,000 start in January 2027.
