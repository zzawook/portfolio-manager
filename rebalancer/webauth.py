"""Password login for the web screen when it is reachable from the internet.

- The password is stored only as a scrypt hash in web-auth.json (mode 600, kept out of
  git). It is set on the server with `python -m rebalancer set-password`.
- A successful login gets a random session id in an HttpOnly cookie. Sessions live in
  memory: they end after `IDLE_HOURS` without use, after `MAX_DAYS` in any case, when the
  password changes, and when the server restarts.
- Failed attempts are limited per client and in total, so the password cannot be
  guessed by trying many times.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

COOKIE_NAME = "pm_session"
IDLE_HOURS = 12
MAX_DAYS = 7
MIN_PASSWORD_LENGTH = 10

# Brute-force limits.
CLIENT_FAILURES = 5  # failures per client ...
CLIENT_WINDOW = 15 * 60  # ... within this many seconds lock that client
TOTAL_FAILURES = 30  # failures from everyone ...
TOTAL_WINDOW = 60 * 60  # ... within this many seconds lock the login for everyone
LOCK_SECONDS = 15 * 60

# scrypt parameters: about 16 MB of memory and a few tens of milliseconds per check.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1


class LoginError(Exception):
    """A login that did not succeed. The message is safe to show."""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def hash_password(password: str, salt: Optional[bytes] = None) -> dict:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return {"scheme": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P, "salt": _b64(salt), "hash": _b64(digest)}


def check_password(password: str, record: dict) -> bool:
    try:
        if record.get("scheme") != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_unb64(record["salt"]),
            n=int(record["n"]),
            r=int(record["r"]),
            p=int(record["p"]),
            dklen=32,
        )
        return hmac.compare_digest(digest, _unb64(record["hash"]))
    except (KeyError, ValueError, TypeError):
        return False


def password_problem(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"비밀번호는 {MIN_PASSWORD_LENGTH}자 이상이어야 해요."
    if password.strip() != password:
        return "비밀번호 앞뒤에 공백을 넣지 마세요."
    return ""


@dataclass
class _Session:
    created: float
    last_used: float
    password_version: str


class WebAuth:
    def __init__(self, path: str | Path, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.clock = clock
        self._lock = threading.Lock()
        self._sessions: dict[str, _Session] = {}
        self._failures: dict[str, list[float]] = {}
        self._all_failures: list[float] = []
        self._locked_until: dict[str, float] = {}
        self._all_locked_until = 0.0

    # The stored password ------------------------------------------------------------

    def _record(self) -> Optional[dict]:
        # Read on every use, so `set-password` takes effect without a restart.
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("hash") else None

    @property
    def configured(self) -> bool:
        return self._record() is not None

    def _version(self, record: dict) -> str:
        return str(record.get("hash", ""))[:16]

    def set_password(self, password: str) -> None:
        problem = password_problem(password)
        if problem:
            raise LoginError(problem)
        record = {**hash_password(password), "updated_at": int(self.clock())}
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        with self._lock:
            self._sessions.clear()  # a new password signs everyone out

    # Logging in ---------------------------------------------------------------------

    def _prune(self, now: float) -> None:
        for key in list(self._failures):
            self._failures[key] = [t for t in self._failures[key] if now - t < CLIENT_WINDOW]
            if not self._failures[key]:
                del self._failures[key]
        self._all_failures = [t for t in self._all_failures if now - t < TOTAL_WINDOW]

    def _wait_message(self, until: float, now: float) -> str:
        minutes = max(1, int((until - now + 59) // 60))
        return f"로그인 시도가 너무 많아요. {minutes}분 뒤에 다시 해 주세요."

    def login(self, password: str, client: str) -> str:
        """Check the password and return a new session id."""
        record = self._record()
        if record is None:
            raise LoginError("아직 비밀번호가 없어요. 서버에서 set-password로 먼저 정해 주세요.")
        now = self.clock()
        with self._lock:
            self._prune(now)
            if now < self._all_locked_until:
                raise LoginError(self._wait_message(self._all_locked_until, now))
            if now < self._locked_until.get(client, 0.0):
                raise LoginError(self._wait_message(self._locked_until[client], now))
        ok = check_password(password, record)  # slow on purpose; done outside the lock
        with self._lock:
            if not ok:
                self._failures.setdefault(client, []).append(now)
                self._all_failures.append(now)
                if len(self._failures[client]) >= CLIENT_FAILURES:
                    self._locked_until[client] = now + LOCK_SECONDS
                    self._failures.pop(client, None)
                if len(self._all_failures) >= TOTAL_FAILURES:
                    self._all_locked_until = now + LOCK_SECONDS
                    self._all_failures.clear()
                raise LoginError("비밀번호가 맞지 않아요.")
            self._failures.pop(client, None)
            self._locked_until.pop(client, None)
            sid = secrets.token_urlsafe(32)
            self._sessions[sid] = _Session(created=now, last_used=now, password_version=self._version(record))
            return sid

    def check(self, sid: str) -> bool:
        """True if the session id is valid. Using it keeps it alive."""
        if not sid:
            return False
        record = self._record()
        if record is None:
            return False
        now = self.clock()
        with self._lock:
            s = self._sessions.get(sid)
            if s is None:
                return False
            expired = (
                now - s.last_used > IDLE_HOURS * 3600
                or now - s.created > MAX_DAYS * 86400
                or s.password_version != self._version(record)
            )
            if expired:
                del self._sessions[sid]
                return False
            s.last_used = now
            return True

    def logout(self, sid: str) -> None:
        with self._lock:
            self._sessions.pop(sid, None)
