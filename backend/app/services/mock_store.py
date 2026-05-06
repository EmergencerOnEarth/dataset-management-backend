from __future__ import annotations

import hashlib
import math
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import UploadFile

from backend.app.core.config import get_settings
from backend.app.core.errors import AppError, NotFoundError


class MockStore:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.uploads: dict[str, dict[str, Any]] = {}
        self.files: dict[str, dict[str, Any]] = {}
        self.directories: dict[str, dict[str, Any]] = {}
        self.import_tasks: dict[str, dict[str, Any]] = {}
        self.export_records: dict[str, dict[str, Any]] = {}
        self.records: dict[str, list[dict[str, Any]]] = {}
        self.images: dict[str, list[dict[str, Any]]] = {}
        self._seed()

    def _now(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    def _id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:12]}"

    def _seed(self) -> None:
        directory_id = "dir_demo_001"
        self.directories[directory_id] = {
            "directoryId": directory_id,
            "directoryName": "中航数据2026样例目录",
            "directoryDescription": "V1.2.7 导入结构模拟数据",
            "importStatus": "SUCCESS",
            "importRecordCount": 86,
            "warningCount": 2,
            "importedAt": "2026-05-06 10:30:00",
            "failureReason": None,
            "deleted": False,
        }
        columns = [
            {"columnKey": "name", "columnTitle": "姓名", "dataType": "STRING", "sourceType": "QUESTIONNAIRE"},
            {"columnKey": "patientId", "columnTitle": "ID", "dataType": "STRING", "sourceType": "QUESTIONNAIRE"},
            {"columnKey": "phone", "columnTitle": "常用电话", "dataType": "STRING", "sourceType": "QUESTIONNAIRE"},
            {"columnKey": "surveyDate", "columnTitle": "调查日期", "dataType": "DATE", "sourceType": "NORMALIZED"},
            {"columnKey": "macularAverageThickness", "columnTitle": "黄斑平均厚度", "dataType": "NUMBER", "sourceType": "OCT_JSON"},
            {"columnKey": "discRnflAvg", "columnTitle": "RNFL平均厚度", "dataType": "NUMBER", "sourceType": "OCT_JSON"},
        ]
        people = [
            ("rec_001", "郑萍", "LGTA00087", "13923779163", "2026-03-03", 271.2, 96.5),
            ("rec_002", "李明", "LGTA00101", "13800001111", "2026-03-06", 268.9, 93.1),
            ("rec_003", "王华", "LGTA00143", "13800002222", "2026-03-10", 275.4, 98.7),
        ]
        self.records[directory_id] = [
            {
                "recordId": record_id,
                "patientId": patient_id,
                "surveyDate": survey_date,
                "hasImages": True,
                "cells": {
                    "name": name,
                    "patientId": patient_id,
                    "phone": phone,
                    "surveyDate": survey_date,
                    "macularAverageThickness": thickness,
                    "discRnflAvg": rnfl,
                },
            }
            for record_id, name, patient_id, phone, survey_date, thickness, rnfl in people
        ]
        self.directories[directory_id]["columns"] = columns
        self.images[directory_id] = [
            self._image("img_001", "LGTA00087", "2026-03-03", "PARSED_FDT", "OD-Color-260303-094001.jpg"),
            self._image("img_002", "LGTA00101", "2026-03-06", "PARSED_DAT", "od-3dscan-macular-20260306.jpg"),
            self._image("img_003", "LGTA00143", "2026-03-10", "ORIGINAL_PNG", "os-3dscan-macular-Thickness.png"),
        ]

    def _image(self, image_id: str, patient_id: str, date: str, source_type: str, name: str) -> dict[str, Any]:
        return {
            "imageId": image_id,
            "patientId": patient_id,
            "surveyDate": date,
            "imageName": name,
            "sourceType": source_type,
            "thumbnailUrl": f"/api/v1/mock-files/{image_id}/thumbnail",
            "previewUrl": f"/api/v1/mock-files/{image_id}/preview",
            "originalUrl": f"/api/v1/mock-files/{image_id}/original",
            "createdAt": self._now(),
            "metadata": {
                "width": 1024,
                "height": 768,
                "eye": "OD" if name.lower().startswith("od") else "OS",
                "acquisitionDatetime": f"{date} 09:30:00",
            },
        }

    def import_config(self) -> dict[str, Any]:
        return {
            "maxFileSize": self.settings.dataset_max_file_size,
            "maxFileSizeText": "8000M以内",
            "allowedExtensions": [".zip"],
            "recommendedChunkSize": self.settings.dataset_chunk_size,
            "folderRules": {
                "questionnaire": {"required": True, "extensions": [".xlsx"], "patientIdRequired": True},
                "biometry": {"required": False, "parse": False, "blockingValidation": False},
                "fundus": {"required": False, "parse": "fdt_to_jpg_mock", "blockingValidation": False},
                "oct": {"required": False, "parse": "dat_json_mock", "blockingValidation": False},
            },
        }

    def _validate_zip(self, file_name: str, file_size: int) -> None:
        if not file_name.lower().endswith(".zip"):
            raise AppError("导入文件格式非zip文件，请检查导入文件。", "DATASET_IMPORT_FILE_TYPE_INVALID")
        if file_size > self.settings.dataset_max_file_size:
            raise AppError(
                "导入文件超过8000m，请检查后重新上传。",
                "DATASET_IMPORT_FILE_SIZE_EXCEEDED",
                details={"maxFileSize": self.settings.dataset_max_file_size},
            )

    def list_directories(self, page_no: int, page_size: int, **filters: Any) -> dict[str, Any]:
        rows = [d for d in self.directories.values() if not d.get("deleted")]
        name = filters.get("directory_name")
        status = filters.get("import_status")
        if name:
            rows = [d for d in rows if name in d["directoryName"]]
        if status:
            rows = [d for d in rows if d["importStatus"] == status]
        rows.sort(key=lambda x: x["importedAt"], reverse=True)
        total = len(rows)
        start = (page_no - 1) * page_size
        page = rows[start : start + page_size]
        records = []
        for row in page:
            item = {k: v for k, v in row.items() if k != "columns"}
            item.update(
                {
                    "canView": row["importStatus"] == "SUCCESS",
                    "canReimport": row["importStatus"] == "FAILED",
                    "canDelete": row["importStatus"] != "IMPORTING",
                    "canExport": row["importStatus"] == "SUCCESS",
                }
            )
            records.append(item)
        return {"records": records, "total": total, "pageNo": page_no, "pageSize": page_size}

    def instant_check(self, body: dict[str, Any]) -> dict[str, Any]:
        self._validate_zip(body["fileName"], body["fileSize"])
        for file_id, meta in self.files.items():
            if meta["fileHash"] == body["fileHash"] and meta["fileSize"] == body["fileSize"]:
                return {
                    "hit": True,
                    "fileId": file_id,
                    "uploadId": None,
                    "uploadedParts": [],
                    "recommendedChunkSize": self.settings.dataset_chunk_size,
                }
        for upload_id, upload in self.uploads.items():
            if upload["fileHash"] == body["fileHash"] and upload["uploadStatus"] in {"INIT", "UPLOADING"}:
                return {
                    "hit": False,
                    "fileId": None,
                    "uploadId": upload_id,
                    "uploadedParts": sorted(upload["parts"].keys()),
                    "recommendedChunkSize": upload["chunkSize"],
                }
        return {
            "hit": False,
            "fileId": None,
            "uploadId": None,
            "uploadedParts": [],
            "recommendedChunkSize": self.settings.dataset_chunk_size,
        }

    def create_upload(self, body: dict[str, Any]) -> dict[str, Any]:
        self._validate_zip(body["fileName"], body["fileSize"])
        upload_id = self._id("upl")
        chunk_size = min(max(int(body.get("chunkSize") or self.settings.dataset_chunk_size), 1024 * 1024), 64 * 1024 * 1024)
        part_count = math.ceil(body["fileSize"] / chunk_size)
        tmp_dir = self.settings.dataset_runtime_dir / "uploads" / upload_id / "parts"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        upload = {
            "uploadId": upload_id,
            "fileName": body["fileName"],
            "fileSize": body["fileSize"],
            "fileHash": body["fileHash"],
            "chunkSize": chunk_size,
            "partCount": part_count,
            "uploadStatus": "INIT",
            "failureReason": None,
            "parts": {},
            "tmpDir": str(tmp_dir),
            "createdAt": self._now(),
        }
        self.uploads[upload_id] = upload
        return {
            "uploadId": upload_id,
            "uploadStatus": "INIT",
            "chunkSize": chunk_size,
            "partCount": part_count,
            "expireAt": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 86400)),
        }

    async def put_part(
        self, upload_id: str, part_number: int, body: bytes, part_hash: str | None
    ) -> dict[str, Any]:
        upload = self.uploads.get(upload_id)
        if not upload:
            raise NotFoundError("上传任务不存在。")
        if part_number < 1 or part_number > upload["partCount"]:
            raise AppError("上传分片序号不正确。", "DATASET_UPLOAD_PART_INVALID")
        calculated = hashlib.sha256(body).hexdigest()
        if part_hash and part_hash != calculated:
            raise AppError("上传分片校验失败。", "DATASET_UPLOAD_PART_HASH_MISMATCH")
        part_path = Path(upload["tmpDir"]) / f"{part_number:08d}.part"
        part_path.write_bytes(body)
        upload["parts"][part_number] = {
            "partNumber": part_number,
            "partSize": len(body),
            "partHash": calculated,
            "etag": calculated[:16],
            "ftpPartPath": str(part_path),
        }
        upload["uploadStatus"] = "UPLOADING"
        return {
            "uploadId": upload_id,
            "partNumber": part_number,
            "etag": calculated[:16],
            "uploadedParts": sorted(upload["parts"].keys()),
            "uploadStatus": upload["uploadStatus"],
        }

    def get_upload(self, upload_id: str) -> dict[str, Any]:
        upload = self.uploads.get(upload_id)
        if not upload:
            raise NotFoundError("上传任务不存在。")
        return {
            "fileName": upload["fileName"],
            "fileSize": upload["fileSize"],
            "chunkSize": upload["chunkSize"],
            "partCount": upload["partCount"],
            "uploadedParts": sorted(upload["parts"].keys()),
            "uploadStatus": upload["uploadStatus"],
            "failureReason": upload["failureReason"],
        }

    def complete_upload(self, upload_id: str, file_hash: str) -> dict[str, Any]:
        upload = self.uploads.get(upload_id)
        if not upload:
            raise NotFoundError("上传任务不存在。")
        missing = [n for n in range(1, upload["partCount"] + 1) if n not in upload["parts"]]
        if missing:
            raise AppError("上传分片不完整，请重新上传缺失分片。", "DATASET_UPLOAD_PART_MISSING", details={"missingParts": missing})
        digest = hashlib.sha256()
        merged_dir = self.settings.dataset_runtime_dir / "uploads" / upload_id
        merged_path = merged_dir / upload["fileName"]
        with merged_path.open("wb") as output:
            for part_no in range(1, upload["partCount"] + 1):
                data = Path(upload["parts"][part_no]["ftpPartPath"]).read_bytes()
                digest.update(data)
                output.write(data)
        calculated = digest.hexdigest()
        if calculated != file_hash or calculated != upload["fileHash"]:
            upload["uploadStatus"] = "FAILED"
            upload["failureReason"] = "文件校验失败"
            raise AppError("文件校验失败，请重新上传。", "DATASET_UPLOAD_HASH_MISMATCH")
        file_id = self._id("file")
        upload["uploadStatus"] = "MERGED"
        self.files[file_id] = {
            "fileId": file_id,
            "fileName": upload["fileName"],
            "fileSize": upload["fileSize"],
            "fileHash": calculated,
            "storageKey": str(merged_path),
        }
        return {
            "fileId": file_id,
            "fileName": upload["fileName"],
            "fileSize": upload["fileSize"],
            "fileHash": calculated,
            "storageKey": str(merged_path),
            "uploadStatus": "MERGED",
        }

    def create_directory(self, body: dict[str, Any]) -> dict[str, Any]:
        if body["fileId"] not in self.files:
            raise NotFoundError("上传文件不存在或尚未合并。")
        directory_id = self._id("dir")
        import_task_id = self._id("imp")
        self.directories[directory_id] = {
            "directoryId": directory_id,
            "directoryName": body["directoryName"],
            "directoryDescription": body.get("directoryDescription") or "",
            "importStatus": "IMPORTING",
            "importRecordCount": 0,
            "warningCount": 0,
            "importedAt": self._now(),
            "failureReason": None,
            "deleted": False,
            "columns": self.directories["dir_demo_001"]["columns"],
        }
        self.import_tasks[import_task_id] = {
            "importTaskId": import_task_id,
            "directoryId": directory_id,
            "importStatus": "IMPORTING",
            "progress": 20,
            "stage": "MOCK_IMPORTING",
            "recordCount": 0,
            "assetCount": 0,
            "warningCount": 0,
            "failureReason": None,
            "queried": False,
        }
        self.records[directory_id] = list(self.records["dir_demo_001"])
        self.images[directory_id] = list(self.images["dir_demo_001"])
        return {
            "directoryId": directory_id,
            "importTaskId": import_task_id,
            "importStatus": "IMPORTING",
            "submittedAt": self._now(),
        }

    def get_import_task(self, import_task_id: str) -> dict[str, Any]:
        task = self.import_tasks.get(import_task_id)
        if not task:
            raise NotFoundError("导入任务不存在。")
        if not task["queried"]:
            task.update(
                {
                    "importStatus": "SUCCESS",
                    "progress": 100,
                    "stage": "MOCK_COMPLETED",
                    "recordCount": 86,
                    "assetCount": 1836,
                    "warningCount": 2,
                    "queried": True,
                }
            )
            directory = self.directories[task["directoryId"]]
            directory["importStatus"] = "SUCCESS"
            directory["importRecordCount"] = 86
            directory["warningCount"] = 2
        return {k: v for k, v in task.items() if k != "queried"}

    def reimport(self, directory_id: str, body: dict[str, Any]) -> dict[str, Any]:
        directory = self.directories.get(directory_id)
        if not directory or directory.get("deleted"):
            raise NotFoundError("数据目录不存在。")
        if body["fileId"] not in self.files:
            raise NotFoundError("上传文件不存在或尚未合并。")
        import_task_id = self._id("imp")
        directory["directoryName"] = body["directoryName"]
        directory["directoryDescription"] = body.get("directoryDescription") or directory["directoryDescription"]
        directory["importStatus"] = "IMPORTING"
        self.import_tasks[import_task_id] = {
            "importTaskId": import_task_id,
            "directoryId": directory_id,
            "importStatus": "IMPORTING",
            "progress": 20,
            "stage": "MOCK_REIMPORTING",
            "recordCount": 0,
            "assetCount": 0,
            "warningCount": 0,
            "failureReason": None,
            "queried": False,
        }
        return {
            "directoryId": directory_id,
            "importTaskId": import_task_id,
            "importAttemptNo": 2,
            "importStatus": "IMPORTING",
        }

    def directory_records(self, directory_id: str, page_no: int, page_size: int, patient_id: str | None) -> dict[str, Any]:
        directory = self.directories.get(directory_id)
        if not directory or directory.get("deleted"):
            raise NotFoundError("数据目录不存在。")
        rows = self.records.get(directory_id, [])
        if patient_id:
            rows = [r for r in rows if r["patientId"] == patient_id]
        start = (page_no - 1) * page_size
        return {
            "columns": directory["columns"],
            "records": rows[start : start + page_size],
            "total": len(rows),
            "pageNo": page_no,
            "pageSize": page_size,
        }

    def delete_directory(self, directory_id: str) -> dict[str, Any]:
        directory = self.directories.get(directory_id)
        if not directory or directory.get("deleted"):
            raise NotFoundError("数据目录不存在。")
        if directory["importStatus"] == "IMPORTING":
            raise AppError("数据导入中，暂不允许删除。", "DATASET_DIRECTORY_IMPORTING")
        directory["deleted"] = True
        return {"directoryId": directory_id, "deleted": True, "deletedAt": self._now()}

    def create_export(self, export_type: str, file_name: str) -> dict[str, Any]:
        export_record_id = self._id("exp")
        data = {
            "exportRecordId": export_record_id,
            "exportType": export_type,
            "exportStatus": "PREPARING",
            "fileName": file_name,
            "expireAt": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 7 * 86400)),
        }
        self.export_records[export_record_id] = data
        return data

    def timeline(self, directory_id: str, patient_id: str) -> dict[str, Any]:
        images = [i for i in self.images.get(directory_id, []) if i["patientId"] == patient_id]
        counts: dict[str, int] = {}
        for image in images:
            counts[image["surveyDate"]] = counts.get(image["surveyDate"], 0) + 1
        dates = [
            {"surveyDate": date, "imageCount": count, "defaultSelected": idx == 0}
            for idx, (date, count) in enumerate(sorted(counts.items(), reverse=True))
        ]
        return {"dates": dates}

    def patient_images(self, directory_id: str, patient_id: str, survey_date: str, page_no: int, page_size: int) -> dict[str, Any]:
        rows = [
            i
            for i in self.images.get(directory_id, [])
            if i["patientId"] == patient_id and i["surveyDate"] == survey_date
        ]
        start = (page_no - 1) * page_size
        return {"records": rows[start : start + page_size], "total": len(rows), "pageNo": page_no, "pageSize": page_size}

    def image_detail(self, directory_id: str, patient_id: str, image_id: str) -> dict[str, Any]:
        for image in self.images.get(directory_id, []):
            if image["patientId"] == patient_id and image["imageId"] == image_id:
                return {
                    "imageId": image["imageId"],
                    "previewUrl": image["previewUrl"],
                    "originalUrl": image["originalUrl"],
                    "metadata": image["metadata"],
                    "sequence": {"current": 1, "total": 1, "sameDateImageIds": [image["imageId"]]},
                }
        raise NotFoundError("影像不存在。")


store = MockStore()

