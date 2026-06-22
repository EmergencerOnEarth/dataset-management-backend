from __future__ import annotations

import argparse
import json
import math
import os
import re
import struct
import tempfile
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

import numpy as np


PARSER_VERSION = "newvision-oct-parser-v3-aspect-2to1-area-preserved"
HEADER_SIZE = 1024
TARGET_BSCAN_ASPECT_RATIO = 2.0


class OctParseError(ValueError):
    pass


class UnsupportedDatFormat(OctParseError):
    pass


@dataclass(frozen=True)
class OCTScanDataHeader:
    nDataType: int
    nFrames: int
    nWidth: int
    nHeight: int
    nBytesPerPixel: int
    nDataOffset: int
    nCompressType: int


@dataclass(frozen=True)
class OCTPatientScanInfo:
    patient_id: str
    name: str
    gender: int
    birthday: str
    email: str
    tel: str
    check_time: int
    scan_type: int
    scan_part: int
    scan_mode: int
    eye: int


@dataclass(frozen=True)
class OCTFileHeaderParsed:
    signature: str
    file_version: int
    scan: OCTScanDataHeader
    patient: OCTPatientScanInfo
    reserved_int: int
    race_type: int
    header_size: int


def align(offset: int, boundary: int) -> int:
    return (offset + (boundary - 1)) & ~(boundary - 1)


def strip_u32_prefix_if_present(raw: bytes) -> bytes:
    if len(raw) >= 4:
        prefix = int.from_bytes(raw[:4], "little")
        if prefix in (0, 1, 2, 3, 4):
            return raw[4:]
    return raw


def decode_fixed_string(raw: bytes) -> str:
    raw = strip_u32_prefix_if_present(raw)
    raw = raw.rstrip(b"\x00")
    if not raw:
        return ""

    if len(raw) >= 2:
        odd_bytes = raw[1::2]
        if odd_bytes and odd_bytes.count(0) / len(odd_bytes) > 0.7:
            return raw.decode("utf-16-le", errors="ignore").rstrip("\x00").strip()

    for encoding in ("utf-8", "gbk", "latin1"):
        try:
            return raw.decode(encoding, errors="ignore").strip()
        except Exception:
            continue
    return raw.decode("latin1", errors="ignore").strip()


def parse_oct_header(
    header_bytes: bytes,
    prefer_time_t_8: bool = True,
) -> OCTFileHeaderParsed:
    if len(header_bytes) < HEADER_SIZE:
        raise OctParseError(f"DAT header is shorter than {HEADER_SIZE} bytes")

    def try_parse(time_t_size: int) -> Optional[OCTFileHeaderParsed]:
        offset = 0

        signature_raw = header_bytes[offset : offset + 4]
        offset += 4
        (file_version,) = struct.unpack_from("<I", header_bytes, offset)
        offset += 4

        scan_values = struct.unpack_from("<7I", header_bytes, offset)
        offset += 7 * 4
        scan = OCTScanDataHeader(*scan_values)

        patient_id_raw = header_bytes[offset : offset + 60]
        offset += 60
        name_raw = header_bytes[offset : offset + 60]
        offset += 60
        (gender,) = struct.unpack_from("<I", header_bytes, offset)
        offset += 4
        birthday_raw = header_bytes[offset : offset + 22]
        offset += 22
        email_raw = header_bytes[offset : offset + 100]
        offset += 100
        tel_raw = header_bytes[offset : offset + 40]
        offset += 40

        offset = align(offset, 8 if time_t_size == 8 else 4)
        if time_t_size == 8:
            (check_time,) = struct.unpack_from("<q", header_bytes, offset)
            offset += 8
        else:
            (check_time,) = struct.unpack_from("<i", header_bytes, offset)
            offset += 4

        scan_type, scan_part, scan_mode, eye = struct.unpack_from("<4I", header_bytes, offset)
        offset += 4 * 4

        offset += 164
        offset = align(offset, 8 if time_t_size == 8 else 4)

        patient = OCTPatientScanInfo(
            patient_id=decode_fixed_string(patient_id_raw),
            name=decode_fixed_string(name_raw),
            gender=int(gender),
            birthday=decode_fixed_string(birthday_raw),
            email=decode_fixed_string(email_raw),
            tel=decode_fixed_string(tel_raw),
            check_time=int(check_time),
            scan_type=int(scan_type),
            scan_part=int(scan_part),
            scan_mode=int(scan_mode),
            eye=int(eye),
        )

        reserved_int, race_type = struct.unpack_from("<2i", header_bytes, offset)
        offset += 2 * 4
        offset += 496
        offset = align(offset, 8)

        parsed = OCTFileHeaderParsed(
            signature=signature_raw.decode("ascii", errors="ignore").rstrip("\x00"),
            file_version=int(file_version),
            scan=scan,
            patient=patient,
            reserved_int=int(reserved_int),
            race_type=int(race_type),
            header_size=offset,
        )

        if parsed.signature != "EOD":
            return None
        if not (1 <= parsed.scan.nFrames <= 10_000_000):
            return None
        if not (1 <= parsed.scan.nWidth <= 100_000):
            return None
        if not (1 <= parsed.scan.nHeight <= 100_000):
            return None
        if parsed.scan.nBytesPerPixel not in (1, 2, 4):
            return None
        if not (0 <= parsed.scan.nDataOffset <= 10_000_000):
            return None

        return parsed

    attempts = (8, 4) if prefer_time_t_8 else (4, 8)
    for time_t_size in attempts:
        parsed = try_parse(time_t_size)
        if parsed is not None:
            return parsed

    raise OctParseError(
        "Unable to parse DAT header: signature or header fields are invalid"
    )


def _frame_dtype(bytes_per_pixel: int) -> np.dtype:
    if bytes_per_pixel == 1:
        return np.dtype("uint8")
    if bytes_per_pixel == 2:
        return np.dtype("<u2")
    if bytes_per_pixel == 4:
        return np.dtype("<u4")
    raise UnsupportedDatFormat(f"Unsupported bytes per pixel: {bytes_per_pixel}")


def open_oct_dat(
    path: Union[str, Path],
    mmap: bool = True,
) -> Tuple[OCTFileHeaderParsed, np.ndarray, Optional[np.ndarray]]:
    path = Path(path)
    with path.open("rb") as file:
        header_bytes = file.read(HEADER_SIZE)

    header = parse_oct_header(header_bytes, prefer_time_t_8=True)
    if header.scan.nCompressType != 0:
        raise UnsupportedDatFormat(
            f"Compressed DAT is not supported: nCompressType={header.scan.nCompressType}"
        )

    data_offset = header.scan.nDataOffset or header.header_size
    if data_offset < header.header_size:
        data_offset = header.header_size

    frame_count = int(header.scan.nFrames)
    width = int(header.scan.nWidth)
    height = int(header.scan.nHeight)
    bytes_per_pixel = int(header.scan.nBytesPerPixel)
    dtype = _frame_dtype(bytes_per_pixel)

    expected_end = data_offset + frame_count * width * height * bytes_per_pixel
    file_size = path.stat().st_size
    if file_size < expected_end:
        raise OctParseError(
            f"DAT payload is shorter than expected: file_size={file_size}, expected_end={expected_end}"
        )

    if mmap:
        frames = np.memmap(
            path,
            mode="r",
            dtype=dtype,
            offset=data_offset,
            shape=(frame_count, width, height),
        )
    else:
        with path.open("rb") as file:
            file.seek(data_offset)
            raw = file.read(frame_count * width * height * bytes_per_pixel)
        frames = np.frombuffer(raw, dtype=dtype).reshape(frame_count, width, height)

    signal = None
    signal_offset = expected_end
    if file_size >= signal_offset + frame_count:
        with path.open("rb") as file:
            file.seek(signal_offset)
            signal = np.frombuffer(file.read(frame_count), dtype=np.uint8)

    return header, frames, signal


def target_bscan_output_shape(
    source_height: int,
    source_width: int,
    *,
    aspect_ratio: float = TARGET_BSCAN_ASPECT_RATIO,
) -> tuple[int, int]:
    source_height = max(1, int(source_height))
    source_width = max(1, int(source_width))
    source_area = source_height * source_width
    target_height = max(1, int(round(math.sqrt(source_area / aspect_ratio))))
    target_width = max(1, int(round(target_height * aspect_ratio)))
    return target_height, target_width


def resize_gray_to_shape(
    image: np.ndarray,
    *,
    target_height: int,
    target_width: int,
) -> np.ndarray:
    image = np.asarray(image, dtype=np.uint8)
    if image.ndim != 2:
        raise ValueError(f"Expected a 2-D grayscale image, got shape={image.shape}")
    height, width = image.shape
    target_height = max(1, int(target_height))
    target_width = max(1, int(target_width))
    if (target_height, target_width) == (height, width):
        return image.copy()

    xs = np.linspace(0, width - 1, target_width, dtype=np.float32)
    x0 = np.floor(xs).astype(np.int32)
    x1 = np.minimum(x0 + 1, width - 1)
    alpha = (xs - x0).astype(np.float32)
    left = image[:, x0].astype(np.float32)
    right = image[:, x1].astype(np.float32)
    horizontal = left * (1.0 - alpha) + right * alpha

    ys = np.linspace(0, height - 1, target_height, dtype=np.float32)
    y0 = np.floor(ys).astype(np.int32)
    y1 = np.minimum(y0 + 1, height - 1)
    beta = (ys - y0).astype(np.float32)[:, None]
    top = horizontal[y0, :]
    bottom = horizontal[y1, :]
    resized = top * (1.0 - beta) + bottom * beta
    return np.clip(resized + 0.5, 0, 255).astype(np.uint8)


def frame_to_uint8(frame: np.ndarray, percentiles: Tuple[float, float] = (1, 99)) -> np.ndarray:
    image = frame.T[::-1, :].astype(np.float32)
    vmin, vmax = np.percentile(image, percentiles)
    scaled = np.clip((image - vmin) / (vmax - vmin + 1e-6), 0, 1)
    target_height, target_width = target_bscan_output_shape(*image.shape)
    return resize_gray_to_shape(
        (scaled * 255).astype(np.uint8),
        target_height=target_height,
        target_width=target_width,
    )


def save_frames_png(
    frames: np.ndarray,
    output_dir: Union[str, Path],
    indices: Optional[Iterable[int]] = None,
    max_frames: Optional[int] = None,
) -> List[Path]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if indices is None:
        selected = list(range(frames.shape[0]))
    else:
        selected = list(indices)
    if max_frames is not None:
        selected = selected[:max_frames]

    saved: List[Path] = []
    for index in selected:
        image = frame_to_uint8(frames[index])
        output_path = output_dir / f"frame_{index:05d}.png"
        write_gray_png(output_path, image)
        saved.append(output_path)
    return saved


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type)
    crc = zlib.crc32(data, crc) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def gray_png_bytes(image: np.ndarray) -> bytes:
    image = np.asarray(image, dtype=np.uint8)
    if image.ndim != 2:
        raise ValueError(f"Expected a 2-D grayscale image, got shape={image.shape}")

    height, width = image.shape
    raw_scanlines = b"".join(b"\x00" + image[row].tobytes() for row in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(b"IHDR", ihdr),
            _png_chunk(b"IDAT", zlib.compress(raw_scanlines)),
            _png_chunk(b"IEND", b""),
        ]
    )


def write_gray_png(path: Union[str, Path], image: np.ndarray) -> None:
    Path(path).write_bytes(gray_png_bytes(image))


def should_parse_dat(path: Union[str, Path]) -> bool:
    path = Path(path)
    return path.suffix.lower() == ".dat" and path.stem.endswith("-001")


def parse_dat_file(
    dat_path: Union[str, Path],
    output_dir: Optional[Union[str, Path]] = None,
    indices: Optional[Iterable[int]] = None,
    max_frames: Optional[int] = None,
    mmap: bool = True,
) -> Dict[str, Any]:
    dat_path = Path(dat_path)
    if output_dir is None:
        output_dir = dat_path.with_name(f"{dat_path.name}.frames")

    header, frames, signal = open_oct_dat(dat_path, mmap=mmap)
    saved_frames = save_frames_png(
        frames,
        output_dir=output_dir,
        indices=indices,
        max_frames=max_frames,
    )

    return {
        "parserVersion": PARSER_VERSION,
        "sourcePath": str(dat_path),
        "outputDir": str(Path(output_dir)),
        "header": asdict(header),
        "frameCount": int(frames.shape[0]),
        "savedFrameCount": len(saved_frames),
        "frames": [str(path) for path in saved_frames],
        "signalCount": None if signal is None else int(signal.shape[0]),
        "signalPreview": None if signal is None else signal[:20].astype(int).tolist(),
    }


def parse_oct_dat_bytes(
    dat_bytes: bytes,
    *,
    write_frame: Callable[[str, bytes], None],
    source_path_for_meta: str = "",
    max_saved_frames: int = 400,
    max_frames_in_file: int = 5000,
    max_dimension: int = 4096,
) -> Dict[str, Any]:
    """Parse OCT ``.dat`` from bytes; PNG 帧通过 ``write_frame(文件名, png_bytes)`` 写出。"""
    if len(dat_bytes) < HEADER_SIZE:
        raise OctParseError(f"DAT header is shorter than {HEADER_SIZE} bytes")
    header = parse_oct_header(dat_bytes[:HEADER_SIZE], prefer_time_t_8=True)
    w, h, nframes = int(header.scan.nWidth), int(header.scan.nHeight), int(header.scan.nFrames)
    if header.scan.nCompressType != 0 or header.scan.nBytesPerPixel not in (1, 2, 4):
        raise UnsupportedDatFormat(
            f"Unsupported DAT format compress={header.scan.nCompressType} "
            f"bpp={header.scan.nBytesPerPixel}"
        )
    reasons: list[str] = []
    if nframes > max_frames_in_file:
        reasons.append("frames")
    output_height, output_width = target_bscan_output_shape(h, w)
    if w > max_dimension or h > max_dimension or output_width > max_dimension or output_height > max_dimension:
        reasons.append("dimensions")
    if reasons:
        return {
            "parserVersion": PARSER_VERSION,
            "sourceType": "OCT_DAT",
            "sourcePath": source_path_for_meta,
            "limitExceeded": True,
            "header": asdict(header),
            "frameCount": nframes,
            "savedFrameCount": 0,
            "frames": [],
            "warnings": ["FILE_PARSE_LIMIT_EXCEEDED"],
            "limitReason": ",".join(reasons),
        }

    fd, tmp_path = tempfile.mkstemp(suffix=".dat")
    os.close(fd)
    warn_codes: list[str] = []
    try:
        Path(tmp_path).write_bytes(dat_bytes)
        header2, frames, signal = open_oct_dat(Path(tmp_path), mmap=True)
        cap = min(int(frames.shape[0]), max_saved_frames)
        if int(frames.shape[0]) > max_saved_frames:
            warn_codes.append("FILE_PARSE_LIMIT_EXCEEDED")
        rel_frames: list[str] = []
        for index in range(cap):
            png = gray_png_bytes(frame_to_uint8(frames[index]))
            fname = f"frame_{index:05d}.png"
            write_frame(fname, png)
            rel_frames.append(fname)
        return {
            "parserVersion": PARSER_VERSION,
            "sourceType": "OCT_DAT",
            "sourcePath": source_path_for_meta,
            "header": asdict(header2),
            "frameCount": int(frames.shape[0]),
            "savedFrameCount": len(rel_frames),
            "frames": rel_frames,
            "signalCount": None if signal is None else int(signal.shape[0]),
            "signalPreview": None if signal is None else signal[:20].astype(int).tolist(),
            "warnings": warn_codes,
        }
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def parse_oct_json_bytes(raw: bytes, *, source_path: str = "", flatten: bool = True) -> Dict[str, Any]:
    data = json.loads(raw.decode("utf-8-sig"))
    result: Dict[str, Any] = {
        "parserVersion": PARSER_VERSION,
        "sourcePath": source_path,
        "raw": data,
    }
    if flatten:
        result["flattened"] = flatten_json(data)
    return result


def load_json_payload(path: Union[str, Path]) -> Any:
    path = Path(path)
    with path.open("r", encoding="utf-8-sig") as file:
        return json.load(file)


def flatten_json(value: Any, prefix: str = "", separator: str = ".") -> Dict[str, Any]:
    flattened: Dict[str, Any] = {}

    if isinstance(value, dict):
        for key, item in value.items():
            child_key = f"{prefix}{separator}{key}" if prefix else str(key)
            flattened.update(flatten_json(item, child_key, separator))
        return flattened

    if isinstance(value, list):
        if all(not isinstance(item, (dict, list)) for item in value):
            flattened[prefix] = value
            return flattened
        for index, item in enumerate(value):
            child_key = f"{prefix}{separator}{index}" if prefix else str(index)
            flattened.update(flatten_json(item, child_key, separator))
        return flattened

    flattened[prefix] = value
    return flattened


def parse_json_file(path: Union[str, Path], flatten: bool = True) -> Dict[str, Any]:
    path = Path(path)
    raw = load_json_payload(path)
    result: Dict[str, Any] = {
        "parserVersion": PARSER_VERSION,
        "sourcePath": str(path),
        "raw": raw,
    }
    if flatten:
        result["flattened"] = flatten_json(raw)
    return result


def extract_oct_path_context(path: Union[str, Path]) -> Dict[str, Optional[str]]:
    path = Path(path)
    parts = path.parts

    pid = None
    check_date = None
    date_source = None

    lowered = [part.lower() for part in parts]
    if "x08-rds" in lowered:
        idx = lowered.index("x08-rds")
        if idx > 0:
            cand = parts[idx - 1]
            if (
                cand.lower() != "info-data"
                and not re.fullmatch(r"\d{8}", cand)
                and not (cand.isdigit() and len(cand) <= 6)
            ):
                pid = cand
        if idx + 1 < len(parts):
            candidate = parts[idx + 1]
            if re.fullmatch(r"\d{8}", candidate):
                check_date = f"{candidate[:4]}-{candidate[4:6]}-{candidate[6:8]}"
                date_source = "x08-rds"
        if pid is None:
            for candidate in reversed(parts[:idx]):
                if candidate.isdigit():
                    continue
                if re.fullmatch(r"[A-Za-z]+[A-Za-z0-9_-]*\d+[A-Za-z0-9_-]*", candidate):
                    pid = candidate
                    break

    if check_date is None:
        for candidate in reversed(parts):
            match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", candidate)
            if match:
                year, month, day = match.groups()
                check_date = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
                date_source = "date-directory"
                break

    return {
        "pid": pid,
        "check_date": check_date,
        "date_source": date_source,
    }


def parse_oct_file(
    path: Union[str, Path],
    output_base_dir: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    path = Path(path)
    context = extract_oct_path_context(path)

    if path.suffix.lower() == ".dat":
        if not should_parse_dat(path):
            return {
                "parserVersion": PARSER_VERSION,
                "sourcePath": str(path),
                "sourceType": "OCT_DAT_SKIPPED",
                "context": context,
                "reason": "DAT filename does not end with -001.dat",
            }
        output_dir = None
        if output_base_dir is not None:
            output_dir = Path(output_base_dir) / f"{path.name}.frames"
        parsed = parse_dat_file(path, output_dir=output_dir)
        parsed["sourceType"] = "OCT_DAT"
        parsed["context"] = context
        return parsed

    if path.suffix.lower() == ".json":
        parsed = parse_json_file(path, flatten=True)
        parsed["sourceType"] = "OCT_JSON"
        parsed["context"] = context
        return parsed

    raise OctParseError(f"Unsupported OCT file type: {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse NewVision OCT DAT or JSON files")
    parser.add_argument("path", type=Path)
    parser.add_argument("--out", type=Path, default=None, help="Base output directory for DAT frames")
    parser.add_argument("--max-frames", type=int, default=None, help="Only save the first N frames")
    args = parser.parse_args()

    path = args.path
    if path.suffix.lower() == ".dat":
        output_dir = None
        if args.out is not None:
            output_dir = args.out / f"{path.name}.frames"
        result = parse_dat_file(path, output_dir=output_dir, max_frames=args.max_frames)
        result["sourceType"] = "OCT_DAT"
        result["context"] = extract_oct_path_context(path)
    elif path.suffix.lower() == ".json":
        result = parse_json_file(path)
        result["sourceType"] = "OCT_JSON"
        result["context"] = extract_oct_path_context(path)
    else:
        raise SystemExit(f"Unsupported file type: {path}")

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
