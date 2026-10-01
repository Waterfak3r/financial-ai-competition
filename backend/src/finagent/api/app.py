"""本机年度分析 API；模型只在明确选择的操作中调用。"""

from __future__ import annotations

import ipaddress
import json
import os
import stat
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt
from starlette.concurrency import run_in_threadpool

from finagent.api.annual_precheck import (
    HashMismatchError,
    create_annual_precheck,
    load_annual_precheck,
)
from finagent.api.annual_report_read import (
    AnnualReportReadError,
    load_annual_report,
    render_evidence_preview,
)
from finagent.api.annual_analysis_jobs import (
    AnnualAnalysisJobError,
    get_annual_analysis_capabilities,
    get_annual_analysis_job_service,
)
from finagent.audit import AuditPersistError, EvidenceRef, audited_complete_chat
from finagent.core.model_settings_store import (
    ModelSettingsStoreError,
    ModelSettingsValidationError,
    delete_local_settings,
    get_model_settings_snapshot,
    resolve_model_settings_draft,
    save_local_settings,
)
from finagent.core.safe_paths import PathBoundaryError
from finagent.ingestion.errors import PdfInputError
from finagent.ingestion.upload_text_pdf import UploadRejected, save_text_pdf_upload
from finagent.llm.chat_completion import ChatMessage, ModelResponseError

_SETTINGS_CACHE_HEADERS = {"Cache-Control": "no-store"}
_MAX_SETTINGS_BODY_BYTES = 12 * 1024
_LOOPBACK_DEV_HOSTS = {"localhost", "127.0.0.1"}
_REPARSE_POINT_ATTRIBUTE = 0x400


class AnnualPrecheckRequest(BaseModel):
    parsed_path: str = Field(..., description="相对 data/processed 的 text_pdf.json 路径")
    source_pdf_path: str = Field(..., description="相对 data/raw 的原始 PDF 路径")
    company_id: str
    report_year: int = Field(..., ge=1900, le=2100)


class AnnualAnalysisJobRequest(BaseModel):
    company_id: str
    report_year: StrictInt = Field(..., ge=1900, le=2100)
    document_id: str
    source_pdf_path: str = Field(..., description="相对 data/raw 的 PDF 路径")
    sha256: str
    mode: Literal["deterministic", "m3_screening", "model_investigation"] = "deterministic"


def create_app(project_root: Path | None = None) -> FastAPI:
    root = Path(project_root).resolve() if project_root is not None else Path(__file__).resolve().parents[4]
    app = FastAPI(title="finagent annual precheck", version="0.1.0")
    app.state.project_root = root
    app.state.annual_analysis_jobs = get_annual_analysis_job_service(root)

    @app.post("/v1/text-pdf-uploads", status_code=201)
    def post_text_pdf_upload(
        file: UploadFile = File(...),
        company_id: str = Form(...),
        report_year: str = Form(...),
    ) -> dict:
        try:
            return save_text_pdf_upload(
                app.state.project_root,
                company_id=company_id,
                report_year=report_year,
                stream=file.file,
            )
        except UploadRejected as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": exc.message},
            ) from None

    @app.post("/v1/annual-prechecks", status_code=201)
    def post_annual_precheck(body: AnnualPrecheckRequest) -> dict:
        try:
            return create_annual_precheck(
                app.state.project_root,
                parsed_path=body.parsed_path,
                source_pdf_path=body.source_pdf_path,
                company_id=body.company_id,
                report_year=body.report_year,
            )
        except HashMismatchError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "source_sha256_mismatch",
                    "message": str(exc),
                    "source_pdf_sha256": exc.source_pdf_sha256,
                    "parsed_source_sha256": exc.parsed_source_sha256,
                },
            ) from None
        except PathBoundaryError as exc:
            raise HTTPException(status_code=400, detail={"code": "invalid_path", "message": str(exc)}) from None
        except PdfInputError as exc:
            raise HTTPException(status_code=400, detail={"code": "invalid_input", "message": str(exc)}) from None
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail={"code": "run_exists", "message": str(exc)}) from None

    @app.get("/v1/annual-prechecks/{run_id}")
    def get_annual_precheck(run_id: str) -> dict:
        try:
            return load_annual_precheck(app.state.project_root, run_id)
        except PathBoundaryError as exc:
            raise HTTPException(status_code=400, detail={"code": "invalid_run_id", "message": str(exc)}) from None
        except FileNotFoundError:
            raise HTTPException(
                status_code=404,
                detail={"code": "run_not_found", "message": "找不到该预检运行。"},
            ) from None

    @app.post("/v1/annual-analysis-jobs", status_code=202)
    def post_annual_analysis_job(body: AnnualAnalysisJobRequest) -> dict:
        try:
            return app.state.annual_analysis_jobs.create(body.model_dump())
        except AnnualAnalysisJobError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": exc.message},
            ) from None

    @app.get("/v1/annual-analysis-capabilities")
    def get_annual_analysis_capabilities_route() -> dict:
        return get_annual_analysis_capabilities(app.state.project_root)

    @app.get("/v1/model-settings")
    def get_model_settings_route(request: Request) -> Response:
        rejected = _settings_request_rejection(request, require_origin=False)
        if rejected is not None:
            return rejected
        try:
            snapshot = get_model_settings_snapshot(app.state.project_root)
        except ModelSettingsStoreError as exc:
            return _settings_error(500, exc.code, exc.message)
        return _settings_json(snapshot.to_dict())

    @app.put("/v1/model-settings")
    async def put_model_settings_route(request: Request) -> Response:
        rejected = _settings_request_rejection(request, require_origin=True)
        if rejected is not None:
            return rejected
        try:
            payload = await _read_model_settings_body(request)
            snapshot = save_local_settings(
                app.state.project_root,
                base_url=payload["base_url"],
                model_name=payload["model_name"],
                api_key=payload.get("api_key", ""),
            )
        except ModelSettingsValidationError as exc:
            return _settings_error(400, exc.code, exc.message)
        except ModelSettingsStoreError as exc:
            status_code = 409 if exc.code == "model_settings_changed" else 500
            return _settings_error(status_code, exc.code, exc.message)
        return _settings_json(snapshot.to_dict())

    @app.delete("/v1/model-settings")
    def delete_model_settings_route(request: Request) -> Response:
        rejected = _settings_request_rejection(request, require_origin=True)
        if rejected is not None:
            return rejected
        try:
            delete_local_settings(app.state.project_root)
            snapshot = get_model_settings_snapshot(app.state.project_root)
        except ModelSettingsStoreError as exc:
            status_code = 409 if exc.code == "model_settings_file_invalid" else 500
            return _settings_error(status_code, exc.code, exc.message)
        return _settings_json(snapshot.to_dict())

    @app.post("/v1/model-settings/test")
    async def test_model_settings_route(request: Request) -> Response:
        rejected = _settings_request_rejection(request, require_origin=True)
        if rejected is not None:
            return rejected
        try:
            payload = await _read_model_settings_body(request)
            settings = resolve_model_settings_draft(
                app.state.project_root,
                base_url=payload["base_url"],
                model_name=payload["model_name"],
                api_key=payload.get("api_key", ""),
            )
        except ModelSettingsValidationError as exc:
            return _settings_error(400, exc.code, exc.message)
        except ModelSettingsStoreError as exc:
            return _settings_error(500, exc.code, exc.message)

        try:
            run_dir = _create_model_connection_test_run_dir(app.state.project_root)
            await run_in_threadpool(
                audited_complete_chat,
                [ChatMessage("user", "请只回复 OK。")],
                settings,
                run_dir=run_dir,
                evidence_refs=[],
                prompt_version="model-connection-test-v1",
                timeout_seconds=8.0,
            )
        except AuditPersistError:
            return _settings_test_result(False, "model_audit_failed", "连接测试未能安全记录，请检查本机运行目录。")
        except ModelResponseError:
            return _settings_test_result(False, "model_connection_failed", "未能连接模型服务，请检查设置后重试。")
        except (OSError, ModelSettingsStoreError):
            return _settings_test_result(False, "model_audit_failed", "连接测试未能安全记录，请检查本机运行目录。")
        except Exception:
            return _settings_test_result(False, "model_connection_failed", "模型连接测试失败，请检查设置后重试。")
        return _settings_test_result(True, "connection_succeeded", "模型连接成功。")

    @app.get("/v1/annual-analysis-jobs/{job_id}")
    def get_annual_analysis_job(job_id: str) -> dict:
        try:
            return app.state.annual_analysis_jobs.get(job_id)
        except AnnualAnalysisJobError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": exc.message},
            ) from None

    @app.get("/v1/annual-analyses/{run_id}/evidence/{evidence_id}/preview.png")
    def get_annual_analysis_evidence_preview(run_id: str, evidence_id: str) -> Response:
        try:
            archive = load_annual_report(app.state.project_root, run_id)
            image, page_number = render_evidence_preview(archive, evidence_id)
        except AnnualReportReadError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": exc.message},
            ) from None
        return Response(
            content=image,
            media_type="image/png",
            headers={
                "X-PDF-Page": str(page_number),
                "X-Report-SHA256": archive.manifest_summary["report_sha256"],
            },
        )

    @app.get("/v1/annual-analyses/{run_id:path}")
    def get_annual_analysis_report(run_id: str) -> dict:
        try:
            archive = load_annual_report(app.state.project_root, run_id)
        except AnnualReportReadError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": exc.message},
            ) from None
        return {
            "report": archive.report,
            "manifest": archive.manifest_summary,
            # This curated index contains only evidence_ids attached to a
            # verified record under report.confirmed.metrics[*].
            "verification_evidences": list(archive.verification_evidences),
        }

    return app


def _settings_request_rejection(request: Request, *, require_origin: bool) -> Response | None:
    host = request.headers.get("host")
    parsed_host = _parse_loopback_host(host)
    if parsed_host is None:
        return _settings_error(403, "local_host_required", "模型设置仅允许通过本机地址访问。")
    if not require_origin:
        return None
    origin = request.headers.get("origin")
    if origin is None:
        return _settings_error(403, "local_origin_required", "模型设置修改仅允许从本机页面发起。")
    parsed_origin = _parse_loopback_origin(origin)
    if parsed_origin is None:
        return _settings_error(403, "local_origin_required", "模型设置修改仅允许从本机页面发起。")
    origin_host, origin_port, origin_scheme = parsed_origin
    host_name, host_port = parsed_host
    same_origin = origin_host == host_name and origin_port == host_port
    dev_origin = (
        origin_scheme == "http"
        and origin_port == 5173
        and host_port == 8000
        and origin_host in _LOOPBACK_DEV_HOSTS
        and host_name in _LOOPBACK_DEV_HOSTS
    )
    if not (same_origin or dev_origin):
        return _settings_error(403, "local_origin_required", "模型设置修改仅允许从本机页面发起。")
    return None


def _parse_loopback_host(value: str | None) -> tuple[str, int | None] | None:
    if not value or any(char in value for char in "\\/?#@\r\n\t "):
        return None
    try:
        parts = urlsplit("//" + value)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if not hostname or parts.username is not None or parts.password is not None:
        return None
    normalized = hostname.lower().rstrip(".")
    if normalized != "localhost":
        try:
            if not ipaddress.ip_address(normalized).is_loopback:
                return None
        except ValueError:
            return None
    return normalized, port


def _parse_loopback_origin(value: str) -> tuple[str, int, str] | None:
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme not in {"http", "https"}
        or not parts.netloc
        or not hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        return None
    normalized = hostname.lower().rstrip(".")
    if normalized != "localhost":
        try:
            if not ipaddress.ip_address(normalized).is_loopback:
                return None
        except ValueError:
            return None
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return normalized, port, parts.scheme


async def _read_model_settings_body(request: Request) -> dict[str, str]:
    length_header = request.headers.get("content-length")
    if length_header is not None:
        try:
            if int(length_header) > _MAX_SETTINGS_BODY_BYTES:
                raise ModelSettingsValidationError("invalid_model_settings", "请求内容超过允许长度。")
        except ValueError:
            raise ModelSettingsValidationError("invalid_model_settings", "请求内容格式无效。") from None
    raw = bytearray()
    try:
        async for chunk in request.stream():
            if len(raw) + len(chunk) > _MAX_SETTINGS_BODY_BYTES:
                raise ModelSettingsValidationError("invalid_model_settings", "请求内容超过允许长度。")
            raw.extend(chunk)
        payload = json.loads(bytes(raw).decode("utf-8"))
    except ModelSettingsValidationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ModelSettingsValidationError("invalid_model_settings", "请求内容必须是有效 JSON。") from None
    if not isinstance(payload, dict) or set(payload) - {"base_url", "model_name", "api_key"}:
        raise ModelSettingsValidationError("invalid_model_settings", "请求字段不受支持。")
    if "base_url" not in payload or "model_name" not in payload:
        raise ModelSettingsValidationError("invalid_model_settings", "请求缺少服务地址或模型名称。")
    if any(type(payload.get(key)) is not str for key in ("base_url", "model_name")):
        raise ModelSettingsValidationError("invalid_model_settings", "服务地址和模型名称必须是文本。")
    if "api_key" in payload and type(payload["api_key"]) is not str:
        raise ModelSettingsValidationError("invalid_model_settings", "密钥必须是文本。")
    return payload


def _create_model_connection_test_run_dir(project_root: Path) -> Path:
    root = Path(project_root).resolve()
    artifacts = root / "artifacts"
    runs = artifacts / "runs"
    for directory in (artifacts, runs):
        try:
            info = directory.lstat()
        except FileNotFoundError:
            directory.mkdir(mode=0o700)
            continue
        except OSError:
            raise ModelSettingsStoreError("model_audit_path_invalid", "本机运行目录无法安全访问。") from None
        if (
            stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE)
            or not stat.S_ISDIR(info.st_mode)
        ):
            raise ModelSettingsStoreError("model_audit_path_invalid", "本机运行目录无法安全访问。")
    run_dir = runs / f"model-connection-test-{uuid4()}"
    try:
        run_dir.mkdir(mode=0o700, exist_ok=False)
    except OSError:
        raise ModelSettingsStoreError("model_audit_path_invalid", "本机运行目录无法安全访问。") from None
    if run_dir.resolve().parent != runs.resolve():
        raise ModelSettingsStoreError("model_audit_path_invalid", "本机运行目录无法安全访问。")
    return run_dir


def _settings_json(payload: dict) -> JSONResponse:
    return JSONResponse(payload, headers=_SETTINGS_CACHE_HEADERS)


def _settings_error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": {"code": code, "message": message}},
        headers=_SETTINGS_CACHE_HEADERS,
    )


def _settings_test_result(success: bool, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"success": success, "code": code, "message": message},
        headers=_SETTINGS_CACHE_HEADERS,
    )


app = create_app()
