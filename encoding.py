"""Qoder custom Base64 encoding/decoding — ported from QoderEncoding.java"""

CUSTOM_ALPHABET = "_doRTgHZBKcGVjlvpC,@aFSx#DPuNJme&i*MzLOEn)sUrthbf%Y^w.(kIQyXqWA!"
STD_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
CUSTOM_PAD = "$"

_C2S: dict[str, str] = {}
_S2C: dict[str, str] = {}

for _i in range(64):
    _C2S[CUSTOM_ALPHABET[_i]] = STD_ALPHABET[_i]
    _S2C[STD_ALPHABET[_i]] = CUSTOM_ALPHABET[_i]
_C2S[CUSTOM_PAD] = "="
_S2C["="] = CUSTOM_PAD


def encode(data: bytes) -> str:
    import base64
    std = base64.b64encode(data).decode("ascii")
    n = len(std)
    a = n // 3
    rearranged = std[n - a :] + std[a : n - a] + std[: a]
    return "".join(_S2C.get(c, c) for c in rearranged)


def decode(encoded: str) -> bytes:
    import base64

    mapped = "".join(_C2S.get(c, c) for c in encoded)
    n = len(mapped)
    a = n // 3
    std = mapped[n - a :] + mapped[a : n - a] + mapped[: a]
    # fix padding
    pad = (4 - len(std) % 4) % 4
    std += "=" * pad
    return base64.b64decode(std)
