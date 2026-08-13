"""Canonical JSON serialization (stage 7B.1.0).

统一 JSON 序列化：`sort_keys=True` + 紧凑 `separators` + `ensure_ascii=False` +
UTF-8。fingerprints 与 bundle writer / loader 共用这一套，避免出现多套不同
serialization。

- `canonical_json_str` → 用于 fingerprint（配合 SHA-256）。
- `canonical_json_bytes` → 用于 bundle 落盘。
"""

import json

_SEPARATORS = (",", ":")


def canonical_json_str(payload: object) -> str:
    """canonical JSON 文本（sort_keys + 紧凑 separators + ensure_ascii=False）。"""
    return json.dumps(payload, sort_keys=True, separators=_SEPARATORS, ensure_ascii=False)


def canonical_json_bytes(payload: object) -> bytes:
    """canonical JSON bytes（UTF-8 编码）。"""
    return canonical_json_str(payload).encode("utf-8")
