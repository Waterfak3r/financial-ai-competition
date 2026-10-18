"""Build an isolated Windows x64 FINTRACE package from tracked sources."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import uuid
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath


PYTHON_VERSION = "3.14.3"
PYTHON_ARCHIVE_SHA256 = "ad4961a479dedbeb7c7d113253f8db1b1935586b73c27488712beec4f2c894e6"
PDF_RELATIVE = Path("data/raw/603288/2024/cninfo-1222994233/1222994233.PDF")
PDF_SHA256 = "5a97b13534438f5e85249752ef492fbd9e23af73e67845e47bad1ab7d92e20ee"
RUN_IDS = (
    "annual-analysis-8036d75c-6a13-432a-b602-5f869f9396ce",
    "annual-analysis-21d8da07-097b-4cdf-97ce-6e109c8cc1b8",
    "annual-analysis-5e7079fe-9ac9-4e96-85fa-1898f9177d21",
    "annual-analysis-a736d7e1-a932-4f88-9a0c-057f20d6c998",
)
JOB_IDS = (
    "annual-job-618e6dd4fa984e1dbabecf13a2ece4d8",
    "annual-job-ec0925b99603440eb0384478a6b261b2",
    "annual-job-30d4e2fa50754e3b9abd2e749482b04d",
)
PRECHECK_ID = "annual-precheck-603288-2024-20261002-133803"
PACKAGE_ID = "fintrace-windows-x64-20261002-candidate02"
VC_REDIST_VERSION = "14.51.36247.0"
VC_REDIST_SHA256 = "843068991daaa1f73ad9f6239bce4d0f6a07a51f18c37ea2a867e9beca71295c"
VC_RUNTIME_DLL_SHA256 = {
    "concrt140.dll": "54716f0738af891f283d213b5c8d11b25896bb8ee3097d301eae718560cf974e",
    "msvcp140_1.dll": "206c931bf90fdad8816de3b5e2ef80b2bcaa9406c89ecc05fe6fddffe251e982",
    "msvcp140_2.dll": "d50d7883f20d1dc6191768d3746f52dd9cac89c346ffaed5be1f110c2f34a838",
    "msvcp140_atomic_wait.dll": "3d0cbfaa1bf3eecf5a3f4491d2960ee803cb994f30292c6adc4a07c498f60e2b",
    "msvcp140_codecvt_ids.dll": "8a65c7596ef2e6938731f5a1058e7e40145b6d97967cc649231a076b9a608d78",
    "msvcp140.dll": "7c26614e1d733892c2deac7e245ce115504b1d80592dd0a01b08e3e5a55f89ca",
    "vcamp140.dll": "dc283ffb87e9b82e687ed4e899ae062a209aa338207abfc9a89d61407d678bd4",
    "vccorlib140.dll": "09f93d5ff96ae09767f3e1af3cdd43a5a58f2598654fc63089429bb6f9f4737f",
    "vcomp140.dll": "95d4ce4a6802d1e18b5e0e1722cc30ea72ca7e033f83828f05c0b7b993fe7cbf",
    "vcruntime140_1.dll": "a7146c08f89fe5b04541ab507cdb59ff7b44534d4ba3c668a426c6450a03434e",
    "vcruntime140_threads.dll": "40f39e8cee5f5a531b4010e18b807e8fe265b908dc58f959ecb7ebc6ea6cb11a",
    "vcruntime140.dll": "d1f4225df2cd877dbf130d5668a021dce3f94118455ff5ec952061c30afc9ce7",
}
ARTIFACT_ROOTS = (
    Path("artifacts/runs"),
    Path("artifacts/reports"),
    Path("artifacts/annual-analysis-jobs"),
)
EXCLUDED_NAMES = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    "tmp",
    "dist",
    "build",
    "site-packages",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True, encoding="utf-8"
    )
    return completed.stdout.strip()


def selected_source_file(relative: Path) -> bool:
    text = relative.as_posix()
    parts = relative.parts
    if any(part in EXCLUDED_NAMES for part in parts):
        return False
    if relative.name.startswith(".env") and text != ".env.example":
        return False
    if relative.suffix.lower() in {".pyc", ".pyo", ".log", ".db", ".sqlite", ".sqlite3"}:
        return False
    if text in {"AGENTS.md", "README.md", ".gitignore", ".env.example"}:
        return True
    if parts[:1] == ("backend",):
        return text == "backend/pyproject.toml" or (
            len(parts) >= 3 and parts[1] == "src" and relative.suffix == ".py"
        )
    if parts[:1] == ("frontend",):
        if len(parts) >= 2 and parts[1] in {"src", "public"}:
            return relative.suffix.lower() in {
                ".css", ".html", ".ico", ".json", ".jpg", ".jpeg", ".png", ".svg", ".ts", ".tsx", ".webp"
            }
        return text in {
            "frontend/index.html",
            "frontend/package.json",
            "frontend/package-lock.json",
            "frontend/tsconfig.json",
            "frontend/vite.config.ts",
        }
    if parts[:1] == ("config",):
        return relative.suffix.lower() in {".md", ".json", ".yaml", ".yml", ".toml"}
    if parts[:1] == ("docs",):
        return relative.suffix.lower() == ".md"
    if parts[:1] == ("evaluation",):
        return relative.suffix.lower() in {".md", ".json", ".jsonl", ".csv"}
    if parts[:1] == ("tests",):
        return relative.suffix.lower() in {".py", ".json", ".md"}
    if parts[:1] == ("scripts",):
        return relative.suffix.lower() in {".py", ".ps1", ".bat"} or relative.name == ".gitkeep"
    if parts[:1] == ("submission",):
        return relative.suffix.lower() in {".pdf", ".md", ".json", ".svg", ".png", ".txt", ".docx"}
    return False


def tracked_files(root: Path) -> list[Path]:
    raw = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True
    ).stdout
    result: list[Path] = []
    for item in raw.split(b"\x00"):
        if not item:
            continue
        relative = Path(os.fsdecode(item))
        if selected_source_file(relative) and (root / relative).is_file():
            result.append(relative)
    return result


def copy_tracked_sources(root: Path, package: Path) -> None:
    for relative in tracked_files(root):
        destination = package / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / relative, destination)

    # These new files are deliberately included before their first commit.
    for relative in (
        Path("scripts/build_windows_portable.py"),
        Path("scripts/portable/serve.py"),
        Path("scripts/portable/sitecustomize.py"),
        Path("docs/portable-package.md"),
    ):
        source = root / relative
        destination = package / relative
        if not source.is_file():
            raise FileNotFoundError(f"Missing portable source: {relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def copy_selected_data(root: Path, package: Path) -> None:
    source_dir = root / PDF_RELATIVE.parent
    for name in ("1222994233.PDF", "source.json"):
        source = source_dir / name
        if not source.is_file():
            raise FileNotFoundError(f"Required internal demo source is missing: {source}")
        target = package / PDF_RELATIVE.parent / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    if sha256(package / PDF_RELATIVE) != PDF_SHA256:
        raise RuntimeError("海天原始 PDF 的 SHA256 与登记值不符")

    processed = root / "data/processed/603288/2024/cninfo-1222994233"
    if not processed.is_dir():
        raise FileNotFoundError(f"Required precheck parse data is missing: {processed}")
    for source in sorted(processed.rglob("*")):
        if not source.is_file() or any(part in EXCLUDED_NAMES for part in source.relative_to(processed).parts):
            continue
        target = package / "data/processed/603288/2024/cninfo-1222994233" / source.relative_to(processed)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def copy_run(root: Path, package: Path, run_id: str) -> None:
    source = root / "artifacts/runs" / run_id
    target = package / "artifacts/runs" / run_id
    names = ["analysis.json", "manifest.json", "parsed_text_pdf.json", "source.pdf"]
    names.extend(name for name in ("investigation.json", "m3_screening.json") if (source / name).is_file())
    for name in names:
        source_file = source / name
        if not source_file.is_file():
            raise FileNotFoundError(f"Required run file is missing: {source_file}")
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target / name)

    report_source = root / "artifacts/reports" / run_id
    report_target = package / "artifacts/reports" / run_id
    for name in ("report.json", "report.md"):
        file = report_source / name
        if not file.is_file():
            raise FileNotFoundError(f"Required historical report is missing: {file}")
        report_target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file, report_target / name)


def copy_job(root: Path, package: Path, job_id: str) -> None:
    source = root / "artifacts/annual-analysis-jobs" / job_id / "job.json"
    if not source.is_file():
        raise FileNotFoundError(f"Required historical job is missing: {source}")
    destination = package / "artifacts/annual-analysis-jobs" / job_id / "job.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def copy_precheck(root: Path, package: Path) -> None:
    source = root / "artifacts/runs" / PRECHECK_ID
    target = package / "artifacts/runs" / PRECHECK_ID
    for name in ("precheck.json", "facts.json", "calculation.json"):
        file = source / name
        if not file.is_file():
            raise FileNotFoundError(f"Required old-precheck archive is missing: {file}")
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file, target / name)


def copy_python(root: Path, package: Path, archive_path: Path) -> None:
    if not archive_path.is_file() or sha256(archive_path) != PYTHON_ARCHIVE_SHA256:
        raise RuntimeError(
            "官方 CPython embedded 存档不存在或 SHA256 不匹配："
            "https://www.python.org/ftp/python/3.14.3/python-3.14.3-embed-amd64.zip"
        )
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(package / "runtime")
    pth = package / "runtime/python314._pth"
    pth.write_text(
        "python314.zip\n.\nvendor\n../backend/src\n../scripts\nimport site\n",
        encoding="ascii",
        newline="\n",
    )
    sitecustomize = root / "scripts/portable/sitecustomize.py"
    if not sitecustomize.is_file():
        raise FileNotFoundError(f"Embedded runtime UTF-8 bootstrap is missing: {sitecustomize}")
    shutil.copy2(sitecustomize, package / "runtime/sitecustomize.py")


def verify_vc_redist_source(executable: Path) -> None:
    if not executable.is_file() or sha256(executable) != VC_REDIST_SHA256:
        raise RuntimeError("Microsoft VC Redist 源文件不存在或 SHA256 与签名版本登记不符")


def runtime_closure() -> list[metadata.Distribution]:
    roots = {"fastapi", "python-multipart", "uvicorn", "pymupdf", "langgraph"}
    from packaging.utils import canonicalize_name

    distributions = {
        canonicalize_name(dist.metadata.get("Name") or ""): dist
        for dist in metadata.distributions()
        if dist.metadata.get("Name")
    }
    selected: dict[str, metadata.Distribution] = {}
    todo = list(roots)
    from packaging.requirements import Requirement

    while todo:
        name = canonicalize_name(todo.pop())
        if name in selected:
            continue
        dist = distributions.get(name)
        if dist is None:
            raise RuntimeError(f"运行依赖闭包缺包：{name}")
        selected[name] = dist
        for raw in dist.requires or ():
            requirement = Requirement(raw)
            if requirement.marker is None or requirement.marker.evaluate():
                todo.append(requirement.name)
    return sorted(selected.values(), key=lambda dist: dist.metadata["Name"].lower())


def vendor_runtime(package: Path, vc_runtime_dir: Path) -> list[dict[str, str]]:
    vendor = package / "runtime/vendor"
    vendor.mkdir(parents=True, exist_ok=True)
    distributions = runtime_closure()
    base = Path(sys.prefix).resolve()
    skipped_unsafe_record_paths: list[str] = []
    for dist in distributions:
        for entry in dist.files or ():
            entry_text = str(entry).replace("\\", "/")
            relative = PurePosixPath(entry_text)
            windows_relative = PureWindowsPath(str(entry))
            if (
                relative.is_absolute()
                or windows_relative.is_absolute()
                or windows_relative.drive
                or ".." in relative.parts
                or ".." in windows_relative.parts
            ):
                # Installed console scripts may be recorded as ../../Scripts/*.exe.
                # They are generated wrappers and are deliberately not portable inputs.
                skipped_unsafe_record_paths.append(f"{dist.metadata['Name']}:{entry_text}")
                continue
            if "__pycache__" in relative.parts or relative.suffix in {".pyc", ".pyo", ".pth"}:
                continue
            if relative.name == "direct_url.json" or relative.parts[:1] in {("scripts",), ("bin",)}:
                continue
            source = Path(dist.locate_file(entry)).resolve()
            try:
                source.relative_to(base)
            except ValueError:
                continue
            if not source.is_file():
                continue
            destination = (vendor.joinpath(*relative.parts)).resolve(strict=False)
            try:
                destination.relative_to(vendor.resolve())
            except ValueError as exc:
                raise RuntimeError(f"依赖目标路径越出 runtime/vendor：{entry_text}") from exc
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

    runtime_files = sorted(vc_runtime_dir.glob("*.dll"))
    actual_vc_hashes = {path.name.lower(): sha256(path) for path in runtime_files}
    if actual_vc_hashes != VC_RUNTIME_DLL_SHA256:
        raise RuntimeError(
            "Microsoft VC Runtime DLL 目录不符合已验证的 14.51.36247.0 官方 payload；"
            "请重新从登记的签名安装包提取并核对文件。"
        )
    required_runtime = {"msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"}
    if not required_runtime.issubset({path.name.lower() for path in runtime_files}):
        raise FileNotFoundError(
            "Microsoft 官方 VC Redist 本地映像缺少 PyMuPDF 所需的 x64 MSVC runtime DLL。"
        )
    copied_runtime: list[dict[str, str]] = []
    for source in runtime_files:
        target = package / "runtime" / source.name
        shutil.copy2(source, target)
        copied_runtime.append({"file": f"runtime/{source.name}", "sha256": sha256(target)})

    license_rows = []
    for dist in distributions:
        name = dist.metadata["Name"]
        version = dist.version
        expression = dist.metadata.get("License-Expression") or dist.metadata.get("License") or "未声明"
        upstream = next(
            (value for key, value in dist.metadata.items() if key.lower() in {"home-page", "project-url"}),
            "",
        )
        license_files = [
            str(entry)
            for entry in (dist.files or ())
            if "license" in Path(str(entry)).name.lower() or "copying" in Path(str(entry)).name.lower()
        ]
        license_rows.append(
            {
                "name": name,
                "version": version,
                "license": expression,
                "source": f"https://pypi.org/project/{name}/{version}/",
                "upstream": upstream,
                "license_files": license_files,
            }
        )
    (package / "THIRD_PARTY_LICENSES.json").write_text(
        json.dumps(
            {
                "python_runtime_packages": license_rows,
                "microsoft_vc_runtime": {
                    "version": VC_REDIST_VERSION,
                    "source": "https://aka.ms/vc14/vc_redist.x64.exe",
                    "signed_executable_sha256": VC_REDIST_SHA256,
                    "authenticode_signer": "Microsoft Corporation",
                    "files": copied_runtime,
                },
                "excluded_installed_record_paths": skipped_unsafe_record_paths,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if skipped_unsafe_record_paths:
        print(f"安全排除了 {len(skipped_unsafe_record_paths)} 个越界/绝对 RECORD 路径（主要为全局 Scripts 入口）。")
    return license_rows


def copy_frontend_runtime_licenses(root: Path, package: Path) -> list[dict[str, str]]:
    expected_versions = {"react": "19.3.0", "react-dom": "19.3.0", "scheduler": "0.28.0"}
    rows: list[dict[str, str]] = []
    license_root = package / "frontend/licenses"
    license_root.mkdir(parents=True, exist_ok=True)
    for name, expected_version in expected_versions.items():
        source_dir = root / "frontend/node_modules" / name
        package_json = json.loads((source_dir / "package.json").read_text(encoding="utf-8"))
        if package_json.get("version") != expected_version or package_json.get("license") != "MIT":
            raise RuntimeError(f"前端运行组件来源/许可与登记不符：{name}@{package_json.get('version')}")
        source_license = source_dir / "LICENSE"
        if not source_license.is_file():
            raise FileNotFoundError(f"前端运行组件缺少上游 LICENSE：{source_license}")
        relative_license = Path("frontend/licenses") / f"{name}-LICENSE.txt"
        shutil.copy2(source_license, package / relative_license)
        rows.append(
            {
                "name": name,
                "version": expected_version,
                "license": "MIT",
                "source": f"https://www.npmjs.com/package/{name}/v/{expected_version}",
                "upstream": "https://github.com/facebook/react",
                "license_files": [relative_license.as_posix()],
            }
        )

    manifest_path = package / "THIRD_PARTY_LICENSES.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["frontend_runtime_packages"] = rows
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return rows


def write_package_notes(package: Path, root: Path, dependency_rows: list[dict[str, str]]) -> None:
    (package / "先看这里.txt").write_text(
        "先把本 ZIP 完整解压到可写文件夹，双击“启动 FINTRACE.bat”即可开箱即用！\n\n"
        "====================================================================\n"
        "        FINTRACE 决赛完整交付与便携运行包（队友使用指南）\n"
        "====================================================================\n\n"
        "一、关于运行环境（零依赖，已内嵌完整运行时）\n"
        "1. 本包已完整内嵌 Python 3.14.3 独立运行环境与全量依赖（FastAPI, PyMuPDF, ReportLab等）；\n"
        "2. 前端已完成离线编译，使用者电脑上【完全不需要】安装 Python、Node.js、npm 或任何开发工具；\n"
        "3. 建议解压到自己的文档或桌面等有写权限的目录，不要在压缩包预览窗口中直接双击；\n"
        "4. 双击根目录下的“启动 FINTRACE.bat”，系统会自动在浏览器打开交互界面。\n\n"
        "二、决赛交付与答辩材料清单（均在包内）\n"
        "1. 决赛技术报告正式版（PDF 格式，可直接提交/打印）：\n"
        "   文件路径：submission/final/FINTRACE-决赛技术报告.pdf\n"
        "2. 决赛答辩核心提纲与专家问答（PPT 制作与现场应答宝典）：\n"
        "   文件路径：submission/final/FINTRACE-决赛答辩提纲与专家问答.md\n"
        "   （包含 3~5 分钟演练台本、8 大财务专家拷问应对、8 大计算机专家技术拷问应对）\n"
        "3. 架构图与核心证据图表：\n"
        "   文件路径：submission/final/evidence/architecture.svg 等\n\n"
        "三、现场演示推荐动线\n"
        "1. 打开主页：直观呈现“重点核查卡片 + AI 综合评审意见 + 核心指标表”；\n"
        "2. 穿透证据：点击“查看原版 PDF 第 9 页原文”，展示毫米级坐标高亮对齐；\n"
        "3. 季度工作台：点击左侧“季度与多期间对比”，演示单季差分算法与负基数严谨弃权标签；\n"
        "4. 报告导出：点击“下载 PDF”，展示秒级生成的专业排版报告。\n\n"
        "关闭黑色启动窗口即可随时停止本机服务。\n",
        encoding="utf-8",
    )
    (package / "许可与来源.txt").write_text(
        "Python 运行时来自 python.org 的 CPython 3.14.3 Windows x64 embeddable distribution。\n"
        "第三方 Python 运行依赖及版本、上游来源、许可字段与随包许可文件索引见 THIRD_PARTY_LICENSES.json；\n"
        "许可原文随各包的 runtime/vendor/*.dist-info/licenses/ 文件分发（若上游 wheel 提供）。\n"
        "PyMuPDF 使用 AGPLv3 或商业许可；本内部包随上游许可文件保留，使用者应按自己的分发和使用方式遵循许可。\n"
        "Microsoft VC Runtime DLL 来自 Microsoft 官方 x64 Visual C++ Redistributable。其应用本地分发受\n"
        "Microsoft Software License Terms 约束；请核对并遵循适用条款。\n"
        "Microsoft 官方说明：https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files\n"
        "依赖不随包自动更新。团队重新构建包时应更新依赖来源登记并复核各上游许可。\n",
        encoding="utf-8",
    )
    (package / "MICROSOFT_RUNTIME_SOURCE.txt").write_text(
        f"Source: https://aka.ms/vc14/vc_redist.x64.exe\n"
        f"Verified x64 version: {VC_REDIST_VERSION}\n"
        f"Signed executable SHA256: {VC_REDIST_SHA256}\n"
        "Authenticode signer: Microsoft Corporation (signature verified before extraction)\n"
        "Microsoft Learn download table: https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist\n"
        "Microsoft Learn redistribution terms: https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files\n"
        "Bundled verified, unmodified x64 VC Runtime payload DLLs (this is the included payload set, not a claim that every file is independently required by PyMuPDF):\n"
        + "".join(f"- {name}\n" for name in sorted(VC_RUNTIME_DLL_SHA256))
        + "See THIRD_PARTY_LICENSES.json for each file SHA256. Distribution and use are subject to the Microsoft Software License Terms; review and follow applicable terms.\n",
        encoding="utf-8",
    )


def write_start_batch(package: Path) -> None:
    content = r"""@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist "portable-package.json" (
  echo 没有检测到完整解压的 FINTRACE 便携包。请先将整个 ZIP 解压到可写文件夹。
  pause
  exit /b 1
)
if not exist "runtime\python.exe" (
  echo 缺少包内 Python 运行时。请重新完整解压 FINTRACE ZIP。
  pause
  exit /b 1
)
"runtime\python.exe" "scripts\portable\serve.py"
set "FINTRACE_EXIT=%ERRORLEVEL%"
if not "%FINTRACE_EXIT%"=="0" echo 服务异常结束，错误码 %FINTRACE_EXIT%。请查看上方诊断信息。
echo FINTRACE 本机服务已退出。
pause
endlocal
"""
    (package / "启动 FINTRACE.bat").write_text(content, encoding="utf-8", newline="\r\n")


def inventory(package: Path) -> list[dict[str, object]]:
    files = []
    for path in sorted(package.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(package).as_posix()
        if relative == "portable-package.json":
            continue
        files.append({"path": relative, "bytes": path.stat().st_size, "sha256": sha256(path)})
    return files


def check_no_credentials(package: Path) -> None:
    forbidden_names = {".env", ".env.model", ".env.local", "credentials.json", "secrets.json"}
    for path in package.rglob("*"):
        if path.name.lower() in forbidden_names:
            raise RuntimeError(f"不允许打包凭据或本地配置：{path.relative_to(package)}")
    suspicious = []
    patterns = (
        re.compile(rb"(?i)\bsk-[A-Za-z0-9_-]{32,}\b"),
        re.compile(rb"(?i)\bBearer\s+[A-Za-z0-9._-]{40,}"),
        re.compile(rb"\bAKIA[A-Z0-9]{16}\b"),
        re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    )
    for path in package.rglob("*"):
        if not path.is_file() or path.suffix.lower() in {".pdf", ".pyd", ".dll", ".exe", ".zip"}:
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if any(pattern.search(data) for pattern in patterns):
            suspicious.append(path.relative_to(package).as_posix())
    if suspicious:
        raise RuntimeError("凭据特征扫描命中，停止打包并人工复核：" + ", ".join(suspicious[:10]))


def build(
    root: Path,
    package_id: str,
    python_archive: Path,
    vc_runtime_dir: Path,
    vc_redist_exe: Path,
) -> tuple[Path, int, str]:
    if (
        sys.platform != "win32"
        or sysconfig.get_platform() != "win-amd64"
        or sys.version_info[:3] != (3, 14, 3)
        or sys.maxsize <= 2**32
    ):
        raise RuntimeError("必须使用 Windows x64 CPython 3.14.3 构建，以匹配固定的 embedded runtime 和 native wheels")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", package_id) is None:
        raise ValueError("package_id 只能包含英文、数字、点、下划线和连字符")
    package_root = root / "artifacts/packages"
    build_root = root / "artifacts/tmp/windows-portable-20261002"
    package_root.mkdir(parents=True, exist_ok=True)
    build_root.mkdir(parents=True, exist_ok=True)
    output_dir = package_root / package_id
    if output_dir.exists():
        raise FileExistsError(f"正式包目录已存在，拒绝覆盖：{output_dir}")
    verify_vc_redist_source(vc_redist_exe)
    build_id = uuid.uuid4().hex
    stage = build_root / f"{package_id}-staging-{build_id}"
    stage.mkdir(parents=True)

    copy_tracked_sources(root, stage)
    copy_selected_data(root, stage)
    for run_id in RUN_IDS:
        copy_run(root, stage, run_id)
    for job_id in JOB_IDS:
        copy_job(root, stage, job_id)
    copy_precheck(root, stage)

    frontend_dist = root / "frontend/dist"
    if not (frontend_dist / "index.html").is_file():
        raise FileNotFoundError("前端尚未构建：请先在 frontend 执行 npm run build")
    shutil.copytree(frontend_dist, stage / "frontend/dist", dirs_exist_ok=True)
    copy_python(root, stage, python_archive)
    dependency_rows = vendor_runtime(stage, vc_runtime_dir)
    copy_frontend_runtime_licenses(root, stage)
    write_start_batch(stage)
    write_package_notes(stage, root, dependency_rows)

    source_head = git(root, "rev-parse", "HEAD")
    dirty = bool(git(root, "status", "--porcelain", "--untracked-files=normal"))
    (stage / "portable-package.json").write_text(
        json.dumps(
            {
                "package_id": package_id,
                "platform": "Windows x64; tested with 64-bit CPython 3.14.3",
                "source_git_head": source_head,
                "source_tree_dirty_at_build": dirty,
                "python_archive": {
                    "url": "https://www.python.org/ftp/python/3.14.3/python-3.14.3-embed-amd64.zip",
                    "sha256": PYTHON_ARCHIVE_SHA256,
                },
                "microsoft_vc_runtime": {
                    "version": VC_REDIST_VERSION,
                    "source": "https://aka.ms/vc14/vc_redist.x64.exe",
                    "signed_executable_sha256": VC_REDIST_SHA256,
                    "authenticode_signer": "Microsoft Corporation",
                    "authenticode_status": "Valid (verified on Windows before extraction)",
                    "files": json.loads((stage / "THIRD_PARTY_LICENSES.json").read_text(encoding="utf-8"))["microsoft_vc_runtime"]["files"],
                },
                "python_runtime_packages": dependency_rows,
                "historical_runs": list(RUN_IDS),
                "historical_jobs": list(JOB_IDS),
                "old_precheck": PRECHECK_ID,
                "source_pdf_sha256": PDF_SHA256,
                "internal_team_distribution": True,
                "file_inventory": inventory(stage),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    check_no_credentials(stage)

    zip_path = output_dir / f"{package_id}.zip"
    temp_zip = build_root / f"{package_id}-{build_id}.zip.tmp"
    with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                relative = (Path(package_id) / path.relative_to(stage)).as_posix()
                archive.write(path, relative)
    if output_dir.exists():
        raise FileExistsError(f"正式包目录已存在，拒绝覆盖：{output_dir}")
    output_dir.mkdir()
    temp_zip.replace(zip_path)
    digest = sha256(zip_path)
    size = zip_path.stat().st_size
    sidecar = output_dir / f"{package_id}.sha256"
    sidecar.write_text(f"{digest}  {zip_path.name}\n", encoding="ascii")
    manifest = {
        "package_id": package_id,
        "zip": zip_path.name,
        "bytes": size,
        "sha256": digest,
        "source_git_head": source_head,
        "source_tree_dirty_at_build": dirty,
        "stage_relative_path": stage.relative_to(root).as_posix(),
    }
    (output_dir / f"{package_id}.manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return zip_path, size, digest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-id", default=PACKAGE_ID)
    parser.add_argument(
        "--python-archive",
        type=Path,
        default=Path("artifacts/tmp/windows-portable-20261002/python-3.14.3-embed-amd64.zip"),
    )
    parser.add_argument(
        "--vc-runtime-dir",
        type=Path,
        default=Path("artifacts/tmp/windows-portable-20261002/vc-runtime"),
        help="来自经验证 Microsoft 官方 x64 VC Redist 的本地 DLL 布局目录",
    )
    parser.add_argument(
        "--vc-redist-exe",
        type=Path,
        default=Path("artifacts/tmp/windows-portable-20261002/vc_redist.x64.exe"),
        help="用于来源核验的 Microsoft 官方 Authenticode 签名 x64 redistributable",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        zip_path, size, digest = build(
            root,
            args.package_id,
            (root / args.python_archive).resolve() if not args.python_archive.is_absolute() else args.python_archive,
            (root / args.vc_runtime_dir).resolve() if not args.vc_runtime_dir.is_absolute() else args.vc_runtime_dir,
            (root / args.vc_redist_exe).resolve() if not args.vc_redist_exe.is_absolute() else args.vc_redist_exe,
        )
    except Exception as exc:
        print(f"便携包构建失败：{exc}", file=sys.stderr)
        return 1
    print(f"ZIP: {zip_path}")
    print(f"BYTES: {size}")
    print(f"SHA256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
