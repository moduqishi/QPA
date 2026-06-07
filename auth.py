"""Admin authentication with HMAC-SHA256 session tokens."""

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path

import yaml
from fastapi import HTTPException, Request, status

CONFIG_PATH = Path(__file__).parent / "config.yaml"
COOKIE_NAME = "qpa_session"


def _load_admin_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f) or {}
    return cfg.get("admin", {}) or {}


def get_admin_password() -> str:
    return _load_admin_config().get("password", "") or ""


def get_session_hours() -> int:
    return _load_admin_config().get("session_hours", 24)


def create_session_token(password: str, hours: int = 24) -> str:
    payload = json.dumps({"exp": int(time.time()) + hours * 3600})
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()
    sig = hmac.new(password.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def verify_session_token(token: str, password: str) -> bool:
    if not password or not token or "." not in token:
        return False
    parts = token.split(".", 1)
    if len(parts) != 2:
        return False
    payload_b64, sig = parts
    expected = hmac.new(password.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("exp", 0) > time.time()
    except Exception:
        return False


async def require_admin(request: Request):
    """FastAPI dependency: raises 401 if admin auth is configured but session is invalid."""
    password = get_admin_password()
    if not password:
        return  # No password configured = auth disabled
    token = request.cookies.get(COOKIE_NAME)
    if not token or not verify_session_token(token, password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


def is_auth_configured() -> bool:
    return bool(get_admin_password())
