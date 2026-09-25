"""把一份文本型 PDF 存入新的文档目录并解析。

不覆盖已有原始资料，不调用模型，也不读取模型密钥。
上传与年度预检是分开的步骤。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path
from typing import BinaryIO

from finagent.ingestion.errors import PdfFormatError, PdfInputError, PdfParseError
from finagent.ingestion.parse_text_pdf import parse_text_pdf

MAX_UPLOAD_BYTES = 32 * 1024 * 1024
_CHUNK = 1024 * 1024
_COMPANY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,64}$")


class UploadRejected(Exception):
    """上传不能接受。消息不含文件正文。"""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def save_text_pdf_upload(
    project_root: Path,
    *,
    company_id: str,
    report_year: object,
    stream: BinaryIO,
    max_bytes: int | None = None,
) -> dict:
    """分块读取上传内容。成功后返回相对 data/raw 与 data/processed 的路径。"""

    company = _company(company_id)
    year = _year(report_year)
    limit = MAX_UPLOAD_BYTES if max_bytes is None else max_bytes
    if type(limit) is not int or limit < 1:
        raise UploadRejected(400, "invalid_limit", "上传大小上限必须是正整数。")
    payload = _read_limited(stream, limit)
    if not payload:
        raise UploadRejected(400, "empty_file", "上传文件为空。")
    if not payload.startswith(b"%PDF"):
        raise UploadRejected(400, "invalid_pdf", "上传内容不是 PDF。")
    document_id = f"upload-{uuid.uuid4().hex}"
    relative_dir = f"{company}/{year}/{document_id}"
    project = project_root.resolve()
    raw_root = project / "data" / "raw"
    processed_root = project / "data" / "processed"
    # 符号链接可能把 data/raw 或 data/processed 指到项目外。解析并拒绝必须发生在任何 mkdir 或写文件之前。
    _assert_data_root_inside(project, raw_root)
    _assert_data_root_inside(project, processed_root)
    raw_dir = _inside(raw_root, Path(company) / str(year) / document_id)
    processed_dir = _inside(processed_root, Path(company) / str(year) / document_id)
    source_file = raw_dir / "source.pdf"
    parsed_file = processed_dir / "text_pdf.json"
    note_file = processed_dir / "processing.md"
    created: list[Path] = []
    try:
        _ensure_root(raw_root, created)
        _mkdir_new(raw_dir, raw_root, created)
        _write_new(source_file, payload, created)
        try:
            parsed = parse_text_pdf(source_file, document_id)
        except (PdfFormatError, PdfInputError, PdfParseError) as exc:
            raise UploadRejected(422, "parse_failed", str(exc)) from None
        digest = hashlib.sha256(payload).hexdigest()
        if parsed.source_sha256 != digest:
            raise UploadRejected(422, "parse_failed", "解析结果的哈希与上传字节不一致。")
        if not any(page.blocks for page in parsed.pages):
            raise UploadRejected(
                422,
                "unsupported_text_pdf",
                "这份 PDF 没有可提取文字，不能作为文本型 PDF 接受。",
            )
        _ensure_root(processed_root, created)
        _mkdir_new(processed_dir, processed_root, created)
        _write_new(parsed_file, (parsed.to_json(indent=2) + "\n").encode("utf-8"), created)
        _write_new(
            note_file,
            _processing_note(
                raw_relative=f"data/raw/{relative_dir}/source.pdf",
                sha256=digest,
                extractor=parsed.extractor,
                extractor_version=parsed.extractor_version,
            ),
            created,
        )
    except Exception:
        _rollback(created)
        raise
    return {
        "document_id": document_id,
        "source_pdf_path": f"{relative_dir}/source.pdf",
        "parsed_path": f"{relative_dir}/text_pdf.json",
        "sha256": digest,
        "page_count": parsed.page_count,
    }


def _company(company_id: str) -> str:
    if not isinstance(company_id, str) or _COMPANY.fullmatch(company_id) is None or ".." in company_id:
        raise UploadRejected(400, "invalid_company", "company_id 只能包含字母、数字、点、下划线和短横线。")
    return company_id


def _year(report_year: object) -> int:
    if isinstance(report_year, bool):
        raise UploadRejected(400, "invalid_year", "report_year 必须是 1900 到 2100 的整数。")
    if isinstance(report_year, int):
        year = report_year
    elif isinstance(report_year, str) and report_year.isdigit():
        year = int(report_year)
    else:
        raise UploadRejected(400, "invalid_year", "report_year 必须是 1900 到 2100 的整数。")
    if year < 1900 or year > 2100:
        raise UploadRejected(400, "invalid_year", "report_year 必须是 1900 到 2100 的整数。")
    return year


def _read_limited(stream: BinaryIO, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        block = stream.read(_CHUNK)
        if not block:
            break
        if not isinstance(block, (bytes, bytearray)):
            raise UploadRejected(400, "invalid_pdf", "上传内容不是二进制 PDF。")
        total += len(block)
        if total > max_bytes:
            raise UploadRejected(
                413,
                "payload_too_large",
                f"上传超过 {max_bytes} 字节上限。",
            )
        chunks.append(bytes(block))
    return b"".join(chunks)


def _assert_data_root_inside(project: Path, root: Path) -> None:
    """data/raw 与 data/processed 解析后必须仍在项目内。不创建任何路径。"""

    try:
        resolved = root.resolve()
    except OSError:
        raise UploadRejected(400, "invalid_path", "数据目录解析到项目之外，已拒绝写入。") from None
    if not resolved.is_relative_to(project):
        raise UploadRejected(400, "invalid_path", "数据目录解析到项目之外，已拒绝写入。")


def _inside(root: Path, relative: Path) -> Path:
    root_resolved = root.resolve()
    resolved = (root_resolved / relative).resolve()
    if resolved == root_resolved or not resolved.is_relative_to(root_resolved):
        raise UploadRejected(400, "invalid_path", "上传路径超出数据目录。")
    return resolved


def _ensure_root(root: Path, created: list[Path]) -> None:
    missing: list[Path] = []
    current = root
    while not current.exists():
        missing.append(current)
        current = current.parent
    for path in reversed(missing):
        path.mkdir()
        created.append(path)


def _mkdir_new(path: Path, root: Path, created: list[Path]) -> None:
    current = root.resolve()
    for part in path.resolve().relative_to(root.resolve()).parts:
        current = current / part
        if current.exists():
            if not current.is_dir() or current.is_symlink():
                raise UploadRejected(409, "path_exists", "目标路径已存在，未覆盖已有资料。")
            continue
        current.mkdir()
        created.append(current)


def _processing_note(
    *,
    raw_relative: str,
    sha256: str,
    extractor: str,
    extractor_version: str,
) -> bytes:
    """只写入程序生成的相对路径和解析器标识，不收录原文件名或密钥。"""

    if not re.fullmatch(
        r"data/raw/[A-Za-z0-9][A-Za-z0-9._-]{0,64}/[0-9]{4}/upload-[0-9a-f]{32}/source\.pdf",
        raw_relative,
    ):
        raise UploadRejected(400, "invalid_path", "处理说明中的原始路径无效。")
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise UploadRejected(422, "parse_failed", "解析结果的哈希与上传字节不一致。")
    parser = extractor if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", extractor) else "unknown"
    version = extractor_version if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", extractor_version) else "unknown"
    text = "\n".join(
        (
            "# 处理说明",
            "",
            f"- 原始文件：{raw_relative}",
            f"- SHA256：{sha256}",
            f"- 解析器：{parser} {version}",
            "- OCR：未执行",
            "- 范围：仅接受有可提取文字的文本型 PDF。图片、扫描页和空白页不转写。",
            "",
            "解析结果在同目录 text_pdf.json。未调用模型，也不是预检或舞弊结论。",
            "",
        )
    )
    return text.encode("utf-8")


def _write_new(path: Path, payload: bytes, created: list[Path]) -> None:
    if path.is_symlink() or path.exists():
        raise UploadRejected(409, "path_exists", "目标文件已存在，未覆盖已有资料。")
    try:
        handle = path.open("xb")
    except FileExistsError:
        raise UploadRejected(409, "path_exists", "目标文件已存在，未覆盖已有资料。") from None
    # xb 已经创建新文件。write 失败时也要登记，回滚才能删掉不完整内容。
    created.append(path)
    try:
        handle.write(payload)
        handle.flush()
    finally:
        handle.close()


def _rollback(created: list[Path]) -> None:
    for path in reversed(created):
        try:
            if path.is_file() and not path.is_symlink():
                path.unlink()
            elif path.is_dir() and not path.is_symlink():
                path.rmdir()
        except OSError:
            continue
