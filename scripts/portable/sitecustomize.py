"""Keep the embedded runtime's CLI streams UTF-8 on Windows system locales."""

from __future__ import annotations

import sys


for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
