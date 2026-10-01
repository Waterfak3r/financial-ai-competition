"""本机 Web 模型配置存取，与 CLI 的环境变量配置模式分开。"""

from __future__ import annotations

import json
import os
import re
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit
from uuid import uuid4

from finagent.core.model_settings import ModelConfigError, ModelSettings, load_model_settings

_FILENAME = ".env.model"
_MAX_SETTINGS_BYTES = 16 * 1024
_MAX_BASE_URL_LENGTH = 2048
_MAX_MODEL_NAME_LENGTH = 256
_MAX_API_KEY_LENGTH = 4096
_SETTING_NAMES = ("MODEL_BASE_URL", "MODEL_NAME", "MODEL_API_KEY")
_REPARSE_POINT_ATTRIBUTE = 0x400
_STORE_LOCK = threading.RLock()


class ModelSettingsStoreError(Exception):
    """模型设置文件或安全写入失败；message 不包含配置值。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ModelSettingsValidationError(Exception):
    """Web 模型配置草稿无效；错误信息不包含输入值。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ModelSettingsSnapshot:
    """可安全返回给本机界面的模型配置状态。"""

    base_url: str | None
    model_name: str | None
    api_key_configured: bool
    configured: bool
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "model_name": self.model_name,
            "api_key_configured": self.api_key_configured,
            "configured": self.configured,
            "source": self.source,
        }


def validate_model_settings(
    base_url: Any,
    model_name: Any,
    api_key: Any,
    *,
    require_api_key: bool = True,
) -> ModelSettings:
    """验证并规范化配置，不访问网络或写文件。"""

    if not isinstance(base_url, str) or not isinstance(model_name, str) or not isinstance(api_key, str):
        raise ModelSettingsValidationError("invalid_model_settings", "模型配置字段必须是文本。")
    if len(base_url) > _MAX_BASE_URL_LENGTH:
        raise ModelSettingsValidationError("invalid_model_settings", "服务地址超过允许长度。")
    if len(model_name) > _MAX_MODEL_NAME_LENGTH:
        raise ModelSettingsValidationError("invalid_model_settings", "模型名称超过允许长度。")
    if len(api_key) > _MAX_API_KEY_LENGTH:
        raise ModelSettingsValidationError("invalid_model_settings", "密钥超过允许长度。")

    normalized_url = base_url.strip()
    normalized_name = model_name.strip()
    normalized_key = api_key.strip()
    if not normalized_url or not normalized_name:
        raise ModelSettingsValidationError("invalid_model_settings", "服务地址和模型名称不能为空。")
    if not _safe_text(normalized_url) or not _safe_text(normalized_name):
        raise ModelSettingsValidationError("invalid_model_settings", "服务地址或模型名称包含无效字符。")
    if not _safe_text(normalized_key):
        raise ModelSettingsValidationError("invalid_model_settings", "密钥包含无效字符。")
    if require_api_key and not normalized_key:
        raise ModelSettingsValidationError("model_api_key_required", "请填写模型服务密钥。")

    _validate_service_url(normalized_url)
    try:
        return load_model_settings(
            {
                "MODEL_BASE_URL": normalized_url,
                "MODEL_NAME": normalized_name,
                "MODEL_API_KEY": normalized_key or "placeholder-unused-key",
            }
        )
    except ModelConfigError:
        raise ModelSettingsValidationError("invalid_model_settings", "模型配置格式无效。") from None


def resolve_model_settings(
    project_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
    timeout_seconds: float = 60.0,
) -> ModelSettings:
    """优先读取 .env.model；无本机文件时回退环境变量，文件损坏时失败关闭。"""

    values = read_local_settings(project_root)
    if values is not None:
        settings = validate_model_settings(
            values["MODEL_BASE_URL"],
            values["MODEL_NAME"],
            values["MODEL_API_KEY"],
        )
        if timeout_seconds <= 0:
            raise ModelConfigError("超时时间必须大于 0。")
        return ModelSettings(settings.base_url, settings.api_key, settings.model, float(timeout_seconds))
    try:
        result = load_model_settings(environ, timeout_seconds=timeout_seconds)
        _validate_service_url(result.base_url)
        validate_model_settings(result.base_url, result.model, result.api_key)
        if timeout_seconds != result.timeout_seconds:
            return ModelSettings(result.base_url, result.api_key, result.model, timeout_seconds)
        return result
    except ModelConfigError:
        raise
    except ModelSettingsValidationError:
        raise ModelConfigError("环境变量中的模型配置格式无效。") from None


def get_model_settings_snapshot(
    project_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> ModelSettingsSnapshot:
    """读取不含密钥的 UI 状态；未配置时不返回部分环境变量。"""

    local_values = read_local_settings(project_root)
    if local_values is not None:
        settings = validate_model_settings(
            local_values["MODEL_BASE_URL"],
            local_values["MODEL_NAME"],
            local_values["MODEL_API_KEY"],
        )
        return ModelSettingsSnapshot(settings.base_url, settings.model, True, True, "local")

    try:
        settings = resolve_model_settings(
            project_root,
            environ=environ,
        )
    except (ModelConfigError, ModelSettingsValidationError):
        return ModelSettingsSnapshot(None, None, False, False, "none")
    return ModelSettingsSnapshot(settings.base_url, settings.model, bool(settings.api_key), True, "environment")


def save_local_settings(
    project_root: Path,
    *,
    base_url: Any,
    model_name: Any,
    api_key: Any,
) -> ModelSettingsSnapshot:
    """以原子替换保存配置；空密钥仅沿用相同地址的有效本机或环境密钥。"""

    root = _checked_project_root(project_root)
    with _STORE_LOCK:
        local_file_invalid = False
        previous_values: dict[str, str] | None = None
        previous_identity: tuple[int, int, int, int] | None = None
        try:
            current = read_local_settings(root, include_identity=True)
        except ModelSettingsStoreError:
            local_file_invalid = True
            previous_identity = _replaceable_plain_file_identity(root / _FILENAME)
            current = None
        if current is not None:
            previous_values, previous_identity = current

        fallback = None if local_file_invalid or previous_values is not None else _environment_settings()
        settings = _resolve_draft(base_url, model_name, api_key, previous_values, fallback)
        new_values = {
            "MODEL_BASE_URL": settings.base_url,
            "MODEL_NAME": settings.model,
            "MODEL_API_KEY": settings.api_key,
        }
        path = root / _FILENAME
        payload = _serialize_settings(new_values)
        _atomic_replace_settings(path, payload, previous_identity)
        return ModelSettingsSnapshot(settings.base_url, settings.model, True, True, "local")


def resolve_model_settings_draft(
    project_root: Path,
    *,
    base_url: Any,
    model_name: Any,
    api_key: Any,
) -> ModelSettings:
    """解析未保存的测试草稿；空密钥仅沿用同地址的有效本机或环境密钥。"""

    current = read_local_settings(project_root)
    fallback = _environment_settings() if current is None else None
    return _resolve_draft(base_url, model_name, api_key, current, fallback)


def delete_local_settings(project_root: Path) -> None:
    """只删除项目专属 .env.model；不修改普通 .env 或环境变量。"""

    root = _checked_project_root(project_root)
    path = root / _FILENAME
    with _STORE_LOCK:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        except OSError:
            raise ModelSettingsStoreError("model_settings_file_invalid", "本机模型配置文件无法安全访问。") from None
        if _is_reparse_point(info) or not stat.S_ISREG(info.st_mode):
            raise ModelSettingsStoreError("model_settings_file_invalid", "本机模型配置文件不是普通文件。")
        try:
            path.unlink()
        except OSError:
            raise ModelSettingsStoreError("model_settings_delete_failed", "无法清除本机模型配置。") from None


def read_local_settings(
    project_root: Path,
    *,
    include_identity: bool = False,
) -> dict[str, str] | tuple[dict[str, str], tuple[int, int, int, int]] | None:
    """严格读取本机设置文件；缺失返回 None，坏文件不会回退环境变量。"""

    root = _checked_project_root(project_root)
    path = root / _FILENAME
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise ModelSettingsStoreError("model_settings_file_invalid", "本机模型配置文件无法安全访问。") from None
    if _is_reparse_point(before) or not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_SETTINGS_BYTES:
        raise ModelSettingsStoreError("model_settings_file_invalid", "本机模型配置文件格式无效或超过大小限制。")

    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if _is_reparse_point(opened) or not stat.S_ISREG(opened.st_mode) or _file_identity(before) != _file_identity(opened):
            raise ModelSettingsStoreError("model_settings_file_invalid", "本机模型配置文件在读取期间发生变化。")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            content = handle.read(_MAX_SETTINGS_BYTES + 1)
        after = path.lstat()
        if _is_reparse_point(after) or _file_identity(before) != _file_identity(after) or len(content) > _MAX_SETTINGS_BYTES:
            raise ModelSettingsStoreError("model_settings_file_invalid", "本机模型配置文件在读取期间发生变化。")
    except ModelSettingsStoreError:
        raise
    except OSError:
        raise ModelSettingsStoreError("model_settings_file_invalid", "本机模型配置文件无法安全读取。") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)

    try:
        parsed = _parse_settings(content.decode("utf-8"))
        validate_model_settings(
            parsed["MODEL_BASE_URL"],
            parsed["MODEL_NAME"],
            parsed["MODEL_API_KEY"],
        )
    except (UnicodeDecodeError, ValueError, ModelSettingsValidationError):
        raise ModelSettingsStoreError("model_settings_file_invalid", "本机模型配置文件格式无效。") from None
    if include_identity:
        return parsed, _file_identity(before)
    return parsed


def _parse_settings(text: str) -> dict[str, str]:
    if not text.endswith("\n") or "\r" in text:
        raise ValueError("invalid settings file")
    lines = text[:-1].split("\n")
    if len(lines) != len(_SETTING_NAMES):
        raise ValueError("invalid settings file")
    result: dict[str, str] = {}
    for expected_name, line in zip(_SETTING_NAMES, lines, strict=True):
        prefix = expected_name + "="
        if not line.startswith(prefix):
            raise ValueError("invalid settings file")
        raw_value = line[len(prefix) :]
        value = json.loads(raw_value)
        if not isinstance(value, str) or expected_name in result:
            raise ValueError("invalid settings file")
        result[expected_name] = value
    if set(result) != set(_SETTING_NAMES):
        raise ValueError("invalid settings file")
    return result


def _resolve_draft(
    base_url: Any,
    model_name: Any,
    api_key: Any,
    previous_values: dict[str, str] | None,
    fallback_settings: ModelSettings | None = None,
) -> ModelSettings:
    if not isinstance(base_url, str) or not isinstance(model_name, str) or not isinstance(api_key, str):
        raise ModelSettingsValidationError("invalid_model_settings", "模型配置字段必须是文本。")
    normalized_url = base_url.strip()
    normalized_key = api_key.strip()
    if not normalized_key:
        if previous_values is not None:
            previous = validate_model_settings(
                previous_values["MODEL_BASE_URL"],
                previous_values["MODEL_NAME"],
                previous_values["MODEL_API_KEY"],
            )
        elif fallback_settings is not None:
            previous = fallback_settings
        else:
            raise ModelSettingsValidationError("model_api_key_required", "请填写模型服务密钥。")
        _validate_service_url(normalized_url)
        if normalized_url.rstrip("/") != previous.base_url:
            raise ModelSettingsValidationError(
                "model_api_key_required_for_endpoint_change",
                "服务地址已更改，请填写该服务对应的密钥。",
            )
        normalized_key = previous.api_key
    return validate_model_settings(normalized_url, model_name, normalized_key)


def _environment_settings() -> ModelSettings | None:
    try:
        settings = load_model_settings()
        _validate_service_url(settings.base_url)
        validate_model_settings(settings.base_url, settings.model, settings.api_key)
        return settings
    except (ModelConfigError, ModelSettingsValidationError):
        return None


def _replaceable_plain_file_identity(path: Path) -> tuple[int, int, int, int] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise ModelSettingsStoreError("model_settings_file_invalid", "本机模型配置文件无法安全访问。") from None
    if _is_reparse_point(info) or not stat.S_ISREG(info.st_mode):
        raise ModelSettingsStoreError("model_settings_file_invalid", "本机模型配置文件不是普通文件。")
    return _file_identity(info)


def _serialize_settings(values: Mapping[str, str]) -> bytes:
    lines = [f"{name}={json.dumps(values[name], ensure_ascii=False)}" for name in _SETTING_NAMES]
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    if len(payload) > _MAX_SETTINGS_BYTES:
        raise ModelSettingsValidationError("invalid_model_settings", "模型配置文件超过大小限制。")
    return payload


def _atomic_replace_settings(
    path: Path,
    payload: bytes,
    expected_identity: tuple[int, int, int, int] | None,
) -> None:
    temporary = path.with_name(f"{_FILENAME}.{uuid4().hex}.tmp")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            current = path.lstat()
        except FileNotFoundError:
            current = None
        except OSError:
            raise ModelSettingsStoreError("model_settings_write_failed", "无法安全更新本机模型配置。") from None
        if expected_identity is None:
            if current is not None:
                raise ModelSettingsStoreError("model_settings_changed", "本机模型配置在保存期间发生变化，请重试。")
        elif (
            current is None
            or _is_reparse_point(current)
            or not stat.S_ISREG(current.st_mode)
            or _file_identity(current) != expected_identity
        ):
            raise ModelSettingsStoreError("model_settings_changed", "本机模型配置在保存期间发生变化，请重试。")
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except ModelSettingsStoreError:
        raise
    except OSError:
        raise ModelSettingsStoreError("model_settings_write_failed", "无法保存本机模型配置。") from None
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _validate_service_url(value: str) -> None:
    if (
        len(value) > _MAX_BASE_URL_LENGTH
        or value != value.strip()
        or "\\" in value
        or "?" in value
        or "#" in value
        or any(ord(char) <= 32 or ord(char) == 127 for char in value)
    ):
        raise ModelSettingsValidationError("invalid_model_settings", "服务地址格式无效。")
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        port = parts.port
        if hostname is not None:
            hostname.encode("idna")
    except (UnicodeError, ValueError):
        raise ModelSettingsValidationError("invalid_model_settings", "服务地址格式无效。") from None
    if (
        parts.scheme not in {"http", "https"}
        or not parts.netloc
        or not hostname
        or parts.username is not None
        or parts.password is not None
        or (port is not None and not 1 <= port <= 65535)
        or parts.path.rstrip("/").endswith("/chat/completions")
    ):
        raise ModelSettingsValidationError("invalid_model_settings", "服务地址必须是有效的 http 或 https 服务根地址。")


def _safe_text(value: str) -> bool:
    return not any(ord(char) < 32 or ord(char) == 127 for char in value)


def _checked_project_root(project_root: Path) -> Path:
    try:
        root = Path(project_root).resolve(strict=True)
        info = root.lstat()
    except (OSError, TypeError, ValueError):
        raise ModelSettingsStoreError("model_settings_root_invalid", "项目配置目录无法安全访问。") from None
    if _is_reparse_point(info) or not stat.S_ISDIR(info.st_mode):
        raise ModelSettingsStoreError("model_settings_root_invalid", "项目配置目录不是普通目录。")
    return root


def _is_reparse_point(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE)


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
