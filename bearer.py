"""Bearer token builder — ported from BearerBuilder.java"""

import base64
import hashlib
import json
import uuid
from dataclasses import dataclass
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA

SERVER_PUBKEY_PEM = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDA8iMH5c02LilrsERw9t6Pv5Nc
4k6Pz1EaDicBMpdpxKduSZu5OANqUq8er4GM95omAGIOPOh+Nx0spthYA2BqGz+l
6HRkPJ7S236FZz73In/KVuLnwI8JJ2CbuJap8kvheCCZpmAWpb/cPx/3Vr/J6I17
XcW+ML9FoCI6AOvOzwIDAQAB
-----END PUBLIC KEY-----"""


@dataclass
class AuthIdentity:
    name: str
    aid: str
    uid: str
    yx_uid: str
    organization_id: str
    organization_name: str
    user_type: str
    security_oauth_token: str
    refresh_token: str


@dataclass
class SessionContext:
    temp_key: bytes
    cosy_key: str
    info: str
    identity: AuthIdentity
    machine_id: str
    machine_token: str
    machine_type: str


def _rsa_encrypt(data: bytes) -> bytes:
    key = RSA.import_key(SERVER_PUBKEY_PEM)
    cipher = PKCS1_v1_5.new(key)
    return cipher.encrypt(data)


def _aes_encrypt(plain: bytes, key: bytes) -> bytes:
    cipher = AES.new(key, AES.MODE_CBC, iv=key)
    pad_len = 16 - len(plain) % 16
    plain += bytes([pad_len] * pad_len)
    return cipher.encrypt(plain)


def _md5_hex(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def _auth_payload_json(identity: AuthIdentity) -> bytes:
    obj = {
        "name": identity.name,
        "aid": identity.aid,
        "uid": identity.uid,
        "yx_uid": identity.yx_uid,
        "organization_id": identity.organization_id,
        "organization_name": identity.organization_name,
        "user_type": identity.user_type,
        "security_oauth_token": identity.security_oauth_token,
        "refresh_token": identity.refresh_token,
    }
    return json.dumps(obj).encode()


def new_session(
    identity: AuthIdentity, machine_id: str, machine_token: str, machine_type: str
) -> SessionContext:
    temp_key = uuid.uuid4().hex[:16].encode("ascii")
    cosy_key = base64.b64encode(_rsa_encrypt(temp_key)).decode()
    info = base64.b64encode(_aes_encrypt(_auth_payload_json(identity), temp_key)).decode()
    return SessionContext(
        temp_key=temp_key,
        cosy_key=cosy_key,
        info=info,
        identity=identity,
        machine_id=machine_id,
        machine_token=machine_token,
        machine_type=machine_type,
    )


def sign_request(
    payload_b64: str, cosy_key: str, cosy_date: str, body: str, path_without_algo: str
) -> str:
    s = f"{payload_b64}\n{cosy_key}\n{cosy_date}\n{body}\n{path_without_algo}"
    return _md5_hex(s)


def build_payload_b64(info: str) -> str:
    m = {
        "cosyVersion": "0.1.43",
        "ideVersion": "",
        "info": info,
        "requestId": str(uuid.uuid4()),
        "version": "v1",
    }
    return base64.b64encode(json.dumps(m).encode()).decode()


def compose_bearer(payload_b64: str, sig: str) -> str:
    return f"Bearer COSY.{payload_b64}.{sig}"
