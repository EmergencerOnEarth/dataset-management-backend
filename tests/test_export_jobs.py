from __future__ import annotations

import io
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
            _append_directory_tree_from_source(zf, st, MagicMock(), "dir_missing")
