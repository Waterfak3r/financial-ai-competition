"""Loaded-code and source fingerprints for the annual-analysis API/CLI contract."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

RUNTIME_IDENTITY_KIND = "fintrace_annual_runtime_identity_v1"
ANNUAL_RUNTIME_PROTOCOL_VERSION = "annual-analysis-cli-v1"
_PROCESS_INSTANCE_ID = uuid4().hex
_PROCESS_STARTED_AT = datetime.now(timezone.utc).isoformat(timespec="seconds")


def resolve_source_root(project_root: Path | None = None) -> Path:
    """Return the source checkout that owns this imported FINTRACE package."""

    if project_root is not None:
        candidate = Path(project_root).resolve()
        if (candidate / "scripts" / "analyze_annual.py").is_file() and (
            candidate / "backend" / "src" / "finagent"
        ).is_dir():
            return candidate
    return Path(__file__).resolve().parents[4]


def source_manifest_fingerprint(project_root: Path | None = None) -> tuple[str, int]:
    """Hash protocol-affecting source and prompt files without following links."""

    root = resolve_source_root(project_root)
    backend_root = root / "backend" / "src" / "finagent"
    paths = [root / "scripts" / "analyze_annual.py"]
    if backend_root.is_dir():
        paths.extend(path for path in backend_root.rglob("*.py") if "__pycache__" not in path.parts)
    prompt_root = root / "config" / "prompts"
    if prompt_root.is_dir():
        paths.extend(prompt_root.rglob("*.md"))

    entries: list[tuple[str, str]] = []
    for path in sorted(set(paths), key=lambda item: item.relative_to(root).as_posix()):
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or _is_reparse_point(info):
                return _digest_json({"error": "source_file_not_regular", "path": path.relative_to(root).as_posix()}), len(entries)
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode) or _is_reparse_point(opened):
                    return _digest_json({"error": "source_file_not_regular", "path": path.relative_to(root).as_posix()}), len(entries)
                with os.fdopen(descriptor, "rb", closefd=False) as handle:
                    data = handle.read(4 * 1024 * 1024 + 1)
            finally:
                os.close(descriptor)
            after = path.lstat()
            if _is_reparse_point(after) or _file_identity(info) != _file_identity(after) or len(data) > 4 * 1024 * 1024:
                return _digest_json({"error": "source_file_changed", "path": path.relative_to(root).as_posix()}), len(entries)
        except OSError:
            return _digest_json({"error": "source_file_unavailable", "path": path.relative_to(root).as_posix()}), len(entries)
        entries.append((path.relative_to(root).as_posix(), hashlib.sha256(data).hexdigest()))

    if not entries:
        return _digest_json({"error": "source_manifest_empty"}), 0
    return _digest_json(entries), len(entries)


def capture_api_runtime_identity(project_root: Path | None = None) -> dict[str, Any]:
    """Capture the API's loaded code once after its route modules are imported."""

    return _capture_identity("api", project_root, include_main=False)


def capture_cli_runtime_identity(project_root: Path | None = None) -> dict[str, Any]:
    """Capture code loaded by ``scripts/analyze_annual.py`` before task work."""

    return _capture_identity("cli", project_root, include_main=True)


def runtime_identity_is_valid(identity: Any) -> bool:
    return (
        isinstance(identity, dict)
        and identity.get("kind") == RUNTIME_IDENTITY_KIND
        and identity.get("protocol_version") == ANNUAL_RUNTIME_PROTOCOL_VERSION
        and identity.get("role") in {"api", "cli"}
        and isinstance(identity.get("fingerprint"), str)
        and len(identity["fingerprint"]) == 64
        and isinstance(identity.get("source_manifest_sha256"), str)
        and len(identity["source_manifest_sha256"]) == 64
        and type(identity.get("source_file_count")) is int
        and identity["source_file_count"] > 0
        and identity.get("loaded_source_match") is True
    )


def _capture_identity(role: str, project_root: Path | None, *, include_main: bool) -> dict[str, Any]:
    root = resolve_source_root(project_root)
    loaded_entries = _loaded_code_entries(root, include_main=include_main)
    loaded_fingerprint = _digest_json(loaded_entries)
    source_fingerprint, source_file_count = source_manifest_fingerprint(root)
    loaded_source_match = _loaded_modules_match_source(root, include_main=include_main)
    payload = {
        "kind": RUNTIME_IDENTITY_KIND,
        "role": role,
        "protocol_version": ANNUAL_RUNTIME_PROTOCOL_VERSION,
        "loaded_code_sha256": loaded_fingerprint,
        "source_manifest_sha256": source_fingerprint,
        "source_file_count": source_file_count,
        "loaded_source_match": loaded_source_match,
    }
    fingerprint = _digest_json(payload)
    return {
        **payload,
        "fingerprint": fingerprint,
        "process_id": os.getpid(),
        "process_instance_id": _PROCESS_INSTANCE_ID,
        "process_started_at": _PROCESS_STARTED_AT,
        "model_calls": 0,
    }


def _loaded_code_entries(root: Path, *, include_main: bool) -> list[tuple[str, str, str]]:
    entries: list[tuple[str, str, str]] = []
    modules = [
        module
        for name, module in tuple(sys.modules.items())
        if isinstance(module, types.ModuleType)
        and (name == "finagent" or name.startswith("finagent."))
    ]
    if include_main:
        main_module = sys.modules.get("__main__")
        main_path = _module_path(main_module) if isinstance(main_module, types.ModuleType) else None
        expected = (root / "scripts" / "analyze_annual.py").resolve()
        if main_path == expected:
            modules.append(main_module)

    for module in modules:
        module_path = _module_path(module)
        if module_path is None or not module_path.is_relative_to(root):
            continue
        for qualname, code in _module_function_codes(module):
            entries.append((module.__name__, qualname, _digest_json(_code_signature(code))))
    return sorted(entries)


def _loaded_modules_match_source(root: Path, *, include_main: bool) -> bool:
    modules = [
        module
        for name, module in tuple(sys.modules.items())
        if isinstance(module, types.ModuleType)
        and (name == "finagent" or name.startswith("finagent."))
    ]
    if include_main:
        main_module = sys.modules.get("__main__")
        if isinstance(main_module, types.ModuleType):
            main_path = _module_path(main_module)
            if main_path == (root / "scripts" / "analyze_annual.py").resolve():
                modules.append(main_module)

    checked = 0
    for module in modules:
        path = _module_path(module)
        if path is None or not path.is_relative_to(root):
            continue
        loaded_codes = dict(_module_function_codes(module))
        if not loaded_codes:
            continue
        try:
            source = path.read_bytes()
            compiled = compile(source, str(path), "exec", dont_inherit=True)
        except (OSError, SyntaxError, UnicodeError):
            return False
        source_codes = _compiled_code_map(compiled)
        explicit_codes = {
            qualname: code for qualname, code in loaded_codes.items() if qualname in source_codes
        }
        generated_codes = _generated_dataclass_code_names(module, source_codes)
        if any(
            qualname not in source_codes and qualname not in generated_codes
            for qualname in loaded_codes
        ):
            return False
        if any(_code_signature(code) != _code_signature(source_codes[qualname]) for qualname, code in explicit_codes.items()):
            return False
        checked += bool(explicit_codes)
    return checked > 0


def _generated_dataclass_code_names(
    module: types.ModuleType,
    source_codes: Mapping[str, types.CodeType],
) -> set[str]:
    """Ignore only methods whose code object bears dataclasses' generated marker."""

    generated_names = {
        "__init__", "__repr__", "__eq__", "__hash__", "__lt__", "__le__",
        "__gt__", "__ge__", "__setattr__", "__delattr__",
    }
    ignored: set[str] = set()
    for value in vars(module).values():
        if not isinstance(value, type) or value.__module__ != module.__name__:
            continue
        fields = getattr(value, "__dataclass_fields__", None)
        if not isinstance(fields, dict):
            continue
        for name in generated_names:
            qualname = f"{value.__qualname__}.{name}"
            member = vars(value).get(name)
            if (
                qualname not in source_codes
                and isinstance(member, types.FunctionType)
                and member.__module__ == module.__name__
                and _is_dataclass_generated_function(member)
            ):
                ignored.add(qualname)
    return ignored


def _is_dataclass_generated_function(function: types.FunctionType) -> bool:
    wrapped = getattr(function, "__wrapped__", None)
    if isinstance(wrapped, types.FunctionType) and _is_dataclass_generated_function(wrapped):
        return True
    code = function.__code__
    return code.co_filename == "<string>" and code.co_qualname.startswith("__create_fn__.<locals>.")


def _module_function_codes(module: types.ModuleType) -> list[tuple[str, types.CodeType]]:
    found: dict[str, types.CodeType] = {}

    def add(value: Any) -> None:
        if isinstance(value, staticmethod | classmethod):
            value = value.__func__
        if isinstance(value, property):
            for candidate in (value.fget, value.fset, value.fdel):
                add(candidate)
            return
        if isinstance(value, types.FunctionType) and value.__module__ == module.__name__:
            found[value.__qualname__] = value.__code__
            return
        if isinstance(value, type) and value.__module__ == module.__name__:
            for member in vars(value).values():
                if isinstance(member, type) and member.__module__ == module.__name__:
                    continue
                add(member)

    for value in vars(module).values():
        add(value)
    return sorted(found.items())


def _compiled_code_map(code: types.CodeType) -> dict[str, types.CodeType]:
    found: dict[str, types.CodeType] = {}

    def visit(candidate: types.CodeType) -> None:
        if candidate.co_name != "<module>":
            found[candidate.co_qualname] = candidate
        for const in candidate.co_consts:
            if isinstance(const, types.CodeType):
                visit(const)

    visit(code)
    return found


def _code_signature(code: types.CodeType) -> dict[str, Any]:
    return {
        "name": code.co_name,
        "qualname": code.co_qualname,
        "firstlineno": code.co_firstlineno,
        "argcount": code.co_argcount,
        "posonlyargcount": code.co_posonlyargcount,
        "kwonlyargcount": code.co_kwonlyargcount,
        "nlocals": code.co_nlocals,
        "stacksize": code.co_stacksize,
        "flags": code.co_flags,
        "bytecode": code.co_code.hex(),
        "linetable": code.co_linetable.hex(),
        "exceptiontable": code.co_exceptiontable.hex(),
        "names": list(code.co_names),
        "varnames": list(code.co_varnames),
        "freevars": list(code.co_freevars),
        "cellvars": list(code.co_cellvars),
        "constants": [_constant_signature(item) for item in code.co_consts],
    }


def _constant_signature(value: Any) -> Any:
    if isinstance(value, types.CodeType):
        return {"code": _code_signature(value)}
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    if isinstance(value, tuple):
        return {"tuple": [_constant_signature(item) for item in value]}
    if isinstance(value, frozenset):
        values = [_constant_signature(item) for item in value]
        return {"frozenset": sorted(values, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=True))}
    if value is Ellipsis:
        return {"ellipsis": True}
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return {"float": value.hex()}
    if isinstance(value, complex):
        return {"complex": [value.real.hex(), value.imag.hex()]}
    return {"type": type(value).__qualname__, "repr": repr(value)}


def _module_path(module: types.ModuleType | None) -> Path | None:
    filename = getattr(module, "__file__", None) if module is not None else None
    if not isinstance(filename, str):
        return None
    try:
        return Path(filename).resolve()
    except (OSError, RuntimeError):
        return None


def _digest_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _is_reparse_point(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
