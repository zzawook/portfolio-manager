#!/usr/bin/env bash
# Run this on your own computer: copy the project to a server over SSH and install it there.
#
#   bash deploy/push.sh <ssh 호스트> [서버 폴더, 기본 portfolio-manager]
#
# <ssh 호스트> is whatever you type after `ssh` to reach the server (an alias from
# ~/.ssh/config, or user@address). If you connect with a key file (ssh -i key.pem),
# pass it as SSH_KEY:
#
#   SSH_KEY=~/keys/hermes.pem bash deploy/push.sh ubuntu@<서버 주소>
#
# Running it again updates the code. On the server, config.yaml, portfolio.yaml (your
# saved targets), .env, the ChatGPT sign-in, the web password and the run history are never overwritten.
set -euo pipefail

HOST="${1:?사용법: bash deploy/push.sh <ssh 호스트> [서버 폴더]}"
DIR="${2:-portfolio-manager}"
cd "$(dirname "${BASH_SOURCE[0]}")/.."

SSH_ARGS=()
if [ -n "${SSH_KEY:-}" ]; then
  SSH_ARGS=(-i "$SSH_KEY")
fi
remote() { ssh ${SSH_ARGS[@]+"${SSH_ARGS[@]}"} "$@"; }

echo "== 코드 보내기: $HOST:~/$DIR"
remote "$HOST" "mkdir -p '$DIR'"
tar -czf - \
  --exclude=./.git --exclude=./.venv --exclude='__pycache__' --exclude=./.pytest_cache \
  --exclude='*.egg-info' --exclude=./.ruff_cache --exclude=./.env --exclude='./state*.json' --exclude=./history \
  --exclude='./chatgpt-auth.json*' --exclude='./web-auth.json*' --exclude=./config.yaml --exclude=./portfolio.yaml \
  . | remote "$HOST" "tar -xzf - -C '$DIR'"

for f in config.yaml portfolio.yaml; do
  if remote "$HOST" "test -f '$DIR/$f'"; then
    echo "$f: 서버에 있는 파일을 그대로 둬요"
  else
    scp ${SSH_ARGS[@]+"${SSH_ARGS[@]}"} -q "$f" "$HOST:$DIR/$f"
    echo "$f: 처음이라 복사했어요"
  fi
done

echo "== 서버에서 설치"
remote -t "$HOST" "cd '$DIR' && bash deploy/install.sh"
