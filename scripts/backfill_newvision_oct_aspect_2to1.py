from __future__ import annotations

import argparse
import json
import struct
import zlib
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import select

from backend.app.core.config import get_settings
from backend.app.db.models import DatasetImageAsset, DatasetImageMetadata
from backend.app.db.session import get_session_factory
from backend.app.parsers.newvision_oct import gray_png_bytes, resize_gray_to_shape, target_bscan_output_shape
from backend.app.storage.backend import get_storage, normalize_storage_path


PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _read_png_gray_filter0(data: bytes) -> np.ndarray:
    if not data.startswith(PNG_SIG):
        raise ValueError("not a PNG file")

    pos = 8
    width = height = None
    idat: list[bytes] = []
    while pos < len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        chunk_type = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, comp, filt, interlace = struct.unpack(
                ">IIBBBBB", chunk
            )
            if (
                bit_depth != 8
                or color_type != 0
                or comp != 0
                or filt != 0
                or interlace != 0
            ):
                raise ValueError(
                    "unsupported PNG format: "
                    f"bit_depth={bit_depth} color_type={color_type} interlace={interlace}"
                )
        elif chunk_type == b"IDAT":
            idat.append(chunk)
        elif chunk_type == b"IEND":
            break

    if width is None or height is None:
        raise ValueError("missing PNG IHDR")

    raw = zlib.decompress(b"".join(idat))
    stride = width + 1
    if len(raw) != stride * height:
        raise ValueError(f"unexpected PNG raw length: got={len(raw)} expected={stride * height}")

    rows = []
    for y in range(height):
        start = y * stride
        filter_type = raw[start]
        if filter_type != 0:
            raise ValueError(f"unsupported PNG scanline filter: {filter_type}")
        rows.append(np.frombuffer(raw[start + 1 : start + 1 + width], dtype=np.uint8).copy())
    return np.vstack(rows)


def _frame_paths(img: DatasetImageAsset, meta: dict[str, Any] | None) -> list[str]:
    parsed = normalize_storage_path(img.parsed_path or "")
    if not parsed:
        return []
    frames_dir = normalize_storage_path(str(Path(parsed).parent))
    oct_dat = (meta or {}).get("octDat") or {}
    frame_names = oct_dat.get("frames") or []
    if frame_names:
        return [f"{frames_dir}/{str(name).strip('/')}" for name in frame_names]
    return [parsed]


def _source_shape_from_metadata(meta: dict[str, Any] | None) -> tuple[int, int] | None:
    oct_dat = (meta or {}).get("octDat") or {}
    scan = ((oct_dat.get("header") or {}).get("scan") or {})
    try:
        width = int(scan.get("nWidth") or 0)
        height = int(scan.get("nHeight") or 0)
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return height, width


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resize existing NewVision OCT parsed PNG frames to area-preserved width:height=2:1."
    )
    parser.add_argument("--directory-id", action="append", default=[], help="Limit to a directoryId")
    parser.add_argument("--patient-id", action="append", default=[], help="Limit to a patientId")
    parser.add_argument("--image-id", action="append", default=[], help="Limit to a specific imageId")
    parser.add_argument("--dry-run", action="store_true", help="Inspect and report without writing PNGs")
    parser.add_argument("--max-images", type=int, default=None)
    args = parser.parse_args()

    settings = get_settings()
    storage = get_storage(settings)
    session_factory = get_session_factory()

    with session_factory() as db:
        stmt = select(DatasetImageAsset).where(
            DatasetImageAsset.deleted.is_(False),
            DatasetImageAsset.parsed_path.is_not(None),
            DatasetImageAsset.source_type == "PARSED_OCT_DAT",
        )
        if args.directory_id:
            stmt = stmt.where(DatasetImageAsset.directory_id.in_(args.directory_id))
        if args.patient_id:
            stmt = stmt.where(DatasetImageAsset.patient_id.in_(args.patient_id))
        if args.image_id:
            stmt = stmt.where(DatasetImageAsset.image_id.in_(args.image_id))
        stmt = stmt.order_by(DatasetImageAsset.directory_id, DatasetImageAsset.patient_id, DatasetImageAsset.image_id)
        if args.max_images is not None:
            stmt = stmt.limit(args.max_images)
        images = list(db.scalars(stmt))

        summary: dict[str, Any] = {
            "dryRun": args.dry_run,
            "targetAspectRatioWH": "2:1",
            "targetSizing": "preserve original DAT pixel area when metadata is available",
            "imageCount": len(images),
            "framesSeen": 0,
            "framesWritten": 0,
            "framesSkippedAlreadyTargetShape": 0,
            "errors": [],
            "images": [],
        }

        for img in images:
            meta_row = db.get(DatasetImageMetadata, img.image_id)
            metadata = meta_row.metadata_json if meta_row else None
            frame_paths = _frame_paths(img, metadata)
            image_summary = {
                "imageId": img.image_id,
                "directoryId": img.directory_id,
                "patientId": img.patient_id,
                "imageName": img.image_name,
                "frameCount": len(frame_paths),
                "written": 0,
                "skippedAlreadyTargetShape": 0,
                "errors": [],
            }
            source_shape = _source_shape_from_metadata(metadata)
            write_items: list[tuple[str, bytes]] = []
            for frame_path in frame_paths:
                summary["framesSeen"] += 1
                try:
                    raw = storage.get_bytes(frame_path)
                    image = _read_png_gray_filter0(raw)
                    target_height, target_width = target_bscan_output_shape(*(source_shape or image.shape))
                    if image.shape == (target_height, target_width):
                        summary["framesSkippedAlreadyTargetShape"] += 1
                        image_summary["skippedAlreadyTargetShape"] += 1
                        continue
                    resized = resize_gray_to_shape(
                        image,
                        target_height=target_height,
                        target_width=target_width,
                    )
                    if not args.dry_run:
                        write_items.append((frame_path, gray_png_bytes(resized)))
                    image_summary["written"] += 1
                    summary["framesWritten"] += 1
                except Exception as exc:  # noqa: BLE001
                    err = {"framePath": frame_path, "error": str(exc)}
                    image_summary["errors"].append(err)
                    summary["errors"].append({**err, "imageId": img.image_id})
            if write_items and not args.dry_run:
                storage.put_bytes_batch(write_items)
            summary["images"].append(image_summary)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
