"""本地年度财务预检 API。无用户账户，不调用云端模型。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from finagent.api.annual_precheck import (
    HashMismatchError,
    create_annual_precheck,
    load_annual_precheck,
)
from finagent.core.safe_paths import PathBoundaryError
from finagent.ingestion.errors import PdfInputError
from finagent.ingestion.upload_text_pdf import UploadRejected, save_text_pdf_upload


class AnnualPrecheckRequest(BaseModel):
    parsed_path: str = Field(..., description="相对 data/processed 的 text_pdf.json 路径")
    source_pdf_path: str = Field(..., description="相对 data/raw 的原始 PDF 路径")
    company_id: str
    report_year: int = Field(..., ge=1900, le=2100)


def create_app(project_root: Path | None = None) -> FastAPI:
    root = Path(project_root).resolve() if project_root is not None else Path(__file__).resolve().parents[4]
    app = FastAPI(title="finagent annual precheck", version="0.1.0")
    app.state.project_root = root

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

    return app


app = create_app()
