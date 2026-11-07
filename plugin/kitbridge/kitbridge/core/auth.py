# ##### BEGIN GPL LICENSE BLOCK #####
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either version 3
#  of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, see <https://www.gnu.org/licenses/>.
#
#  Original code Copyright (C) BlenderKit developers (bk_maya / bk_client).
#  Adapted for Kitbridge.
#
# ##### END GPL LICENSE BLOCK #####

"""OAuth2 PKCE authentication, token persistence, and non-blocking token refresh."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import string
import threading
import time
import urllib.parse
import webbrowser
from typing import Any, Callable, Dict, Optional, Tuple

from . import secret_store
from .client import DEFAULT_SERVER, BKClient
from .tasks import Dispatcher, TaskDict

log = logging.getLogger("kitbridge")

# BlenderKit's public OAuth2 application client_id (shared by the Blender/Maya/Rhino add-ons
# and hardcoded in bk_client/client/constants.go).
# NOTE: Ask BlenderKit for a dedicated Kitbridge client_id before public release.
CLIENT_ID = "IdFRwa3SGA8eMpzhRVFMg5Ts8sPK93xBjif93x0F"

TOKEN_SECRET_KEY = "tokens"
TOKEN_REFRESH_MARGIN_SECONDS = 3 * 24 * 3600  # Refresh 3 days before expiry
_REFRESH_COOLDOWN_SECONDS = 60.0

_PKCE_ALPHABET = string.ascii_letters + string.digits

_state_lock = threading.Lock()
_last_refresh_attempt: float = 0.0
_cached_profile: Optional[Dict[str, Any]] = None


# ── PKCE S256 ────────────────────────────────────────────────────────────────


def generate_pkce_verifier(length: int = 128) -> str:
    """Generate a RFC 7636 PKCE ``code_verifier`` of *length* characters (43..128)."""
    if not (43 <= length <= 128):
        raise ValueError("PKCE code_verifier length must be between 43 and 128 characters")
    return "".join(secrets.choice(_PKCE_ALPHABET) for _ in range(length))


def compute_pkce_challenge(code_verifier: str) -> str:
    """Compute the RFC 7636 S256 ``code_challenge`` for *code_verifier*."""
    if not code_verifier:
        raise ValueError("code_verifier must be non-empty")
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def generate_pkce_pair(length: int = 128) -> Tuple[str, str]:
    """Return a ``(code_verifier, code_challenge)`` tuple for OAuth2 PKCE S256."""
    verifier = generate_pkce_verifier(length=length)
    challenge = compute_pkce_challenge(verifier)
    return verifier, challenge


def generate_oauth_state(nbytes: int = 16) -> str:
    """Generate a random OAuth2 ``state`` parameter."""
    return secrets.token_urlsafe(nbytes)


def build_authorize_url(
    port: str,
    code_challenge: str,
    state: str,
    *,
    server: str = DEFAULT_SERVER,
    client_id: str = CLIENT_ID,
) -> str:
    """Build the browser OAuth2 authorization URL targeting localhost *port*."""
    redirect_uri = f"http://localhost:{port}/consumer/exchange/"
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    base = server.rstrip("/")
    return f"{base}/o/authorize/?{urllib.parse.urlencode(params)}"


# ── Token Persistence ────────────────────────────────────────────────────────


def load_tokens(
    *,
    backend: Optional[str] = None,
    file_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Load token metadata from ``secret_store``. Returns an empty dict if none."""
    raw = secret_store.get_secret(TOKEN_SECRET_KEY, backend=backend, file_path=file_path)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Stored token JSON is malformed; ignoring.")
        return {}
    return parsed if isinstance(parsed, dict) else {}


def save_tokens(
    access_token: str,
    refresh_token: str = "",
    expires_in: int = 0,
    *,
    expires_at: Optional[int] = None,
    backend: Optional[str] = None,
    file_path: Optional[str] = None,
) -> bool:
    """Persist token metadata to ``secret_store`` without logging any token values."""
    token_clean = (access_token or "").strip()
    if not token_clean:
        raise ValueError("access_token must be non-empty")

    exp_in = max(0, int(expires_in or 0))
    if expires_at is not None:
        exp_at = max(0, int(expires_at))
    else:
        exp_at = int(time.time()) + exp_in if exp_in > 0 else 0

    payload: Dict[str, Any] = {
        "access_token": token_clean,
        "refresh_token": (refresh_token or "").strip(),
        "expires_in": exp_in,
        "expires_at": exp_at,
    }
    ok = secret_store.set_secret(
        TOKEN_SECRET_KEY,
        json.dumps(payload),
        backend=backend,
        file_path=file_path,
    )
    if ok:
        log.info("Saved authentication credentials to secret store.")
    return ok


def set_manual_api_key(
    api_key: str,
    *,
    backend: Optional[str] = None,
    file_path: Optional[str] = None,
) -> bool:
    """Store a manually pasted BlenderKit API key in ``secret_store``."""
    cleaned = (api_key or "").strip()
    if not cleaned:
        raise ValueError("api_key must not be empty")
    return save_tokens(
        access_token=cleaned,
        refresh_token="",
        expires_in=0,
        expires_at=0,
        backend=backend,
        file_path=file_path,
    )


def clear_tokens(
    *,
    backend: Optional[str] = None,
    file_path: Optional[str] = None,
) -> None:
    """Delete stored tokens from ``secret_store`` and clear cached profile."""
    global _cached_profile, _last_refresh_attempt
    secret_store.delete_secret(TOKEN_SECRET_KEY, backend=backend, file_path=file_path)
    with _state_lock:
        _cached_profile = None
        _last_refresh_attempt = 0.0
    log.info("Cleared stored authentication credentials.")


def is_logged_in(
    *,
    backend: Optional[str] = None,
    file_path: Optional[str] = None,
) -> bool:
    """Return ``True`` if a non-empty access token is stored."""
    tokens = load_tokens(backend=backend, file_path=file_path)
    return bool(tokens.get("access_token"))


def needs_token_refresh(
    tokens: Dict[str, Any],
    *,
    now: Optional[float] = None,
) -> bool:
    """Return ``True`` if *tokens* has a refresh_token and is within 3 days of expiry."""
    refresh_tok = str(tokens.get("refresh_token") or "")
    expires_at = int(tokens.get("expires_at") or 0)
    if not refresh_tok or expires_at <= 0:
        return False
    current_time = time.time() if now is None else now
    return current_time >= (expires_at - TOKEN_REFRESH_MARGIN_SECONDS)


def _trigger_nonblocking_refresh(
    client: BKClient,
    refresh_token_str: str,
    api_key: str,
) -> None:
    global _last_refresh_attempt
    now = time.monotonic()
    with _state_lock:
        if _last_refresh_attempt > 0 and (now - _last_refresh_attempt) < _REFRESH_COOLDOWN_SECONDS:
            return
        _last_refresh_attempt = now

    def _worker() -> None:
        try:
            client.refresh_token(refresh_token_str, api_key=api_key)
            log.info("Requested asynchronous OAuth token refresh via bk_client.")
        except Exception as exc:
            log.warning("Non-blocking token refresh request failed: %s", exc)

    threading.Thread(
        target=_worker,
        name="kitbridge-token-refresh",
        daemon=True,
    ).start()


def get_api_key(
    client: Optional[BKClient] = None,
    *,
    backend: Optional[str] = None,
    file_path: Optional[str] = None,
) -> str:
    """Return the current access token (or ``""``) and trigger non-blocking refresh if due."""
    tokens = load_tokens(backend=backend, file_path=file_path)
    access_token = str(tokens.get("access_token") or "")
    if not access_token:
        return ""

    if client is not None and needs_token_refresh(tokens):
        refresh_tok = str(tokens.get("refresh_token") or "")
        if refresh_tok:
            _trigger_nonblocking_refresh(client, refresh_tok, access_token)

    return access_token


# ── Login / Logout / Profile Flows ───────────────────────────────────────────


def start_login(
    client: BKClient,
    *,
    open_browser: bool = True,
    browser_opener: Optional[Callable[[str], Any]] = None,
) -> str:
    """Initiate OAuth2 PKCE login with *client* and optionally open the system browser.

    Returns the authorization URL.
    """
    port = client.ensure_running()
    code_verifier, code_challenge = generate_pkce_pair()
    state = generate_oauth_state()

    client.oauth2_verification_data(code_verifier=code_verifier, state=state)
    auth_url = build_authorize_url(
        port=port,
        code_challenge=code_challenge,
        state=state,
        server=client.server,
    )
    log.info("Prepared OAuth2 PKCE login flow on loopback port %s", port)
    if open_browser:
        opener = browser_opener or webbrowser.open
        try:
            opener(auth_url)
        except Exception as exc:
            log.warning("Failed to open system browser for OAuth2 login: %s", exc)
    return auth_url


def logout(
    client: Optional[BKClient] = None,
    *,
    backend: Optional[str] = None,
    file_path: Optional[str] = None,
) -> None:
    """Revoke OAuth tokens via *client* (if available) and delete local credentials."""
    tokens = load_tokens(backend=backend, file_path=file_path)
    access_token = str(tokens.get("access_token") or "")
    refresh_token_str = str(tokens.get("refresh_token") or "")
    clear_tokens(backend=backend, file_path=file_path)

    if refresh_token_str and client is not None:
        try:
            client.oauth2_logout(refresh_token_str, api_key=access_token)
        except Exception as exc:
            log.debug("Server-side oauth2/logout call failed: %s", exc)


def request_user_profile(
    client: BKClient,
    *,
    api_key: Optional[str] = None,
    backend: Optional[str] = None,
    file_path: Optional[str] = None,
) -> bool:
    """Schedule an asynchronous user profile fetch if an API key is available."""
    key = api_key if api_key is not None else get_api_key(client, backend=backend, file_path=file_path)
    if not key:
        return False
    try:
        client.get_user_profile(api_key=key)
        return True
    except Exception as exc:
        log.warning("Failed to request user profile: %s", exc)
        return False


def get_cached_profile() -> Optional[Dict[str, Any]]:
    """Return a shallow copy of the most recently fetched user profile, if any."""
    with _state_lock:
        return dict(_cached_profile) if _cached_profile is not None else None


# ── Dispatcher Task Handlers ─────────────────────────────────────────────────


def _extract_tokens_from_result(result: Dict[str, Any]) -> Tuple[str, str, int]:
    access_token = str(result.get("access_token") or "").strip()
    refresh_token = str(result.get("refresh_token") or "").strip()
    try:
        expires_in = int(result.get("expires_in") or 0)
    except (TypeError, ValueError):
        expires_in = 0
    return access_token, refresh_token, expires_in


def handle_login_task(
    task: TaskDict,
    *,
    client: Optional[BKClient] = None,
    backend: Optional[str] = None,
    file_path: Optional[str] = None,
) -> None:
    """Handle a ``login`` task reported by ``bk_client``."""
    status = str(task.get("status") or "")
    if status == "finished":
        result = task.get("result")
        if isinstance(result, dict):
            access_token, refresh_token, expires_in = _extract_tokens_from_result(result)
            if access_token:
                save_tokens(
                    access_token=access_token,
                    refresh_token=refresh_token,
                    expires_in=expires_in,
                    backend=backend,
                    file_path=file_path,
                )
                if client is not None:
                    request_user_profile(
                        client,
                        api_key=access_token,
                        backend=backend,
                        file_path=file_path,
                    )
                return
        log.error("Login task finished without an access_token in result.")
    elif status == "error":
        log.error("OAuth login task failed: %s", task.get("message") or "unknown error")


def handle_token_refresh_task(
    task: TaskDict,
    *,
    backend: Optional[str] = None,
    file_path: Optional[str] = None,
) -> None:
    """Handle a ``token_refresh`` task reported by ``bk_client``."""
    status = str(task.get("status") or "")
    if status == "finished":
        result = task.get("result")
        if isinstance(result, dict):
            access_token, refresh_token, expires_in = _extract_tokens_from_result(result)
            if access_token:
                save_tokens(
                    access_token=access_token,
                    refresh_token=refresh_token,
                    expires_in=expires_in,
                    backend=backend,
                    file_path=file_path,
                )
                log.info("OAuth token refresh succeeded.")
                return
        log.warning("token_refresh task finished without an access_token.")
    elif status == "error":
        log.warning("OAuth token refresh failed: %s", task.get("message") or "unknown error")


def handle_logout_task(task: TaskDict) -> None:
    """Handle an ``oauth2/logout`` task reported by ``bk_client``."""
    status = str(task.get("status") or "")
    if status == "finished":
        log.info("Server confirmed OAuth2 logout.")
    elif status == "error":
        log.debug("OAuth2 logout task reported error: %s", task.get("message") or "")


def handle_user_profile_task(task: TaskDict) -> None:
    """Handle a ``profiles/get_user_profile`` task reported by ``bk_client``."""
    global _cached_profile
    status = str(task.get("status") or "")
    if status == "finished":
        result = task.get("result")
        user_data: Optional[Dict[str, Any]] = None
        if isinstance(result, dict):
            inner = result.get("user")
            user_data = inner if isinstance(inner, dict) else result
        if user_data is not None:
            with _state_lock:
                _cached_profile = dict(user_data)
            log.info("Updated cached BlenderKit user profile.")
    elif status == "error":
        log.warning("User profile fetch failed: %s", task.get("message") or "unknown error")


def register_auth_handlers(
    dispatcher: Dispatcher,
    client: Optional[BKClient] = None,
    *,
    backend: Optional[str] = None,
    file_path: Optional[str] = None,
) -> None:
    """Register standard auth task callbacks on *dispatcher*."""
    dispatcher.register(
        "login",
        lambda task: handle_login_task(
            task,
            client=client,
            backend=backend,
            file_path=file_path,
        ),
    )
    dispatcher.register(
        "token_refresh",
        lambda task: handle_token_refresh_task(
            task,
            backend=backend,
            file_path=file_path,
        ),
    )
    dispatcher.register("oauth2/logout", handle_logout_task)
    dispatcher.register("profiles/get_user_profile", handle_user_profile_task)
