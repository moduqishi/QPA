"""PAT pool manager with round-robin and fill strategies, quota tracking"""

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone as tz

import httpx
import yaml

from .bearer import AuthIdentity, SessionContext, build_payload_b64, compose_bearer, new_session, sign_request
from .encoding import encode as qoder_encode
from .signature import APPCODE, current_date, sign

CONFIG_PATH = Path(__file__).parent / "config.yaml"


@dataclass
class Account:
    name: str
    pat: str
    enabled: bool = True
    # Runtime
    session: Optional[SessionContext] = None
    user_info: Optional[dict] = None
    last_status: Optional[dict] = None
    last_quota: Optional[dict] = None
    last_quota_time: float = 0
    is_expired: bool = False
    is_quota_exceeded: bool = False
    request_count: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def to_config_dict(self) -> dict:
        return {"name": self.name, "pat": self.pat, "enabled": self.enabled}

    def masked_pat(self) -> str:
        if len(self.pat) < 8:
            return "***"
        return self.pat[:6] + "..." + self.pat[-4:]

    def quota_remaining(self) -> int | None:
        if not self.last_quota:
            return None
        acts = self.last_quota.get("data", {}).get("activities", [])
        total = 0
        for a in acts:
            total += a.get("remaining", 0)
        return total

    def quota_info_list(self) -> list[dict]:
        if not self.last_quota:
            return []
        acts = self.last_quota.get("data", {}).get("activities", [])
        result = []
        for a in acts:
            result.append({
                "name": a.get("modelName", ""),
                "model_keys": a.get("modelKeys", []),
                "limit": a.get("limit", 0),
                "used": a.get("used", 0),
                "remaining": a.get("remaining", 0),
                "reset_at": a.get("resetAt", 0),
                "status_text": a.get("statusText", ""),
                "description": a.get("description", ""),
                "tag": a.get("tag", ""),
                "eligible": a.get("eligible", False),
            })
        return result


class PatPool:
    def __init__(self, config_path: str | Path | None = None):
        self.config_path = Path(config_path) if config_path else CONFIG_PATH
        self.accounts: list[Account] = []
        self.strategy: str = "fill"
        self.default_context_length: int = 1_000_000
        self.model_list: list[dict] = []
        self.host: str = "0.0.0.0"
        self.port: int = 8963
        self._rr_index: int = 0
        self._lock = threading.Lock()
        self.load_config()

    def load_config(self):
        if not self.config_path.exists():
            return
        with open(self.config_path, "r") as f:
            cfg = yaml.safe_load(f) or {}

        server = cfg.get("server", {})
        self.host = server.get("host", "0.0.0.0")
        self.port = server.get("port", 8963)
        self.strategy = cfg.get("strategy", "fill")
        models_cfg = cfg.get("models", {})
        self.default_context_length = models_cfg.get("default_context_length", 1_000_000)
        self.model_list = models_cfg.get("list", [
            {"id": "lite", "display_name": "Lite", "owned_by": "qoder"},
            {"id": "qmodel_latest", "display_name": "Qwen3.7-Max", "owned_by": "qoder"},
        ])
        self.accounts = []
        for acc_cfg in cfg.get("accounts", []):
            self.accounts.append(Account(
                name=acc_cfg.get("name", "Unnamed"),
                pat=acc_cfg.get("pat", ""),
                enabled=acc_cfg.get("enabled", True),
            ))

    def save_config(self):
        cfg = {
            "server": {"host": self.host, "port": self.port},
            "accounts": [a.to_config_dict() for a in self.accounts],
            "strategy": self.strategy,
            "models": {
                "default_context_length": self.default_context_length,
                "list": self.model_list,
            },
        }
        with open(self.config_path, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    def init_account(self, account: Account) -> bool:
        try:
            jt, sess = _exchange_job_token(account.pat)
            account.session = sess
            account.user_info = jt
            account.is_expired = False
            # First quota fetch (may fail if session is too fresh)
            self.refresh_quota(account)
            # Warm up the inference session so the first real request doesn't
            # trigger a Qoder cold-start / 503.
            _warmup_session(sess)
            return True
        except Exception as e:
            print(f"[pool] init failed for '{account.name}': {e}")
            account.is_expired = True
            return False

    def init_all(self):
        for acc in self.accounts:
            if acc.enabled and acc.pat:
                ok = self.init_account(acc)
                if ok:
                    name = acc.user_info.get("name", "?")
                    plan = acc.last_status.get("plan", "?") if acc.last_status else "?"
                    remaining = acc.quota_remaining()
                    print(f"[pool] {acc.name}: OK (user={name}, plan={plan}, remaining={remaining})")
                else:
                    print(f"[pool] {acc.name}: FAILED")
            else:
                print(f"[pool] {acc.name}: DISABLED")

    def refresh_quota(self, account: Account) -> dict | None:
        if not account.session:
            return None
        try:
            status = _fetch_user_status(account.session, account.user_info.get("id", ""))
            account.last_status = status
            account.is_quota_exceeded = status.get("isQuotaExceeded", False)
        except Exception as e:
            print(f"[pool] status check failed for '{account.name}' (non-critical): {e}")
        try:
            quota = _fetch_quota(account.session)
            account.last_quota = quota
            account.last_quota_time = time.time()
            remaining = account.quota_remaining()
            if remaining is not None and remaining <= 0:
                account.is_quota_exceeded = True
            return quota
        except Exception as e:
            print(f"[pool] quota refresh failed for '{account.name}': {e}")
            return None

    def refresh_all_quotas(self):
        for acc in self.accounts:
            if acc.enabled and acc.session:
                self.refresh_quota(acc)

    def get_account(self) -> Account | None:
        with self._lock:
            enabled = [a for a in self.accounts if a.enabled and a.session and not a.is_expired]
            if not enabled:
                return None

            if self.strategy == "round_robin":
                for _ in range(len(enabled)):
                    acc = enabled[self._rr_index % len(enabled)]
                    self._rr_index = (self._rr_index + 1) % len(enabled)
                    if not acc.is_quota_exceeded:
                        acc.request_count += 1
                        return acc
                # All exceeded, still return one
                acc = enabled[0]
                acc.request_count += 1
                return acc
            else:  # fill
                for acc in enabled:
                    if not acc.is_quota_exceeded:
                        acc.request_count += 1
                        return acc
                acc = enabled[0]
                acc.request_count += 1
                return acc

    def add_account(self, name: str, pat: str, enabled: bool = True) -> Account:
        acc = Account(name=name, pat=pat, enabled=enabled)
        if enabled:
            self.init_account(acc)
        self.accounts.append(acc)
        self.save_config()
        return acc

    def remove_account(self, index: int) -> bool:
        if 0 <= index < len(self.accounts):
            self.accounts.pop(index)
            self._rr_index = 0
            self.save_config()
            return True
        return False

    def toggle_account(self, index: int) -> bool:
        if 0 <= index < len(self.accounts):
            acc = self.accounts[index]
            acc.enabled = not acc.enabled
            if acc.enabled and not acc.session:
                self.init_account(acc)
            self.save_config()
            return True
        return False

    def set_strategy(self, strategy: str):
        self.strategy = strategy
        self._rr_index = 0
        self.save_config()

    def get_status_summary(self) -> dict:
        accounts_info = []
        for i, acc in enumerate(self.accounts):
            info = {
                "index": i,
                "name": acc.name,
                "pat_masked": acc.masked_pat(),
                "enabled": acc.enabled,
                "initialized": acc.session is not None,
                "is_expired": acc.is_expired,
                "is_quota_exceeded": acc.is_quota_exceeded,
                "request_count": acc.request_count,
                "user_name": acc.user_info.get("name", "") if acc.user_info else "",
                "user_type": acc.last_status.get("userType", "") if acc.last_status else "",
                "plan": acc.last_status.get("plan", "") if acc.last_status else "",
                "user_tag": acc.last_status.get("userTag", "") if acc.last_status else "",
                "email": acc.last_status.get("email", "") if acc.last_status else "",
                "quota_remaining": acc.quota_remaining(),
                "quota_details": acc.quota_info_list(),
                "next_reset": acc.last_quota.get("data", {}).get("activities", [{}])[0].get("resetAt", 0) if acc.last_quota else 0,
            }
            accounts_info.append(info)
        return {
            "strategy": self.strategy,
            "accounts": accounts_info,
            "models": self.model_list,
            "default_context_length": self.default_context_length,
            "host": self.host,
            "port": self.port,
        }


def _exchange_job_token(pat: str) -> tuple[dict, SessionContext]:
    import uuid, base64
    machine_id = str(uuid.uuid4())
    machine_token = base64.urlsafe_b64encode(
        (str(uuid.uuid4()) + str(uuid.uuid4()))[:50].encode()
    ).rstrip(b"=").decode()
    machine_type = str(uuid.uuid4()).replace("-", "")[:18]
    date = current_date()
    sig = sign(date)
    inner = {"personalToken": pat, "securityOauthToken": "", "refreshToken": "", "needRefresh": False, "authInfo": {}}
    outer = {"payload": json.dumps(inner), "encodeVersion": "1"}
    body = qoder_encode(json.dumps(outer).encode())
    headers = {
        "cosy-machinetoken": machine_token, "cosy-machinetype": machine_type,
        "login-version": "v2", "appcode": APPCODE, "accept": "application/json",
        "accept-encoding": "identity", "cosy-version": "0.1.43", "cosy-clienttype": "5",
        "date": date, "signature": sig, "content-type": "application/json",
        "cosy-machineid": machine_id, "user-agent": "Go-http-client/2.0",
    }
    resp = httpx.post("https://center.qoder.sh/algo/api/v3/user/jobToken?Encode=1", content=body, headers=headers, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"jobToken HTTP {resp.status_code}")
    jt = resp.json()
    identity = AuthIdentity(
        name=jt.get("name", ""), aid=jt.get("id", ""), uid=jt.get("id", ""),
        yx_uid="", organization_id="", organization_name="",
        user_type=jt.get("userType", "personal_standard"),
        security_oauth_token=jt.get("securityOauthToken", ""),
        refresh_token=jt.get("refreshToken", ""),
    )
    sess = new_session(identity, machine_id, machine_token, machine_type)
    return jt, sess


def _fetch_user_status(sess: SessionContext, user_id: str) -> dict:
    date = current_date()
    sig = sign(date)
    inner = {"userId": user_id, "personalToken": "", "securityOauthToken": "", "refreshToken": "", "needRefresh": False, "authInfo": {}}
    outer = {"payload": json.dumps(inner), "encodeVersion": "1"}
    body = qoder_encode(json.dumps(outer).encode())
    headers = {
        "cosy-machinetoken": sess.machine_token, "cosy-machinetype": sess.machine_type,
        "login-version": "v2", "appcode": APPCODE, "accept": "application/json",
        "accept-encoding": "identity", "cosy-version": "0.1.43", "cosy-clienttype": "5",
        "date": date, "signature": sig, "content-type": "application/json",
        "cosy-machineid": sess.machine_id, "cosy-user": sess.identity.uid,
        "user-agent": "Go-http-client/2.0",
    }
    resp = httpx.post("https://center.qoder.sh/algo/api/v3/user/status?Encode=1", content=body, headers=headers, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"status HTTP {resp.status_code}")
    return resp.json()


def _warmup_session(sess: SessionContext):
    """Send a minimal inference request to warm up the Qoder session.

    Qoder's inference backend cold-starts on the first request after
    session exchange. This ping ensures the first real request doesn't
    trigger a 503 / timeout.
    """
    try:
        import uuid
        from urllib.parse import urlparse

        warm_body = json.dumps({
            "request_id": str(uuid.uuid4()),
            "stream": True,
            "messages": [{"role": "user", "content": "ping",
                         "response_meta": {"id": "", "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}},
                         "reasoning_content_signature": ""}],
            "parameters": {"max_tokens": 1, "temperature": 0.0, "context_length": 1000000},
            "model_config": {"key": "lite", "is_reasoning": False},
            "chat_context": {
                "chatPrompt": "",
                "extra": {
                    "context": [],
                    "modelConfig": {"is_reasoning": False, "key": "lite"},
                    "originalContent": {"type": "text", "text": "ping"},
                },
                "text": {"type": "text", "text": "ping"},
            },
            "is_reply": True,
            "is_retry": False,
            "session_id": str(uuid.uuid4()),
            "code_language": "",
            "source": 1,
            "version": "3",
            "chat_prompt": "",
            "aliyun_user_type": sess.identity.user_type,
            "session_type": "qodercli",
            "agent_id": "agent_common",
            "task_id": "common",
        })
        body = qoder_encode(warm_body.encode())

        url = "https://api3.qoder.sh/algo/api/v2/service/pro/sse/agent_chat_generation?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1"
        parsed = urlparse(url)
        path_sig = parsed.path[5:] if parsed.path.startswith("/algo") else parsed.path

        payload_b64 = build_payload_b64(sess.info)
        date = str(int(time.time()))
        sig = sign_request(payload_b64, sess.cosy_key, date, body.decode("latin-1"), path_sig)
        bearer = compose_bearer(payload_b64, sig)

        headers = {
            "cosy-data-policy": "AGREE",
            "content-type": "application/json",
            "cosy-machinetype": sess.machine_type,
            "cosy-clienttype": "5",
            "cosy-user": sess.identity.uid,
            "cosy-key": sess.cosy_key,
            "cosy-date": date,
            "cosy-clientip": "169.254.198.161",
            "accept": "application/json",
            "accept-encoding": "identity",
            "cosy-version": "0.1.43",
            "cosy-machineid": sess.machine_id,
            "cosy-machinetoken": sess.machine_token,
            "login-version": "v2",
            "user-agent": "Go-http-client/2.0",
            "authorization": bearer,
        }
        resp = httpx.post(url, content=body, headers=headers, timeout=20)
        if resp.status_code == 200:
            return
        print(f"[pool] warm-up returned HTTP {resp.status_code} (non-critical)")
    except Exception as e:
        print(f"[pool] warm-up failed (non-critical): {e}")


def _fetch_quota(sess: SessionContext) -> dict:
    """Fetch free quota info via /api/v2/activity"""
    from urllib.parse import urlparse
    url = "https://api3.qoder.sh/algo/api/v2/activity"
    parsed = urlparse(url)
    path_sig = parsed.path[5:] if parsed.path.startswith("/algo") else parsed.path
    payload_b64 = build_payload_b64(sess.info)
    date = str(int(time.time()))
    sig = sign_request(payload_b64, sess.cosy_key, date, "", path_sig)
    bearer = compose_bearer(payload_b64, sig)
    headers = {
        "cosy-data-policy": "AGREE",
        "cosy-machinetype": sess.machine_type, "cosy-clienttype": "5",
        "cosy-user": sess.identity.uid, "cosy-key": sess.cosy_key,
        "cosy-date": date, "cosy-clientip": "169.254.198.161",
        "accept": "application/json", "accept-encoding": "identity",
        "cosy-version": "0.1.43", "cosy-machineid": sess.machine_id,
        "cosy-machinetoken": sess.machine_token, "login-version": "v2",
        "user-agent": "Go-http-client/2.0", "authorization": bearer,
    }
    resp = httpx.get(url, headers=headers, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"activity HTTP {resp.status_code}")
    return resp.json()
