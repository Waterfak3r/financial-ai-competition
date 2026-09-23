"""从环境变量读取供应方中立的模型连接配置。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

_REQUIRED = ("MODEL_BASE_URL", "MODEL_API_KEY", "MODEL_NAME")


class ModelConfigError(Exception):
    """配置不完整或地址不合法。消息不包含密钥。"""


@dataclass(frozen=True, slots=True)
class ModelSettings:
    """Chat Completions 根地址、密钥和模型名。"""

    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 60.0

    def __repr__(self) -> str:
        return (
            "ModelSettings("
            f"base_url={self.base_url!r}, model={self.model!r}, "
            f"timeout_seconds={self.timeout_seconds!r})"
        )

    def chat_completions_url(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"


def load_model_settings(
    environ: Mapping[str, str] | None = None,
    *,
    timeout_seconds: float = 60.0,
) -> ModelSettings:
    """读取 MODEL_BASE_URL、MODEL_API_KEY、MODEL_NAME。不访问网络。"""

    source = os.environ if environ is None else environ
    values = {name: str(source.get(name, "")).strip() for name in _REQUIRED}
    missing = [name for name, value in values.items() if value == ""]
    if missing:
        raise ModelConfigError("缺少模型配置：" + "、".join(missing))
    if timeout_seconds <= 0:
        raise ModelConfigError("超时时间必须大于 0。")
    base_url = _validate_base_url(values["MODEL_BASE_URL"])
    return ModelSettings(
        base_url=base_url,
        api_key=values["MODEL_API_KEY"],
        model=values["MODEL_NAME"],
        timeout_seconds=float(timeout_seconds),
    )


def _validate_base_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or parts.netloc == "":
        raise ModelConfigError("MODEL_BASE_URL 必须是 http 或 https 地址。")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ModelConfigError("MODEL_BASE_URL 只能包含协议、主机和路径。")
    path = parts.path.rstrip("/")
    if path.endswith("/chat/completions"):
        raise ModelConfigError("MODEL_BASE_URL 应填写服务根地址，不要包含 /chat/completions。")
    return value.rstrip("/")
