"""
Content-Range validation for multipart uploads (acceptance I-11a–I-11e).

Header format: ``bytes <start>-<end>/<total>`` where ``<total>`` is full file size.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from backend.app.core.errors import AppError


_RANGE_RE = re.compile(r"^\s*bytes\s+(\d+)-(\d+)/(\d+)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedRange:
    start: int
    end: int
    total: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1


def parse_content_range(header: str | None, file_size: int) -> ParsedRange:
    if header is None or not header.strip():
        raise AppError(
            "缺少 Content-Range 请求头。",
            "DATASET_VALIDATION_ERROR",
            code=42201,
            status_code=422,
            details={"field": "Content-Range"},
        )
    m = _RANGE_RE.match(header)
    if not m:
        raise AppError(
            "Content-Range 格式无效。",
            "DATASET_VALIDATION_ERROR",
            code=42201,
            status_code=422,
            details={"field": "Content-Range", "value": header},
        )
    start, end, total = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if total != file_size:
        raise AppError(
            "Content-Range 总长度与上传任务文件大小不一致。",
            "DATASET_VALIDATION_ERROR",
            code=42201,
            status_code=422,
            details={"totalInHeader": total, "fileSize": file_size},
        )
    if start < 0 or end < start or end >= file_size:
        raise AppError(
            "分片区间超出文件范围或无效。",
            "DATASET_VALIDATION_ERROR",
            code=42201,
            status_code=422,
            details={"start": start, "end": end, "fileSize": file_size},
        )
    return ParsedRange(start=start, end=end, total=total)


def validate_part_against_task(
    *,
    part_number: int,
    part_count: int,
    chunk_size: int,
    file_size: int,
    range_: ParsedRange,
    body_len: int,
) -> None:
    if range_.length != body_len:
        raise AppError(
            "分片大小与声明区间不一致。",
            "DATASET_UPLOAD_PART_SIZE_MISMATCH",
            details={"expectedLength": range_.length, "bodyLength": body_len},
        )

    exp_start = (part_number - 1) * chunk_size
    if part_number < part_count:
        exp_end = part_number * chunk_size - 1
    else:
        exp_end = file_size - 1

    if range_.start != exp_start or range_.end != exp_end:
        raise AppError(
            "Content-Range 与分片序号不一致。",
            "DATASET_UPLOAD_RANGE_PART_MISMATCH",
            details={
                "partNumber": part_number,
                "expectedStart": exp_start,
                "expectedEnd": exp_end,
                "actualStart": range_.start,
                "actualEnd": range_.end,
            },
        )


def merge_expects_no_gaps(parts: list[tuple[int, int, int]], file_size: int) -> None:
    """
    ``parts``: list of (part_number, range_start, range_end) after sort by start.
    Raises DATASET_UPLOAD_PART_MISSING if holes or overlap.
    """
    if not parts:
        raise AppError("上传分片不完整，请重新上传缺失分片。", "DATASET_UPLOAD_PART_MISSING")
    parts = sorted(parts, key=lambda x: x[1])
    if parts[0][1] != 0:
        raise AppError("上传分片不完整，请重新上传缺失分片。", "DATASET_UPLOAD_PART_MISSING")
    cur_end = parts[0][2]
    for pn, st, ed in parts[1:]:
        if st != cur_end + 1:
            raise AppError("上传分片不完整，请重新上传缺失分片。", "DATASET_UPLOAD_PART_MISSING")
        cur_end = max(cur_end, ed)
    if cur_end != file_size - 1:
        raise AppError("上传分片不完整，请重新上传缺失分片。", "DATASET_UPLOAD_PART_MISSING")
