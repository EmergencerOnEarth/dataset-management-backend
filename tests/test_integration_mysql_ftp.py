"""
真实 MySQL + FTP 全链路集成测试
=================================
运行前提：
  1. MySQL 可连（默认本机 Docker：127.0.0.1:3306 / eye_research_dataset），表由应用启动 ``init_db`` 创建
  2. 本机 FTP：``python3 scripts/run_local_ftp_server.py``（pyftpdlib，127.0.0.1:2121 / dataset / change-me）
  3. ``STORAGE_BACKEND=ftp`` 且 ``FTP_*`` 与 FTP 服务器一致
  4. ``PYTEST_INTEGRATION=1``
  5. OCT：优先使用院方样例 *-001.dat（路径见 ``_golden_oct_dat_path``）；不存在时自动生成最小合法 EOD 单帧
     *-001.dat，保证本机可跑通解析与导出

一键回归（推荐）：``bash scripts/run_local_fullstack_regression.sh``

手动示例：

  PYTEST_INTEGRATION=1 MYSQL_COMPOSE_ONLY=true MYSQL_HOST=127.0.0.1 \\
  STORAGE_BACKEND=ftp FTP_HOST=127.0.0.1 FTP_PORT=2121 \\
  FTP_USER=dataset FTP_PASSWORD=change-me FTP_ROOT=/dataset \\
  python3 -m pytest tests/test_integration_mysql_ftp.py -v

注意：请勿与默认 ``STORAGE_BACKEND=local`` 的单元测试在同一进程先导入 ``main`` 再跑本文件；
请单独执行本文件或先 ``get_settings.cache_clear()``（本模块 ``client`` fixture 已处理）。

覆盖范围：
  T01～T08  上传 / MySQL / FTP / 导出闭环（新视野：FDT 为 JPEG 魔数、OCT 为 *-001.dat + json）
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import struct
import time
import zipfile
from pathlib import Path

import numpy as np
import pytest
from openpyxl import Workbook

pytestmark = pytest.mark.skipif(
    os.environ.get("PYTEST_INTEGRATION") != "1",
    reason="需要 PYTEST_INTEGRATION=1 + 真实 MySQL + FTP（见文件头注释）",
)

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.parsers.image_stubs import STUB_JPEG_BYTES  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _resp(response):
    payload = response.json()
    assert payload["code"] == 0, f"API error: {payload}"
    return payload["data"]


def _upload_zip(client: TestClient, content: bytes, filename: str) -> dict:
    fh = hashlib.sha256(content).hexdigest()
    n = len(content)
    u = _resp(
        client.post(
            "/api/v1/dataset-upload/uploads",
            json={
                "fileName": filename,
                "fileSize": n,
                "fileHash": fh,
                "chunkSize": 4 * 1024 * 1024,
            },
        )
    )
    upload_id = u["uploadId"]
    chunk = int(u["chunkSize"])
    part_count = int(u["partCount"])
    for part_number in range(1, part_count + 1):
        start = (part_number - 1) * chunk
        end = min(part_number * chunk - 1, n - 1)
        body = content[start : end + 1]
        pr = _resp(
            client.put(
                f"/api/v1/dataset-upload/uploads/{upload_id}/parts/{part_number}",
                content=body,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Range": f"bytes {start}-{end}/{n}",
                },
            )
        )
        assert pr["partNumber"] == part_number
    return _resp(
        client.post(
            f"/api/v1/dataset-upload/uploads/{upload_id}/complete",
            json={"fileHash": fh},
        )
    )


def _wait_import(client: TestClient, task_id: str, timeout: int = 180) -> dict:
    for _ in range(timeout * 20):
        task = _resp(client.get(f"/api/v1/dataset-import/tasks/{task_id}"))
        if task["importStatus"] in ("SUCCESS", "FAILED"):
            return task
        time.sleep(0.05)
    raise TimeoutError(f"Import task {task_id} did not complete within {timeout}s")


def _wait_export_by_db(export_id: str, timeout: int = 30) -> "ExportRecord":
    """直接查 MySQL，避免依赖不存在的 GET /exports 接口。"""
    from backend.app.db.models import ExportRecord
    from backend.app.db.session import get_session_factory

    for _ in range(timeout * 20):
        fac = get_session_factory()
        db = fac()
        try:
            rec = db.get(ExportRecord, export_id)
            if rec and rec.export_status in ("DONE", "FAILED"):
                return rec
        finally:
            db.close()
        time.sleep(0.05)
    raise TimeoutError(f"Export {export_id} did not complete within {timeout}s")


def _golden_oct_dat_path() -> Path:
    """新视野标准样例 *-001.dat（与本仓库 test-data 对齐）。"""
    return (
        Path(__file__).resolve().parents[1]
        / "test-data/upload-samples/local/测试上传数据/中航数据/2026/OCT/2026-3-3/"
        "X08-data(2026-03-03-2026-03-03)/database/info-data/50/LGTA00087/"
        "x08-rds/20260303/od-3dscan-macular-20260303-092714-001.dat"
    )


def _minimal_oct_001_dat_bytes() -> bytes:
    """最小 EOD *-001.dat：1 帧 8×8、uint8、无压缩，与 ``newvision_oct.open_oct_dat`` 约定一致。"""
    buf = bytearray(1024)
    buf[0:4] = b"EOD\x00"
    off = 4
    struct.pack_into("<I", buf, off, 1)
    off += 4
    struct.pack_into("<7I", buf, off, 0, 1, 8, 8, 1, 1024, 0)
    off += 28
    off += 120
    struct.pack_into("<I", buf, off, 0)
    off += 4
    off += 22 + 100 + 40
    off = (off + 7) & ~7
    struct.pack_into("<q", buf, off, 0)
    off += 8
    struct.pack_into("<4I", buf, off, 0, 0, 0, 0)
    off += 16
    off += 164
    off = (off + 7) & ~7
    struct.pack_into("<2i", buf, off, 0, 0)
    off += 8
    off += 496
    off = (off + 7) & ~7
    frame = np.arange(64, dtype=np.uint8).reshape(8, 8)
    return bytes(buf) + frame.tobytes()


def _oct_dat_bytes_for_zip() -> bytes:
    golden = _golden_oct_dat_path()
    if golden.is_file():
        return golden.read_bytes()
    return _minimal_oct_001_dat_bytes()


def _build_test_zip() -> bytes:
    """问卷 + 眼底（JPEG 魔数 FDT）+ OCT *-001.dat/json（院方样例或合成最小 EOD dat）。"""
    xbio = io.BytesIO()
    wb = Workbook()
    ws = wb.active
    ws.append(["患者ID", "调查日期", "姓名", "性别"])
    ws.append(["INT_PT_001", "2026-05-08", "集成测试患者", "男"])
    ws.append(["INT_PT_001", "2026-05-07", "集成测试患者", "男"])
    ws.append(["INT_PT_002", "2026-05-08", "另一患者", "女"])
    wb.save(xbio)
    xbio.seek(0)

    oct_json = json.dumps({
        "eye_axial_length": 24.5,
        "eye_sphere": -2.5,
        "snr": 42.1,
    }).encode()

    oct_dat = _oct_dat_bytes_for_zip()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("问卷/int_test.xlsx", xbio.read())
        zf.writestr("眼底照相/INT_PT_001/2026-05-08/fundus_01.fdt", STUB_JPEG_BYTES)
        zf.writestr("眼底照相/INT_PT_001/2026-05-07/fundus_02.fdt", STUB_JPEG_BYTES)
        zf.writestr("眼底照相/INT_PT_002/2026-05-08/fundus_p2.fdt", STUB_JPEG_BYTES)
        rel_oct = "oct/INT_PT_001/2026-05-08/oct_scan-001.dat"
        zf.writestr(rel_oct, oct_dat)
        zf.writestr(rel_oct.replace(".dat", ".json"), oct_json)
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# Module fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    import backend.app.core.config as cfg
    import backend.app.db.session as db_sess

    cfg.get_settings.cache_clear()
    db_sess._engine = None  # noqa: SLF001
    db_sess._SessionLocal = None  # noqa: SLF001
    from backend.app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def imported_directory(client: TestClient):
    """上传并导入测试包，所有 T0x 共享，模块结束时清理。"""
    content = _build_test_zip()
    completed = _upload_zip(client, content, "int_test.zip")
    created = _resp(
        client.post(
            "/api/v1/dataset-directories",
            json={"directoryName": "INT_集成测试目录_20260508", "fileId": completed["fileId"]},
        )
    )
    task = _wait_import(client, created["importTaskId"])
    assert task["importStatus"] == "SUCCESS", f"Import FAILED: {task}"

    yield {
        "directoryId": created["directoryId"],
        "importTaskId": created["importTaskId"],
        "task": task,
    }

    _cleanup_directory(created["directoryId"])


def _cleanup_directory(directory_id: str) -> None:
    import pymysql, re, ftplib
    from backend.app.core.config import get_settings

    settings = get_settings()

    # FTP 清理
    try:
        ftp = ftplib.FTP()
        ftp.connect(settings.ftp_host, settings.ftp_port, timeout=5)
        ftp.login(settings.ftp_user, settings.ftp_password)
        for prefix in [
            f"dataset/import/raw_zip/{directory_id}",
            f"dataset/import/raw_tree/{directory_id}",
            f"dataset/import/parsed/{directory_id}",
        ]:
            _ftp_rmtree(ftp, prefix)
        ftp.quit()
    except Exception as e:
        print(f"[cleanup] FTP error: {e}")

    # MySQL 清理（export_record 无 directory_id，单独处理）
    try:
        url = settings.database_url or ""
        m = re.match(r"mysql\+pymysql://([^:]+):([^@]*)@([^:]+):(\d+)/(\w+)", url)
        if not m:
            return
        user, pwd, host, port, db = m.group(1), m.group(2), m.group(3), int(m.group(4)), m.group(5)
        conn = pymysql.connect(host=host, port=port, user=user, password=pwd, database=db)
        cur = conn.cursor()
        # 先查出 export_record_ids
        cur.execute(
            "SELECT export_record_id FROM export_record "
            "WHERE payload_json LIKE %s",
            (f'%"{directory_id}"%',),
        )
        exp_ids = [r[0] for r in cur.fetchall()]
        if exp_ids:
            fmt = ",".join(["%s"] * len(exp_ids))
            cur.execute(f"DELETE FROM export_record WHERE export_record_id IN ({fmt})", exp_ids)
        for table in [
            "dataset_import_warning",
            "dataset_image_metadata",
            "dataset_image_asset",
            "dataset_dynamic_column",
            "dataset_questionnaire_record",
            "dataset_import_task",
            "dataset_directory",
        ]:
            cur.execute(f"DELETE FROM {table} WHERE directory_id = %s", (directory_id,))
        conn.commit()
        conn.close()
        print(f"[cleanup] Deleted directory {directory_id} from MySQL")
    except Exception as e:
        print(f"[cleanup] MySQL error: {e}")


def _ftp_rmtree(ftp, path: str) -> None:
    try:
        items = ftp.nlst(path)
    except Exception:
        return
    for item in items:
        item = item.replace("\\", "/")
        if item in (".", "..") or item.endswith("/.") or item.endswith("/.."):
            continue
        try:
            ftp.delete(item)
        except Exception:
            _ftp_rmtree(ftp, item)


# ─────────────────────────────────────────────────────────────────────────────
# T01：导入成功
# ─────────────────────────────────────────────────────────────────────────────

def test_T01_import_success(imported_directory):
    task = imported_directory["task"]
    assert task["importStatus"] == "SUCCESS"
    assert task["recordCount"] >= 3, f"Expected >=3 questionnaire rows, got recordCount={task['recordCount']}"
    assert task["assetCount"] >= 4, f"Expected >=4 image assets, got assetCount={task['assetCount']}"


# ─────────────────────────────────────────────────────────────────────────────
# T02：MySQL 校验
# ─────────────────────────────────────────────────────────────────────────────

def test_T02_mysql_questionnaire_records(imported_directory):
    from backend.app.db.models import DatasetQuestionnaireRecord
    from backend.app.db.session import get_session_factory
    from sqlalchemy import select

    did = imported_directory["directoryId"]
    db = get_session_factory()()
    try:
        rows = db.execute(
            select(DatasetQuestionnaireRecord).where(DatasetQuestionnaireRecord.directory_id == did)
        ).scalars().all()
        assert len(rows) >= 3
        pids = {r.patient_id for r in rows}
        assert "INT_PT_001" in pids
        assert "INT_PT_002" in pids
        dates_pt1 = {r.survey_date for r in rows if r.patient_id == "INT_PT_001"}
        assert "2026-05-08" in dates_pt1
        assert "2026-05-07" in dates_pt1
    finally:
        db.close()


def test_T02_mysql_image_assets_with_parsed_path(imported_directory):
    from backend.app.db.models import DatasetImageAsset
    from backend.app.db.session import get_session_factory
    from sqlalchemy import select

    did = imported_directory["directoryId"]
    db = get_session_factory()()
    try:
        assets = db.execute(
            select(DatasetImageAsset).where(DatasetImageAsset.directory_id == did)
        ).scalars().all()
        assert len(assets) >= 4

        # 允许无 parsed_path 的资产类型：FUNDUS_FDT（原始 fdt 无 jpg）和 OCT_JSON（JSON 本身无预览图）
        _no_preview_types = {"FUNDUS_FDT", "OCT_JSON"}
        for a in assets:
            if not a.parsed_path:
                assert a.source_type in _no_preview_types, (
                    f"Asset {a.image_name} (source_type={a.source_type}) has no parsed_path"
                )
                continue
            low = a.parsed_path.lower()
            assert low.endswith((".jpg", ".png")), f"unexpected parsed_path suffix: {a.parsed_path}"
    finally:
        db.close()


def test_T02_mysql_oct_json_asset_records(imported_directory):
    """NV-09 / DC-07：每个 OCT JSON 文件应有独立 DatasetImageAsset(OCT_JSON) 记录，
    且 metadata_json 包含原始 JSON 内容（raw 字段）与扁平化摘要（flattened 字段）。"""
    from backend.app.db.models import DatasetImageAsset, DatasetImageMetadata
    from backend.app.db.session import get_session_factory
    from sqlalchemy import select

    did = imported_directory["directoryId"]
    db = get_session_factory()()
    try:
        json_assets = db.execute(
            select(DatasetImageAsset).where(
                DatasetImageAsset.directory_id == did,
                DatasetImageAsset.source_type == "OCT_JSON",
            )
        ).scalars().all()
        assert json_assets, "No OCT_JSON asset records found"

        for ja in json_assets:
            meta = db.get(DatasetImageMetadata, ja.image_id)
            assert meta is not None, f"No metadata for OCT_JSON asset {ja.image_id}"
            mj = meta.metadata_json or {}
            assert mj.get("sourceType") == "OCT_JSON"
            assert mj.get("parserVersion") == "newvision-v1.1.0"
            assert "raw" in mj, f"raw JSON missing in metadata_json for {ja.image_name}"
            assert "flattened" in mj, f"flattened missing for {ja.image_name}"
            raw = mj["raw"]
            assert raw is not None, "raw JSON should not be null"
    finally:
        db.close()


def test_T02_mysql_oct_dynamic_columns(imported_directory):
    from backend.app.db.models import DatasetDynamicColumn, DatasetQuestionnaireRecord
    from backend.app.db.session import get_session_factory
    from sqlalchemy import select

    did = imported_directory["directoryId"]
    db = get_session_factory()()
    try:
        cols = db.execute(
            select(DatasetDynamicColumn).where(DatasetDynamicColumn.directory_id == did)
        ).scalars().all()

        oct_cols = {c.column_key for c in cols if c.source_type == "OCT_JSON"}
        # flatten('oct') + key 'eye_axial_length' → 'oct_eye_axial_length'
        assert "oct_eye_axial_length" in oct_cols, f"OCT cols: {oct_cols}"
        assert "oct_eye_sphere" in oct_cols
        assert "oct_snr" in oct_cols

        # 对应问卷行 cells 应有 oct 字段值
        rows = db.execute(
            select(DatasetQuestionnaireRecord).where(
                DatasetQuestionnaireRecord.directory_id == did,
                DatasetQuestionnaireRecord.patient_id == "INT_PT_001",
                DatasetQuestionnaireRecord.survey_date == "2026-05-08",
            )
        ).scalars().all()
        assert rows, "No record for INT_PT_001 / 2026-05-08"
        cells = rows[0].normalized_row_json or {}
        assert float(cells.get("oct_eye_sphere", 0)) == pytest.approx(-2.5)
        assert float(cells.get("oct_eye_axial_length", 0)) == pytest.approx(24.5)
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# T03：FTP 落盘校验
# ─────────────────────────────────────────────────────────────────────────────

def test_T03_ftp_raw_zip_exists(imported_directory):
    from backend.app.core.config import get_settings
    from backend.app.storage.backend import get_storage

    did = imported_directory["directoryId"]
    storage = get_storage(get_settings())
    assert storage.exists(f"/dataset/import/raw_zip/{did}/source.zip")


def test_T03_ftp_raw_tree_files(imported_directory):
    from backend.app.db.models import DatasetImageAsset
    from backend.app.db.session import get_session_factory
    from backend.app.core.config import get_settings
    from backend.app.storage.backend import get_storage
    from sqlalchemy import select

    did = imported_directory["directoryId"]
    storage = get_storage(get_settings())
    db = get_session_factory()()
    try:
        assets = db.execute(
            select(DatasetImageAsset).where(DatasetImageAsset.directory_id == did)
        ).scalars().all()
        for asset in assets:
            assert storage.exists(asset.original_path), (
                f"original_path missing on FTP: {asset.original_path}"
            )
    finally:
        db.close()


def test_T03_ftp_parsed_jpgs(imported_directory):
    from backend.app.db.models import DatasetImageAsset
    from backend.app.db.session import get_session_factory
    from backend.app.core.config import get_settings
    from backend.app.storage.backend import get_storage
    from sqlalchemy import select

    did = imported_directory["directoryId"]
    storage = get_storage(get_settings())
    db = get_session_factory()()
    try:
        assets = db.execute(
            select(DatasetImageAsset).where(DatasetImageAsset.directory_id == did)
        ).scalars().all()
        for asset in assets:
            if asset.parsed_path:
                assert storage.exists(asset.parsed_path), (
                    f"parsed_path missing on FTP: {asset.parsed_path}"
                )
    finally:
        db.close()


def test_T03_ftp_oct_json_sidecar(imported_directory):
    from backend.app.db.models import DatasetImageAsset
    from backend.app.db.session import get_session_factory
    from backend.app.core.config import get_settings
    from backend.app.storage.backend import get_storage, normalize_storage_path
    from backend.app.services.export_jobs import _oct_json_logical_path
    from sqlalchemy import select

    did = imported_directory["directoryId"]
    storage = get_storage(get_settings())
    db = get_session_factory()()
    try:
        assets = db.execute(
            select(DatasetImageAsset).where(DatasetImageAsset.directory_id == did)
        ).scalars().all()
        oct_assets = [a for a in assets if (a.image_name or "").lower().endswith(".dat")]
        assert oct_assets, "No OCT DAT assets found in MySQL"
        for asset in oct_assets:
            jpath = _oct_json_logical_path(normalize_storage_path(asset.original_path))
            assert jpath and storage.exists(jpath), f"OCT JSON sidecar missing: {jpath}"
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# T04：患者导出 zip 内容
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def patient_export_zip(client: TestClient, imported_directory):
    from backend.app.core.config import get_settings
    from backend.app.storage.backend import get_storage

    did = imported_directory["directoryId"]
    exp = _resp(
        client.post(
            f"/api/v1/dataset-directories/{did}/patients/INT_PT_001/export",
            json={"includeParsedImages": True, "includeOriginalAttachments": True},
        )
    )
    rec = _wait_export_by_db(exp["exportRecordId"], timeout=30)
    assert rec.export_status == "DONE", f"Export FAILED: {rec.failure_reason}"

    settings = get_settings()
    storage = get_storage(settings)
    blob = storage.get_bytes(rec.ftp_path)
    zf = zipfile.ZipFile(io.BytesIO(blob))
    yield zf
    zf.close()


def test_T04_export_has_questionnaire_json(patient_export_zip):
    names = patient_export_zip.namelist()
    assert "questionnaire_rows.json" in names
    rows = json.loads(patient_export_zip.read("questionnaire_rows.json"))
    # INT_PT_001 有 2 条问卷（2026-05-07 和 2026-05-08）
    assert len(rows) >= 2


def test_T04_export_has_original_fdt(patient_export_zip):
    names = patient_export_zip.namelist()
    fdt_files = [n for n in names if n.lower().endswith(".fdt")]
    assert fdt_files, f"No .fdt in export. Files: {names}"


def test_T04_export_has_parsed_image(patient_export_zip):
    names = patient_export_zip.namelist()
    parsed = [
        n
        for n in names
        if n.startswith("images/parsed/")
        and n.lower().endswith((".jpg", ".jpeg", ".png"))
    ]
    assert parsed, f"No parsed preview files in export. Files: {names}"
    for p in parsed:
        blob = patient_export_zip.read(p)
        if p.lower().endswith(".png"):
            assert blob[:8] == b"\x89PNG\r\n\x1a\n", f"{p} is not a valid PNG"
        else:
            assert blob[:2] == b"\xff\xd8", f"{p} is not a valid JPEG (header={blob[:2].hex()})"


def test_T04_oct_export_has_multiple_frame_png_if_present(patient_export_zip):
    """患者导出：`images/parsed/.../*.frames/frame_*.png` 应多帧（沿用 metadata octDat.frames）。"""
    names = patient_export_zip.namelist()
    posix = [n.replace("\\", "/") for n in names]
    frame_pngs = [
        n
        for n in posix
        if n.startswith("images/parsed/")
        and ".frames/" in n
        and n.lower().endswith(".png")
    ]
    if not frame_pngs:
        pytest.skip("本夹具 ZIP 无语义 OCT 帧或解析未生成 frames")
    assert len(frame_pngs) >= 3, (
        f"PARSED_OCT_DAT 应导出多条帧 PNG；got {len(frame_pngs)}: {frame_pngs[:6]}"
    )
    names = patient_export_zip.namelist()
    oct_jsons = [n for n in names if n.startswith("images/oct_json/") and n.lower().endswith(".json")]
    assert oct_jsons, f"No OCT json in export. Files: {names}"
    for j in oct_jsons:
        data = json.loads(patient_export_zip.read(j))
        assert "eye_axial_length" in data or "eye_sphere" in data, (
            f"Unexpected OCT json content: {data}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# T05：目录导出 zip 结构
# ─────────────────────────────────────────────────────────────────────────────

def test_T05_directory_export_structure(client: TestClient, imported_directory):
    from backend.app.core.config import get_settings
    from backend.app.storage.backend import get_storage

    did = imported_directory["directoryId"]
    exp = _resp(
        client.post(
            "/api/v1/dataset-directories/export",
            json={
                "directoryIds": [did],
                "includeOriginalTable": True,
                "includeMergedTable": True,
                "includeParsedImages": True,
                "includeOriginalAttachments": True,
            },
        )
    )
    rec = _wait_export_by_db(exp["exportRecordId"], timeout=30)
    assert rec.export_status == "DONE", f"Dir export FAILED: {rec.failure_reason}"

    settings = get_settings()
    storage = get_storage(settings)
    blob = storage.get_bytes(rec.ftp_path)
    names = zipfile.ZipFile(io.BytesIO(blob)).namelist()

    xlsx_files = [n for n in names if n.lower().endswith(".xlsx")]
    fdt_files = [n for n in names if n.lower().endswith(".fdt")]
    assert xlsx_files, f"No xlsx in directory export. Files: {names}"
    assert fdt_files, f"No fdt in directory export. Files: {names}"

    posix_names = [n.replace("\\", "/") for n in names]
    assert any("_parsed_derived/" in pn for pn in posix_names), (
        f"I-33/NV: 目录导出应在 `{{did}}/_parsed_derived/` 下包含解析产物（JPG 或 OCT 帧）。Got: {names[:80]}"
    )
    png_frames_export = [
        n
        for n in posix_names
        if "_parsed_derived/" in n and ".frames/" in n and n.lower().endswith(".png")
    ]
    jpg_derived = [
        n
        for n in posix_names
        if "_parsed_derived/" in n and n.lower().endswith(".jpg") and ".frames/" not in n
    ]
    assert jpg_derived or png_frames_export, (
        f"应在 _parsed_derived 下导出眼底 JPG 或 OCT `.frames/` PNG。Files snippet: {names[:40]}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# T06：无资源 PID 导出拒绝
# ─────────────────────────────────────────────────────────────────────────────

def test_T06_empty_patient_export_rejected(client: TestClient, imported_directory):
    did = imported_directory["directoryId"]
    resp = client.post(
        f"/api/v1/dataset-directories/{did}/patients/NONEXISTENT_XYZ/export",
        json={},
    )
    assert resp.status_code == 400
    assert resp.json().get("errorCode") == "DATASET_EXPORT_PATIENT_EMPTY"


# ─────────────────────────────────────────────────────────────────────────────
# T07：surveyDates 过滤
# ─────────────────────────────────────────────────────────────────────────────

def test_T07_survey_dates_filter(client: TestClient, imported_directory):
    from backend.app.core.config import get_settings
    from backend.app.storage.backend import get_storage

    did = imported_directory["directoryId"]
    exp = _resp(
        client.post(
            f"/api/v1/dataset-directories/{did}/patients/INT_PT_001/export",
            json={
                "includeParsedImages": True,
                "includeOriginalAttachments": True,
                "surveyDates": ["2026-05-07"],
            },
        )
    )
    rec = _wait_export_by_db(exp["exportRecordId"], timeout=30)
    assert rec.export_status == "DONE", f"Filtered export FAILED: {rec.failure_reason}"

    settings = get_settings()
    storage = get_storage(settings)
    blob = storage.get_bytes(rec.ftp_path)
    rows = json.loads(zipfile.ZipFile(io.BytesIO(blob)).read("questionnaire_rows.json"))

    # 只应包含 2026-05-07 的行（INT_PT_001 只有 1 条）
    assert len(rows) == 1, f"Expected 1 filtered row, got {len(rows)}: {rows}"
    row = rows[0]
    assert "2026-05-07" in str(row), f"Row does not contain expected date: {row}"


# ─────────────────────────────────────────────────────────────────────────────
# T08：originalUrl 格式（patient_images 需要 surveyDate 参数）
# ─────────────────────────────────────────────────────────────────────────────

def test_T08_original_url_is_api_endpoint(client: TestClient, imported_directory):
    did = imported_directory["directoryId"]
    images_data = _resp(
        client.get(
            f"/api/v1/dataset-directories/{did}/patients/INT_PT_001/images",
            params={"surveyDate": "2026-05-08"},
        )
    )
    records = images_data.get("records") or images_data  # 兼容分页或列表返回
    assert records, "No images returned for INT_PT_001 2026-05-08"
    for img in records:
        url = img.get("originalUrl", "")
        assert url.startswith("/api/v1/dataset-files/"), (
            f"originalUrl should be controlled API path, got: {url}"
        )
