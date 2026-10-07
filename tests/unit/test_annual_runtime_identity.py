from __future__ import annotations

import sys
import types
from pathlib import Path

from finagent.core.annual_runtime_identity import (
    _compiled_code_map,
    _generated_dataclass_code_names,
    _loaded_modules_match_source,
    _module_function_codes,
    capture_api_runtime_identity,
    runtime_identity_is_valid,
)


def test_api_identity_is_a_loaded_code_snapshot() -> None:
    identity = capture_api_runtime_identity()

    assert runtime_identity_is_valid(identity)
    assert identity["role"] == "api"
    assert identity["loaded_source_match"] is True
    assert identity["model_calls"] == 0


def test_dataclass_synthetic_method_is_ignored_but_removed_explicit_method_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module_name = "finagent._runtime_identity_fixture"
    source_path = tmp_path / "runtime_fixture.py"
    current_source = (
        "from dataclasses import dataclass\n"
        "@dataclass(slots=True)\n"
        "class Record:\n"
        "    value: int\n"
        "def stable(value):\n"
        "    return value\n"
    )
    source_path.write_text(current_source, encoding="utf-8")
    module = types.ModuleType(module_name)
    module.__file__ = str(source_path)
    monkeypatch.setitem(sys.modules, module_name, module)
    exec(compile(current_source, str(source_path), "exec", dont_inherit=True), module.__dict__)

    source_codes = _compiled_code_map(compile(current_source, str(source_path), "exec", dont_inherit=True))
    assert "Record.__init__" in _generated_dataclass_code_names(module, source_codes)
    assert _loaded_modules_match_source(tmp_path, include_main=False)

    previous_version = (
        "def __init__(self, value):\n"
        "    self.value = value\n"
    )
    previous_namespace: dict[str, object] = {}
    exec(compile(previous_version, str(source_path), "exec", dont_inherit=True), previous_namespace)
    previous_init = previous_namespace["__init__"]
    previous_init.__module__ = module_name  # type: ignore[attr-defined]
    previous_init.__qualname__ = "Record.__init__"  # type: ignore[attr-defined]
    module.Record.__init__ = previous_init  # type: ignore[attr-defined]

    assert "Record.__init__" not in _generated_dataclass_code_names(module, source_codes)
    assert _loaded_modules_match_source(tmp_path, include_main=False) is False


def test_contextmanager_identity_keeps_wrapper_and_source_and_rejects_replacements(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module_name = "finagent._runtime_contextmanager_fixture"
    source_path = tmp_path / "contextmanager_fixture.py"
    current_source = (
        "from contextlib import contextmanager\n"
        "@contextmanager\n"
        "def gate():\n"
        "    yield None\n"
        "def alternate():\n"
        "    yield None\n"
        "def make_wrapper(func):\n"
        "    def wrapper(*args, **kwargs):\n"
        "        return func(*args, **kwargs)\n"
        "    return wrapper\n"
    )
    source_path.write_text(current_source, encoding="utf-8")
    module = types.ModuleType(module_name)
    module.__file__ = str(source_path)
    monkeypatch.setitem(sys.modules, module_name, module)
    exec(compile(current_source, str(source_path), "exec", dont_inherit=True), module.__dict__)

    wrapper_key = "gate.__contextmanager_wrapper__"
    loaded_codes = dict(_module_function_codes(module))
    assert "gate" in loaded_codes
    assert wrapper_key in loaded_codes
    assert _loaded_modules_match_source(tmp_path, include_main=False) is True

    source_function = module.gate.__wrapped__
    original_source_code = source_function.__code__
    source_function.__code__ = module.alternate.__code__
    assert _loaded_modules_match_source(tmp_path, include_main=False) is False
    source_function.__code__ = original_source_code

    original_wrapper = module.gate
    replacement = module.make_wrapper(source_function)
    replacement.__module__ = module_name
    replacement.__name__ = original_wrapper.__name__
    replacement.__qualname__ = original_wrapper.__qualname__
    replacement.__wrapped__ = source_function
    module.gate = replacement
    assert _loaded_modules_match_source(tmp_path, include_main=False) is False
