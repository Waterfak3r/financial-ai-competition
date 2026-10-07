"""Killable one-request HTTP worker used by chat_completion on every call."""

from __future__ import annotations

import base64
import json
import os
import queue
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from finagent.core.model_call_ledger import (
    ModelCallAdmissionError,
    admission_gate_is_open,
    mark_provider_request_started,
)


def main() -> int:
    try:
        request_data = json.loads(sys.stdin.buffer.read(2 * 1024 * 1024 + 1).decode("utf-8"))
        if not isinstance(request_data, dict) or set(request_data) != {
            "url", "api_key", "model", "messages", "timeout_seconds", "admission_id"
        }:
            return _write({"ok": False, "kind": "invalid_request"}, 2)
        if _cancelled_or_parent_gone():
            return _write({"ok": False, "kind": "cancelled"}, 75)
        gate_db = os.environ.get("FINTRACE_CALL_GATE_DB")
        gate_key = os.environ.get("FINTRACE_CALL_GATE_KEY")
        admission_id = request_data.get("admission_id")
        if not isinstance(admission_id, str):
            return _write({"ok": False, "kind": "invalid_request"}, 2)
        if gate_db:
            if not gate_key or not admission_gate_is_open(Path(gate_db), gate_key, admission_id):
                return _write({"ok": False, "kind": "admission", "code": "call_gate_closed"}, 0)
            try:
                mark_provider_request_started(Path(gate_db), gate_key, admission_id)
            except ModelCallAdmissionError as exc:
                return _write({"ok": False, "kind": "admission", "code": exc.code}, 0)
        if _cancelled_or_parent_gone():
            return _write({"ok": False, "kind": "cancelled"}, 75)
        outcome: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        thread = threading.Thread(target=_network_call, args=(request_data, outcome), daemon=True)
        thread.start()
        while thread.is_alive():
            if _cancelled_or_parent_gone():
                # This process boundary is the only owner of the blocked socket.
                # os._exit kills the daemon network thread and closes that socket.
                os._exit(75)
            thread.join(0.05)
        return _write(outcome.get_nowait(), 0)
    except BaseException:
        return _write({"ok": False, "kind": "worker_error"}, 3)


def _network_call(request_data: dict[str, Any], outcome: queue.Queue[dict[str, Any]]) -> None:
    try:
        payload = {
            "model": request_data["model"],
            "messages": request_data["messages"],
            "stream": False,
        }
        request = Request(
            request_data["url"],
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {request_data['api_key']}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        with build_opener(_NoRedirect()).open(request, timeout=float(request_data["timeout_seconds"])) as response:
            raw = response.read()
        outcome.put({"ok": True, "raw_b64": base64.b64encode(raw).decode("ascii")})
    except HTTPError as exc:
        outcome.put({"ok": False, "kind": "http", "status": int(exc.code)})
    except (TimeoutError, socket.timeout):
        outcome.put({"ok": False, "kind": "timeout"})
    except URLError:
        outcome.put({"ok": False, "kind": "connection"})
    except BaseException:
        outcome.put({"ok": False, "kind": "transport"})


def _cancelled_or_parent_gone() -> bool:
    cancel_path = os.environ.get("FINTRACE_CALL_CANCEL_FILE")
    if cancel_path and os.path.exists(cancel_path):
        return True
    heartbeat_path = os.environ.get("FINTRACE_CALL_LIVENESS_FILE")
    if heartbeat_path:
        try:
            return time.time() - os.stat(heartbeat_path).st_mtime > 5.0
        except OSError:
            return True
    return False


def _write(payload: dict[str, Any], exit_code: int) -> int:
    try:
        sys.stdout.write(json.dumps(payload, separators=(",", ":"), ensure_ascii=True) + "\n")
        sys.stdout.flush()
    except OSError:
        return 4
    return exit_code


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


if __name__ == "__main__":
    raise SystemExit(main())
