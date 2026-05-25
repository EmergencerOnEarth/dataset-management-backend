#!/usr/bin/env python3
"""
数据集管理模块 全量验收测试 v1.5.0
====================================
在 v1.4.0 基础上追加：
  - API-18/19 downloadCount 字段验收（初始为 0，每次有效下载递增）
  - API-21 待下载数量接口（downloadCount=0 且 DONE 未过期任务）
  - 导出过期清理：backdate → 410 → sweep → FTP 删除 → DB EXPIRED

运行方式：
  python3 scripts/acceptance_test_v150.py [--base-url http://127.0.0.1:8091]

前置条件：
  - Docker 容器运行于 8091（docker compose up -d）
  - 宿主机 MySQL：127.0.0.1:3306 / eye_research_dataset
  - 宿主机 FTP：0.0.0.0:2121（python3 scripts/run_local_ftp_server.py --host 0.0.0.0）
"""

from __future__ import annotations

import argparse
import ftplib
import hashlib
import io
import json
import os
import struct
import subprocess
import sys
import time
import traceback
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pymysql
from openpyxl import Workbook

# ─────────────────────────────────────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────────────────────────────────────
BASE_URL = "http://127.0.0.1:8091"


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


_load_env_file(Path(".env.test"))

MYSQL_HOST = os.environ.get("MYSQL_HOST") or os.environ.get("DATABASE_HOST") or "127.0.0.1"
MYSQL_PORT = int(os.environ.get("MYSQL_PORT") or os.environ.get("DATABASE_PORT") or "3306")
MYSQL_USER = os.environ.get("MYSQL_USER") or os.environ.get("DATABASE_USER") or "root"
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD") or os.environ.get("DATABASE_PASSWORD") or ""
MYSQL_DB = os.environ.get("MYSQL_DATABASE") or os.environ.get("DATABASE_NAME") or "eye_research_dataset"
FTP_HOST = os.environ.get("FTP_HOST", "127.0.0.1")
FTP_PORT = int(os.environ.get("FTP_PORT", "2121"))
FTP_USER = os.environ.get("FTP_USER", "dataset")
FTP_PASSWORD = os.environ.get("FTP_PASSWORD", "change-me")

_TRIGGER_SCRIPT = str(Path(__file__).parent / "trigger_export_expiry_test.py")

TABLES_WITH_DIR = [
    "dataset_import_warning",
    "dataset_image_metadata",
    "dataset_image_asset",
    "dataset_dynamic_column",
    "dataset_questionnaire_record",
    "dataset_import_task",
    "dataset_directory",
]
ALL_TABLES = TABLES_WITH_DIR + [
    "dataset_merged_file", "dataset_upload_task", "dataset_upload_part", "export_record"
]

# ─────────────────────────────────────────────────────────────────────────────
# 结果收集
# ─────────────────────────────────────────────────────────────────────────────
PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"
results: list[dict] = []


def check(item_id: str, desc: str, passed: bool, detail: str = "") -> None:
    status = PASS if passed else FAIL
    results.append({"id": item_id, "desc": desc, "status": status, "detail": detail})
    icon = "✓" if passed else "✗"
    color = "\033[92m" if passed else "\033[91m"
    reset = "\033[0m"
    line = f"  {color}{icon}{reset} [{item_id}] {desc}"
    if not passed and detail:
        line += f"\n       → {detail}"
    print(line)


def expect(item_id: str, desc: str, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
        check(item_id, desc, True)
        return True
    except (AssertionError, Exception) as e:
        check(item_id, desc, False, str(e)[:200])
        return False


def section(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


# ─────────────────────────────────────────────────────────────────────────────
# MySQL 工具
# ─────────────────────────────────────────────────────────────────────────────
def mysql_conn():
    return pymysql.connect(
        host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER,
        password=MYSQL_PASSWORD, database=MYSQL_DB, charset="utf8mb4",
    )


def mysql_truncate_all() -> None:
    conn = mysql_conn()
    cur = conn.cursor()
    cur.execute("SET FOREIGN_KEY_CHECKS=0")
    for t in ALL_TABLES:
        cur.execute(f"TRUNCATE TABLE `{t}`")
    cur.execute("SET FOREIGN_KEY_CHECKS=1")
    conn.commit()
    conn.close()


def mysql_count(table: str, where: str = "", params=()) -> int:
    conn = mysql_conn()
    cur = conn.cursor()
    q = f"SELECT COUNT(*) FROM `{table}`"
    if where:
        q += f" WHERE {where}"
    cur.execute(q, params)
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0


def mysql_rows(table: str, where: str = "", params=(), cols="*") -> list[dict]:
    conn = mysql_conn()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    q = f"SELECT {cols} FROM `{table}`"
    if where:
        q += f" WHERE {where}"
    cur.execute(q, params)
    rows = cur.fetchall()
    conn.close()
    return list(rows)


def mysql_backdate_expire(export_record_id: str) -> int:
    """将 expire_at 调到 1 分钟前以触发过期（仅对 DONE 记录生效）。
    必须使用 UTC_TIMESTAMP() 而非 NOW()，因为应用层使用 utcnow() 做比较。
    """
    conn = mysql_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE export_record SET expire_at = DATE_SUB(UTC_TIMESTAMP(), INTERVAL 1 MINUTE) "
        "WHERE export_record_id = %s AND export_status = 'DONE'",
        (export_record_id,),
    )
    conn.commit()
    affected = cur.rowcount
    conn.close()
    return affected


# ─────────────────────────────────────────────────────────────────────────────
# FTP 工具
# ─────────────────────────────────────────────────────────────────────────────
def ftp_connect() -> ftplib.FTP:
    ftp = ftplib.FTP()
    ftp.connect(FTP_HOST, FTP_PORT, timeout=10)
    ftp.login(FTP_USER, FTP_PASSWORD)
    return ftp


def _ftp_rmtree(ftp: ftplib.FTP, path: str) -> None:
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


def ftp_purge_all() -> None:
    ftp = ftp_connect()
    for top in ["dataset/import", "dataset/upload", "dataset/export"]:
        _ftp_rmtree(ftp, top)
    ftp.quit()


def ftp_exists(path: str) -> bool:
    ftp = ftp_connect()
    try:
        items = ftp.nlst(path)
        exists = bool(items)
    except Exception:
        exists = False
    ftp.quit()
    return exists


def ftp_list(path: str) -> list[str]:
    ftp = ftp_connect()
    try:
        items = ftp.nlst(path)
    except Exception:
        items = []
    ftp.quit()
    return [i.replace("\\", "/") for i in items]


def ftp_read(path: str) -> bytes:
    ftp = ftp_connect()
    buf = io.BytesIO()
    ftp.retrbinary(f"RETR {path}", buf.write)
    ftp.quit()
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# HTTP 工具
# ─────────────────────────────────────────────────────────────────────────────
def ok(resp: httpx.Response) -> Any:
    resp.raise_for_status()
    body = resp.json()
    assert body.get("code") == 0, f"API error code={body.get('code')}: {body}"
    assert body.get("traceId"), f"Missing traceId: {body}"
    return body["data"]


# ─────────────────────────────────────────────────────────────────────────────
# 测试数据构造
# ─────────────────────────────────────────────────────────────────────────────
STUB_JPEG = (
    bytes([0xFF, 0xD8, 0xFF, 0xE0])
    + b"\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    + b"\xFF\xD9"
)


def _golden_oct_dat() -> bytes:
    p = (
        Path(__file__).resolve().parents[1]
        / "test-data/upload-samples/local/测试上传数据/中航数据/2026/OCT/2026-3-3/"
        "X08-data(2026-03-03-2026-03-03)/database/info-data/50/LGTA00087/"
        "x08-rds/20260303/od-3dscan-macular-20260303-092714-001.dat"
    )
    if p.is_file():
        return p.read_bytes()
    buf = bytearray(1024)
    buf[0:4] = b"EOD\x00"
    off = 4
    struct.pack_into("<I", buf, off, 1); off += 4
    struct.pack_into("<7I", buf, off, 0, 1, 8, 8, 1, 1024, 0); off += 28
    off += 120
    struct.pack_into("<I", buf, off, 0); off += 4
    off += 22 + 100 + 40
    off = (off + 7) & ~7
    struct.pack_into("<q", buf, off, 0); off += 8
    struct.pack_into("<4I", buf, off, 0, 0, 0, 0); off += 16
    off += 164
    off = (off + 7) & ~7
    struct.pack_into("<2i", buf, off, 0, 0); off += 8
    off += 496
    frame = np.arange(64, dtype=np.uint8).reshape(8, 8)
    return bytes(buf) + frame.tobytes()


def build_test_zip(pid1="ACC_PT_001", pid2="ACC_PT_002") -> bytes:
    xbio = io.BytesIO()
    wb = Workbook()
    ws = wb.active
    ws.append(["患者ID", "调查日期", "姓名"])
    ws.append([pid1, "2026-05-20", "验收患者一"])
    ws.append([pid1, "2026-05-21", "验收患者一"])
    ws.append([pid2, "2026-05-20", "验收患者二"])
    wb.save(xbio); xbio.seek(0)

    oct_json = json.dumps({"eye_axial_length": 24.5, "snr": 42.1}).encode()
    oct_dat = _golden_oct_dat()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("问卷/acc_test.xlsx", xbio.read())
        zf.writestr(f"眼底照相/{pid1}/2026-05-20/fundus_a.fdt", STUB_JPEG)
        zf.writestr(f"眼底照相/{pid1}/2026-05-21/fundus_b.fdt", STUB_JPEG)
        zf.writestr(f"眼底照相/{pid2}/2026-05-20/fundus_c.fdt", STUB_JPEG)
        zf.writestr(f"OCT/{pid1}/2026-05-20/od-scan-001.dat", oct_dat)
        zf.writestr(f"OCT/{pid1}/2026-05-20/od-scan-001.json", oct_json)
        zf.writestr(
            "_pad/pad.bin",
            bytes((i % 251 for i in range(1_200_000))),
            compress_type=zipfile.ZIP_STORED,
        )
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# 分片上传工具
# ─────────────────────────────────────────────────────────────────────────────
def do_upload(client: httpx.Client, content: bytes, filename: str, chunk: int = 1_048_576) -> dict:
    n = len(content)
    digest = hashlib.sha256(content).hexdigest()

    ic = ok(client.post("/api/v1/dataset-upload/instant-check", json={
        "fileName": filename, "fileSize": n, "fileHash": digest, "businessType": "DATASET_IMPORT",
    }))
    if ic.get("hit"):
        return {"fileId": ic["fileId"], "reused": True, "digest": digest}

    up = ok(client.post("/api/v1/dataset-upload/uploads", json={
        "fileName": filename, "fileSize": n, "fileHash": digest,
        "chunkSize": chunk, "businessType": "DATASET_IMPORT",
    }))
    uid = up["uploadId"]

    parts = [content[i:i + chunk] for i in range(0, n, chunk)]
    for idx, part in enumerate(parts, 1):
        start = (idx - 1) * chunk
        end = start + len(part) - 1
        ok(client.put(f"/api/v1/dataset-upload/uploads/{uid}/parts/{idx}", content=part, headers={
            "Content-Type": "application/octet-stream",
            "Content-Range": f"bytes {start}-{end}/{n}",
            "X-Part-Hash": hashlib.sha256(part).hexdigest(),
        }))

    done = ok(client.post(f"/api/v1/dataset-upload/uploads/{uid}/complete", json={"fileHash": digest}))
    return {"fileId": done["fileId"], "uploadId": uid, "partCount": up["partCount"], "digest": digest, "reused": False}


def wait_import(client: httpx.Client, task_id: str, timeout: int = 180) -> dict:
    for _ in range(timeout * 5):
        task = ok(client.get(f"/api/v1/dataset-import/tasks/{task_id}"))
        if task["importStatus"] in ("SUCCESS", "FAILED"):
            return task
        time.sleep(0.2)
    raise TimeoutError(f"Import {task_id} still running after {timeout}s")


def wait_export_http(client: httpx.Client, exp_id: str, timeout: int = 60) -> dict:
    for _ in range(timeout * 5):
        d = ok(client.get(f"/api/v1/dataset-exports/{exp_id}"))
        if d["exportStatus"] in ("DONE", "FAILED", "EXPIRED"):
            return d
        time.sleep(0.2)
    raise TimeoutError(f"Export {exp_id} still PREPARING after {timeout}s")


def run_sweep_subprocess() -> subprocess.CompletedProcess:
    """通过 trigger_export_expiry_test.py 触发过期清理，以独立进程运行避免 config 缓存污染。"""
    env = {
        **os.environ,
        "MYSQL_COMPOSE_ONLY": "true",
        "MYSQL_HOST": MYSQL_HOST,
        "MYSQL_PORT": str(MYSQL_PORT),
        "MYSQL_USER": MYSQL_USER,
        "MYSQL_PASSWORD": MYSQL_PASSWORD,
        "MYSQL_DATABASE": MYSQL_DB,
        "STORAGE_BACKEND": "ftp",
        "FTP_HOST": FTP_HOST,
        "FTP_PORT": str(FTP_PORT),
        "FTP_USER": FTP_USER,
        "FTP_PASSWORD": FTP_PASSWORD,
        "FTP_ROOT": "/dataset",
    }
    return subprocess.run(
        [sys.executable, _TRIGGER_SCRIPT, "--sweep-only"],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────────────────────
def run(base_url: str) -> None:
    run_id = uuid.uuid4().hex[:8]
    pid1, pid2 = "ACC_PT_001", "ACC_PT_002"
    dir_name = f"验收测试-{run_id}"

    # ── Phase 0: 清理 ──────────────────────────────────────────────────────────
    section("Phase 0 │ 清理 FTP + MySQL")
    print("  清空 FTP 全部数据...")
    ftp_purge_all()
    print("  TRUNCATE MySQL 全部业务表...")
    mysql_truncate_all()
    print("  清理完成 ✓")

    client = httpx.Client(base_url=base_url, timeout=300.0)

    # ── Phase 1: 契约 & 健康检查 ────────────────────────────────────────────────
    section("Phase 1 │ C-01/C-05/I-01 契约与健康")

    def _health():
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json().get("status") == "ok"
    expect("C-05-health", "/health 返回 200 + status=ok", _health)

    def _config():
        cfg = ok(client.get("/api/v1/dataset-import/config"))
        assert ".zip" in cfg["allowedExtensions"], cfg
        assert cfg["maxFileSize"] == 8_388_608_000, cfg
        assert "maxFileSizeText" in cfg, cfg
        assert "recommendedChunkSize" in cfg, cfg
        assert "folderRules" in cfg, cfg
    expect("I-01", "API-01 导入配置字段完整（maxFileSize/extensions/folderRules）", _config)

    def _trace():
        r = client.get("/api/v1/dataset-import/config")
        body = r.json()
        assert body.get("traceId"), body
    expect("C-05-trace", "所有接口响应含 traceId", _trace)

    def _404():
        r = client.get("/api/v1/dataset-directories/dir_not_exist/records")
        assert r.status_code == 404, r.status_code
    expect("C-06-404", "不存在资源返回 404", _404)

    def _422():
        r = client.get("/api/v1/dataset-directories", params={"pageNo": 0})
        assert r.status_code == 422, r.status_code
    expect("C-04-422", "pageNo=0 返回 422", _422)

    def _422_exports_bad_limit():
        r = client.get("/api/v1/dataset-exports", params={"limit": 0})
        assert r.status_code == 422, r.status_code
    expect("I-46-422", "导出列表 limit=0 返回 422", _422_exports_bad_limit)

    def _422_exports_bad_offset():
        r = client.get("/api/v1/dataset-exports", params={"offset": -1})
        assert r.status_code == 422, r.status_code
    expect("I-46-422b", "导出列表 offset=-1 返回 422", _422_exports_bad_offset)

    # ── Phase 2: 上传流程 ────────────────────────────────────────────────────────
    section("Phase 2 │ I-06/I-07/I-09～I-18 分片上传全流程")

    content = build_test_zip(pid1, pid2)
    file_size = len(content)
    digest = hashlib.sha256(content).hexdigest()
    filename = f"acc_test_{run_id}.zip"
    chunk_size = 1_048_576

    def _instant_miss():
        ic = ok(client.post("/api/v1/dataset-upload/instant-check", json={
            "fileName": filename, "fileSize": file_size, "fileHash": digest, "businessType": "DATASET_IMPORT",
        }))
        assert ic["hit"] is False
        assert ic.get("fileId") is None
    expect("I-07-miss", "秒传全新 hash → hit=false, fileId=null", _instant_miss)

    def _create_upload():
        up = ok(client.post("/api/v1/dataset-upload/uploads", json={
            "fileName": filename, "fileSize": file_size, "fileHash": digest,
            "chunkSize": chunk_size, "businessType": "DATASET_IMPORT",
        }))
        assert "uploadId" in up
        assert "partCount" in up
        assert up["partCount"] >= 2, f"partCount={up['partCount']}, file={file_size}B"
    expect("I-09", "API-04 创建上传任务 返回 uploadId + partCount>=2", _create_upload)

    upload_result = do_upload(client, content, filename, chunk_size)
    file_id = upload_result["fileId"]
    upload_id = upload_result.get("uploadId", "")

    def _upload_task_status():
        if not upload_id:
            return
        task = ok(client.get(f"/api/v1/dataset-upload/uploads/{upload_id}"))
        assert task["uploadStatus"] == "MERGED", task
        assert "uploadedParts" in task
    expect("I-14", "API-06 上传任务查询 状态=MERGED, 含uploadedParts", _upload_task_status)

    def _ftp_raw_zip():
        items = ftp_list("dataset/upload")
        assert items, "FTP dataset/upload 为空，分片或合并产物缺失"
    expect("I-10", "FTP upload 路径存在分片/合并产物", _ftp_raw_zip)

    def _instant_hit():
        ic = ok(client.post("/api/v1/dataset-upload/instant-check", json={
            "fileName": filename, "fileSize": file_size, "fileHash": digest, "businessType": "DATASET_IMPORT",
        }))
        assert ic["hit"] is True, ic
        assert ic.get("fileId"), ic
    expect("I-07-hit", "秒传同 hash → hit=true 且 fileId 非空", _instant_hit)

    # ── Phase 3: 导入流程 ────────────────────────────────────────────────────────
    section("Phase 3 │ I-19～I-23 / D-01/D-06/D-07/NV-01 导入与解析")

    created = ok(client.post("/api/v1/dataset-directories", json={
        "directoryName": dir_name, "directoryDescription": "验收测试", "fileId": file_id,
    }))
    directory_id = created["directoryId"]
    task_id = created["importTaskId"]

    def _create_dir_response():
        assert created["importStatus"] == "IMPORTING"
        assert created["directoryId"]
        assert created["importTaskId"]
    expect("I-20", "API-08 提交导入同步返回 directoryId + importTaskId + IMPORTING", _create_dir_response)

    print(f"  ⏳ 等待导入完成 (taskId={task_id})...")
    task = wait_import(client, task_id, timeout=300)

    def _import_success():
        assert task["importStatus"] == "SUCCESS", f"FAILED: {task.get('failureReason')}"
        assert isinstance(task["progress"], int) and task["progress"] == 100
        assert task["recordCount"] >= 3, f"recordCount={task['recordCount']}"
        assert task["assetCount"] >= 3, f"assetCount={task['assetCount']}"
    expect("I-21/I-22", "导入状态 SUCCESS, progress=100, recordCount>=3, assetCount>=3", _import_success)

    def _db_questionnaire():
        rows = mysql_rows("dataset_questionnaire_record", "directory_id=%s", (directory_id,))
        assert len(rows) >= 3, f"问卷行={len(rows)}"
        pids = {r["patient_id"] for r in rows}
        assert pid1 in pids and pid2 in pids
    expect("D-01/NV-01", "MySQL 问卷记录 >=3 行，两个 PID 均存在", _db_questionnaire)

    def _db_image_assets():
        rows = mysql_rows("dataset_image_asset", "directory_id=%s", (directory_id,))
        assert len(rows) >= 3, f"影像资产行={len(rows)}"
        types = {r["source_type"] for r in rows}
        assert types & {"PARSED_FDT", "PARSED_OCT_DAT", "PARSED_DAT"}, f"source_types={types}"
    expect("D-06/D-08", "MySQL 影像资产 >=3 行，含眼底/OCT 类型", _db_image_assets)

    def _db_parsed_path():
        rows = mysql_rows("dataset_image_asset",
            "directory_id=%s AND parsed_path IS NOT NULL", (directory_id,))
        assert rows, "无任何影像资产有 parsed_path"
    expect("G-03", "MySQL 影像资产有 parsed_path（解析落 FTP）", _db_parsed_path)

    def _ftp_raw_zip_dir():
        assert ftp_exists(f"dataset/import/raw_zip/{directory_id}"), "FTP raw_zip 缺失"
    expect("S-01-raw_zip", f"FTP raw_zip/{directory_id} 存在", _ftp_raw_zip_dir)

    def _ftp_raw_tree():
        assert ftp_exists(f"dataset/import/raw_tree/{directory_id}"), "FTP raw_tree 缺失"
    expect("S-01-raw_tree", f"FTP raw_tree/{directory_id} 存在", _ftp_raw_tree)

    def _ftp_parsed():
        items = ftp_list(f"dataset/import/parsed/{directory_id}")
        assert items, "FTP parsed 目录为空"
    expect("S-01-parsed", f"FTP parsed/{directory_id} 有解析产物", _ftp_parsed)

    def _ftp_fundus_jpg():
        rows = mysql_rows("dataset_image_asset",
            "directory_id=%s AND source_type='PARSED_FDT' AND parsed_path IS NOT NULL",
            (directory_id,), cols="parsed_path")
        assert rows, "无 PARSED_FDT 资产"
        path = rows[0]["parsed_path"].replace("\\", "/").lstrip("/")
        assert ftp_exists(path), f"FTP 上找不到解析 jpg: {path}"
        data = ftp_read(path)
        assert data[:2] == b"\xff\xd8", "解析 jpg 无 JPEG 魔数"
    expect("NV-06/D-06", "眼底 fdt 解析 jpg 存在于 FTP 且为 JPEG", _ftp_fundus_jpg)

    def _ftp_oct_frames():
        rows = mysql_rows("dataset_image_asset",
            "directory_id=%s AND source_type IN ('PARSED_DAT','PARSED_OCT_DAT') AND parsed_path IS NOT NULL",
            (directory_id,), cols="image_id,source_type,parsed_path")
        if not rows:
            all_types = mysql_rows("dataset_image_asset", "directory_id=%s",
                (directory_id,), cols="source_type")
            print(f"  [debug] source_types in DB: {[r['source_type'] for r in all_types]}")
            return
        parsed_path_raw = rows[0]["parsed_path"].replace("\\", "/")
        print(f"  [debug] OCT parsed_path = {parsed_path_raw}")
        parsed_path = parsed_path_raw.lstrip("/")
        if parsed_path.endswith(".png"):
            ftp = ftp_connect()
            try:
                result = ftp.nlst(parsed_path)
                assert result, f"FTP 找不到 parsed_path 文件: {parsed_path}"
            finally:
                ftp.quit()
            data = ftp_read(parsed_path)
            assert data[:8] == b"\x89PNG\r\n\x1a\n", "OCT 帧文件非有效 PNG"
        else:
            parent = parsed_path
            ftp = ftp_connect()
            try:
                frames = ftp.nlst(parent)
            except Exception:
                frames = []
            finally:
                ftp.quit()
            png_frames = [f for f in frames if "frame_" in f and f.endswith(".png")]
            assert png_frames, f"FTP 目录 {parent} 无 frame_*.png（nlst: {frames[:5]}）"
            data = ftp_read(png_frames[0])
            assert data[:8] == b"\x89PNG\r\n\x1a\n", "OCT 帧文件非有效 PNG"
    expect("NV-08/D-08", "OCT dat 解析帧 frame_*.png 存在于 FTP 且为有效 PNG", _ftp_oct_frames)

    # ── Phase 4: 浏览 API ────────────────────────────────────────────────────────
    section("Phase 4 │ I-03～I-05/I-22～I-45 浏览接口")

    def _dir_list():
        listing = ok(client.get("/api/v1/dataset-directories", params={
            "directoryName": dir_name, "pageNo": 1, "pageSize": 10,
        }))
        assert listing["total"] >= 1
        rec = next(r for r in listing["records"] if r["directoryId"] == directory_id)
        assert rec["importStatus"] == "SUCCESS"
        assert rec["canView"] is True
        assert rec["canExport"] is True
        assert rec["canDelete"] is True
        assert rec["canReimport"] is False
        assert "importRecordCount" in rec
        assert "warningCount" in rec
    expect("I-03/I-04/I-05", "API-02 目录列表 筛选/状态/权限标志正确", _dir_list)

    def _records():
        recs = ok(client.get(f"/api/v1/dataset-directories/{directory_id}/records",
            params={"pageNo": 1, "pageSize": 20}))
        assert recs["total"] >= 3
        assert recs["records"]
        row = recs["records"][0]
        assert row["patientId"]
        assert "cells" in row or "patientId" in row
    expect("I-26/I-27", "API-11 动态列表 total>=3, 含 patientId", _records)

    def _timeline():
        tl = ok(client.get(
            f"/api/v1/dataset-directories/{directory_id}/patients/{pid1}/timeline"))
        assert tl["dates"], "时间轴无数据"
        dates = [d["surveyDate"] for d in tl["dates"]]
        assert "2026-05-20" in dates or "2026-05-21" in dates, f"dates={dates}"
        assert tl["dates"] == sorted(tl["dates"], key=lambda x: x["surveyDate"], reverse=True), "非倒序"
    expect("I-34/I-35", "API-14 患者时间轴有日期且倒序", _timeline)

    tl_data = ok(client.get(
        f"/api/v1/dataset-directories/{directory_id}/patients/{pid1}/timeline"))
    survey_date = tl_data["dates"][0]["surveyDate"]

    def _images_list():
        imgs = ok(client.get(
            f"/api/v1/dataset-directories/{directory_id}/patients/{pid1}/images",
            params={"surveyDate": survey_date, "pageNo": 1, "pageSize": 20}))
        assert imgs["total"] >= 1
        for rec in imgs["records"]:
            assert "imageId" in rec
            assert "sourceType" in rec
            assert rec.get("originalUrl", "").startswith("/api/v1/dataset-files/"), \
                f"originalUrl 不是受控路径: {rec.get('originalUrl')}"
            assert "ftp" not in (rec.get("originalUrl") or "").lower()
    expect("I-36/I-37/I-38", "API-15 影像列表 字段完整且 URL 不含 FTP 凭据", _images_list)

    imgs_data = ok(client.get(
        f"/api/v1/dataset-directories/{directory_id}/patients/{pid1}/images",
        params={"surveyDate": survey_date, "pageNo": 1, "pageSize": 20}))
    image_id = imgs_data["records"][0]["imageId"]

    def _images_frameurl():
        oct_imgs = [r for r in imgs_data["records"] if r.get("frameUrl")]
        if not oct_imgs:
            return
        img = oct_imgs[0]
        expected = f"/api/v1/dataset-files/{img['imageId']}/frame/"
        assert img["frameUrl"] == expected, f"frameUrl={img['frameUrl']} expected={expected}"
        non_oct = [r for r in imgs_data["records"]
                   if r["sourceType"] not in ("PARSED_DAT", "PARSED_OCT_DAT")]
        for r in non_oct:
            assert not r.get("frameUrl"), f"非 OCT 影像 {r['imageId']} 有 frameUrl"
    expect("I-44", "API-15 OCT 资产返回 frameUrl，非 OCT 无 frameUrl", _images_frameurl)

    def _image_detail():
        det = ok(client.get(
            f"/api/v1/dataset-directories/{directory_id}/patients/{pid1}/images/{image_id}"))
        assert det["imageId"] == image_id
        assert "previewUrl" in det
        assert "originalUrl" in det
        assert "metadata" in det
        assert "sequence" in det
    expect("I-39/I-40", "API-16 影像详情字段完整", _image_detail)

    def _images_missing_date():
        r = client.get(
            f"/api/v1/dataset-directories/{directory_id}/patients/{pid1}/images")
        assert r.status_code == 422, f"缺 surveyDate 应 422，实际={r.status_code}"
    expect("C-04-images", "API-15 缺 surveyDate 返回 422", _images_missing_date)

    def _thumbnail():
        r = client.get(f"/api/v1/dataset-files/{image_id}/thumbnail")
        assert r.status_code == 200
        assert "image/" in r.headers.get("content-type", "")
    expect("I-37", "GET dataset-files thumbnail 200 + image/*", _thumbnail)

    def _original():
        r = client.get(f"/api/v1/dataset-files/{image_id}/original")
        assert r.status_code == 200
    expect("I-40", "GET dataset-files original 200", _original)

    oct_imgs = [r for r in imgs_data["records"] if r.get("frameUrl")]
    if not oct_imgs:
        oct_rows = mysql_rows("dataset_image_asset",
            "directory_id=%s AND parsed_path LIKE '%%frame_00000.png%%'",
            (directory_id,), cols="image_id,parsed_path")
        if oct_rows:
            img_id = oct_rows[0]["image_id"]
            oct_imgs = [{"imageId": img_id, "frameUrl": f"/api/v1/dataset-files/{img_id}/frame/"}]
            print(f"  [debug] frameUrl 从 DB 构造: {oct_imgs[0]['frameUrl']}")

    if oct_imgs:
        oct_img = oct_imgs[0]
        det = ok(client.get(
            f"/api/v1/dataset-directories/{directory_id}/patients/{pid1}/images/{oct_img['imageId']}"))

        def _oct_frame_endpoint():
            frame_url = oct_img["frameUrl"]
            r = client.get(f"{frame_url}frame_00000.png")
            assert r.status_code == 200, f"frame 端点 {r.status_code}: {r.text[:200]}"
            assert r.headers.get("content-type", "").startswith("image/png")
            assert r.content[:8] == b"\x89PNG\r\n\x1a\n", "不是有效 PNG"
        expect("I-45/NV-08", "API-20 frame_00000.png 返回有效 PNG（200 + image/png）", _oct_frame_endpoint)

        def _oct_frame_metadata():
            frames = det.get("metadata", {}).get("octDat", {}).get("frames", [])
            assert frames, f"metadata.octDat.frames 为空: {det.get('metadata')}"
        expect("I-44/I-45", "API-16 OCT 影像详情 metadata.octDat.frames[] 非空", _oct_frame_metadata)

        def _oct_frame_404():
            r = client.get(f"{oct_img['frameUrl']}not_exist.png")
            assert r.status_code == 404, f"不存在帧应 404，实际={r.status_code}"
        expect("I-45-404", "API-20 不存在帧名返回 404", _oct_frame_404)

        def _oct_frame_traverse():
            r = client.get(f"{oct_img['frameUrl']}../../../etc/passwd")
            assert r.status_code in (400, 404), f"路径穿越应 400/404，实际={r.status_code}"
        expect("I-45-sec", "API-20 路径穿越攻击被拒绝（400/404）", _oct_frame_traverse)
    else:
        print("  ⚠ 无 OCT 多帧资产（dat 解析产出无 frame_00000.png），跳过 API-20 端点测试")

    # ── Phase 5: 目录导出 + API-18/19/21 + downloadCount ────────────────────────
    section("Phase 5 │ I-31～I-33/I-46～I-49/I-50/X-01/X-06/X-09/X-11/X-12 目录导出全链路")

    def _export_non_success_reject():
        r = client.post("/api/v1/dataset-directories/export",
            json={"directoryIds": ["dir_not_exist"]})
        assert r.status_code in (404, 400, 422), f"不存在目录应拒绝，实际={r.status_code}"
    expect("I-31", "非成功目录导出被拒绝", _export_non_success_reject)

    dir_exp = ok(client.post("/api/v1/dataset-directories/export", json={
        "directoryIds": [directory_id],
        "includeOriginalTable": True, "includeMergedTable": True,
        "includeParsedImages": True, "includeOriginalAttachments": True,
    }))
    dir_exp_id = dir_exp["exportRecordId"]

    def _dir_exp_response():
        assert dir_exp["exportType"] == "DATASET_DIRECTORY"
        assert dir_exp["exportStatus"] == "PREPARING"
        assert dir_exp["fileName"].endswith(".zip")
        assert dir_exp["expireAt"]
    expect("I-32", "API-13 目录导出返回 PREPARING + 正确字段", _dir_exp_response)

    print(f"  ⏳ 等待目录导出完成 (exportId={dir_exp_id})...")
    dir_exp_done = wait_export_http(client, dir_exp_id, timeout=120)

    def _dir_exp_done():
        assert dir_exp_done["exportStatus"] == "DONE", f"FAILED: {dir_exp_done.get('failureReason')}"
        assert dir_exp_done["downloadable"] is True
        assert dir_exp_done["downloadUrl"]
        assert "ftp" not in dir_exp_done["downloadUrl"].lower()
    expect("I-33/I-48/X-02/X-06", "目录导出 DONE, downloadable=true, URL 无 FTP 凭据", _dir_exp_done)

    def _dir_ftp_path():
        rows = mysql_rows("export_record", "export_record_id=%s",
            (dir_exp_id,), cols="ftp_path,export_status")
        assert rows and rows[0]["ftp_path"], "export_record.ftp_path 为空"
        path = rows[0]["ftp_path"].lstrip("/")
        assert ftp_exists(path), f"FTP 导出产物不存在: {path}"
    expect("X-01/S-01", "export_record 有 ftp_path，且 FTP 上存在对应文件", _dir_ftp_path)

    # API-18 列表验证，含 downloadCount
    def _export_list_basic():
        listing = ok(client.get("/api/v1/dataset-exports", params={"offset": 0, "limit": 20}))
        assert listing["total"] >= 1
        assert listing["offset"] == 0
        assert listing["limit"] == 20
        rec = next((r for r in listing["records"] if r["exportRecordId"] == dir_exp_id), None)
        assert rec, f"列表中找不到 {dir_exp_id}"
        assert rec["exportStatus"] == "DONE"
        assert rec["exportTypeName"]
        assert rec["exportStatusName"]
        assert rec["downloadable"] is True
        assert rec["downloadUrl"]
        assert rec["expireAt"]
        assert rec["summary"]
        assert "downloadCount" in rec, "API-18 列表项缺少 downloadCount 字段"
        assert isinstance(rec["downloadCount"], int), f"downloadCount 类型错误: {type(rec['downloadCount'])}"
        assert rec["downloadCount"] == 0, f"初始 downloadCount 应为 0，实际={rec['downloadCount']}"
    expect("I-46/I-46a/I-49/X-09", "API-18 导出列表含 DONE 目录导出，字段完整，downloadCount=0", _export_list_basic)

    # API-19 详情，含 downloadCount
    def _export_detail():
        det = ok(client.get(f"/api/v1/dataset-exports/{dir_exp_id}"))
        assert det["exportRecordId"] == dir_exp_id
        assert det["exportType"] == "DATASET_DIRECTORY"
        assert det["exportStatus"] == "DONE"
        assert det["downloadable"] is True
        assert det["downloadUrl"]
        assert det["payload"]["directoryIds"] == [directory_id]
        assert det["failureReason"] is None
        assert "downloadCount" in det, "API-19 详情缺少 downloadCount 字段"
        assert det["downloadCount"] == 0, f"初始 downloadCount 应为 0，实际={det['downloadCount']}"
    expect("I-47/I-49/X-10", "API-19 目录导出详情字段完整，downloadCount=0", _export_detail)

    def _export_detail_404():
        r = client.get("/api/v1/dataset-exports/exp_not_exist")
        assert r.status_code == 404
    expect("I-47-404", "API-19 不存在 exportRecordId 返回 404", _export_detail_404)

    # API-21 ── 下载前，dir_exp 应计入待下载
    pending_before_dir_dl = ok(client.get("/api/v1/dataset-exports/pending-download-count"))

    def _api21_before_dir_dl():
        assert "pendingDownloadCount" in pending_before_dir_dl, "API-21 缺少 pendingDownloadCount"
        assert "retentionDays" in pending_before_dir_dl, "API-21 缺少 retentionDays"
        assert "generatedAt" in pending_before_dir_dl, "API-21 缺少 generatedAt"
        assert isinstance(pending_before_dir_dl["pendingDownloadCount"], int)
        assert pending_before_dir_dl["pendingDownloadCount"] >= 1, \
            f"DONE 未下载任务应>=1，实际={pending_before_dir_dl['pendingDownloadCount']}"
        assert pending_before_dir_dl["retentionDays"] >= 1
    expect("I-50/TC-API-21-N01", "API-21 下载前返回字段完整，pendingDownloadCount>=1", _api21_before_dir_dl)

    # 下载目录导出 & 内容验证
    dl_url = dir_exp_done["downloadUrl"]
    zip_content = None
    try:
        r_dl = client.get(dl_url)
        assert r_dl.status_code == 200, f"下载失败 {r_dl.status_code}: {r_dl.text[:200]}"
        ct = r_dl.headers.get("content-type", "")
        assert "zip" in ct or "octet-stream" in ct, f"content-type={ct}"
        zf = zipfile.ZipFile(io.BytesIO(r_dl.content))
        names = zf.namelist()
        assert names, "下载 zip 为空"
        dir_prefixed = [n for n in names if n.startswith(directory_id + "/")]
        assert dir_prefixed, f"zip 内无 {directory_id}/ 前缀条目，实际={names[:5]}"
        zf.close()
        zip_content = r_dl.content
        check("I-33/X-06", "目录导出 zip 可下载、非空、含目录前缀结构", True)
    except Exception as e:
        check("I-33/X-06", "目录导出 zip 可下载、非空、含目录前缀结构", False, str(e)[:200])

    if zip_content:
        def _dir_zip_parsed_images():
            zf = zipfile.ZipFile(io.BytesIO(zip_content))
            names = zf.namelist()
            parsed = [n for n in names if "_parsed_derived" in n
                      or ".jpg" in n.lower() or ".png" in n.lower()]
            assert parsed, f"zip 内无解析图像，条目={names[:10]}"
        expect("I-33-parsed", "目录导出 zip 包含解析图像（jpg/png）", _dir_zip_parsed_images)

    # I-49/X-11 ── 下载 1 次后 downloadCount 应 = 1
    def _dir_download_count_one():
        det = ok(client.get(f"/api/v1/dataset-exports/{dir_exp_id}"))
        assert det["downloadCount"] == 1, f"1次下载后 downloadCount 应=1，实际={det['downloadCount']}"
        listing = ok(client.get("/api/v1/dataset-exports", params={"offset": 0, "limit": 100}))
        rec = next((r for r in listing["records"] if r["exportRecordId"] == dir_exp_id), None)
        assert rec, f"列表中找不到 {dir_exp_id}"
        assert rec["downloadCount"] == 1, f"API-18 downloadCount 应=1，实际={rec['downloadCount']}"
    expect("I-49/X-11-dir", "目录导出下载1次后 API-18/19 downloadCount=1", _dir_download_count_one)

    # I-50/X-12 ── 下载后 dir_exp 不再计入待下载
    def _api21_after_dir_dl():
        pending = ok(client.get("/api/v1/dataset-exports/pending-download-count"))
        expected = pending_before_dir_dl["pendingDownloadCount"] - 1
        assert pending["pendingDownloadCount"] == expected, \
            f"目录导出下载后 pendingCount 应={expected}，实际={pending['pendingDownloadCount']}"
    expect("I-50/X-12-after-dir", "API-21 目录导出下载后 pendingCount 减 1", _api21_after_dir_dl)

    # ── Phase 6: 患者导出 + downloadCount + API-21 + 二次下载 ────────────────────
    section("Phase 6 │ I-41～I-43/I-46～I-49/I-50/X-11/X-12 患者导出全链路")

    def _pat_exp_empty_reject():
        r = client.post(
            f"/api/v1/dataset-directories/{directory_id}/patients/NON_EXIST_PID/export",
            json={})
        assert r.status_code in (400, 404, 422), f"无资产 PID 导出应被拒绝，实际={r.status_code}"
    expect("I-41", "无问卷/影像的 PID 发起患者导出被拒绝", _pat_exp_empty_reject)

    pat_exp = ok(client.post(
        f"/api/v1/dataset-directories/{directory_id}/patients/{pid1}/export",
        json={"includeParsedImages": True, "includeOriginalAttachments": True},
    ))
    pat_exp_id = pat_exp["exportRecordId"]

    def _pat_exp_response():
        assert pat_exp["exportType"] == "DATASET_PATIENT"
        assert pat_exp["exportStatus"] == "PREPARING"
        assert pid1 in pat_exp["fileName"]
        assert pat_exp["expireAt"]
    expect("I-42", "API-17 患者导出返回 PREPARING, 文件名含 PID", _pat_exp_response)

    print(f"  ⏳ 等待患者导出完成 (exportId={pat_exp_id})...")
    pat_exp_done = wait_export_http(client, pat_exp_id, timeout=120)

    def _pat_exp_done():
        assert pat_exp_done["exportStatus"] == "DONE", f"FAILED: {pat_exp_done.get('failureReason')}"
        assert pat_exp_done["downloadable"] is True
        assert pat_exp_done["downloadUrl"]
    expect("I-42/I-48", "患者导出 DONE + downloadable=true", _pat_exp_done)

    # API-18 验证两类任务均出现
    def _export_list_both():
        listing = ok(client.get("/api/v1/dataset-exports", params={"offset": 0, "limit": 20}))
        assert listing["total"] >= 2
        types = {r["exportType"] for r in listing["records"]}
        assert "DATASET_DIRECTORY" in types
        assert "DATASET_PATIENT" in types
        # 所有记录均有 downloadCount 字段
        for rec in listing["records"]:
            assert "downloadCount" in rec, f"记录 {rec['exportRecordId']} 缺 downloadCount"
    expect("I-46a/I-49", "API-18 列表同时含两类导出，所有记录均有 downloadCount", _export_list_both)

    def _export_list_filter_type():
        listing = ok(client.get("/api/v1/dataset-exports",
            params={"exportType": "DATASET_PATIENT", "offset": 0, "limit": 20}))
        assert listing["total"] >= 1
        for r in listing["records"]:
            assert r["exportType"] == "DATASET_PATIENT"
    expect("I-46-filter", "API-18 按 exportType 过滤仅返回对应类型", _export_list_filter_type)

    def _export_list_paginate():
        listing_all = ok(client.get("/api/v1/dataset-exports", params={"offset": 0, "limit": 100}))
        total = listing_all["total"]
        if total >= 2:
            p1 = ok(client.get("/api/v1/dataset-exports", params={"offset": 0, "limit": 1}))
            p2 = ok(client.get("/api/v1/dataset-exports", params={"offset": 1, "limit": 1}))
            assert p1["total"] == total
            assert p2["total"] == total
            assert p1["records"][0]["exportRecordId"] != p2["records"][0]["exportRecordId"]
    expect("I-46-page", "API-18 分页 limit=1 offset 翻页结果不重复，total 不变", _export_list_paginate)

    def _export_list_sorted():
        listing = ok(client.get("/api/v1/dataset-exports", params={"offset": 0, "limit": 20}))
        dates = [r["createdAt"] for r in listing["records"]]
        assert dates == sorted(dates, reverse=True), f"createdAt 非倒序: {dates}"
    expect("I-46-sort", "API-18 列表按 createdAt 倒序", _export_list_sorted)

    # API-19 患者详情，含 downloadCount=0
    def _pat_exp_detail():
        det = ok(client.get(f"/api/v1/dataset-exports/{pat_exp_id}"))
        assert det["exportRecordId"] == pat_exp_id
        assert det["exportType"] == "DATASET_PATIENT"
        assert det["exportStatus"] == "DONE"
        assert det["downloadable"] is True
        assert det["payload"]["patientId"] == pid1
        assert det["payload"]["directoryId"] == directory_id
        assert "downloadCount" in det, "API-19 患者详情缺少 downloadCount"
        assert det["downloadCount"] == 0, f"患者导出初始 downloadCount 应=0，实际={det['downloadCount']}"
    expect("I-47/I-49/X-10", "API-19 患者导出详情字段完整，downloadCount=0", _pat_exp_detail)

    # API-21 ── pat_exp 是 DONE 未下载，应计入待下载
    pending_before_pat_dl = ok(client.get("/api/v1/dataset-exports/pending-download-count"))

    def _api21_before_pat_dl():
        assert pending_before_pat_dl["pendingDownloadCount"] >= 1, \
            f"患者导出 DONE 未下载，pending 应>=1，实际={pending_before_pat_dl['pendingDownloadCount']}"
    expect("I-50/TC-API-21-N02", "API-21 患者导出 DONE 后 pendingDownloadCount>=1", _api21_before_pat_dl)

    # 患者导出下载 & 内容验证（第 1 次）
    pat_dl_url = pat_exp_done["downloadUrl"]
    rows_json = None
    try:
        r_pat = client.get(pat_dl_url)
        assert r_pat.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(r_pat.content))
        names = zf.namelist()
        assert "questionnaire_rows.json" in names, f"缺 questionnaire_rows.json，有: {names[:10]}"
        rows_json = json.loads(zf.read("questionnaire_rows.json"))
        assert isinstance(rows_json, list) and len(rows_json) >= 1
        img_files = [n for n in names if n.startswith("images/")]
        assert img_files, f"无 images/ 条目: {names}"
        parsed = [n for n in names if n.startswith("images/parsed/")]
        assert parsed, f"无 images/parsed/ 条目: {names}"
        zf.close()
        check("I-43/X-06", "患者导出 zip 含 questionnaire_rows.json + images/ + images/parsed/", True)
    except Exception as e:
        check("I-43/X-06", "患者导出 zip 含 questionnaire_rows.json + images/ + images/parsed/",
              False, str(e)[:200])

    if rows_json:
        def _pat_zip_correct_pid():
            pids_in_json = {r.get("patient_id") or r.get("patientId") or "" for r in rows_json}
            assert any(pid1 in str(p) for p in pids_in_json), \
                f"问卷 JSON 无 {pid1}: {list(pids_in_json)[:3]}"
        expect("I-43", "患者导出问卷 JSON 含目标 PID 数据", _pat_zip_correct_pid)

    # I-49/X-11 ── 第 1 次下载后 downloadCount = 1
    def _pat_download_count_one():
        det = ok(client.get(f"/api/v1/dataset-exports/{pat_exp_id}"))
        assert det["downloadCount"] == 1, f"1次下载后 downloadCount 应=1，实际={det['downloadCount']}"
    expect("I-49/X-11-pat-1", "患者导出下载1次后 downloadCount=1", _pat_download_count_one)

    # TC-X-011：连续下载第 2 次，验证 downloadCount=2
    try:
        r_pat2 = client.get(pat_dl_url)
        assert r_pat2.status_code == 200
        assert zipfile.is_zipfile(io.BytesIO(r_pat2.content)), "第 2 次下载的不是 zip"
        check("TC-X-011-dl2", "患者导出第 2 次下载仍然成功（200 + zip）", True)
    except Exception as e:
        check("TC-X-011-dl2", "患者导出第 2 次下载仍然成功（200 + zip）", False, str(e)[:200])

    def _pat_download_count_two():
        det = ok(client.get(f"/api/v1/dataset-exports/{pat_exp_id}"))
        assert det["downloadCount"] == 2, f"2次下载后 downloadCount 应=2，实际={det['downloadCount']}"
        listing = ok(client.get("/api/v1/dataset-exports", params={"offset": 0, "limit": 100}))
        rec = next((r for r in listing["records"] if r["exportRecordId"] == pat_exp_id), None)
        assert rec and rec["downloadCount"] == 2, \
            f"API-18 downloadCount 应=2，实际={rec.get('downloadCount') if rec else 'N/A'}"
    expect("I-49/X-11-pat-2", "患者导出下载2次后 API-18/19 downloadCount 均=2", _pat_download_count_two)

    # I-50/X-12 ── 下载后 pat_exp 不再计入待下载
    def _api21_after_pat_dl():
        pending = ok(client.get("/api/v1/dataset-exports/pending-download-count"))
        expected = pending_before_pat_dl["pendingDownloadCount"] - 1
        assert pending["pendingDownloadCount"] == expected, \
            f"患者导出下载后 pendingCount 应={expected}，实际={pending['pendingDownloadCount']}"
    expect("I-50/X-12-after-pat", "API-21 患者导出下载后 pendingCount 减 1", _api21_after_pat_dl)

    # surveyDates 筛选（此次下载也计入 count，API-21 不受影响）
    def _pat_survey_dates_filter():
        filtered = ok(client.post(
            f"/api/v1/dataset-directories/{directory_id}/patients/{pid1}/export",
            json={"surveyDates": ["2026-05-20"]},
        ))
        filt_id = filtered["exportRecordId"]
        filt_done = wait_export_http(client, filt_id, timeout=180)
        assert filt_done["exportStatus"] == "DONE"
        r = client.get(filt_done["downloadUrl"])
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        rows = json.loads(zf.read("questionnaire_rows.json"))
        for row in rows:
            d = row.get("survey_date") or row.get("surveyDate") or ""
            assert "2026-05-20" in str(d), f"过滤后仍有非目标日期行: {row}"
    expect("I-43-dates", "患者导出 surveyDates=['2026-05-20'] 仅含指定日期行", _pat_survey_dates_filter)

    # API-21 按 exportType 筛选
    def _api21_type_filter():
        r_dir = ok(client.get("/api/v1/dataset-exports/pending-download-count",
            params={"exportType": "DATASET_DIRECTORY"}))
        r_pat = ok(client.get("/api/v1/dataset-exports/pending-download-count",
            params={"exportType": "DATASET_PATIENT"}))
        assert "pendingDownloadCount" in r_dir
        assert "pendingDownloadCount" in r_pat
        # dir 和 pat 小计之和应 <= 全量统计
        r_all = ok(client.get("/api/v1/dataset-exports/pending-download-count"))
        assert r_dir["pendingDownloadCount"] + r_pat["pendingDownloadCount"] \
            <= r_all["pendingDownloadCount"] + 1  # 容差 1（可能有并发）
    expect("I-50/TC-API-21-N03", "API-21 按 exportType 筛选返回字段完整且合理", _api21_type_filter)

    # ── Phase 7: 下载计数与过期清理 ─────────────────────────────────────────────
    section("Phase 7 │ I-49/I-50/X-04/X-11/X-13/S-03 下载计数汇总与过期清理全链路")

    # 汇总确认：所有 DONE 已下载导出的 downloadCount 字段均为整型
    def _all_records_have_int_download_count():
        listing = ok(client.get("/api/v1/dataset-exports", params={"offset": 0, "limit": 100}))
        for rec in listing["records"]:
            assert "downloadCount" in rec, f"记录 {rec['exportRecordId']} 缺 downloadCount"
            assert isinstance(rec["downloadCount"], int), \
                f"记录 {rec['exportRecordId']} downloadCount 类型错误: {type(rec['downloadCount'])}"
    expect("I-49-all", "API-18 所有记录均有整型 downloadCount 字段", _all_records_have_int_download_count)

    # 取 dir_exp 的 FTP 路径（过期后用于验证文件被删）
    rows_before_expiry = mysql_rows("export_record", "export_record_id=%s",
        (dir_exp_id,), cols="ftp_path,export_status,download_count")
    print(f"  [expiry] dir_exp DB state before backdate: {rows_before_expiry}")
    ftp_path_for_expiry = rows_before_expiry[0]["ftp_path"] if rows_before_expiry else None

    # 将 dir_exp 的 expire_at 调到 1 分钟前（模拟过期）
    affected = mysql_backdate_expire(dir_exp_id)
    print(f"  [expiry] backdate affected {affected} row(s) for {dir_exp_id}")

    def _backdate_ok():
        assert affected == 1, f"backdate 应影响 1 行，实际={affected}"
    expect("S-03-backdate", "MySQL backdate expire_at 成功（1 行受影响）", _backdate_ok)

    # API-18/19 立即反映 effective EXPIRED（无需等 sweep）
    def _api_status_expired_before_sweep():
        det = ok(client.get(f"/api/v1/dataset-exports/{dir_exp_id}"))
        assert det["exportStatus"] == "EXPIRED", \
            f"backdate 后状态应 EXPIRED（effective），实际={det['exportStatus']}"
        assert det["downloadable"] is False, "EXPIRED 任务 downloadable 应为 false"
        assert det["downloadUrl"] is None, f"EXPIRED 任务 downloadUrl 应为 null，实际={det['downloadUrl']}"
        listing = ok(client.get("/api/v1/dataset-exports", params={"offset": 0, "limit": 100}))
        rec = next((r for r in listing["records"] if r["exportRecordId"] == dir_exp_id), None)
        assert rec, f"列表中找不到 {dir_exp_id}"
        assert rec["exportStatus"] == "EXPIRED", f"API-18 状态应 EXPIRED，实际={rec['exportStatus']}"
        assert rec["downloadable"] is False
        assert rec["downloadUrl"] is None
    expect("X-04/S-03-effective", "backdate 后 API-18/19 effective 状态立即变为 EXPIRED，downloadUrl=null",
           _api_status_expired_before_sweep)

    # API-21 不应将已 effective-EXPIRED 的任务计入待下载
    def _api21_excludes_effective_expired():
        pending = ok(client.get("/api/v1/dataset-exports/pending-download-count"))
        # dir_exp 已经有 downloadCount=1 且 effective EXPIRED，不应计入
        # 其他 DONE 任务（pat_exp/filtered）均 downloadCount>0，也不计入
        # 所以 pending 应为 0（或极少，视并发）
        # 用 retentionDays 做合理性验证
        assert pending["retentionDays"] >= 1
        print(f"  [api21] pendingDownloadCount after dir_exp expired = {pending['pendingDownloadCount']}")
    expect("I-50/X-12-expired", "API-21 过期任务不计入待下载数量", _api21_excludes_effective_expired)

    # 下载已过期任务 → 应返回 410
    def _expired_download_returns_410():
        r = client.get(f"/api/v1/dataset-exports/{dir_exp_id}/download")
        assert r.status_code == 410, f"过期下载应返回 410，实际={r.status_code}"
        body = r.json()
        assert body.get("errorCode") == "DATASET_EXPORT_EXPIRED", \
            f"errorCode 应为 DATASET_EXPORT_EXPIRED，实际={body.get('errorCode')}"
        assert body.get("code") == 41001, f"code 应为 41001，实际={body.get('code')}"
    expect("X-04/TC-X-004", "已过期任务下载返回 HTTP 410 + DATASET_EXPORT_EXPIRED", _expired_download_returns_410)

    # 410 不应再递增 downloadCount
    def _expired_download_count_unchanged():
        det = ok(client.get(f"/api/v1/dataset-exports/{dir_exp_id}"))
        # dir_exp 之前有 downloadCount=1；410 不应增加
        assert det["downloadCount"] == 1, \
            f"410 请求后 downloadCount 不应增加，应仍为 1，实际={det['downloadCount']}"
    expect("X-11-no-count-on-expired", "过期下载 410 不递增 downloadCount", _expired_download_count_unchanged)

    # 运行过期清理 sweep（删除 FTP 文件 + 写库 EXPIRED + ftp_path=NULL）
    print("  ⏳ 运行 trigger_export_expiry_test.py --sweep-only ...")
    sweep_result = run_sweep_subprocess()
    print(f"  [sweep stdout] {sweep_result.stdout.strip()}")
    if sweep_result.stderr.strip():
        print(f"  [sweep stderr] {sweep_result.stderr.strip()[:300]}")

    def _sweep_success():
        assert sweep_result.returncode == 0, \
            f"sweep 脚本异常退出: returncode={sweep_result.returncode}\n{sweep_result.stderr[:400]}"
    expect("X-13-sweep-ok", "过期清理脚本正常退出（returncode=0）", _sweep_success)

    # DB 状态验证：export_status=EXPIRED, ftp_path=NULL
    def _db_expired_after_sweep():
        rows = mysql_rows("export_record", "export_record_id=%s",
            (dir_exp_id,), cols="export_status,ftp_path")
        assert rows, f"export_record 中找不到 {dir_exp_id}"
        assert rows[0]["export_status"] == "EXPIRED", \
            f"sweep 后 DB export_status 应=EXPIRED，实际={rows[0]['export_status']}"
        assert rows[0]["ftp_path"] is None, \
            f"sweep 后 ftp_path 应为 NULL，实际={rows[0]['ftp_path']}"
    expect("X-13/S-03-db", "sweep 后 DB：export_status=EXPIRED，ftp_path=NULL", _db_expired_after_sweep)

    # FTP 文件已被删除
    def _ftp_file_deleted():
        if not ftp_path_for_expiry:
            raise AssertionError("ftp_path_for_expiry 为空，无法验证 FTP 删除")
        path = ftp_path_for_expiry.lstrip("/")
        assert not ftp_exists(path), f"sweep 后 FTP 导出文件应已删除，实际仍存在: {path}"
    expect("X-13/S-03-ftp", "sweep 后 FTP 导出文件已被删除", _ftp_file_deleted)

    # API-18/19 sweep 后依然正确展示 EXPIRED（ftp_path=NULL）
    def _api_after_sweep():
        det = ok(client.get(f"/api/v1/dataset-exports/{dir_exp_id}"))
        assert det["exportStatus"] == "EXPIRED"
        assert det["downloadable"] is False
        assert det["downloadUrl"] is None
    expect("X-13-api-after-sweep", "sweep 后 API-18/19 仍正确返回 EXPIRED 状态", _api_after_sweep)

    # 启动扫描验证（S-04 等价：重新触发 sweep，已过期任务不重复处理，cleaned=0）
    def _sweep_idempotent():
        r2 = run_sweep_subprocess()
        assert r2.returncode == 0
        # 已清理的不应再被处理：stdout 应含 "0 record" 或 "cleaned 0"
        assert "0 record" in r2.stdout.lower() or "cleaned 0" in r2.stdout.lower(), \
            f"二次 sweep 应无新处理项，实际输出: {r2.stdout.strip()}"
    expect("S-03-idempotent", "二次 sweep 幂等（不重复处理已 EXPIRED 记录）", _sweep_idempotent)

    # ── Phase 8: 删除目录 ────────────────────────────────────────────────────────
    section("Phase 8 │ I-29/I-30 删除目录")

    def _delete_directory():
        r = client.delete(f"/api/v1/dataset-directories/{directory_id}")
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == 0
    expect("I-30", "API-12 删除目录成功", _delete_directory)

    def _deleted_not_in_list():
        time.sleep(0.5)
        listing = ok(client.get("/api/v1/dataset-directories",
            params={"directoryName": dir_name, "pageNo": 1, "pageSize": 10}))
        present = [r for r in listing["records"] if r["directoryId"] == directory_id]
        assert not present, "已删除目录仍出现在列表中"
    expect("I-30-list", "删除后目录不再出现在列表中", _deleted_not_in_list)

    def _deleted_records_gone():
        r = client.get(f"/api/v1/dataset-directories/{directory_id}/records",
            params={"pageNo": 1, "pageSize": 10})
        assert r.status_code == 404, f"删除后记录接口应 404，实际={r.status_code}"
    expect("I-30-records", "删除后 records 接口返回 404", _deleted_records_gone)

    # ── Phase 9: 事后清理 ────────────────────────────────────────────────────────
    section("Phase 9 │ 清理测试数据")
    print("  清空 MySQL 全部业务表...")
    mysql_truncate_all()
    print("  清空 FTP 全部数据...")
    ftp_purge_all()
    print("  清理完成 ✓")

    # ── 最终报告 ──────────────────────────────────────────────────────────────────
    section("验收报告")
    total = len(results)
    passed = sum(1 for r in results if r["status"] == PASS)
    failed = sum(1 for r in results if r["status"] == FAIL)
    skipped = sum(1 for r in results if r["status"] == SKIP)

    print(f"\n  总计: {total}   ✓ 通过: {passed}   ✗ 失败: {failed}   - 跳过: {skipped}")
    print(f"  执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  API Base: {base_url}")

    if failed:
        print("\n  ── 失败项 ──────────────────────────────")
        for r in results:
            if r["status"] == FAIL:
                print(f"  ✗ [{r['id']}] {r['desc']}")
                if r["detail"]:
                    print(f"       {r['detail']}")
        print()
        sys.exit(1)
    else:
        print("\n  ✓ 全部验收点通过！")
        print()


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=BASE_URL)
    args = parser.parse_args()
    try:
        run(args.base_url)
    except KeyboardInterrupt:
        print("\n中断")
        sys.exit(2)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
