from __future__ import annotations

import argparse
import hashlib

import httpx


def unwrap(response: httpx.Response) -> dict:
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0:
        raise AssertionError(payload)
    return payload["data"]


def run(base_url: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        health = client.get("/health").json()
        assert health["status"] == "ok"

        config = unwrap(client.get("/api/v1/dataset-import/config"))
        assert config["allowedExtensions"] == [".zip"]

        listing = unwrap(client.get("/api/v1/dataset-directories"))
        assert listing["total"] >= 1

        content = b"remote smoke mock zip"
        digest = hashlib.sha256(content).hexdigest()
        upload = unwrap(
            client.post(
                "/api/v1/dataset-upload/uploads",
                json={
                    "fileName": "remote-smoke.zip",
                    "fileSize": len(content),
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
                headers={"X-Part-Hash": digest, "Content-Type": "application/octet-stream"},
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
        task = unwrap(client.get(f"/api/v1/dataset-import/tasks/{created['importTaskId']}"))
        assert task["importStatus"] == "SUCCESS"

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

