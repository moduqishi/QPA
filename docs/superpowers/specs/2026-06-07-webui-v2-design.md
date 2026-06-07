# QPA WebUI v2 — Design Spec

## Overview

Redesign the QPA admin panel from an inline HTML string into a proper modular web application with sidebar navigation, usage analytics, admin authentication, and API key management.

## Goals

1. Fix performance issues (synchronous quota refresh blocking API responses, no loading states)
2. Add usage statistics with SQLite persistence (by account, model, day)
3. Add admin password protection with cookie-based sessions
4. Add multi-key API access control with create/revoke management

## Architecture

### File Structure

```
QPA/
├── main.py          # Slimmed: core /v1/* routes + app startup only
├── admin.py         # NEW: all /admin/api/* routes, extracted from main.py
├── auth.py          # NEW: admin password auth (cookie + HMAC session)
├── apikeys.py       # NEW: API key CRUD + validation, SQLite storage
├── usage.py         # NEW: usage stats collection + query, SQLite storage
├── pool.py          # Existing, unchanged
├── qoder_client.py  # Existing, unchanged
├── bearer.py        # Existing, unchanged
├── encoding.py      # Existing, unchanged
├── signature.py     # Existing, unchanged
├── static/          # NEW: frontend static files
│   ├── admin.html   # Admin panel SPA shell (replaces inline HTML)
│   ├── admin.js     # Frontend logic (vanilla JS + Chart.js)
│   └── admin.css    # Styles
├── config.yaml      # NEW: admin.password, admin.session_hours fields
├── data/            # NEW: SQLite database directory
│   └── qpa.db       # Usage logs + API keys
├── run.py           # Existing, unchanged
└── ...
```

### Data Flow

```
Incoming request → auth middleware checks /v1/* for valid API Key
                 → select PAT account from pool
                 → process request
                 → on completion → usage.py records (model, tokens, latency, account)
                 → return response

Admin panel → GET /admin/ serves static/admin.html
            → JS checks cookie for session token
            → no token or expired → show login overlay
            → user enters password → POST /admin/api/login
            → backend compares config.yaml admin.password
            → pass → issue session token (HMAC signed, 24h expiry)
            → Set-Cookie: qpa_session=<token>; HttpOnly; Path=/
            → frontend hides login, loads panel

All /admin/api/* endpoints (except /login) use FastAPI Dependency
injection to check cookie; return 401 if invalid.
```

## Database Design

SQLite database at `data/qpa.db`, two tables:

### usage_logs

```sql
CREATE TABLE usage_logs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp         INTEGER NOT NULL,        -- Unix seconds
    date_key          TEXT NOT NULL,           -- "2026-06-07" for daily aggregation
    account           TEXT NOT NULL,           -- PAT account name
    model             TEXT NOT NULL,           -- requested model ID
    prompt_tokens     INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens      INTEGER DEFAULT 0,
    latency_ms        INTEGER DEFAULT 0,       -- response time in ms
    stream            INTEGER DEFAULT 0,       -- 1 if streaming request
    finish_reason     TEXT DEFAULT ''
);

CREATE INDEX idx_usage_date ON usage_logs(date_key);
CREATE INDEX idx_usage_account_date ON usage_logs(account, date_key);
```

### api_keys

```sql
CREATE TABLE api_keys (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash     TEXT NOT NULL UNIQUE,    -- SHA256 of the key (never store plaintext)
    key_prefix   TEXT NOT NULL,           -- "sk-qpa-8f..." first 8 chars for identification
    name         TEXT NOT NULL,           -- user-given name
    note         TEXT DEFAULT '',         -- optional note
    created_at   INTEGER NOT NULL,
    last_used_at INTEGER DEFAULT 0,
    is_active    INTEGER DEFAULT 1
);
```

### Usage Collection

- Usage is recorded asynchronously after response completion (does not block response return)
- Uses a background task or `asyncio.create_task` to write to SQLite
- Dashboard queries aggregate by day/account/model using SQL GROUP BY

## Authentication

### Admin Password Auth

- Password stored in `config.yaml` under `admin.password`
- If `admin.password` is empty or not set, admin panel remains unauthenticated (backward compatible)
- Session token structure: `base64(payload) + "." + HMAC-SHA256(secret=admin_password, payload)`
  - payload: `{"exp": <unix_timestamp>}` (default 24h expiry)
- Token stored in `qpa_session` cookie (HttpOnly, Path=/)
- All `/admin/api/*` endpoints (except `/login`) check cookie via FastAPI Depends

### config.yaml additions

```yaml
admin:
  password: "your-admin-password"
  session_hours: 24          # login session duration
```

### API Key Validation

```
Client POST /v1/chat/completions
  → read Authorization: Bearer sk-xxx header
  → if api_keys table has records:
      → SHA256(incoming key) → compare with key_hash
      → match and is_active=1 → allow, update last_used_at
      → otherwise → return 401
  → if api_keys table is empty (no keys created):
      → backward compatible, allow all requests (current behavior)
```

This means existing clients are unaffected until the first API key is created.

## WebUI Design

### Layout

Sidebar navigation (left) + main content area (right). Classic admin panel layout.

- **Sidebar**: Logo + 4 nav items + user info footer
- **Responsive**: Sidebar collapses to bottom tab bar on mobile (<768px)

### Pages

#### 1. Dashboard (仪表盘)

- **Stats cards** (4): Today's requests, Token consumption, Active accounts, Average latency
  - Each shows value + % change vs yesterday
- **Trend chart**: Bar chart (Chart.js) showing requests + tokens over 7/30 days
  - Filter: Today / 7 days / 30 days
- **Model stats table**: Per-model breakdown of requests, prompt tokens, completion tokens, avg latency

#### 2. Account Management (账号管理)

- **Strategy toggle** in header: Fill / Round Robin
- **Account cards** in 2-column grid:
  - Name + status badge
  - User info, plan, masked PAT, request count
  - Quota progress bar with remaining/total
  - Action buttons: refresh, toggle, delete
- **Add account form** at bottom: name + PAT input + add button

#### 3. API Keys (API 密钥)

- **Warning banner** when keys exist: "API authentication is enabled"
- **Create form**: name + note + create button
  - On creation: show full key once in a modal, user must copy immediately
- **Keys table**: name, key prefix, note, created date, last used, status, revoke button
  - Revoked keys shown with strikethrough and reduced opacity

#### 4. Settings (设置)

- **Security section**: Change admin password, Session duration
- **Service section**: Dispatch strategy, Context window size, Auto quota refresh toggle
- **Data section**: Clear usage history (destructive action with confirmation)

### Login Overlay

- Full-screen overlay when no valid session
- Centered card with logo, password input, login button
- On success: overlay fades out, panel loads

### Performance Improvements

| Old Problem | New Solution |
|---|---|
| `/admin/api/status` synchronously refreshes all quotas | API reads cached data only; quota refresh runs in background task |
| No loading states | Skeleton loading animations on initial load |
| No button feedback | Spinner on buttons + toast notifications |
| Entire HTML sent as string | Static files with browser caching (Cache-Control headers) |
| 30s polling blocks on slow networks | Polling only reads cache; manual refresh button for on-demand quota sync |

## API Endpoints

### New Admin API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | /admin/api/login | No | Authenticate with password, returns session cookie |
| POST | /admin/api/logout | Yes | Clear session cookie |
| GET | /admin/api/status | Yes | Cached status summary (no synchronous quota refresh) |
| POST | /admin/api/accounts/refresh-all | Yes | Trigger background quota refresh for all accounts |
| GET | /admin/api/usage/summary | Yes | Aggregated usage stats (by day/model/account) |
| GET | /admin/api/usage/trend | Yes | Time-series data for charts |
| GET | /admin/api/apikeys | Yes | List all API keys (with masked hashes) |
| POST | /admin/api/apikeys | Yes | Create new API key, returns full key once |
| DELETE | /admin/api/apikeys/{id} | Yes | Revoke an API key |
| POST | /admin/api/settings/password | Yes | Update admin password |
| POST | /admin/api/settings/config | Yes | Update runtime settings |

### Existing Endpoints (unchanged)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | /v1/models | API Key* | List available models |
| POST | /v1/chat/completions | API Key* | Chat completion |
| GET | /admin/ | No | Serve admin HTML (login check in JS) |

*API Key auth only enforced when keys exist in database.

## Backward Compatibility

- If `admin.password` is not set → admin panel works without login (current behavior)
- If no API keys created → `/v1/*` endpoints accept any/no auth (current behavior)
- Existing `config.yaml` files work without modification (new fields are optional)
- `data/` directory and SQLite DB created automatically on first use

## Technology Choices

- **Frontend**: Vanilla JS + Chart.js (CDN), no build step required
- **Backend**: FastAPI (existing), new modules for auth/usage/apikeys
- **Database**: SQLite via Python `sqlite3` stdlib (no new dependencies)
- **Auth**: HMAC-SHA256 session tokens, no JWT library needed
- **No npm/Node**: Project remains pure Python, Docker build unchanged
