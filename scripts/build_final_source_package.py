"""Build and verify a FINTRACE source ZIP from an explicit Git commit."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import unicodedata
import uuid
import zipfile
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


FORMAT_VERSION = 1
ZIP_ROOT = "FINTRACE_SOURCE"
TARGET_ENVIRONMENT = "Windows x64; CPython 3.14.3"
RUNTIME_ROOTS = ("fastapi", "python-multipart", "uvicorn", "pymupdf", "langgraph")
LOCK_PATH = "backend/requirements-final-win.txt"
LOCK_SCOPE_LINE = f"# Tested dependency scope: {TARGET_ENVIRONMENT}."
OUTPUT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
SUPPORTED_GIT_MODES = {"100644", "100755"}
FORBIDDEN_PARTS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    "tmp",
    "dist",
    "build",
    "site-packages",
    "artifacts",
    "raw",
    "processed",
    "proposal",
    "video",
    "tem",
    ".playwright-mcp",
}
WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{n}" for n in range(1, 10)),
    *(f"lpt{n}" for n in range(1, 10)),
}
ROOT_FILES = {"AGENTS.md", "README.md", "start.bat", ".gitignore", ".env.example"}
FRONTEND_SOURCE_SUFFIXES = {
    ".css",
    ".html",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".jsx",
    ".json",
    ".png",
    ".scss",
    ".svg",
    ".ts",
    ".tsx",
    ".webp",
}
TEXT_SOURCE_SUFFIXES = {".csv", ".json", ".jsonl", ".md", ".txt"}
FINAL_REPORT_SUFFIXES = {".bib", ".csv", ".docx", ".json", ".md", ".pdf", ".png", ".svg", ".tex", ".txt"}
SUSPICIOUS_SECRET_PATTERNS = (
    re.compile(rb"(?i)\bsk-[A-Za-z0-9_-]{32,}\b"),
    re.compile(rb"(?i)\bBearer\s+[A-Za-z0-9._-]{40,}"),
    re.compile(rb"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


class PackageError(RuntimeError):
    """A fail-closed source package validation error."""


@dataclass(frozen=True)
class GitEntry:
    path: str
    mode: str
    object_type: str
    object_id: str


@dataclass(frozen=True)
class SnapshotFile:
    path: str
    data: bytes
    git_blob: str


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run_git(root: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise PackageError(f"Git 命令失败（{' '.join(args[:3])}）：{detail}")
    return result.stdout


def validate_package_id(package_id: str) -> None:
    if OUTPUT_ID_RE.fullmatch(package_id) is None:
        raise PackageError("package_id 仅允许英文、数字、点、下划线和连字符，且长度为 1–101")


def validate_git_path(path: str) -> str:
    """Reject Git paths that cannot safely become a Windows ZIP entry."""
    if not path or "\x00" in path or "\\" in path:
        raise PackageError(f"Git 路径为空或包含非法分隔符：{path!r}")
    try:
        path.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise PackageError("Git 路径不是有效 UTF-8，拒绝打包") from exc
    posix = PurePosixPath(path)
    windows = PureWindowsPath(path)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or path.startswith("/")
    ):
        raise PackageError(f"Git 路径不是安全相对路径：{path!r}")
    for part in path.split("/"):
        if re.search(r'[<>:"|?*\x00-\x1f]', part) or part.endswith((".", " ")):
            raise PackageError(f"Git 路径含 Windows 不支持的名称：{path!r}")
        stem = part.split(".", 1)[0].casefold()
        if stem in WINDOWS_RESERVED_NAMES:
            raise PackageError(f"Git 路径使用 Windows 保留名称：{path!r}")
    return path


def parse_git_tree(raw: bytes) -> list[GitEntry]:
    entries: list[GitEntry] = []
    normalized: dict[str, str] = {}
    for record in raw.split(b"\x00"):
        if not record:
            continue
        try:
            metadata_part, raw_path = record.split(b"\t", 1)
            mode_b, type_b, object_id_b = metadata_part.split(b" ", 2)
            path = raw_path.decode("utf-8", errors="strict")
            mode = mode_b.decode("ascii")
            object_type = type_b.decode("ascii")
            object_id = object_id_b.decode("ascii")
        except (ValueError, UnicodeError) as exc:
            raise PackageError("Git tree 项格式无效或路径不是 UTF-8") from exc
        validate_git_path(path)
        key = unicodedata.normalize("NFC", path).casefold()
        previous = normalized.get(key)
        if previous is not None:
            raise PackageError(f"Git 路径重复或在 Windows 上大小写/Unicode 归一化冲突：{previous!r}, {path!r}")
        normalized[key] = path
        if mode == "120000":
            raise PackageError(f"Git 快照含符号链接，拒绝打包：{path}")
        if mode not in SUPPORTED_GIT_MODES or object_type != "blob":
            raise PackageError(f"Git mode/type 不支持，拒绝打包：{mode} {object_type} {path}")
        entries.append(GitEntry(path, mode, object_type, object_id))
    return entries


def resolve_commit(root: Path, source_commit: str) -> str:
    if re.fullmatch(r"[0-9a-fA-F]{40,64}", source_commit) is None:
        raise PackageError("请提供完整十六进制 Git commit ID，不能使用 HEAD、分支名或短 hash")
    commit = _run_git(
        root,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{source_commit}^{{commit}}",
    ).decode("ascii").strip()
    if commit.lower() != source_commit.lower():
        raise PackageError("指定值不是规范的完整 commit ID")
    return commit.lower()


def read_commit_snapshot(root: Path, source_commit: str) -> tuple[str, str, dict[str, SnapshotFile]]:
    commit = resolve_commit(root, source_commit)
    tree = _run_git(root, "rev-parse", f"{commit}^{{tree}}").decode("ascii").strip()
    entries = parse_git_tree(_run_git(root, "ls-tree", "-r", "-z", "--full-tree", commit))
    snapshot: dict[str, SnapshotFile] = {}
    for entry in entries:
        if not selected_source_file(entry.path):
            continue
        data = _run_git(root, "cat-file", "blob", entry.object_id)
        snapshot[entry.path] = SnapshotFile(entry.path, data, entry.object_id)

    selected_samples = validate_sample_manifests(root, source_commit, entries)
    for sample in selected_samples:
        snapshot[sample.path] = sample
    ensure_required_snapshot(snapshot)
    validate_empty_env_example(snapshot[".env.example"].data)
    scan_for_credentials(snapshot)
    return commit, tree, snapshot


def selected_source_file(path: str) -> bool:
    """Allowlist source/document files; runtime artifacts are never candidates."""
    validate_git_path(path)
    parts = path.split("/")
    name = parts[-1]
    if name.startswith(".env") and path != ".env.example":
        return False
    if any(part.casefold() in FORBIDDEN_PARTS for part in parts):
        return False
    suffix = PurePosixPath(name).suffix.lower()
    if path in ROOT_FILES:
        return True
    if parts[0] == "backend":
        if path in {"backend/pyproject.toml", LOCK_PATH}:
            return True
        return len(parts) >= 4 and parts[1:3] == ["src", "finagent"] and (suffix == ".py" or name == ".gitkeep")
    if parts[0] == "frontend":
        if path in {
            "frontend/index.html",
            "frontend/package.json",
            "frontend/package-lock.json",
            "frontend/tsconfig.json",
            "frontend/vite.config.ts",
        }:
            return True
        return len(parts) >= 3 and parts[1] in {"src", "public"} and (suffix in FRONTEND_SOURCE_SUFFIXES or name == ".gitkeep")
    if parts[0] == "config":
        return len(parts) >= 3 and parts[1] in {"prompts", "rules"} and (suffix in {".md", ".json", ".toml", ".yaml", ".yml"} or name == ".gitkeep")
    if parts[0] == "scripts":
        return suffix in {".bat", ".ps1", ".py"} or name == ".gitkeep"
    if parts[0] == "docs":
        return suffix == ".md"
    if parts[0] == "tests":
        return suffix in {".csv", ".json", ".jsonl", ".md", ".py", ".txt"} or name == ".gitkeep"
    if parts[0] == "evaluation":
        return suffix in {".csv", ".json", ".jsonl", ".md"} or name == ".gitkeep"
    if parts[0] == "submission" and len(parts) >= 2 and parts[1] == "final":
        return suffix in FINAL_REPORT_SUFFIXES or name == ".gitkeep"
    return False


def _blob_by_path(root: Path, commit: str, path: str) -> SnapshotFile:
    # Callers only pass a path already present in `git ls-tree` and validated.
    object_id = _run_git(root, "rev-parse", f"{commit}:{path}").decode("ascii").strip()
    data = _run_git(root, "cat-file", "blob", object_id)
    return SnapshotFile(path, data, object_id)


def _manifest_text(manifest: dict[str, Any], keys: tuple[str, ...]) -> str:
    return " ".join(str(manifest.get(key, "")) for key in keys).casefold()


def _sample_permission_is_explicit(text: str) -> bool:
    return (
        ("可随" in text or "允许随" in text)
        and "比赛提交" in text
        and "团队演示包" in text
        and "分发" in text
    )


def _is_original_synthetic(text: str) -> bool:
    explicitly_non_real = any(phrase in text for phrase in ("不对应真实", "非真实", "不是任何真实"))
    return "原创" in text and "模拟" in text and explicitly_non_real


def validate_sample_manifests(
    root: Path, source_commit: str, entries: list[GitEntry]
) -> list[SnapshotFile]:
    by_path = {entry.path: entry for entry in entries}
    candidate_pdfs = {
        path
        for path in by_path
        if (path.startswith("data/samples/annual/") or path.startswith("data/samples/quarterly/")) and PurePosixPath(path).suffix.lower() == ".pdf"
    }
    sample_roots = ("data/samples/annual", "data/samples/quarterly")
    manifested: dict[str, str] = {}
    includes: dict[str, SnapshotFile] = {}

    for sample_root in sample_roots:
        manifest_path = f"{sample_root}/source_manifest.json"
        if manifest_path not in by_path:
            if any(path.startswith(sample_root + "/") for path in candidate_pdfs):
                raise PackageError(f"原创样例 PDF 缺少受控 source_manifest.json：{sample_root}")
            continue
        manifest_file = _blob_by_path(root, source_commit, manifest_path)
        try:
            manifest = json.loads(manifest_file.data.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise PackageError(f"样例来源 manifest 无法解析：{manifest_path}") from exc
        if not isinstance(manifest, dict):
            raise PackageError(f"样例来源 manifest 顶层必须是 object：{manifest_path}")
        expected_dataset = {
            "data/samples/annual": "fintrace-annual-synthetic-v1",
            "data/samples/quarterly": "fintrace-quarterly-synthetic-v1",
        }[sample_root]
        if manifest.get("dataset_id") != expected_dataset:
            raise PackageError(f"样例 dataset_id 不属于已审阅的原创数据集：{manifest_path}")
        top_text = _manifest_text(manifest, ("nature", "data_nature", "use_conditions"))
        generator = manifest.get("generator")
        generator_path = generator.get("path") if isinstance(generator, dict) else generator
        expected_generator = f"scripts/prepare_{'annual' if sample_root.endswith('/annual') else 'quarterly'}_samples.py"
        if generator_path != expected_generator:
            raise PackageError(f"样例 manifest generator 与已审阅数据集不符：{manifest_path}")
        if sample_root.endswith("/annual"):
            if manifest.get("data_nature") != "original_synthetic":
                raise PackageError(f"年度样例 data_nature 必须是 original_synthetic：{manifest_path}")
            records = manifest.get("records")
            if "documents" in manifest:
                raise PackageError(f"年度样例必须使用受控 records schema：{manifest_path}")
        else:
            records = manifest.get("documents")
            if "records" in manifest:
                raise PackageError(f"季度样例必须使用受控 documents schema：{manifest_path}")
        if not isinstance(records, list) or not records:
            raise PackageError(f"样例 manifest 没有 records/documents：{manifest_path}")
        if not _is_original_synthetic(top_text):
            raise PackageError(f"样例 manifest 未明确标明原创模拟且不对应真实披露：{manifest_path}")

        distribution = manifest.get("distribution")
        if distribution is not None:
            if not isinstance(distribution, dict) or distribution.get("competition_submission") is not True or distribution.get("team_demonstration_bundle") is not True:
                raise PackageError(f"样例 manifest 未允许比赛提交与团队演示分发：{manifest_path}")
            if distribution.get("model_training") is not False or distribution.get("fraud_label") is not False:
                raise PackageError(f"样例 manifest 未明确排除模型训练和舞弊标签用途：{manifest_path}")
        elif not _sample_permission_is_explicit(str(manifest.get("use_conditions", ""))):
            raise PackageError(f"样例 manifest 未提供明确分发条件：{manifest_path}")

        includes[manifest_path] = manifest_file
        for record in records:
            if not isinstance(record, dict):
                raise PackageError(f"样例 manifest 含非 object 记录：{manifest_path}")
            record_text = _manifest_text(record, ("nature", "use_conditions", "license")) + " " + top_text
            if not _is_original_synthetic(record_text):
                raise PackageError(f"样例记录未明确标为原创模拟/非真实：{manifest_path}")
            conditions = str(record.get("use_conditions", manifest.get("use_conditions", "")))
            if distribution is None and not _sample_permission_is_explicit(conditions):
                raise PackageError(f"样例记录未明确允许分发：{manifest_path}")
            license_text = str(record.get("license", "")).strip()
            if license_text and not _sample_permission_is_explicit(license_text + " " + conditions):
                # A separate license may state permission in English; the use conditions
                # remain the authoritative explicit scope for this controlled package.
                if not _sample_permission_is_explicit(conditions):
                    raise PackageError(f"样例许可范围不明确：{manifest_path}")
            if not license_text and not conditions:
                raise PackageError(f"样例记录缺少许可或使用条件：{manifest_path}")

            if "file" in record:
                if not sample_root.endswith("/annual") or record.get("not_a_fraud_label") is not True:
                    raise PackageError(f"年度样例记录须使用受控 schema 并明确排除舞弊标签：{manifest_path}")
                filename = str(record["file"])
                if "/" in filename or "\\" in filename or PurePosixPath(filename).name != filename:
                    raise PackageError(f"年度样例 manifest 文件名必须是 basename：{filename!r}")
                pdf_path = f"{sample_root}/{filename}"
                checksum = record.get("sha256")
            else:
                if not sample_root.endswith("/quarterly"):
                    raise PackageError(f"非季度样例记录不能使用 source_pdf schema：{manifest_path}")
                pdf_path = str(record.get("source_pdf", ""))
                prefix = sample_root + "/"
                if not pdf_path.startswith(prefix) or PurePosixPath(pdf_path).name != pdf_path[len(prefix):]:
                    raise PackageError(f"季度样例 source_pdf 必须位于登记目录且不能含子路径：{pdf_path!r}")
                checksum = record.get("source_sha256")
                if not str(record.get("source_url", "")).startswith("original-synthetic://"):
                    raise PackageError(f"季度样例 source_url 必须标识原创模拟来源：{manifest_path}")
            validate_git_path(pdf_path)
            if PurePosixPath(pdf_path).suffix.lower() != ".pdf":
                raise PackageError(f"manifest 只能将 PDF 登记为样例输入：{pdf_path}")
            if pdf_path in manifested:
                raise PackageError(f"样例 PDF 在 manifest 中重复登记：{pdf_path}")
            if not isinstance(checksum, str) or SHA256_RE.fullmatch(checksum.lower()) is None:
                raise PackageError(f"样例 PDF 缺少有效 SHA256：{pdf_path}")
            if pdf_path not in by_path:
                raise PackageError(f"样例 PDF 未包含在指定 Git commit 中：{pdf_path}")
            pdf = _blob_by_path(root, source_commit, pdf_path)
            actual = sha256_bytes(pdf.data)
            if actual != checksum.lower():
                raise PackageError(f"样例 PDF SHA256 与受控 manifest 不符：{pdf_path}")
            manifested[pdf_path] = actual
            includes[pdf_path] = pdf

        for path in by_path:
            if not path.startswith(sample_root + "/"):
                continue
            filename = PurePosixPath(path).name
            if filename in {"README.md", "useconditions.md"} or path == manifest_path:
                file = _blob_by_path(root, source_commit, path)
                includes[path] = file

    if candidate_pdfs != set(manifested):
        missing = sorted(candidate_pdfs - set(manifested))
        absent = sorted(set(manifested) - candidate_pdfs)
        raise PackageError(f"样例 PDF 清单不完整：未登记={missing}; Git 中不存在={absent}")
    return [includes[path] for path in sorted(includes)]


def ensure_required_snapshot(snapshot: dict[str, SnapshotFile]) -> None:
    required = ROOT_FILES | {
        "backend/pyproject.toml",
        LOCK_PATH,
        "backend/src/finagent/api/app.py",
        "frontend/index.html",
        "frontend/package.json",
        "frontend/package-lock.json",
        "frontend/tsconfig.json",
        "frontend/vite.config.ts",
        "frontend/src/main.tsx",
        "evaluation/README.md",
        "scripts/analyze_annual.py",
    }
    missing = sorted(required - snapshot.keys())
    if missing:
        raise PackageError("Git 源码快照缺少决赛源码包必需文件：" + ", ".join(missing))
    if not any(path.startswith("backend/src/finagent/") and path.endswith(".py") for path in snapshot):
        raise PackageError("Git 源码快照没有后端 Python 实现")
    if not any(path.startswith("config/prompts/") and path.endswith(".md") for path in snapshot):
        raise PackageError("Git 源码快照没有程序提示词")
    if not any(path.startswith("frontend/src/") for path in snapshot):
        raise PackageError("Git 源码快照没有前端源码")


def validate_empty_env_example(data: bytes) -> None:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError as exc:
        raise PackageError(".env.example 不是 UTF-8") from exc
    found: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if match:
            key, value = match.groups()
            if key not in {"MODEL_BASE_URL", "MODEL_API_KEY", "MODEL_NAME"}:
                raise PackageError(f".env.example 含源码包不允许的环境变量：{key}")
            if key in found:
                raise PackageError(f".env.example 重复定义 {key}")
            found[key] = value.strip().strip("\"'")
        else:
            raise PackageError(".env.example 含无法验证的环境变量赋值")
    required = {"MODEL_BASE_URL", "MODEL_API_KEY", "MODEL_NAME"}
    if set(found) != required or any(value for value in found.values()):
        raise PackageError(".env.example 必须包含且仅包含空值模型配置项")


def scan_for_credentials(files: dict[str, SnapshotFile]) -> None:
    forbidden_names = {".env", ".env.model", ".env.local", "credentials.json", "secrets.json"}
    suspicious: list[str] = []
    for path, source in files.items():
        name = PurePosixPath(path).name.casefold()
        if name in forbidden_names or (name.startswith(".env") and path != ".env.example"):
            raise PackageError(f"凭据或本机环境文件不允许进入源码包：{path}")
        if path == ".env.example":
            continue
        if any(pattern.search(source.data) for pattern in SUSPICIOUS_SECRET_PATTERNS):
            suspicious.append(path)
        if re.search(rb"(?im)^\s*MODEL_API_KEY\s*=\s*\S+", source.data):
            suspicious.append(path)
    if suspicious:
        raise PackageError("源码内容命中凭据特征，拒绝打包：" + ", ".join(sorted(set(suspicious)))
        )


def _packaging_modules() -> tuple[Any, Any, Any]:
    try:
        from packaging.markers import default_environment
        from packaging.requirements import Requirement
        from packaging.utils import canonicalize_name
    except ImportError as exc:
        raise PackageError("构建器需要当前开发/测试环境中的 packaging 元数据解析库") from exc
    return Requirement, canonicalize_name, default_environment


def runtime_closure() -> list[metadata.Distribution]:
    Requirement, canonicalize_name, default_environment = _packaging_modules()
    distributions: dict[str, metadata.Distribution] = {}
    for dist in metadata.distributions():
        name = dist.metadata.get("Name")
        if name:
            canonical = canonicalize_name(name)
            if canonical in distributions and distributions[canonical].version != dist.version:
                raise PackageError(f"本机有多个版本的运行依赖：{name}")
            distributions[canonical] = dist

    selected: dict[str, metadata.Distribution] = {}
    requested_extras: dict[str, set[str]] = defaultdict(set)
    processed_extras: dict[str, set[str]] = defaultdict(set)
    pending: deque[str] = deque(RUNTIME_ROOTS)
    environment = default_environment()
    while pending:
        canonical = canonicalize_name(pending.popleft())
        dist = distributions.get(canonical)
        if dist is None:
            raise PackageError(f"当前运行依赖闭包缺少已安装 distribution：{canonical}")
        selected[canonical] = dist
        contexts = {"", *requested_extras[canonical]}
        new_contexts = contexts - processed_extras[canonical]
        if not new_contexts:
            continue
        processed_extras[canonical].update(new_contexts)
        for raw in dist.requires or ():
            requirement = Requirement(raw)
            enabled = requirement.marker is None or any(
                requirement.marker.evaluate({**environment, "extra": extra}) for extra in contexts
            )
            if not enabled:
                continue
            child_name = canonicalize_name(requirement.name)
            child = distributions.get(child_name)
            if child is None:
                raise PackageError(f"本机运行依赖闭包缺少 {requirement.name}（由 {dist.metadata['Name']} 声明）")
            if requirement.specifier and not requirement.specifier.contains(child.version, prereleases=True):
                raise PackageError(
                    f"本机版本不满足依赖声明：{dist.metadata['Name']} -> {requirement}; installed={child.version}"
                )
            new_child_extras = set(requirement.extras) - requested_extras[child_name]
            if new_child_extras:
                requested_extras[child_name].update(new_child_extras)
                pending.append(child_name)
            elif child_name not in selected:
                pending.append(child_name)
    return sorted(selected.values(), key=lambda dist: dist.metadata["Name"].casefold())


def distribution_license(dist: metadata.Distribution) -> tuple[str, str]:
    if dist.metadata.get("License-Expression"):
        return "License-Expression", str(dist.metadata.get("License-Expression"))
    if dist.metadata.get("License"):
        return "License", str(dist.metadata.get("License"))
    return "License metadata", "Not declared in installed metadata"


def distribution_urls(dist: metadata.Distribution) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for value in dist.metadata.get_all("Project-URL") or ():
        if "," in value:
            label, url = value.split(",", 1)
            rows.append({"label": label.strip(), "url": url.strip()})
        elif value.startswith(("https://", "http://")):
            rows.append({"label": "Project-URL", "url": value.strip()})
    homepage = dist.metadata.get("Home-page")
    if homepage and not any(row["url"] == homepage for row in rows):
        rows.insert(0, {"label": "Home-page", "url": str(homepage)})
    return rows


def runtime_metadata_rows(distributions: list[metadata.Distribution]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for dist in distributions:
        field, license_value = distribution_license(dist)
        name = str(dist.metadata["Name"])
        rows.append(
            {
                "name": name,
                "version": dist.version,
                "license_field": field,
                "license": license_value,
                "pypi": f"https://pypi.org/project/{name}/{dist.version}/",
                "upstream": distribution_urls(dist),
            }
        )
    return rows


def render_runtime_lock(distributions: list[metadata.Distribution] | None = None) -> str:
    packages = distributions or runtime_closure()
    roots = ", ".join(RUNTIME_ROOTS)
    lines = [
        "# FINTRACE final source package: installed runtime closure version pins.",
        LOCK_SCOPE_LINE,
        f"# Roots: {roots}.",
        "# Runtime markers were evaluated on the environment above; this is not a cross-platform lock.",
        "# pytest and other test-only dependencies come from the [test] extra; httpx is pinned here because runtime closure requires it.",
    ]
    lines.extend(f"{dist.metadata['Name']}=={dist.version}" for dist in packages)
    return "\n".join(lines) + "\n"


def validate_runtime_lock(data: bytes, distributions: list[metadata.Distribution]) -> dict[str, str]:
    Requirement, canonicalize_name, _ = _packaging_modules()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError as exc:
        raise PackageError(f"{LOCK_PATH} 不是 UTF-8") from exc
    if LOCK_SCOPE_LINE not in text.splitlines():
        raise PackageError(f"{LOCK_PATH} 未限定為 {TARGET_ENVIRONMENT}")
    pins: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            requirement = Requirement(stripped)
        except Exception as exc:
            raise PackageError(f"{LOCK_PATH} 中存在非精確依賴 pin：{line}") from exc
        if requirement.marker is not None or requirement.extras or len(requirement.specifier) != 1:
            raise PackageError(f"{LOCK_PATH} 中只允許無 marker 的精確 == pin：{line}")
        specifier = next(iter(requirement.specifier))
        if specifier.operator != "==" or "*" in specifier.version:
            raise PackageError(f"{LOCK_PATH} 中只允許精確 == pin：{line}")
        key = canonicalize_name(requirement.name)
        if key in pins:
            raise PackageError(f"{LOCK_PATH} 重複 pin：{requirement.name}")
        pins[key] = specifier.version
    expected = {canonicalize_name(dist.metadata["Name"]): dist.version for dist in distributions}
    if pins != expected:
        missing = sorted(set(expected) - set(pins))
        extra = sorted(set(pins) - set(expected))
        mismatched = sorted(name for name in set(pins) & set(expected) if pins[name] != expected[name])
        raise PackageError(
            f"{LOCK_PATH} 與本機 runtime dependency closure 不符；missing={missing}, extra={extra}, version_mismatch={mismatched}"
        )
    if not set(RUNTIME_ROOTS).issubset(pins):
        raise PackageError("运行版本锁缺少一个或多个指定 runtime roots")
    return pins


def refresh_runtime_lock(root: Path) -> Path:
    """Write the measured lock file explicitly; it never runs during package build."""
    if not runtime_environment_matches():
        raise PackageError(f"只允许在 {TARGET_ENVIRONMENT} 环境生成本机受测锁")
    destination = root / LOCK_PATH
    with destination.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(render_runtime_lock())
    return destination


def runtime_environment_matches() -> bool:
    return (
        sys.platform == "win32"
        and sysconfig.get_platform() == "win-amd64"
        and sys.version_info[:3] == (3, 14, 3)
        and sys.maxsize > 2**32
    )


def verify_runtime_environment() -> None:
    if not runtime_environment_matches():
        raise PackageError(f"源码包当前受测依赖锁只适用于 {TARGET_ENVIRONMENT}")


def _is_reparse_or_symlink(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _ensure_plain_directory_chain(root: Path, destination: Path) -> None:
    root_resolved = root.resolve(strict=True)
    try:
        relative = destination.relative_to(root)
    except ValueError as exc:
        raise PackageError(f"输出路径越出项目根目录：{destination}") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() or _is_reparse_or_symlink(current):
            if _is_reparse_or_symlink(current):
                raise PackageError(f"拒绝经过符号链接或 reparse point：{current}")
            if current.resolve(strict=True) != root_resolved and root_resolved not in current.resolve(strict=True).parents:
                raise PackageError(f"输出路径解析后越出工作区：{current}")


def _zip_rel(path: str) -> str:
    validate_git_path(path)
    return f"{ZIP_ROOT}/{path}"


def _safe_extract_member(name: str) -> str:
    prefix = ZIP_ROOT + "/"
    if not name.startswith(prefix):
        raise PackageError(f"ZIP 条目不在固定目录 {ZIP_ROOT}/ 下：{name}")
    relative = name[len(prefix):]
    validate_git_path(relative)
    return relative


def _inventory_row(path: str, data: bytes, provenance: str, git_blob: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "path": path,
        "bytes": len(data),
        "sha256": sha256_bytes(data),
        "provenance": provenance,
    }
    if git_blob is not None:
        row["git_blob"] = git_blob
    return row


def _asset_checksums(files: dict[str, SnapshotFile]) -> dict[str, Any]:
    def rows(prefix: str) -> list[dict[str, str]]:
        return [
            {"path": path, "sha256": sha256_bytes(source.data)}
            for path, source in sorted(files.items())
            if path.startswith(prefix)
        ]

    frontend_lock = files["frontend/package-lock.json"]
    runtime_lock = files[LOCK_PATH]
    return {
        "prompts": rows("config/prompts/"),
        "rules": rows("config/rules/"),
        "frontend_lock": {"path": "frontend/package-lock.json", "sha256": sha256_bytes(frontend_lock.data)},
        "runtime_lock": {"path": LOCK_PATH, "sha256": sha256_bytes(runtime_lock.data)},
    }


def _verify_generated_license_source(dist: metadata.Distribution, entry: Any) -> tuple[bytes, str]:
    text = str(entry).replace("\\", "/")
    relative = PurePosixPath(text)
    windows = PureWindowsPath(text)
    if relative.is_absolute() or windows.is_absolute() or windows.drive or ".." in relative.parts:
        raise PackageError(f"安装 distribution 的许可证 RECORD 路径不安全：{text}")
    if not any(token in relative.name.casefold() for token in ("license", "copying", "notice", "copyright")):
        raise PackageError(f"非许可证文件进入许可证收集：{text}")
    source = Path(dist.locate_file(entry))
    base = Path(dist.locate_file("")).resolve(strict=True)
    try:
        resolved_source = source.resolve(strict=True)
        resolved_source.relative_to(base)
    except (OSError, ValueError) as exc:
        raise PackageError(f"已安装依赖许可证解析后越出 site-packages：{relative.name}") from exc
    if _is_reparse_or_symlink(source) or not resolved_source.is_file():
        raise PackageError(f"已安装依赖的许可证文件不可读取或为链接：{relative.name}")
    data = resolved_source.read_bytes()
    if len(data) > 2 * 1024 * 1024:
        raise PackageError(f"许可证文件超出 2 MiB 限制：{relative.name}")
    return data, text


def collect_runtime_licenses(
    stage: Path, distributions: list[metadata.Distribution]
) -> tuple[dict[str, Any], list[tuple[str, bytes]]]:
    files_to_write: list[tuple[str, bytes]] = []
    package_rows: list[dict[str, Any]] = []
    for dist in distributions:
        name = str(dist.metadata["Name"])
        version = dist.version
        field, license_value = distribution_license(dist)
        files = list(dist.files or ())
        candidates = [
            entry
            for entry in files
            if any(token in PurePosixPath(str(entry).replace("\\", "/")).name.casefold() for token in ("license", "copying", "notice", "copyright"))
        ]
        deduplicated: dict[str, Any] = {}
        for entry in candidates:
            deduplicated[str(entry).replace("\\", "/")] = entry
        attached: list[dict[str, str]] = []
        target_folder = f"THIRD_PARTY_LICENSES/python/{re.sub(r'[^A-Za-z0-9._-]+', '_', name)}-{version}"
        used_names: set[str] = set()
        for source_name, entry in sorted(deduplicated.items()):
            data, normalized_source = _verify_generated_license_source(dist, entry)
            basename = PurePosixPath(normalized_source).name
            target_name = basename
            counter = 1
            while target_name.casefold() in used_names:
                stem = PurePosixPath(basename).stem
                suffix = PurePosixPath(basename).suffix
                target_name = f"{stem}-{counter}{suffix}"
                counter += 1
            used_names.add(target_name.casefold())
            target_path = f"{target_folder}/{target_name}"
            files_to_write.append((target_path, data))
            attached.append(
                {
                    "path": target_path,
                    "source_metadata_path": normalized_source,
                    "bytes": len(data),
                    "sha256": sha256_bytes(data),
                }
            )
        package_rows.append(
            {
                "name": name,
                "version": version,
                "license_metadata_field": field,
                "license_metadata_value": license_value,
                "pypi": f"https://pypi.org/project/{name}/{version}/",
                "upstream": distribution_urls(dist),
                "license_files": attached,
            }
        )
    manifest = {
        "scope": TARGET_ENVIRONMENT,
        "runtime_roots": list(RUNTIME_ROOTS),
        "runtime_python_packages": package_rows,
        "notice": "Installed metadata and bundled license texts are recorded as source references; this is not a complete distribution-law review.",
    }
    return manifest, files_to_write


def _write_file(stage: Path, relative: str, data: bytes) -> None:
    validate_git_path(relative)
    target = stage.joinpath(*PurePosixPath(relative).parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or _is_reparse_or_symlink(target):
        raise PackageError(f"staging 中拒绝覆盖已有路径：{relative}")
    target.write_bytes(data)


def _write_zip(zip_path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(zip_path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
        for archive_path in sorted(members):
            validate_git_path(archive_path)
            info = zipfile.ZipInfo(archive_path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.flag_bits = 0x800
            archive.writestr(info, members[archive_path])


def _verify_internal_inventory(members: dict[str, bytes], package_metadata: dict[str, Any]) -> None:
    expected_rows = package_metadata.get("files")
    if not isinstance(expected_rows, list):
        raise PackageError("SOURCE_PACKAGE.json 缺少 file inventory")
    expected = {row.get("path"): row for row in expected_rows if isinstance(row, dict)}
    actual_paths = {path[len(ZIP_ROOT) + 1:]: data for path, data in members.items() if path != f"{ZIP_ROOT}/SOURCE_PACKAGE.json"}
    if set(expected) != set(actual_paths):
        raise PackageError("SOURCE_PACKAGE.json inventory 路径与 ZIP 内容不一致")
    for path, data in actual_paths.items():
        row = expected[path]
        if row.get("bytes") != len(data) or row.get("sha256") != sha256_bytes(data):
            raise PackageError(f"SOURCE_PACKAGE.json inventory 校验失败：{path}")
    checksums = package_metadata.get("behavior_assets")
    if not isinstance(checksums, dict):
        raise PackageError("SOURCE_PACKAGE.json 缺少提示词/规则/lock 校验记录")
    for group in ("prompts", "rules"):
        rows = checksums.get(group)
        if not isinstance(rows, list):
            raise PackageError(f"behavior_assets.{group} 必须是 list")
        for row in rows:
            path = row.get("path")
            data = actual_paths.get(path)
            if data is None or sha256_bytes(data) != row.get("sha256"):
                raise PackageError(f"提示词/规则哈希校验失败：{path}")
        prefix = f"config/{group}/"
        actual_asset_paths = {
            path
            for path in actual_paths
            if path.startswith(prefix)
        }
        if {row.get("path") for row in rows} != actual_asset_paths:
            raise PackageError(f"{group} 校验清单与 ZIP 实际资产不完整匹配")
    for key in ("frontend_lock", "runtime_lock"):
        row = checksums.get(key)
        if not isinstance(row, dict):
            raise PackageError(f"缺少 {key} 哈希记录")
        expected_path = "frontend/package-lock.json" if key == "frontend_lock" else LOCK_PATH
        if row.get("path") != expected_path:
            raise PackageError(f"{key} 清单指向非预期文件")
        data = actual_paths.get(expected_path)
        if data is None or sha256_bytes(data) != row.get("sha256"):
            raise PackageError(f"{key} 哈希校验失败")
    license_manifest_path = "THIRD_PARTY_LICENSES/python/manifest.json"
    license_manifest_data = actual_paths.get(license_manifest_path)
    if license_manifest_data is None:
        raise PackageError("ZIP 缺少 runtime third-party license manifest")
    try:
        license_manifest = json.loads(license_manifest_data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PackageError("runtime third-party license manifest 无法解析") from exc
    if (
        license_manifest.get("scope") != package_metadata.get("tested_environment")
        or license_manifest.get("runtime_roots") != package_metadata.get("runtime_roots")
        or package_metadata.get("license_inventory") != license_manifest_path
    ):
        raise PackageError("third-party license manifest 与受测运行环境/roots 不一致")
    license_packages = license_manifest.get("runtime_python_packages")
    if not isinstance(license_packages, list):
        raise PackageError("third-party license manifest 缺少依赖清单")
    runtime_rows_for_licenses = package_metadata.get("runtime_packages")
    runtime_ids = {
        (str(row.get("name", "")).casefold(), str(row.get("version", "")))
        for row in runtime_rows_for_licenses
        if isinstance(row, dict)
    } if isinstance(runtime_rows_for_licenses, list) else set()
    license_ids = {
        (str(row.get("name", "")).casefold(), str(row.get("version", "")))
        for row in license_packages
        if isinstance(row, dict)
    }
    if license_ids != runtime_ids or len(license_ids) != len(license_packages):
        raise PackageError("third-party license manifest 依赖项与 runtime pins 不一致")
    referenced_license_paths = {license_manifest_path}
    for package in license_packages:
        license_files = package.get("license_files")
        if not isinstance(license_files, list):
            raise PackageError("third-party license manifest 的 license_files 格式无效")
        for license_file in license_files:
            if not isinstance(license_file, dict):
                raise PackageError("third-party license manifest 含无效许可证条目")
            path = license_file.get("path")
            source_metadata_path = license_file.get("source_metadata_path")
            if not isinstance(path, str) or not path.startswith("THIRD_PARTY_LICENSES/python/"):
                raise PackageError("third-party license path 越出专属许可目录")
            if not isinstance(source_metadata_path, str):
                raise PackageError("third-party license 缺少相对 metadata 来源路径")
            source_path = PurePosixPath(source_metadata_path)
            source_windows = PureWindowsPath(source_metadata_path)
            if (
                source_path.is_absolute()
                or source_windows.is_absolute()
                or source_windows.drive
                or ".." in source_path.parts
                or "\\" in source_metadata_path
            ):
                raise PackageError("third-party license manifest 含宿主绝对路径或越界 metadata 路径")
            data = actual_paths.get(path)
            if (
                data is None
                or license_file.get("bytes") != len(data)
                or license_file.get("sha256") != sha256_bytes(data)
            ):
                raise PackageError(f"third-party license 文件长度/SHA256 不匹配：{path}")
            if path in referenced_license_paths:
                raise PackageError(f"third-party license 路径重复：{path}")
            referenced_license_paths.add(path)
    actual_license_paths = {
        path for path in actual_paths if path.startswith("THIRD_PARTY_LICENSES/python/")
    }
    if referenced_license_paths != actual_license_paths:
        raise PackageError("third-party license manifest 与 ZIP 许可证文件集合不一致")
    runtime_rows = package_metadata.get("runtime_packages")
    if not isinstance(runtime_rows, list):
        raise PackageError("SOURCE_PACKAGE.json 缺少受测依赖版本清单")
    pins = {}
    for line in actual_paths[LOCK_PATH].decode("utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            name, sep, version = line.partition("==")
            if not sep:
                raise PackageError("运行依赖锁存在非精确 pin")
            pins[name.casefold().replace("_", "-")] = version
    declared = {
        re.sub(r"[-_.]+", "-", str(row["name"])).casefold(): str(row["version"])
        for row in runtime_rows
    }
    if pins != declared:
        raise PackageError("源码包依赖锁与受测 runtime version list 不一致")
    validate_empty_env_example(actual_paths[".env.example"])

    sample_rows = package_metadata.get("sample_inputs")
    if not isinstance(sample_rows, list):
        raise PackageError("SOURCE_PACKAGE.json 缺少样例输入登记")
    actual_sample_paths = {
        path
        for path in actual_paths
        if (path.startswith("data/samples/annual/") or path.startswith("data/samples/quarterly/")) and PurePosixPath(path).suffix.lower() == ".pdf"
    }
    declared_samples: dict[str, dict[str, Any]] = {}
    for row in sample_rows:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise PackageError("SOURCE_PACKAGE.json 含无效样例输入记录")
        path = row["path"]
        data = actual_paths.get(path)
        if data is None or PurePosixPath(path).suffix.lower() != ".pdf":
            raise PackageError(f"样例输入清单指向缺失或非 PDF 文件：{path}")
        if row.get("sha256") != sha256_bytes(data) or row.get("bytes") != len(data):
            raise PackageError(f"样例输入 SHA256/长度校验失败：{path}")
        if row.get("nature") != "original_synthetic; see committed source_manifest.json":
            raise PackageError(f"样例输入性质缺少来源 manifest 绑定：{path}")
        if path in declared_samples:
            raise PackageError(f"样例输入重复登记：{path}")
        declared_samples[path] = row
    if set(declared_samples) != actual_sample_paths:
        raise PackageError("ZIP 中的样例 PDF 与 SOURCE_PACKAGE.json 样例清单不一致")


def verify_artifact_directory(package_dir: Path) -> dict[str, Any]:
    if _is_reparse_or_symlink(package_dir):
        raise PackageError(f"拒绝验证符号链接或 junction 包目录：{package_dir}")
    package_id = package_dir.name
    validate_package_id(package_id)
    zip_path = package_dir / f"{package_id}.zip"
    sidecar_path = package_dir / f"{package_id}.sha256"
    manifest_path = package_dir / f"{package_id}.manifest.json"
    try:
        sidecar = sidecar_path.read_text(encoding="ascii").strip()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PackageError(f"源码包外部清单或 SHA sidecar 无法读取：{package_dir}") from exc
    if manifest.get("package_id") != package_id or manifest.get("zip") != zip_path.name:
        raise PackageError("外部 manifest 的 package_id/ZIP 名称不一致")
    actual_sha = sha256_file(zip_path)
    actual_bytes = zip_path.stat().st_size
    if sidecar != f"{actual_sha}  {zip_path.name}" or manifest.get("sha256") != actual_sha or manifest.get("bytes") != actual_bytes:
        raise PackageError("ZIP SHA256/长度与外部 manifest 或 .sha256 不一致")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise PackageError("外部 manifest 缺少逐文件 inventory")
    expected: dict[str, dict[str, Any]] = {}
    for row in files:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise PackageError("外部 manifest 含有无效 inventory 项")
        path = row["path"]
        _safe_extract_member(path)
        if path in expected:
            raise PackageError(f"外部 inventory 路径重复：{path}")
        expected[path] = row
    seen: set[str] = set()
    members: dict[str, bytes] = {}
    with zipfile.ZipFile(zip_path, "r") as archive:
        for item in archive.infolist():
            if item.is_dir():
                continue
            relative = _safe_extract_member(item.filename)
            if item.filename in seen:
                raise PackageError(f"ZIP 内重复路径：{item.filename}")
            seen.add(item.filename)
            mode = (item.external_attr >> 16) & 0xFFFF
            if item.create_system == 3 and stat.S_ISLNK(mode):
                raise PackageError(f"ZIP 内存在符号链接：{item.filename}")
            if _is_excluded_archive_path(relative):
                raise PackageError(f"ZIP 包含禁止路径：{relative}")
            if not _allowed_archive_source_path(relative):
                raise PackageError(f"ZIP 条目不属于源码包允许的文件范围：{relative}")
            data = archive.read(item)
            row = expected.get(item.filename)
            if row is None or row.get("bytes") != len(data) or row.get("sha256") != sha256_bytes(data):
                raise PackageError(f"逐文件 inventory 校验失败：{item.filename}")
            members[item.filename] = data
    if seen != set(expected):
        raise PackageError(f"ZIP 与外部 inventory 项不一致：missing={sorted(set(expected)-seen)}, extra={sorted(seen-set(expected))}")
    metadata_path = f"{ZIP_ROOT}/SOURCE_PACKAGE.json"
    if metadata_path not in members:
        raise PackageError("ZIP 缺少 SOURCE_PACKAGE.json")
    internal = json.loads(members[metadata_path].decode("utf-8"))
    if internal.get("package_id") != package_id or internal.get("format_version") != FORMAT_VERSION:
        raise PackageError("ZIP 内 SOURCE_PACKAGE.json 身份或格式版本不匹配")
    if (
        internal.get("source", {}).get("git_commit") != manifest.get("source_commit")
        or internal.get("source", {}).get("git_tree") != manifest.get("source_tree")
        or internal.get("tested_environment") != manifest.get("tested_environment")
        or internal.get("fixed_zip_root") != manifest.get("fixed_zip_root")
    ):
        raise PackageError("ZIP 内外的源码快照、平台或 ZIP 根目录声明不一致")
    _verify_internal_inventory(members, internal)
    for relative in members:
        if relative != metadata_path and _is_git_source_path(relative[len(ZIP_ROOT) + 1:]):
            if PurePosixPath(relative).name != ".env.example" and any(
                pattern.search(members[relative]) for pattern in SUSPICIOUS_SECRET_PATTERNS
            ):
                raise PackageError(f"ZIP 中源码命中凭据特征：{relative}")
    return manifest


def _is_git_source_path(path: str) -> bool:
    return not path.startswith("THIRD_PARTY_LICENSES/")


def _allowed_archive_source_path(path: str) -> bool:
    if path == "SOURCE_PACKAGE.json" or path.startswith("THIRD_PARTY_LICENSES/python/"):
        return True
    if selected_source_file(path):
        return True
    parts = path.split("/")
    if len(parts) == 3 and parts[:2] in (["data", "samples"],):
        return False
    if len(parts) >= 4 and parts[:2] == ["data", "samples"] and parts[2] in {"annual", "quarterly"}:
        basename = parts[-1]
        return PurePosixPath(basename).suffix.lower() == ".pdf" or basename in {
            "README.md",
            "source_manifest.json",
            "useconditions.md",
        }
    return False


def _is_excluded_archive_path(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1].casefold()
    if name.startswith(".env") and path != ".env.example":
        return True
    if any(part.casefold() in FORBIDDEN_PARTS for part in parts):
        return True
    return False


def build_source_package(
    root: Path,
    source_commit: str,
    package_id: str,
) -> tuple[Path, dict[str, Any]]:
    validate_package_id(package_id)
    verify_runtime_environment()
    root = root.resolve(strict=True)
    commit, tree, files = read_commit_snapshot(root, source_commit)
    distributions = runtime_closure()
    validate_runtime_lock(files[LOCK_PATH].data, distributions)
    runtime_rows = runtime_metadata_rows(distributions)

    package_dir = root / "artifacts" / "packages" / package_id
    tmp_task_dir = root / "artifacts" / "tmp" / f"final-source-package-{package_id}"
    evaluation_dir = root / "artifacts" / "evaluations" / f"final-source-package-{package_id}"
    for destination in (package_dir, tmp_task_dir, evaluation_dir):
        _ensure_plain_directory_chain(root, destination)
        if destination.exists():
            raise FileExistsError(f"package ID / task ID 已存在，拒绝覆盖：{destination.relative_to(root)}")

    # The ignored artifacts skeleton may not exist on a fresh checkout. Create
    # only the owned destination parents, then re-check every component for
    # junctions/reparse points before making the unique leaf directories.
    for parent in (package_dir.parent, tmp_task_dir.parent, evaluation_dir.parent):
        _ensure_plain_directory_chain(root, parent)
        parent.mkdir(parents=True, exist_ok=True)
        _ensure_plain_directory_chain(root, parent)

    # Package and evaluation IDs are created exclusively; temporary staging is
    # retained under the project's required task-specific tmp directory.
    tmp_task_dir.mkdir(parents=True)
    stage = tmp_task_dir / f"staging-{uuid.uuid4().hex}"
    stage.mkdir()
    staged: dict[str, bytes] = {}
    source_inventory: list[dict[str, Any]] = []
    for path, source in sorted(files.items()):
        _write_file(stage, path, source.data)
        staged[path] = source.data
        source_inventory.append(_inventory_row(path, source.data, "git_commit_blob", source.git_blob))

    licenses, license_files = collect_runtime_licenses(stage, distributions)
    for path, data in license_files:
        _write_file(stage, path, data)
        staged[path] = data
        source_inventory.append(_inventory_row(path, data, "installed_distribution_license"))
    license_manifest_path = "THIRD_PARTY_LICENSES/python/manifest.json"
    license_manifest_data = json.dumps(licenses, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    _write_file(stage, license_manifest_path, license_manifest_data)
    staged[license_manifest_path] = license_manifest_data
    source_inventory.append(
        _inventory_row(license_manifest_path, license_manifest_data, "installed_distribution_license_manifest")
    )

    package_metadata = {
        "format_version": FORMAT_VERSION,
        "package_id": package_id,
        "fixed_zip_root": ZIP_ROOT,
        "source": {"git_commit": commit, "git_tree": tree, "construction": "git ls-tree + git cat-file blob"},
        "tested_environment": TARGET_ENVIRONMENT,
        "runtime_roots": list(RUNTIME_ROOTS),
        "runtime_packages": runtime_rows,
        "behavior_assets": _asset_checksums(files),
        "sample_inputs": _sample_input_records(files),
        "license_inventory": "THIRD_PARTY_LICENSES/python/manifest.json",
        "files": source_inventory,
        "limitations": [
            "This is a source package, not an embedded Python/Node runtime or a clean-machine installation test.",
            "Runtime pins describe the tested Windows x64 CPython 3.14.3 environment only.",
            "Real source PDFs not explicitly licensed by a committed sample manifest are excluded; use the official source guide and SHA in project data-policy documentation.",
            "Installed license metadata and copied texts do not constitute a complete distribution-law review.",
        ],
    }
    internal_manifest = json.dumps(package_metadata, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    _write_file(stage, "SOURCE_PACKAGE.json", internal_manifest)
    staged["SOURCE_PACKAGE.json"] = internal_manifest

    # Apply archive safety rules before writing the ZIP.
    staged_snapshot = {path: SnapshotFile(path, data, "") for path, data in staged.items()}
    scan_for_credentials(staged_snapshot)
    archive_members = {_zip_rel(path): data for path, data in staged.items()}
    _verify_internal_inventory(archive_members, package_metadata)

    temp_zip = tmp_task_dir / f"{package_id}-{uuid.uuid4().hex}.zip"
    _write_zip(temp_zip, archive_members)
    zip_digest = sha256_file(temp_zip)
    zip_size = temp_zip.stat().st_size
    external_manifest = {
        "format_version": FORMAT_VERSION,
        "package_id": package_id,
        "zip": f"{package_id}.zip",
        "bytes": zip_size,
        "sha256": zip_digest,
        "source_commit": commit,
        "source_tree": tree,
        "fixed_zip_root": ZIP_ROOT,
        "tested_environment": TARGET_ENVIRONMENT,
        "files": [
            {"path": _zip_rel(path), "bytes": len(data), "sha256": sha256_bytes(data)}
            for path, data in sorted(staged.items())
        ],
    }
    package_dir.mkdir()
    final_zip = package_dir / f"{package_id}.zip"
    temp_zip.replace(final_zip)
    (package_dir / f"{package_id}.sha256").write_text(f"{zip_digest}  {final_zip.name}\n", encoding="ascii")
    (package_dir / f"{package_id}.manifest.json").write_text(
        json.dumps(external_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    evaluation_dir.mkdir()
    verified = verify_artifact_directory(package_dir)
    report = {
        "package_id": package_id,
        "result": "passed",
        "source_commit": commit,
        "source_tree": tree,
        "zip": final_zip.name,
        "zip_bytes": zip_size,
        "zip_sha256": zip_digest,
        "file_count": len(verified["files"]),
        "tested_environment": TARGET_ENVIRONMENT,
        "verification": [
            "outer ZIP SHA256 and length",
            "external per-file byte length and SHA256 inventory",
            "fixed ZIP root and safe paths",
            "internal prompt/rule/frontend-lock/runtime-lock hashes",
            "runtime license manifest paths/bytes/SHA256 and host-path exclusion",
            "sample manifest nature/use-condition/SHA256 checks",
        ],
        "limitations": package_metadata["limitations"],
    }
    (evaluation_dir / "verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return final_zip, external_manifest


def _sample_input_records(files: dict[str, SnapshotFile]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path, source in sorted(files.items()):
        if not (path.startswith("data/samples/annual/") or path.startswith("data/samples/quarterly/")) or PurePosixPath(path).suffix.lower() != ".pdf":
            continue
        rows.append({"path": path, "bytes": len(source.data), "sha256": sha256_bytes(source.data), "nature": "original_synthetic; see committed source_manifest.json"})
    return rows


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-commit", help="必须是完整 40/64 位 Git commit ID；只从该 commit tree 读取源码")
    parser.add_argument("--package-id", help="新的唯一包标识；不会覆盖已有输出")
    parser.add_argument("--verify", type=Path, help="只读验证 artifacts/packages/<package_id> 目录")
    parser.add_argument("--write-runtime-lock", action="store_true", help="显式刷新本机受测环境的 backend/requirements-final-win.txt")
    args = parser.parse_args(argv)
    try:
        if args.verify:
            manifest = verify_artifact_directory(args.verify.resolve(strict=True))
            print(f"VERIFIED: {args.verify}")
            print(f"SOURCE COMMIT: {manifest['source_commit']}")
            print(f"ZIP SHA256: {manifest['sha256']}")
            return 0
        if args.write_runtime_lock:
            path = refresh_runtime_lock(_repo_root())
            print(f"WROTE: {path}")
            return 0
        if not args.source_commit or not args.package_id:
            parser.error("构建时必须同时提供 --source-commit 和 --package-id")
        zip_path, manifest = build_source_package(_repo_root(), args.source_commit, args.package_id)
    except (PackageError, FileExistsError, OSError, ValueError, KeyError) as exc:
        print(f"源码包操作失败：{exc}", file=sys.stderr)
        return 1
    print(f"ZIP: {zip_path}")
    print(f"BYTES: {manifest['bytes']}")
    print(f"SHA256: {manifest['sha256']}")
    print(f"SOURCE COMMIT: {manifest['source_commit']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
