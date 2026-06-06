"""Qoder request signature — ported from Signature.java"""

import hashlib
from datetime import datetime, timezone

APPCODE = "cosy"
SECRET = "d2FyLCB3YXIgbmV2ZXIgY2hhbmdlcw=="  # base64("war, war never changes")
SEP = "&"


def current_date() -> str:
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")


def sign(date: str) -> str:
    s = APPCODE + SEP + SECRET + SEP + date
    return hashlib.md5(s.encode()).hexdigest()
