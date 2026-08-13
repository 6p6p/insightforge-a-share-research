"""Bundle 底层 IO 辅助（stage 7B.1.1A）。

稳定错误包装：FileNotFoundError / JSONDecodeError / pydantic ValidationError
一律翻译为 `EvalContractError`，message 只报 artifact kind + reason code，不泄露
raw JSON / label content / source bytes。
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from app.eval.errors import EvalContractError


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """temp file → `os.replace` 的原子写（避免半写）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_raw_bytes(path: Path, kind: str) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise EvalContractError(f"{kind} 不存在") from exc
    except OSError as exc:
        raise EvalContractError(f"{kind} 读取失败") from exc


def read_json_dict(path: Path, kind: str) -> dict[str, Any]:
    raw = read_raw_bytes(path, kind)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvalContractError(f"{kind} 不是合法 UTF-8 JSON") from exc
    if not isinstance(data, dict):
        raise EvalContractError(f"{kind} 必须是 JSON object")
    return data


def read_json_model[T: BaseModel](path: Path, model: type[T], kind: str) -> T:
    data = read_json_dict(path, kind)
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise EvalContractError(f"{kind} 契约校验失败") from exc
