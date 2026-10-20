"""Sign in with ChatGPT: let the in-app chat use a ChatGPT Plus or Pro plan.

Since September 2026 OpenAI lets open-source and personal self-hosted apps send
Responses API requests on the user's ChatGPT plan instead of an API key (developer
preview). The flow, from https://developers.openai.com/siwc/token-sharing-open-source :

1. The app opens https://auth.openai.com/api/accounts/authorize with PKCE, state and
   nonce, the scopes below, and `client_id=dynamic_agent_client` the first time.
   OpenAI then issues a client id for this app and user.
2. ChatGPT sends the browser back to http://127.0.0.1:<port>/auth/callback. When the
   app runs on a server, that address reaches it through the SSH tunnel that already
   carries the web screen, so the tunnel must use the same port on both ends.
3. The app exchanges the code at https://auth.openai.com/api/accounts/oauth/token and
   keeps the tokens in chatgpt-auth.json next to config.yaml (owner-only, not in git).
4. Requests go to https://api.openai.com/v1/responses with the access token. Access
   tokens last an hour; the refresh token renews them.

No API key and no client secret are involved. Usage counts against the ChatGPT plan.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote, urlencode

import requests

AUTHORIZE_URL = "https://auth.openai.com/api/accounts/authorize"
TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
DISCOVERY_URL = "https://auth.openai.com/.well-known/openid-configuration"
ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
DYNAMIC_CLIENT = "dynamic_agent_client"
APP_NAME = "portfolio-manager"
CALLBACK_PATH = "/auth/callback"
PENDING_TTL = 10 * 60
REFRESH_MARGIN = 120  # refresh this many seconds before the access token runs out


class ChatGPTAuthError(RuntimeError):
    """Sign-in or token problem. The message is written for the person using the app."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _jwt_claims(token: Optional[str]) -> dict:
    """Read the claims of a JWT. The signature is not checked here: the ID token comes
    straight from the token endpoint over TLS, which OpenID Connect allows in place of
    signature validation (Core 1.0, section 3.1.3.7)."""
    if not token or token.count(".") != 2:
        return {}
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, json.JSONDecodeError):
        return {}


def _epoch(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class ChatGPTAuth:
    """One ChatGPT sign-in for this host, kept in a small JSON file."""

    def __init__(self, path: str | Path, session: Optional[requests.Session] = None, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.session = session or requests.Session()
        self.clock = clock
        self._lock = threading.Lock()
        self._pending: dict[str, dict] = {}
        self.data: dict[str, Any] = self._load()
        if not self.data.get("host_id"):
            # Stable and opaque per host, as the docs ask; never an email or user id.
            self.data["host_id"] = f"urn:uuid:{uuid.uuid4()}"
            self._save()

    # Storage -----------------------------------------------------------------

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    # Status ------------------------------------------------------------------

    @property
    def granted_scopes(self) -> set[str]:
        return set(str(self.data.get("scope") or "").split())

    @property
    def signed_in(self) -> bool:
        return bool(self.data.get("access_token") or self.data.get("refresh_token"))

    @property
    def plan_ready(self) -> bool:
        return self.signed_in and PLAN_SCOPE in self.granted_scopes

    def status(self) -> dict:
        if not self.signed_in:
            reason = "ChatGPT 계정으로 로그인하면 채팅이 켜져요. Plus나 Pro 플랜 사용량을 써요."
        elif not self.plan_ready:
            reason = "로그인은 됐지만 플랜 사용 권한이 없어요. 다시 로그인할 때 플랜 사용을 허용해 주세요. Plus나 Pro 플랜만 쓸 수 있어요."
        else:
            reason = ""
        return {
            "kind": "chatgpt",
            "signed_in": self.signed_in,
            "plan_usage": self.plan_ready,
            "email": self.data.get("email") or "",
            "reason": reason,
        }

    # Sign-in -----------------------------------------------------------------

    def start(self, redirect_uri: str) -> str:
        """Build the authorize URL for a new sign-in and remember what the callback must match."""
        verifier = _b64url(secrets.token_bytes(48))
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)
        client_id = self.data.get("client_id") or DYNAMIC_CLIENT
        params = {
            "client_id": client_id,
            "ext_agent_host_id": self.data["host_id"],
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "resource": RESOURCE,
            "state": state,
            "nonce": nonce,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
        if client_id == DYNAMIC_CLIENT:
            params["agent_name_hint"] = APP_NAME
        elif self.data.get("id_token"):
            params["id_token_hint"] = self.data["id_token"]
        if self.data.get("email"):
            params["login_hint"] = self.data["email"]
        now = self.clock()
        with self._lock:
            self._pending = {k: v for k, v in self._pending.items() if now - v["created"] < PENDING_TTL}
            self._pending[state] = {
                "verifier": verifier,
                "nonce": nonce,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "created": now,
            }
        return f"{AUTHORIZE_URL}?{urlencode(params, quote_via=quote)}"

    def finish(self, params: dict) -> dict:
        """Handle the browser's return to /auth/callback. Returns the new status."""
        state = str(params.get("state") or "")
        with self._lock:
            pending = self._pending.pop(state, None)
        if pending is None or self.clock() - pending["created"] > PENDING_TTL:
            raise ChatGPTAuthError("로그인 요청을 찾을 수 없어요. 화면에서 'ChatGPT로 로그인'을 다시 눌러 주세요.")
        if params.get("error"):
            if params.get("error") == "access_denied":
                raise ChatGPTAuthError("ChatGPT 로그인을 취소했어요.")
            raise ChatGPTAuthError(f"ChatGPT 로그인이 실패했어요 ({params.get('error')}).")
        code = params.get("code")
        if not code:
            raise ChatGPTAuthError("로그인 응답에 code가 없어요. 다시 시도해 주세요.")

        client_id = pending["client_id"]
        issued = params.get("client_id")
        if client_id == DYNAMIC_CLIENT:
            if not issued or issued == DYNAMIC_CLIENT:
                raise ChatGPTAuthError("ChatGPT가 이 앱을 등록하지 못했어요. 다시 로그인해 주세요.")
            client_id = str(issued)
        elif issued and issued != client_id:
            raise ChatGPTAuthError("ChatGPT가 다른 앱 등록 정보를 보냈어요. 안전을 위해 로그인을 멈췄어요.")

        tokens = self._token_request(
            {
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "code_verifier": pending["verifier"],
                "redirect_uri": pending["redirect_uri"],
                "resource": RESOURCE,
            }
        )
        claims = _jwt_claims(tokens.get("id_token"))
        self._check_id_token(claims, client_id, pending["nonce"])
        with self._lock:
            self.data["client_id"] = client_id  # saved only after a successful exchange
            self._store_tokens(tokens)
            self.data["sub"] = claims.get("sub", "")
            self.data["email"] = claims.get("email", self.data.get("email", ""))
            self._save()
        return self.status()

    def _check_id_token(self, claims: dict, client_id: str, nonce: str) -> None:
        if not claims:
            raise ChatGPTAuthError("ChatGPT가 보낸 ID 토큰을 읽을 수 없어요. 다시 로그인해 주세요.")
        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        problems = []
        if str(claims.get("iss", "")).rstrip("/") != ISSUER:
            problems.append("issuer")
        if client_id not in audiences:
            problems.append("audience")
        if claims.get("nonce") != nonce:
            problems.append("nonce")
        exp = _epoch(claims.get("exp"))
        if exp is not None and exp < self.clock() - 60:
            problems.append("expired")
        if problems:
            raise ChatGPTAuthError(f"ChatGPT 로그인 응답을 확인하지 못했어요 ({', '.join(problems)}). 다시 로그인해 주세요.")

    def _store_tokens(self, tokens: dict) -> None:
        now = self.clock()
        self.data["access_token"] = tokens.get("access_token", "")
        if tokens.get("refresh_token"):
            self.data["refresh_token"] = tokens["refresh_token"]  # rotates: always keep the newest
        if tokens.get("id_token"):
            self.data["id_token"] = tokens["id_token"]
        if tokens.get("scope"):
            self.data["scope"] = tokens["scope"]
        expires_in = tokens.get("expires_in")
        self.data["expires_at"] = now + float(expires_in) if expires_in else now + 3600
        self.data["earliest_refresh_at"] = _epoch(tokens.get("earliest_refresh_at"))

    def _token_request(self, form: dict) -> dict:
        try:
            resp = self.session.post(TOKEN_URL, data=form, headers={"Accept": "application/json"}, timeout=30)
        except requests.RequestException as e:
            raise ChatGPTAuthError(f"ChatGPT 로그인 서버에 연결할 수 없어요 ({type(e).__name__}).") from e
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400:
            error = body.get("error") if isinstance(body, dict) else None
            if error == "invalid_grant":
                raise ChatGPTAuthError("ChatGPT 로그인이 만료됐어요. 'ChatGPT로 로그인'을 다시 눌러 주세요.")
            raise ChatGPTAuthError(f"ChatGPT 로그인 서버가 거절했어요 (HTTP {resp.status_code} {error or ''}).".replace(" )", ")"))
        if not isinstance(body, dict) or not body.get("access_token"):
            raise ChatGPTAuthError("ChatGPT 로그인 서버에서 토큰이 오지 않았어요.")
        return body

    # Using the tokens --------------------------------------------------------------

    def access_token(self) -> str:
        """A valid access token, refreshed when it is about to run out."""
        with self._lock:
            if not self.signed_in:
                raise ChatGPTAuthError("ChatGPT에 로그인돼 있지 않아요. 채팅 창의 'ChatGPT로 로그인'을 눌러 주세요.")
            now = self.clock()
            expires_at = float(self.data.get("expires_at") or 0)
            if self.data.get("access_token") and now < expires_at - REFRESH_MARGIN:
                return self.data["access_token"]
            earliest = self.data.get("earliest_refresh_at")
            if earliest and now < float(earliest) and self.data.get("access_token") and now < expires_at:
                return self.data["access_token"]  # too early to refresh, and still valid
            refresh = self.data.get("refresh_token")
            if not refresh:
                self._clear_tokens()
                raise ChatGPTAuthError("ChatGPT 로그인이 만료됐어요. 'ChatGPT로 로그인'을 다시 눌러 주세요.")
            try:
                tokens = self._token_request(
                    {
                        "grant_type": "refresh_token",
                        "client_id": self.data.get("client_id", ""),
                        "refresh_token": refresh,
                        "resource": RESOURCE,
                    }
                )
            except ChatGPTAuthError:
                if now >= expires_at:
                    self._clear_tokens()
                raise
            self._store_tokens(tokens)
            self._save()
            return self.data["access_token"]

    def invalidate_access(self) -> None:
        """The API refused the access token: refresh it before the next request."""
        with self._lock:
            self.data["expires_at"] = 0
            self.data["earliest_refresh_at"] = None

    def _clear_tokens(self) -> None:
        # Keep host_id, client_id and the last ID token: the next sign-in reuses them.
        for key in ("access_token", "refresh_token", "scope", "expires_at", "earliest_refresh_at"):
            self.data.pop(key, None)
        self._save()

    def sign_out(self) -> bool:
        """Revoke the refresh token (best effort) and forget the tokens. True if revocation was confirmed."""
        with self._lock:
            refresh = self.data.get("refresh_token")
            client_id = self.data.get("client_id")
            confirmed = False
            if refresh and client_id:
                try:
                    endpoint = self.session.get(DISCOVERY_URL, timeout=15).json().get("revocation_endpoint")
                    if endpoint:
                        resp = self.session.post(
                            endpoint,
                            data={"token": refresh, "token_type_hint": "refresh_token", "client_id": client_id},
                            timeout=15,
                        )
                        confirmed = resp.status_code == 200
                except (requests.RequestException, ValueError, AttributeError):
                    confirmed = False
            self._clear_tokens()
            return confirmed
