from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from finagent.core.model_settings import ModelSettings
from finagent.core.model_settings_store import (
    ModelSettingsStoreError,
    ModelSettingsValidationError,
    delete_local_settings,
    get_model_settings_snapshot,
    resolve_model_settings,
    resolve_model_settings_draft,
    save_local_settings,
)


_BASE_URL = "https://models.example.invalid/v1"
_MODEL = "model-name"
_KEY = 'secret-key with=$symbols "quotes" \\ slash'


def test_local_settings_round_trip_and_reload_without_exposing_key(tmp_path: Path) -> None:
    snapshot = save_local_settings(
        tmp_path,
        base_url=_BASE_URL,
        model_name=_MODEL,
        api_key=_KEY,
    )

    assert snapshot.to_dict() == {
        "base_url": _BASE_URL,
        "model_name": _MODEL,
        "api_key_configured": True,
        "configured": True,
        "source": "local",
    }
    assert resolve_model_settings(tmp_path) == ModelSettings(_BASE_URL, _KEY, _MODEL)
    assert get_model_settings_snapshot(tmp_path).to_dict() == snapshot.to_dict()
    assert 'MODEL_API_KEY="' in (tmp_path / ".env.model").read_text(encoding="utf-8")
    assert list(tmp_path.glob(".env.model.*.tmp")) == []
    if os.name != "nt":
        assert stat.S_IMODE((tmp_path / ".env.model").stat().st_mode) == 0o600


def test_environment_fallback_and_same_endpoint_key_reuse(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MODEL_BASE_URL", _BASE_URL)
    monkeypatch.setenv("MODEL_API_KEY", _KEY)
    monkeypatch.setenv("MODEL_NAME", _MODEL)

    assert resolve_model_settings(tmp_path) == ModelSettings(_BASE_URL, _KEY, _MODEL)
    assert get_model_settings_snapshot(tmp_path).to_dict() == {
        "base_url": _BASE_URL,
        "model_name": _MODEL,
        "api_key_configured": True,
        "configured": True,
        "source": "environment",
    }

    saved = save_local_settings(
        tmp_path,
        base_url=_BASE_URL + "/",
        model_name="updated-model",
        api_key="",
    )
    assert saved.source == "local"
    assert resolve_model_settings(tmp_path) == ModelSettings(_BASE_URL, _KEY, "updated-model")


def test_empty_key_is_kept_only_for_same_saved_endpoint(tmp_path: Path) -> None:
    save_local_settings(tmp_path, base_url=_BASE_URL, model_name=_MODEL, api_key=_KEY)
    original = (tmp_path / ".env.model").read_bytes()

    with pytest.raises(ModelSettingsValidationError) as changed:
        save_local_settings(
            tmp_path,
            base_url="https://other.example.invalid/v1",
            model_name=_MODEL,
            api_key="",
        )
    assert changed.value.code == "model_api_key_required_for_endpoint_change"
    assert (tmp_path / ".env.model").read_bytes() == original

    save_local_settings(
        tmp_path,
        base_url="https://other.example.invalid/v1",
        model_name=_MODEL,
        api_key="new-secret",
    )
    assert resolve_model_settings(tmp_path).api_key == "new-secret"


def test_invalid_input_does_not_overwrite_valid_settings(tmp_path: Path) -> None:
    save_local_settings(tmp_path, base_url=_BASE_URL, model_name=_MODEL, api_key=_KEY)
    original = (tmp_path / ".env.model").read_bytes()

    with pytest.raises(ModelSettingsValidationError):
        save_local_settings(
            tmp_path,
            base_url="https://user:password@models.example.invalid/v1",
            model_name=_MODEL,
            api_key="replacement",
        )
    with pytest.raises(ModelSettingsValidationError):
        save_local_settings(tmp_path, base_url=_BASE_URL, model_name=_MODEL, api_key="x\r\ninjected")
    assert (tmp_path / ".env.model").read_bytes() == original
    assert resolve_model_settings(tmp_path).api_key == _KEY


def test_bad_local_file_fails_closed_and_can_be_repaired_with_new_key(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("MODEL_BASE_URL", _BASE_URL)
    monkeypatch.setenv("MODEL_API_KEY", "environment-secret")
    monkeypatch.setenv("MODEL_NAME", _MODEL)
    path = tmp_path / ".env.model"
    path.write_text("MODEL_API_KEY=broken\n", encoding="utf-8")

    with pytest.raises(ModelSettingsStoreError) as error:
        resolve_model_settings(tmp_path)
    assert error.value.code == "model_settings_file_invalid"
    with pytest.raises(ModelSettingsStoreError):
        get_model_settings_snapshot(tmp_path)

    with pytest.raises(ModelSettingsValidationError) as missing_key:
        save_local_settings(tmp_path, base_url=_BASE_URL, model_name=_MODEL, api_key="")
    assert missing_key.value.code == "model_api_key_required"

    save_local_settings(tmp_path, base_url=_BASE_URL, model_name=_MODEL, api_key="replacement-secret")
    assert resolve_model_settings(tmp_path).api_key == "replacement-secret"


def test_draft_test_can_reuse_environment_key_but_never_another_endpoint(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("MODEL_BASE_URL", _BASE_URL)
    monkeypatch.setenv("MODEL_API_KEY", _KEY)
    monkeypatch.setenv("MODEL_NAME", _MODEL)

    reused = resolve_model_settings_draft(
        tmp_path,
        base_url=_BASE_URL,
        model_name="draft-model",
        api_key="",
    )
    assert reused == ModelSettings(_BASE_URL, _KEY, "draft-model")
    with pytest.raises(ModelSettingsValidationError) as changed:
        resolve_model_settings_draft(
            tmp_path,
            base_url="https://other.example.invalid/v1",
            model_name=_MODEL,
            api_key="",
        )
    assert changed.value.code == "model_api_key_required_for_endpoint_change"


def test_delete_removes_only_app_settings_and_environment_falls_back(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / ".env").write_text("do-not-change", encoding="utf-8")
    save_local_settings(tmp_path, base_url=_BASE_URL, model_name=_MODEL, api_key="local-secret")
    monkeypatch.setenv("MODEL_BASE_URL", "https://environment.example.invalid/v1")
    monkeypatch.setenv("MODEL_API_KEY", "environment-secret")
    monkeypatch.setenv("MODEL_NAME", "environment-model")

    delete_local_settings(tmp_path)

    assert not (tmp_path / ".env.model").exists()
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "do-not-change"
    assert get_model_settings_snapshot(tmp_path).to_dict() == {
        "base_url": "https://environment.example.invalid/v1",
        "model_name": "environment-model",
        "api_key_configured": True,
        "configured": True,
        "source": "environment",
    }


@pytest.mark.parametrize(
    "base_url",
    [
        "file:///tmp/model",
        "https://user:pass@example.invalid/v1",
        "https://example.invalid/v1/chat/completions",
        "https://example.invalid:99999/v1",
        "https://bad host/v1",
    ],
)
def test_invalid_service_urls_are_rejected_without_echo(base_url: str, tmp_path: Path) -> None:
    with pytest.raises(ModelSettingsValidationError) as error:
        save_local_settings(tmp_path, base_url=base_url, model_name=_MODEL, api_key=_KEY)
    assert base_url not in str(error.value)


def test_symlinked_settings_file_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("not a config", encoding="utf-8")
    settings = tmp_path / ".env.model"
    try:
        settings.symlink_to(target)
    except OSError:
        pytest.skip("当前环境不能创建符号链接。")

    with pytest.raises(ModelSettingsStoreError):
        resolve_model_settings(tmp_path)
    with pytest.raises(ModelSettingsStoreError):
        delete_local_settings(tmp_path)

