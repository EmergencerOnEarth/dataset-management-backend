#!/usr/bin/env python3
"""
MySQL 元数据 + FTP 大文件存储 全链路自测（供测试人员复现与环境验收）。

步骤：健康检查 → 导入配置 → 秒传检测 → 初始化上传 → **多分片** PUT（Content-Range）→
合并 complete → 创建目录并异步导入 → 轮询导入任务 → 目录列表 / 动态记录 / 时间轴 /
影像列表（若有）→ 可选目录导出。

前置条件：
  1. 已创建库 ``eye_research_dataset``（utf8mb4）
  2. 本机 FTP 已启动（见 ``scripts/run_local_ftp_server.py``），与 ``FTP_*`` 一致
  3. API 已用 ``DATABASE_URL`` + ``STORAGE_BACKEND=ftp`` 启动

用法::

  export PYTHONPATH=.
  python3 scripts/integration_mysql_ftp_e2e.py --base-url http://127.0.0.1:8092
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import time
import uuid
import zipfile

import httpx
from openpyxl import Workbook


def ok(resp: httpx.Response) -> dict:
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 0:
        raise AssertionError(f"业务失败: {body}")
    assert body.get("traceId"), body
    return body["data"]


def build_zip_over_1mib() -> bytes:
    """生成 >1MiB 的 zip，保证在 chunkSize=1MiB 时 partCount>=2。"""
    xbio = io.BytesIO()
    wb = Workbook()
    ws = wb.active
    ws.append(["患者ID", "调查日期", "姓名"])
    ws.append(["LGTA99001", "2026-05-07", "集成测试用户"])
    wb.save(xbio)
    xbio.seek(0)
    pad = os.urandom(1100000)  # 不可压缩占位，ZIP_STORED 保证压缩包体积 >1MiB
    buf = io.BytesIO()
    tag = uuid.uuid4().hex[:8]
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"问卷/integration_{tag}.xlsx", xbio.read())
        zf.writestr("眼底照相/LGTA99001/20260507/sample.fdt", b"fdt-placeholder-" + tag.encode())
        zi = zipfile.ZipInfo("padding/junk.bin")
        zi.compress_type = zipfile.ZIP_STORED
        zf.writestr(zi, pad)
    raw = buf.getvalue()
    if len(raw) <= 1024 * 1024:
        raise RuntimeError(f"zip 太小 {len(raw)}，请增大 padding")
    return raw


def run_flow(base_url: str) -> None:
    run_id = uuid.uuid4().hex[:10]
    dir_name = f"集成E2E-{run_id}"

    with httpx.Client(base_url=base_url, timeout=300.0) as client:
        h = client.get("/health").json()
        assert h.get("status") == "ok", h
        print("1) /health OK")

        cfg = ok(client.get("/api/v1/dataset-import/config"))
        assert ".zip" in cfg.get("allowedExtensions", []), cfg
        print("2) GET dataset-import/config OK")

        content = build_zip_over_1mib()
        n = len(content)
        digest = hashlib.sha256(content).hexdigest()
        chunk = 1024 * 1024

        ic = ok(
            client.post(
                "/api/v1/dataset-upload/instant-check",
                json={
                    "fileName": f"e2e_{run_id}.zip",
                    "fileSize": n,
                    "fileHash": digest,
                    "businessType": "DATASET_IMPORT",
                },
            )
        )
        assert ic["hit"] is False, ic
        print("3) POST instant-check (hit=false) OK")

        upload = ok(
            client.post(
                "/api/v1/dataset-upload/uploads",
                json={
                    "fileName": f"e2e_{run_id}.zip",
                    "fileSize": n,
                    "fileHash": digest,
                    "chunkSize": chunk,
                    "businessType": "DATASET_IMPORT",
                },
            )
        )
        uid = upload["uploadId"]
        pc = upload["partCount"]
        assert pc >= 2, f"期望至少 2 片，实际 partCount={pc} fileSize={n}"
        print(f"4) POST uploads OK uploadId={uid} partCount={pc}")

        p1 = content[:chunk]
        p2 = content[chunk:]
        h1 = hashlib.sha256(p1).hexdigest()
        h2 = hashlib.sha256(p2).hexdigest()

        ok(
            client.put(
                f"/api/v1/dataset-upload/uploads/{uid}/parts/1",
                content=p1,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Range": f"bytes 0-{chunk - 1}/{n}",
                    "X-Part-Hash": h1,
                },
            )
        )
        print("5) PUT part 1 OK")

        ok(
            client.put(
                f"/api/v1/dataset-upload/uploads/{uid}/parts/2",
                content=p2,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Range": f"bytes {chunk}-{n - 1}/{n}",
                    "X-Part-Hash": h2,
                },
            )
        )
        print("6) PUT part 2 OK")

        st_task = ok(client.get(f"/api/v1/dataset-upload/uploads/{uid}"))
        assert set(st_task["uploadedParts"]) == {1, 2}, st_task

        done = ok(
            client.post(
                f"/api/v1/dataset-upload/uploads/{uid}/complete",
                json={"fileHash": digest},
            )
        )
        file_id = done["fileId"]
        assert done["uploadStatus"] == "MERGED"
        print(f"7) POST complete OK fileId={file_id}")

        created = ok(
            client.post(
                "/api/v1/dataset-directories",
                json={
                    "directoryName": dir_name,
                    "directoryDescription": "MySQL+FTP 集成自测",
                    "fileId": file_id,
                    "originalFileName": f"e2e_{run_id}.zip",
                },
            )
        )
        directory_id = created["directoryId"]
        imp_id = created["importTaskId"]
        print(f"8) POST dataset-directories OK directoryId={directory_id} importTaskId={imp_id}")

        task: dict = {}
        for i in range(300):
            task = ok(client.get(f"/api/v1/dataset-import/tasks/{imp_id}"))
            if task["importStatus"] in ("SUCCESS", "FAILED"):
                break
            time.sleep(0.2)
        assert task.get("importStatus") == "SUCCESS", task
        print(f"9) 导入完成 SUCCESS recordCount={task.get('recordCount')} assetCount={task.get('assetCount')}")

        listing = ok(
            client.get(
                "/api/v1/dataset-directories",
                params={"directoryName": "集成E2E", "pageNo": 1, "pageSize": 20},
            )
        )
        ids = {r["directoryId"] for r in listing["records"]}
        assert directory_id in ids, listing
        print("10) GET dataset-directories 筛选 OK")

        records = ok(
            client.get(
                f"/api/v1/dataset-directories/{directory_id}/records",
                params={"pageNo": 1, "pageSize": 10},
            )
        )
        assert records["total"] >= 1
        row = records["records"][0]
        assert row["patientId"] == "LGTA99001"
        print("11) GET records 动态列表 OK")

        timeline = ok(
            client.get(
                f"/api/v1/dataset-directories/{directory_id}/patients/LGTA99001/timeline",
            )
        )
        assert timeline["dates"], timeline
        sdate = timeline["dates"][0]["surveyDate"]
        print(f"12) GET timeline OK dates={timeline['dates']}")

        imgs = ok(
            client.get(
                f"/api/v1/dataset-directories/{directory_id}/patients/LGTA99001/images",
                params={"surveyDate": sdate, "pageNo": 1, "pageSize": 10},
            )
        )
        assert imgs["total"] >= 1, imgs
        image_id = imgs["records"][0]["imageId"]
        print(f"13) GET images OK imageId={image_id}")

        thumb = client.get(f"/api/v1/dataset-files/{image_id}/thumbnail")
        assert thumb.status_code == 200
        assert thumb.headers.get("content-type", "").startswith("image/")
        print("14) GET dataset-files thumbnail OK")

        export_data = ok(
            client.post(
                "/api/v1/dataset-directories/export",
                json={"directoryIds": [directory_id]},
            )
        )
        exp_id = export_data["exportRecordId"]
        time.sleep(1.0)

        page = ok(
            client.get(
                "/api/v1/dataset-directories",
                params={"directoryName": "集成E2E", "pageNo": 1, "pageSize": 50},
            )
        )
        succ = next((r for r in page["records"] if r["directoryId"] == directory_id), None)
        assert succ is not None and succ["importStatus"] == "SUCCESS" and succ["canExport"] is True
        print(f"15) 目录导出任务已投递 exportRecordId={exp_id}")

    print("\nINTEGRATION_E2E_OK — 请将以下信息交给测试:")
    print(f"  DATABASE_URL={os.environ.get('DATABASE_URL', '(见启动环境)')}")
    print(f"  BASE_URL={base_url}")
    print(f"  directoryId={directory_id}")
    print(f"  importTaskId={imp_id}")
    print(f"  lastRunTag={run_id}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default=os.environ.get("E2E_BASE_URL", "http://127.0.0.1:8092"))
    args = p.parse_args()
    run_flow(args.base_url.rstrip("/"))


if __name__ == "__main__":
    main()
