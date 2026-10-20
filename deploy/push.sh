#!/usr/bin/env bash
# Run this on your own computer: copy the project to a server over SSH and install it there.
#
#   bash deploy/push.sh <ssh 호스트> [서버 폴더, 기본 portfolio-manager]
#
# <ssh 호스트> is whatever you type after `ssh` to reach the server (an alias from
# ~/.ssh/config, or user@address). Running it again updates the code. On the server,
# config.yaml, portfolio.yaml (your saved targets), .env, the ChatGPT sign-in and the
# run history are never overwritten.
set -euo pipefail

HOST="${1:?사용법: bash deploy/push.sh <ssh 호스트> [서버 폴더]}"
DIR="${2:-portfolio-manager}"
cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "== 코드 보내기: $HOST:~/$DIR"
ssh "$HOST" "mkdir -p '$DIR'"
tar -czf - \
  --exclude=./.git --exclude=./.venv --exclude='__pycache__' --exclude=./.pytest_cache \
  --exclude='*.egg-info' --exclude=./.env --exclude='./state*.json' --exclude=./history \
  --exclude='./chatgpt-auth.json*' --exclude=./config.yaml --exclude=./portfolio.yaml \
  . | ssh "$HOST" "tar -xzf - -C '$DIR'"

for f in config.yaml portfolio.yaml; do
  if ssh "$HOST" "test -f '$DIR/$f'"; then
    echo "$f: 서버에 있는 파일을 그대로 둬요"
  else
    scp -q "$f" "$HOST:$DIR/$f"
    echo "$f: 처음이라 복사했어요"
  fi
done

echo "== 서버에서 설치"
ssh -t "$HOST" "cd '$DIR' && bash deploy/install.sh"
