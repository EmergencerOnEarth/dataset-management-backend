from __future__ import annotations

import argparse
import hashlib
import io
import time
import zipfile

import httpx
from openpyxl import Workbook


def unwrap(response: httpx.Response) -> dict:
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0:
        raise AssertionError(payload)
    return payload["data"]


def _minimal_zip_with_xlsx() -> bytes:
    xbio = io.BytesIO()
    wb = Workbook()
    ws = wb.active
    ws.append(["患者ID", "调查日期", "姓名"])
    ws.append(["LGTA00087", "2026-03-03", "冒烟"])
    wb.save(xbio)
    xbio.seek(0)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("问卷/smoke.xlsx", xbio.read())
    return buf.getvalue()


def run(base_url: str) -> None:
    with httpx.Client(base_url=base_url, timeout=120.0) as client:
        health = client.get("/health").json()
        assert health["status"] == "ok"

        config = unwrap(client.get("/api/v1/dataset-import/config"))
        assert config["allowedExtensions"] == [".zip"]

        listing = unwrap(client.get("/api/v1/dataset-directories"))
        assert listing["total"] >= 1

        content = _minimal_zip_with_xlsx()
        digest = hashlib.sha256(content).hexdigest()
        n = len(content)
        upload = unwrap(
            client.post(
                "/api/v1/dataset-upload/uploads",
                json={
                    "fileName": "remote-smoke.zip",
                    "fileSize": n,
                    "fileHash": digest,
                    "chunkSize": 1024 * 1024,
                    "businessType": "DATASET_IMPORT",
                },
            )
        )
        unwrap(
            client.put(
                f"/api/v1/dataset-upload/uploads/{upload['uploadId']}/parts/1",
                content=content,
                headers={
                    "X-Part-Hash": digest,
                    "Content-Type": "application/octet-stream",
                    "Content-Range": f"bytes 0-{n - 1}/{n}",
                },
            )
        )
        completed = unwrap(
            client.post(
                f"/api/v1/dataset-upload/uploads/{upload['uploadId']}/complete",
                json={"fileHash": digest},
            )
        )
        created = unwrap(
            client.post(
                "/api/v1/dataset-directories",
                json={
                    "directoryName": "远程冒烟目录",
                    "directoryDescription": "测试服务器接口冒烟",
                    "fileId": completed["fileId"],
                },
            )
        )
        tid = created["importTaskId"]
        st = None
        task: dict = {}
        for _ in range(200):
            task = unwrap(client.get(f"/api/v1/dataset-import/tasks/{tid}"))
            st = task["importStatus"]
            if st in ("SUCCESS", "FAILED"):
                break
            time.sleep(0.1)
        assert st == "SUCCESS", task

        records = unwrap(client.get("/api/v1/dataset-directories/dir_demo_001/records"))
        assert records["records"]

        timeline = unwrap(client.get("/api/v1/dataset-directories/dir_demo_001/patients/LGTA00087/timeline"))
        assert timeline["dates"]

        print("SMOKE_TEST_OK")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8091")
    args = parser.parse_args()
    run(args.base_url)
