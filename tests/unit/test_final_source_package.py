"""决赛源码包：Git 快照来源、纳入边界、校验与篡改发现。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
import zipfile
from pathlib import Path

import pytest

from scripts import build_final_source_package as package_builder


@pytest.fixture
def package_tmp_path() -> Path:
    project_root = Path(__file__).resolve().parents[2]
    parent = project_root / "artifacts/tmp/final-source-package-unit-tests"
    parent.mkdir(parents=True, exist_ok=True)
    task_root = parent / uuid.uuid4().hex
    task_root.mkdir()
    return task_root


def _git(root: Path, *args: str, input_bytes: bytes | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
    return result.stdout.decode("utf-8").strip()


def _write(root: Path, path: str, data: bytes | str) -> None:
    target = root / Path(*path.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "--all")
    _git(root, "commit", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _make_repo(tmp_path: Path, *, controlled_samples: bool = False) -> tuple[Path, str]:
    root = tmp_path / "snapshot repo with spaces"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Source Package Test")
    _git(root, "config", "user.email", "source-package-test@example.invalid")
    files = {
        "AGENTS.md": "# test instructions\n",
        "README.md": "committed README bytes\n",
        "start.bat": "@echo off\r\n",
        ".gitignore": ".env*\n!.env.example\n",
        ".env.example": "MODEL_BASE_URL=\nMODEL_API_KEY=\nMODEL_NAME=\n",
        ".env.model": "MODEL_API_KEY=not-a-real-committed-fixture-token\n",
        "backend/pyproject.toml": "[project]\nname='fixture'\n",
        "backend/src/finagent/api/app.py": "COMMITTED_APP = True\n",
        "frontend/index.html": "<div id='root'></div>\n",
        "frontend/package.json": "{\"name\":\"fixture\"}\n",
        "frontend/package-lock.json": "{\"name\":\"fixture\",\"lockfileVersion\":3}\n",
        "frontend/tsconfig.json": "{}\n",
        "frontend/vite.config.ts": "export default {}\n",
        "frontend/src/main.tsx": "export {}\n",
        "frontend/public/favicon.svg": "<svg/>\n",
        "config/prompts/system.md": "prompt snapshot\n",
        "config/rules/m3_v1.yaml": "rules: []\n",
        "scripts/analyze_annual.py": "print('cli')\n",
        "scripts/build_final_source_package.py": "fixture builder source\n",
        "docs/architecture.md": "architecture snapshot\n",
        "tests/unit/test_core.py": "def test_core(): pass\n",
        "evaluation/README.md": "controlled protocol\n",
        "evaluation/cases/controlled.json": "{}\n",
        "submission/final/.gitkeep": "",
        "submission/final/report-source.md": "original final report source\n",
        "submission/final/report-source.tex": "\\documentclass{article}\n",
        "submission/final/report.pdf": b"team-authored final report PDF fixture",
        "submission/final/evidence/annual-sample-whitelist.json": "{\"records\":[]}\n",
        "submission/final/evidence/quarterly-sample-audit.json": "{\"documents\":[]}\n",
        "submission/final/evidence/reproduction.txt": "deterministic reproduction notes\n",
        "submission/final/evidence/feature-summary.csv": "feature,status\nsource-package,ready\n",
        "submission/final/evidence/original-ui.png": b"synthetic png evidence fixture",
        "submission/final/evidence/original-diagram.svg": "<svg xmlns=\"http://www.w3.org/2000/svg\"/>\n",
        "submission/proposal/fintrace-project-proposal.docx": b"committed old proposal sentinel",
        "submission/proposal/proposal-review-notes.md": "private proposal review sentinel",
        "data/raw/issuer/annual/real-report.pdf": b"unlicensed historical PDF sentinel",
        "data/processed/issuer/annual/parsed.json": "{\"derived\":true}\n",
        "artifacts/runs/old-run/request.json": "{\"request\":\"historical sentinel\"}\n",
        "artifacts/runs/old-run/response.json": "{\"response\":\"historical sentinel\"}\n",
        "artifacts/annual-analysis-jobs/old-job/job.json": "{}\n",
        "artifacts/evaluations/old-eval/evidence.json": "{}\n",
    }
    for path, data in files.items():
        _write(root, path, data)
    _write(root, package_builder.LOCK_PATH, package_builder.render_runtime_lock().encode("utf-8"))
    if controlled_samples:
        _write_valid_sample_manifests(root)
    commit = _commit(root, "fixture snapshot")
    return root, commit


def _write_valid_sample_manifests(root: Path) -> dict[str, bytes]:
    annual_pdf = b"original synthetic annual fixture PDF"
    quarterly_pdf = b"original synthetic quarterly fixture PDF"
    annual_hash = hashlib.sha256(annual_pdf).hexdigest()
    quarterly_hash = hashlib.sha256(quarterly_pdf).hexdigest()
    annual_conditions = "原创模拟，不对应真实公司。可随 FINTRACE 比赛提交件和团队演示包分发。不得作为真实事实或舞弊标签。"
    quarterly_conditions = "可随FINTRACE比赛提交件和团队演示包分发；不可作为真实财务或舞弊结论"
    annual = {
        "schema_version": 1,
        "dataset_id": "fintrace-annual-synthetic-v1",
        "nature": "原创模拟",
        "data_nature": "original_synthetic",
        "use_conditions": annual_conditions,
        "distribution": {
            "competition_submission": True,
            "team_demonstration_bundle": True,
            "model_training": False,
            "fraud_label": False,
        },
        "generator": {"path": "scripts/prepare_annual_samples.py", "version": "1.2.0"},
        "records": [
            {
                "sample_id": "sim-annual-fixture",
                "file": "sim-annual-fixture.pdf",
                "sha256": annual_hash,
                "nature": "原创模拟",
                "use_conditions": annual_conditions,
                "license": "Team-authored synthetic content; redistribution permitted.",
                "not_a_fraud_label": True,
            }
        ],
    }
    quarterly = {
        "schema_version": 1,
        "dataset_id": "fintrace-quarterly-synthetic-v1",
        "company_id": "SIM-QUARTERLY-TEST",
        "nature": "原创人工构造模拟样例，明确不是任何真实公司的财报",
        "use_conditions": quarterly_conditions,
        "generator": "scripts/prepare_quarterly_samples.py",
        "documents": [
            {
                "document_id": "sim-quarterly-fixture",
                "source_pdf": "data/samples/quarterly/sim-quarterly-fixture.pdf",
                "source_sha256": quarterly_hash,
                "source_url": "original-synthetic://fintrace/sim-quarterly-fixture",
                "nature": "原创人工构造模拟报告；不对应真实公司或真实财务披露",
                "use_conditions": "允许随FINTRACE比赛提交件和团队演示包分发；不得当作真实财务事实或舞弊标签",
                "report_type": "quarterly",
            }
        ],
    }
    _write(root, "scripts/prepare_annual_samples.py", "# annual fixture\n")
    _write(root, "scripts/prepare_quarterly_samples.py", "# quarterly fixture\n")
    _write(root, "data/samples/annual/source_manifest.json", json.dumps(annual, ensure_ascii=False))
    _write(root, "data/samples/annual/sim-annual-fixture.pdf", annual_pdf)
    _write(root, "data/samples/annual/README.md", "Annual sample readme\n")
    _write(root, "data/samples/annual/useconditions.md", annual_conditions)
    _write(root, "data/samples/quarterly/source_manifest.json", json.dumps(quarterly, ensure_ascii=False))
    _write(root, "data/samples/quarterly/sim-quarterly-fixture.pdf", quarterly_pdf)
    _write(root, "data/samples/quarterly/README.md", "Quarterly sample readme\n")
    return {"annual": annual_pdf, "quarterly": quarterly_pdf}


def _zip_members(zip_path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(zip_path) as archive:
        return {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}


def test_build_reads_exact_commit_and_excludes_dirty_secrets_history_and_proposals(package_tmp_path: Path) -> None:
    root, commit = _make_repo(package_tmp_path)
    _write(root, "README.md", "dirty README must not be packaged\n")
    _write(root, ".env.model", "MODEL_API_KEY=not-a-real-test-token\n")
    _write(root, "submission/proposal/user-edited.docx", b"dirty user proposal sentinel")
    _write(root, "backend/src/finagent/api/app.py", "DIRTY_APP = True\n")

    zip_path, manifest = package_builder.build_source_package(root, commit, "fixture-source-v1")
    members = _zip_members(zip_path)
    prefix = f"{package_builder.ZIP_ROOT}/"

    assert members[prefix + "README.md"] == b"committed README bytes\n"
    assert members[prefix + "backend/src/finagent/api/app.py"] == b"COMMITTED_APP = True\n"
    assert members[prefix + ".env.example"] == b"MODEL_BASE_URL=\nMODEL_API_KEY=\nMODEL_NAME=\n"
    assert prefix + ".env.model" not in members
    assert manifest["source_commit"] == commit
    assert all("submission/proposal/" not in name for name in members)
    assert all("artifacts/" not in name for name in members)
    assert all("data/raw/" not in name and "data/processed/" not in name for name in members)
    assert all(".env.model" not in name and "historical sentinel" not in data.decode("utf-8", errors="ignore") for name, data in members.items())
    assert (root / "artifacts/tmp/final-source-package-fixture-source-v1").is_dir()
    verification = root / "artifacts/evaluations/final-source-package-fixture-source-v1/verification.json"
    assert json.loads(verification.read_text(encoding="utf-8"))["result"] == "passed"
    assert package_builder.verify_artifact_directory(zip_path.parent)["sha256"] == manifest["sha256"]


def test_unique_package_id_refuses_to_overwrite_existing_zip(package_tmp_path: Path) -> None:
    root, commit = _make_repo(package_tmp_path)
    zip_path, manifest = package_builder.build_source_package(root, commit, "fixture-source-unique")
    original = zip_path.read_bytes()

    with pytest.raises(FileExistsError, match="拒绝覆盖"):
        package_builder.build_source_package(root, commit, "fixture-source-unique")

    assert zip_path.read_bytes() == original
    assert package_builder.verify_artifact_directory(zip_path.parent)["sha256"] == manifest["sha256"]


@pytest.mark.parametrize("path", ["../outside", "/absolute/file", "C:/outside", "a\\b", "folder/CON.txt", "folder/trailing. "])
def test_unsafe_windows_or_archive_paths_are_rejected(path: str) -> None:
    with pytest.raises(package_builder.PackageError):
        package_builder.validate_git_path(path)


def test_git_snapshot_rejects_symlink_mode_and_unsupported_gitlink(package_tmp_path: Path) -> None:
    root, _ = _make_repo(package_tmp_path)
    blob = _git(root, "hash-object", "-w", "--stdin", input_bytes=b"target.txt")
    _git(root, "update-index", "--add", "--cacheinfo", f"120000,{blob},backend/src/finagent/symlink.py")
    _git(root, "commit", "-m", "add git symlink mode")
    commit = _git(root, "rev-parse", "HEAD")

    with pytest.raises(package_builder.PackageError, match="符号链接"):
        package_builder.read_commit_snapshot(root, commit)

    with pytest.raises(package_builder.PackageError, match="mode/type 不支持"):
        package_builder.parse_git_tree(b"160000 commit " + b"a" * 40 + b"\tbackend/submodule\x00")


@pytest.mark.parametrize(
    "env_example",
    [
        "MODEL_BASE_URL=\nMODEL_API_KEY=fixture-value\nMODEL_NAME=\n",
        "MODEL_BASE_URL=\nMODEL_API_KEY=\nMODEL_NAME=\nOTHER_TOKEN=fixture-value\n",
    ],
)
def test_env_example_must_contain_only_blank_model_settings(package_tmp_path: Path, env_example: str) -> None:
    root, _ = _make_repo(package_tmp_path)
    _write(root, ".env.example", env_example)
    commit = _commit(root, "invalid env example")

    with pytest.raises(package_builder.PackageError, match=".env.example"):
        package_builder.read_commit_snapshot(root, commit)


def test_valid_annual_and_quarterly_sample_manifests_bind_exact_pdf_hashes(package_tmp_path: Path) -> None:
    root, _ = _make_repo(package_tmp_path)
    sample_bytes = _write_valid_sample_manifests(root)
    commit = _commit(root, "add controlled synthetic samples")

    zip_path, manifest = package_builder.build_source_package(root, commit, "fixture-source-samples")
    members = _zip_members(zip_path)
    prefix = f"{package_builder.ZIP_ROOT}/"
    for name, payload in sample_bytes.items():
        relative = "annual/sim-annual-fixture.pdf" if name == "annual" else "quarterly/sim-quarterly-fixture.pdf"
        assert members[prefix + "data/samples/" + relative] == payload
    assert len(manifest["files"]) == len(members)
    internal = json.loads(members[prefix + "SOURCE_PACKAGE.json"])
    assert {row["path"] for row in internal["sample_inputs"]} == {
        "data/samples/annual/sim-annual-fixture.pdf",
        "data/samples/quarterly/sim-quarterly-fixture.pdf",
    }


@pytest.mark.parametrize("failure", ["nature", "permission", "sha256", "unmanifested-pdf"])
def test_unapproved_or_unbound_sample_pdf_fails_closed(package_tmp_path: Path, failure: str) -> None:
    root, _ = _make_repo(package_tmp_path)
    _write_valid_sample_manifests(root)
    manifest_path = root / "data/samples/annual/source_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if failure == "nature":
        manifest["data_nature"] = "real_company_report"
        manifest["nature"] = "真实公司年报"
    elif failure == "permission":
        manifest["distribution"]["competition_submission"] = False
    elif failure == "sha256":
        manifest["records"][0]["sha256"] = "0" * 64
    else:
        _write(root, "data/samples/annual/unmanifested.pdf", b"not in manifest")
    _write(root, "data/samples/annual/source_manifest.json", json.dumps(manifest, ensure_ascii=False))
    commit = _commit(root, f"add invalid {failure} sample")

    with pytest.raises(package_builder.PackageError):
        package_builder.read_commit_snapshot(root, commit)


def test_zip_member_tampering_is_found_even_if_outer_zip_digest_is_rewritten(package_tmp_path: Path) -> None:
    root, commit = _make_repo(package_tmp_path)
    zip_path, _ = package_builder.build_source_package(root, commit, "fixture-source-tamper")
    members = _zip_members(zip_path)
    target = f"{package_builder.ZIP_ROOT}/README.md"
    members[target] = b"tampered content\n"
    rewritten = root / "tampered.zip"
    package_builder._write_zip(rewritten, members)
    rewritten.replace(zip_path)

    manifest_path = zip_path.parent / f"{zip_path.stem}.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    digest = package_builder.sha256_file(zip_path)
    manifest["sha256"] = digest
    manifest["bytes"] = zip_path.stat().st_size
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (zip_path.parent / f"{zip_path.stem}.sha256").write_text(f"{digest}  {zip_path.name}\n", encoding="ascii")

    with pytest.raises(package_builder.PackageError, match="逐文件 inventory 校验失败"):
        package_builder.verify_artifact_directory(zip_path.parent)


def test_source_manifest_binds_prompt_rules_and_frontend_and_runtime_locks(package_tmp_path: Path) -> None:
    root, commit = _make_repo(package_tmp_path)
    zip_path, _ = package_builder.build_source_package(root, commit, "fixture-source-behavior-hashes")
    members = _zip_members(zip_path)
    internal = json.loads(members[f"{package_builder.ZIP_ROOT}/SOURCE_PACKAGE.json"])
    assets = internal["behavior_assets"]

    assert assets["prompts"] == [
        {"path": "config/prompts/system.md", "sha256": hashlib.sha256(b"prompt snapshot\n").hexdigest()}
    ]
    assert assets["rules"] == [
        {"path": "config/rules/m3_v1.yaml", "sha256": hashlib.sha256(b"rules: []\n").hexdigest()}
    ]
    assert assets["frontend_lock"]["path"] == "frontend/package-lock.json"
    assert assets["runtime_lock"]["path"] == package_builder.LOCK_PATH
    assert internal["tested_environment"] == package_builder.TARGET_ENVIRONMENT
    assert package_builder.verify_artifact_directory(zip_path.parent)["source_commit"] == commit


def test_final_evidence_assets_are_included_and_license_manifest_binds_local_texts(package_tmp_path: Path) -> None:
    root, commit = _make_repo(package_tmp_path)
    zip_path, _ = package_builder.build_source_package(root, commit, "fixture-source-final-evidence")
    members = _zip_members(zip_path)
    prefix = f"{package_builder.ZIP_ROOT}/"
    expected_final_evidence = {
        "submission/final/report-source.md",
        "submission/final/report-source.tex",
        "submission/final/report.pdf",
        "submission/final/evidence/annual-sample-whitelist.json",
        "submission/final/evidence/quarterly-sample-audit.json",
        "submission/final/evidence/reproduction.txt",
        "submission/final/evidence/feature-summary.csv",
        "submission/final/evidence/original-ui.png",
        "submission/final/evidence/original-diagram.svg",
    }
    assert expected_final_evidence <= {name[len(prefix):] for name in members}
    assert all("submission/proposal/" not in name for name in members)
    assert all("submission/video/" not in name for name in members)
    assert all("artifacts/" not in name for name in members)

    license_manifest_path = prefix + "THIRD_PARTY_LICENSES/python/manifest.json"
    license_manifest_bytes = members[license_manifest_path]
    license_manifest = json.loads(license_manifest_bytes)
    runtime_paths = {
        path[len(prefix):]: data
        for path, data in members.items()
        if path.startswith(prefix + "THIRD_PARTY_LICENSES/python/")
    }
    referenced = {"THIRD_PARTY_LICENSES/python/manifest.json"}
    for distribution in license_manifest["runtime_python_packages"]:
        for license_file in distribution["license_files"]:
            path = license_file["path"]
            data = runtime_paths[path]
            assert license_file["bytes"] == len(data)
            assert license_file["sha256"] == hashlib.sha256(data).hexdigest()
            assert not Path(license_file["source_metadata_path"]).is_absolute()
            assert ":" not in license_file["source_metadata_path"]
            referenced.add(path)
    assert referenced == set(runtime_paths)
    assert str(root.resolve()).encode("utf-8") not in license_manifest_bytes
    assert package_builder.verify_artifact_directory(zip_path.parent)["source_commit"] == commit
