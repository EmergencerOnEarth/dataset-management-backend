"""
Placeholder binary → image and OCT sidecar parsing hooks.

Real fdt/dat decoders will plug in here without changing the import orchestration.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Minimal valid JPEG (1×1 pixel) for local / test until real fdt/dat pipeline exists.
STUB_JPEG_BYTES = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00\xff\xdb\x00C\x00\x08\x06\x06"
    b"\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a"
    b"\x1f\x1e\x1d\x1a\x1c\x1c $.\' \",#\x1c\x1c(7),01444\x1f\'9=82<.342\xff\xc0\x00\x11\x08\x00\x01"
    b"\x00\x01\x01\x01\x11\x00\x02\x11\x01\x03\x11\x01\xff\xc4\x00\x14\x00\x01\x00\x00\x00\x00\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x08\xff\xc4\x00\x14\x10\x01\x00\x00\x00\x00\x00\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00"
    b"\xaa\xff\xd9"
)


def parse_fdt_to_jpeg_stub(_path: Path) -> bytes:
    """Replace with real fdt decoder; currently returns a tiny JPEG placeholder."""
    return STUB_JPEG_BYTES


def parse_dat_to_jpeg_stub(_path: Path) -> bytes:
    """Replace with real dat raster decoder."""
    return STUB_JPEG_BYTES


def parse_oct_json_metadata(path: Path) -> dict[str, Any]:
    """Load OCT companion json; flatten with ``oct_`` key prefix for dynamic columns."""
    return parse_oct_json_metadata_bytes(path.read_bytes())


def parse_oct_json_metadata_bytes(raw: bytes) -> dict[str, Any]:
    data = json.loads(raw.decode("utf-8", errors="replace"))
    if not isinstance(data, dict):
        return {"oct_root": data}

    def flatten(obj: Any, prefix: str = "oct") -> dict[str, Any]:
        out: dict[str, Any] = {}
        if isinstance(obj, dict):
            for k, v in obj.items():
                key = f"{prefix}_{k}"
                if isinstance(v, dict):
                    out.update(flatten(v, key))
                else:
                    out[key] = v
        else:
            out[prefix] = obj
        return out

    return flatten(data)


def oct_json_scalar_columns(raw: bytes, *, max_keys: int = 48) -> dict[str, Any]:
    """Scalar OCT JSON entries for动态列 keys；复合结构不入列。"""
    flat = parse_oct_json_metadata_bytes(raw)
    out: dict[str, Any] = {}
    for k in sorted(flat.keys()):
        if len(out) >= max_keys:
            break
        v = flat[k]
        if isinstance(v, (dict, list)):
            continue
        out[k] = v
    return out
