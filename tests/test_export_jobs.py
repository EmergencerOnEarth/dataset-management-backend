from __future__ import annotations

import binascii
import io
import struct
import zipfile
from unittest.mock import MagicMock

import pytest

from backend.app.services.export_jobs import ExportSourceZipMissing, _append_directory_tree_from_source


def test_append_directory_tree_requires_source_zip():
    st = MagicMock()
    st.exists.return_value = False
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        with pytest.raises(ExportSourceZipMissing):
            _append_directory_tree_from_source(
                zf, st, MagicMock(), "dir_missing", arc_prefix="缺失目录"
            )


def _stored_zip_with_raw_filename(filename_bytes: bytes, payload: bytes) -> bytes:
    crc = binascii.crc32(payload) & 0xFFFFFFFF
    local = struct.pack(
        "<IHHHHHIIIHH",
        0x04034B50,
        20,
        0,
        0,
        0,
        0,
        crc,
        len(payload),
        len(payload),
        len(filename_bytes),
        0,
    ) + filename_bytes + payload
    central_offset = len(local)
    central = struct.pack(
        "<IHHHHHHIIIHHHHHII",
        0x02014B50,
        20,
        20,
        0,
        0,
        0,
        0,
        crc,
        len(payload),
        len(payload),
        len(filename_bytes),
        0,
        0,
        0,
        0,
        0,
        0,
    ) + filename_bytes
    eocd = struct.pack(
        "<IHHHHIIH",
        0x06054B50,
        0,
        0,
        1,
        1,
        len(central),
        central_offset,
        0,
    )
    return local + central + eocd


def test_append_directory_tree_decodes_gbk_source_zip_names():
    raw_zip = _stored_zip_with_raw_filename("问卷/问卷.xlsx".encode("gbk"), b"xlsx-bytes")
    st = MagicMock()
    st.exists.return_value = True
    st.get_bytes.return_value = raw_zip

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        _append_directory_tree_from_source(
            zf,
            st,
            MagicMock(),
            "dir_gbk",
            arc_prefix="导出目录",
        )

    names = zipfile.ZipFile(io.BytesIO(buf.getvalue())).namelist()
    assert "导出目录/问卷/问卷.xlsx" in names
    assert not any("╬" in name for name in names)
