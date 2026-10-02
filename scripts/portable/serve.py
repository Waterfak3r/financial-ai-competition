"""Serve the bundled FINTRACE web client and local FastAPI application."""

from __future__ import annotations

import mimetypes
import os
import hashlib
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import quote

from starlette.responses import FileResponse, PlainTextResponse


ROOT = Path(__file__).resolve().parents[2]
STATIC_ROOT = (ROOT / "frontend" / "dist").resolve()
DEFAULT_RUN_ID = "annual-analysis-8036d75c-6a13-432a-b602-5f869f9396ce"
DEFAULT_PORT_START = 18765
DEFAULT_PORT_END = 18785
_DLL_DIRECTORY_HANDLES: list[object] = []
_INSTANCE_MUTEX_HANDLE: int | None = None


def _prepare_runtime() -> None:
    if sys.version_info[:3] != (3, 14, 3) or sys.maxsize <= 2**32:
        raise RuntimeError("便携 Python 版本或架构不匹配；请重新完整解压官方发行包。")

    for key in tuple(os.environ):
        if key.upper().startswith("MODEL_"):
            os.environ.pop(key, None)

    for dll_dir in (ROOT / "runtime", ROOT / "runtime" / "vendor"):
        if dll_dir.is_dir():
            try:
                _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(str(dll_dir)))
            except (AttributeError, OSError):
                pass
    for dll_dir in (ROOT / "runtime" / "vendor").glob("*.libs"):
        try:
            _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(str(dll_dir)))
        except OSError:
            pass

    required = (
        ROOT / "portable-package.json",
        ROOT / "frontend" / "dist" / "index.html",
        ROOT / "data" / "raw" / "603288" / "2024" / "cninfo-1222994233" / "1222994233.PDF",
    )
    missing = [path.relative_to(ROOT).as_posix() for path in required if not path.is_file()]
    if missing:
        raise RuntimeError("未找到完整解压的便携包文件：" + "、".join(missing))

    os.chdir(ROOT)


def _check_artifacts_writable() -> None:
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    probe = artifacts / ".portable-write-check"
    try:
        probe.write_text("ok", encoding="ascii")
        probe.unlink()
    except OSError as exc:
        raise RuntimeError("便携包所在目录不可写。请解压到文档、桌面等可写目录后重试。") from exc


def _acquire_instance_mutex() -> bool:
    """Allow one worker queue per extracted copy of the package."""
    global _INSTANCE_MUTEX_HANDLE
    if os.name != "nt":
        raise RuntimeError("Windows 10/11 x64 才能运行此便携包。")
    import ctypes

    digest = hashlib.sha256(str(ROOT).casefold().encode("utf-8")).hexdigest()[:24]
    name = f"Local\\FINTRACEPortable-{digest}"
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    create_mutex.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int
    ctypes.set_last_error(0)
    handle = create_mutex(None, 0, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        close_handle(handle)
        return False
    _INSTANCE_MUTEX_HANDLE = int(handle)
    return True


def _available_port() -> int:
    for port in range(DEFAULT_PORT_START, DEFAULT_PORT_END + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(
        f"本机端口 {DEFAULT_PORT_START}–{DEFAULT_PORT_END} 都被占用。"
        "请关闭占用程序后重试；便携包不会结束其他程序。"
    )


def _inside_static_root(request_path: str) -> Path | None:
    if "\x00" in request_path or "\\" in request_path:
        return None
    relative = request_path.lstrip("/")
    try:
        candidate = (STATIC_ROOT / relative).resolve(strict=True)
        candidate.relative_to(STATIC_ROOT)
    except (OSError, RuntimeError, ValueError):
        return None
    if not candidate.is_file():
        return None
    return candidate


class PortableASGI:
    """Keep the product API and frontend in one loopback-only process."""

    def __init__(self, api_app: Any) -> None:
        self.api_app = api_app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.api_app(scope, receive, send)
            return

        path = scope.get("path", "/")
        if path == "/api" or path.startswith("/api/"):
            forwarded = dict(scope)
            forwarded_path = path[4:] or "/"
            forwarded["path"] = forwarded_path
            forwarded["raw_path"] = forwarded_path.encode("utf-8")
            await self.api_app(forwarded, receive, send)
            return

        method = scope.get("method", "GET").upper()
        if method not in {"GET", "HEAD"}:
            response = PlainTextResponse("Method Not Allowed", status_code=405, headers={"Allow": "GET, HEAD"})
            await response(scope, receive, send)
            return

        if path == "/":
            file_path = STATIC_ROOT / "index.html"
        else:
            file_path = _inside_static_root(path)
            if file_path is None:
                response = PlainTextResponse("Not Found", status_code=404)
                await response(scope, receive, send)
                return

        response = FileResponse(
            file_path,
            media_type=mimetypes.guess_type(file_path.name)[0] or "application/octet-stream",
            headers={"X-Content-Type-Options": "nosniff"},
        )
        await response(scope, receive, send)


def _open_browser_when_ready(url: str, target: str) -> None:
    local_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(60):
        try:
            request = urllib.request.Request(url, headers={"Accept": "application/json"})
            with local_opener.open(request, timeout=1.0) as response:
                if response.status == 200:
                    webbrowser.open(target, new=2)
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.5)
    print("服务未能在一分钟内就绪；请检查上方错误并手动打开本机地址。", flush=True)


def main() -> int:
    try:
        _prepare_runtime()
        if not _acquire_instance_mutex():
            print("此便携包已经在运行。请切换到已有启动窗口和浏览器页面。", flush=True)
            return 0
        _check_artifacts_writable()
        port = _available_port()
        from finagent.api.app import create_app
        import uvicorn

        app = PortableASGI(create_app(project_root=ROOT))
        base = f"http://127.0.0.1:{port}"
        target = f"{base}/?run_id={quote(DEFAULT_RUN_ID)}"
        ready_url = f"{base}/api/v1/annual-analyses/{DEFAULT_RUN_ID}"
        threading.Thread(
            target=_open_browser_when_ready,
            args=(ready_url, target),
            name="portable-browser-open",
            daemon=True,
        ).start()

        print(f"FINTRACE 已在本机启动：{base}", flush=True)
        print("正在打开已完成的 AI 年报分析。按 Ctrl+C 可结束本次服务。", flush=True)
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="info", access_log=False)
        return 0
    except KeyboardInterrupt:
        print("FINTRACE 服务已停止。", flush=True)
        return 0
    except Exception as exc:
        print(f"FINTRACE 启动失败：{exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
