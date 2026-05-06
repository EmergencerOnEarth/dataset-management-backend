from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient

from backend.app.main import app


client = TestClient(app)


def data(response):
    payload = response.json()
    assert payload["code"] == 0, payload
    assert payload["traceId"]
    return payload["data"]


def test_import_config_and_directory_list():
    config = data(client.get("/api/v1/dataset-import/config"))
    assert config["maxFileSize"] == 8_388_608_000
    assert config["allowedExtensions"] == [".zip"]

    listing = data(client.get("/api/v1/dataset-directories", params={"pageNo": 1, "pageSize": 5}))
    assert listing["total"] >= 1
    assert listing["records"][0]["canView"] is True


def test_upload_flow_and_import_task():
    content = b"mock zip bytes"
    file_hash = hashlib.sha256(content).hexdigest()
    check = data(
        client.post(
            "/api/v1/dataset-upload/instant-check",
            json={
                "fileName": "测试上传数据.zip",
                "fileSize": len(content),
                "fileHash": file_hash,
                "businessType": "DATASET_IMPORT",
            },
        )
    )
    assert check["hit"] is False

    upload = data(
        client.post(
            "/api/v1/dataset-upload/uploads",
            json={
                "fileName": "测试上传数据.zip",
                "fileSize": len(content),
                "fileHash": file_hash,
                "chunkSize": 1024 * 1024,
                "businessType": "DATASET_IMPORT",
            },
        )
    )
    assert upload["partCount"] == 1

    part = data(
        client.put(
            f"/api/v1/dataset-upload/uploads/{upload['uploadId']}/parts/1",
            content=content,
            headers={"X-Part-Hash": file_hash, "Content-Type": "application/octet-stream"},
        )
    )
    assert part["uploadedParts"] == [1]

    completed = data(
        client.post(
            f"/api/v1/dataset-upload/uploads/{upload['uploadId']}/complete",
            json={"fileHash": file_hash},
        )
    )
    assert completed["uploadStatus"] == "MERGED"

    created = data(
        client.post(
            "/api/v1/dataset-directories",
            json={
                "directoryName": "测试目录",
                "directoryDescription": "mock 接口测试",
                "fileId": completed["fileId"],
                "originalFileName": "测试上传数据.zip",
            },
        )
    )
    assert created["importStatus"] == "IMPORTING"

    task = data(client.get(f"/api/v1/dataset-import/tasks/{created['importTaskId']}"))
    assert task["importStatus"] == "SUCCESS"
    assert task["progress"] == 100


def test_records_patient_images_and_exports():
    records = data(client.get("/api/v1/dataset-directories/dir_demo_001/records"))
    assert records["columns"]
    assert records["records"][0]["patientId"] == "LGTA00087"

    timeline = data(client.get("/api/v1/dataset-directories/dir_demo_001/patients/LGTA00087/timeline"))
    assert timeline["dates"][0]["surveyDate"] == "2026-03-03"

    images = data(
        client.get(
            "/api/v1/dataset-directories/dir_demo_001/patients/LGTA00087/images",
            params={"surveyDate": "2026-03-03"},
        )
    )
    assert images["records"][0]["imageId"] == "img_001"

    detail = data(client.get("/api/v1/dataset-directories/dir_demo_001/patients/LGTA00087/images/img_001"))
    assert detail["metadata"]["width"] == 1024

    directory_export = data(
        client.post("/api/v1/dataset-directories/export", json={"directoryIds": ["dir_demo_001"]})
    )
    assert directory_export["exportType"] == "DATASET_DIRECTORY"

    patient_export = data(
        client.post("/api/v1/dataset-directories/dir_demo_001/patients/LGTA00087/export", json={})
    )
    assert patient_export["exportType"] == "DATASET_PATIENT"


def test_error_response_for_invalid_upload_type():
    response = client.post(
        "/api/v1/dataset-upload/instant-check",
        json={
            "fileName": "bad.xlsx",
            "fileSize": 100,
            "fileHash": "abc",
            "businessType": "DATASET_IMPORT",
        },
    )
    assert response.status_code == 400
    payload = response.json()
    assert payload["errorCode"] == "DATASET_IMPORT_FILE_TYPE_INVALID"

