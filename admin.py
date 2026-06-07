"""Admin API routes — extracted from main.py with auth, usage, and apikeys."""

import asyncio
import threading
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, FileResponse

from .auth import (
    require_admin, create_session_token, get_admin_password,
    get_session_hours, is_auth_configured, COOKIE_NAME,
)
from . import usage as usage_mod
from . import apikeys as apikeys_mod

router = APIRouter(prefix="/admin")

# Reference to pool — set by main.py on startup
pool = None
CONFIG_PATH = Path(__file__).parent / "config.yaml"
STATIC_DIR = Path(__file__).parent / "static"


def set_pool(p):
    global pool
    pool = p


# ─── Serve WebUI ───

@router.get("", response_class=FileResponse)
@router.get("/", response_class=FileResponse)
async def admin_ui():
    return FileResponse(STATIC_DIR / "admin.html")


# ─── Auth endpoints ───

@router.post("/api/login")
async def login(request: Request, response: Response):
    data = await request.json()
    password_input = data.get("password", "")
    admin_pw = get_admin_password()
    if not admin_pw:
        return {"ok": True, "auth_required": False}
    if password_input != admin_pw:
        return JSONResponse({"ok": False, "error": "Invalid password"}, status_code=401)
    hours = get_session_hours()
    token = create_session_token(admin_pw, hours)
    response.set_cookie(COOKIE_NAME, token, httponly=True, path="/", max_age=hours * 3600)
    return {"ok": True, "auth_required": True}


@router.post("/api/logout")
async def logout(response: Response, _=Depends(require_admin)):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/api/auth/check")
async def auth_check(request: Request):
    """Check if auth is configured and if current session is valid."""
    admin_pw = get_admin_password()
    if not admin_pw:
        return {"auth_required": False, "authenticated": True}
    token = request.cookies.get(COOKIE_NAME)
    from .auth import verify_session_token
    valid = bool(token and verify_session_token(token, admin_pw))
    return {"auth_required": True, "authenticated": valid}


# ─── Status (cached, no sync refresh) ───

@router.get("/api/status")
async def admin_status(_=Depends(require_admin)):
    return pool.get_status_summary()


# ─── Account management ───

@router.post("/api/accounts")
async def admin_add_account(request: Request, _=Depends(require_admin)):
    data = await request.json()
    name = data.get("name", "Unnamed")
    pat = data.get("pat", "")
    enabled = data.get("enabled", True)
    if not pat:
        return JSONResponse({"ok": False, "error": "PAT is required"}, status_code=400)
    acc = pool.add_account(name, pat, enabled)
    if enabled:
        pool.init_account(acc)
    return {"ok": True, "account": {"name": acc.name, "enabled": acc.enabled, "is_expired": acc.is_expired}}


@router.delete("/api/accounts/{index}")
async def admin_remove_account(index: int, _=Depends(require_admin)):
    ok = pool.remove_account(index)
    return {"ok": ok}


@router.post("/api/accounts/{index}/toggle")
async def admin_toggle_account(index: int, _=Depends(require_admin)):
    ok = pool.toggle_account(index)
    return {"ok": ok}


@router.post("/api/accounts/{index}/refresh")
async def admin_refresh_account(index: int, _=Depends(require_admin)):
    if 0 <= index < len(pool.accounts):
        acc = pool.accounts[index]
        if acc.session:
            pool.refresh_quota(acc)
            return {"ok": True}
    return {"ok": False}


@router.post("/api/accounts/refresh-all")
async def admin_refresh_all(_=Depends(require_admin)):
    """Trigger background quota refresh for all accounts."""
    def _bg_refresh():
        pool.refresh_all_quotas()
    t = threading.Thread(target=_bg_refresh, daemon=True)
    t.start()
    return {"ok": True, "message": "Refresh started in background"}


# ─── Strategy ───

@router.post("/api/strategy")
async def admin_set_strategy(request: Request, _=Depends(require_admin)):
    data = await request.json()
    strategy = data.get("strategy", "fill")
    if strategy not in ("round_robin", "fill"):
        return JSONResponse({"ok": False, "error": "Invalid strategy"}, status_code=400)
    pool.set_strategy(strategy)
    return {"ok": True, "strategy": strategy}


# ─── Usage statistics ───

@router.get("/api/usage/today")
async def usage_today(_=Depends(require_admin)):
    return usage_mod.get_today_stats()


@router.get("/api/usage/summary")
async def usage_summary(days: int = 7, _=Depends(require_admin)):
    return usage_mod.get_summary(days)


@router.get("/api/usage/trend")
async def usage_trend(days: int = 7, _=Depends(require_admin)):
    return usage_mod.get_trend(days)


# ─── API Keys ───

@router.get("/api/apikeys")
async def list_api_keys(_=Depends(require_admin)):
    return {"keys": apikeys_mod.list_keys(), "auth_enabled": apikeys_mod.has_any_keys()}


@router.post("/api/apikeys")
async def create_api_key(request: Request, _=Depends(require_admin)):
    data = await request.json()
    name = data.get("name", "Unnamed")
    note = data.get("note", "")
    result = apikeys_mod.create_key(name, note)
    return {"ok": True, **result}


@router.delete("/api/apikeys/{key_id}")
async def revoke_api_key(key_id: int, _=Depends(require_admin)):
    ok = apikeys_mod.revoke_key(key_id)
    return {"ok": ok}


# ─── Settings ───

@router.post("/api/settings/password")
async def change_password(request: Request, _=Depends(require_admin)):
    data = await request.json()
    new_pw = data.get("password", "")
    if not new_pw:
        return JSONResponse({"ok": False, "error": "Password cannot be empty"}, status_code=400)
    # Update config.yaml
    cfg = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
    if "admin" not in cfg:
        cfg["admin"] = {}
    cfg["admin"]["password"] = new_pw
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return {"ok": True}


@router.post("/api/settings/config")
async def update_config(request: Request, _=Depends(require_admin)):
    data = await request.json()
    cfg = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
    if "admin" not in cfg:
        cfg["admin"] = {}
    if "session_hours" in data:
        cfg["admin"]["session_hours"] = int(data["session_hours"])
    if "context_length" in data:
        if "models" not in cfg:
            cfg["models"] = {}
        cfg["models"]["default_context_length"] = int(data["context_length"])
        pool.default_context_length = int(data["context_length"])
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return {"ok": True}


@router.post("/api/settings/clear-usage")
async def clear_usage(_=Depends(require_admin)):
    usage_mod.clear_history()
    return {"ok": True}
