"""Qoder API client — ported from SignatureApiClient + BearerApiClient + JobTokenClient"""

import json
import time
from typing import Any

import httpx

from .bearer import (
    AuthIdentity,
    SessionContext,
    build_payload_b64,
    compose_bearer,
    new_session,
    sign_request,
)
from .encoding import encode as qoder_encode
from .signature import APPCODE, current_date, sign


def exchange_job_token(pat: str) -> dict:
    """Exchange a PAT for a job token (session info)."""
    machine_id = str(__import__("uuid").uuid4())
    machine_token = __import__("base64").urlsafe_b64encode(
        (str(__import__("uuid").uuid4()) + str(__import__("uuid").uuid4()))[:50].encode()
    ).rstrip(b"=").decode()
    machine_type = str(__import__("uuid").uuid4()).replace("-", "")[:18]

    date = current_date()
    sig = sign(date)

    inner = {
        "personalToken": pat,
        "securityOauthToken": "",
        "refreshToken": "",
        "needRefresh": False,
        "authInfo": {},
    }
    outer = {
        "payload": json.dumps(inner),
        "encodeVersion": "1",
    }
    body = qoder_encode(json.dumps(outer).encode())

    headers = {
        "cosy-machinetoken": machine_token,
        "cosy-machinetype": machine_type,
        "login-version": "v2",
        "appcode": APPCODE,
        "accept": "application/json",
        "accept-encoding": "identity",
        "cosy-version": "0.1.43",
        "cosy-clienttype": "5",
        "date": date,
        "signature": sig,
        "content-type": "application/json",
        "cosy-machineid": machine_id,
        "user-agent": "Go-http-client/2.0",
    }

    resp = httpx.post(
        "https://center.qoder.sh/algo/api/v3/user/jobToken?Encode=1",
        content=body,
        headers=headers,
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"jobToken HTTP {resp.status_code} body={resp.text}")

    jt = resp.json()
    identity = AuthIdentity(
        name=jt.get("name", ""),
        aid=jt.get("id", ""),
        uid=jt.get("id", ""),
        yx_uid="",
        organization_id="",
        organization_name="",
        user_type=jt.get("userType", "personal_standard"),
        security_oauth_token=jt.get("securityOauthToken", ""),
        refresh_token=jt.get("refreshToken", ""),
    )
    sess = new_session(identity, machine_id, machine_token, machine_type)
    return jt, sess


def _build_stream_headers_common(sess: SessionContext, extra_headers: dict[str, str] | None = None) -> dict[str, str]:
    h = {
        "cosy-data-policy": "AGREE",
        "content-type": "application/json",
        "cosy-machinetype": sess.machine_type,
        "cosy-clienttype": "5",
        "cosy-user": sess.identity.uid,
        "cosy-key": sess.cosy_key,
        "cache-control": "no-cache",
        "accept": "text/event-stream",
        "cosy-clientip": "169.254.198.161",
        "accept-encoding": "identity",
        "cosy-version": "0.1.43",
        "cosy-machineid": sess.machine_id,
        "cosy-machinetoken": sess.machine_token,
        "login-version": "v2",
        "user-agent": "Go-http-client/2.0",
    }
    if extra_headers:
        h.update(extra_headers)
    return h


def _sign_and_auth(sess: SessionContext, url: str, body: str) -> tuple[str, str, str]:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    path_query = parsed.path
    path_sig = path_query[5:] if path_query.startswith("/algo") else path_query

    payload_b64 = build_payload_b64(sess.info)
    date = str(int(time.time()))
    sig = sign_request(payload_b64, sess.cosy_key, date, body, path_sig)
    bearer = compose_bearer(payload_b64, sig)
    return bearer, date, sig


async def open_stream(sess: SessionContext, url: str, body_obj: dict, extra_headers: dict[str, str] | None = None):
    """Async SSE stream — yields raw lines from the Qoder API.

    Uses httpx.AsyncClient with no read timeout so long-reasoning tasks
    (5+ minutes) are not cut off prematurely.  Raises RuntimeError on
    non-200 status.
    """
    body = qoder_encode(json.dumps(body_obj).encode())
    bearer, cosy_date, _ = _sign_and_auth(sess, url, body)

    headers = _build_stream_headers_common(sess, extra_headers)
    headers["authorization"] = bearer
    headers["cosy-date"] = cosy_date

    # read=None: never timeout waiting for the next chunk from the upstream model
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=30, read=None, write=30, pool=30)
    ) as client:
        async with client.stream("POST", url, content=body, headers=headers) as resp:
            if resp.status_code != 200:
                err = await resp.aread()
                raise RuntimeError(f"HTTP {resp.status_code} body={err.decode()}")
            async for line in resp.aiter_lines():
                if line:
                    yield line
