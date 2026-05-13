"""
供应商布局识别与兼容解码入口（导入流水线通过 ``detect_dataset_package_vendor`` 分发）。
"""

from __future__ import annotations

from pathlib import Path

from backend.app.parsers import image_stubs

# 导入流水线仅对已登记 layout key 执行解析；新供应商在此扩展。
SUPPORTED_PACKAGE_LAYOUTS: frozenset[str] = frozenset({"newvision_v1"})


def decode_fdt_to_jpeg(rel_path_in_zip: str, raw_bytes: bytes) -> bytes:
    """
    兼容旧调用方：若 ``raw_bytes`` 为 JPEG 魔数则原样返回，否则返回桩图。
    正式导入请使用 ``newvision.fundus_fdt_maybe_jpeg``。
    """
    if len(raw_bytes) >= 2 and raw_bytes[0:2] == b"\xff\xd8":
        return raw_bytes
    return image_stubs.parse_fdt_to_jpeg_stub(Path(rel_path_in_zip))


def decode_oct_dat_to_jpeg(rel_path_in_zip: str, raw_bytes: bytes) -> bytes:
    """保留桩实现供未接入真实 OCT 的测试路径使用。"""
    del raw_bytes
    return image_stubs.parse_dat_to_jpeg_stub(Path(rel_path_in_zip))


def detect_dataset_package_vendor(extracted_relative_paths: list[str]) -> str:
    """
    识别 zip 解压后的相对路径布局，返回 layout key。

    约定：zip 内放置 ``.dataset_vendor/acme_v1.marker`` 可标记自定义供应商；
    否则只要存在 ``.xlsx`` 即按 **新视野** 管线处理（问卷筛选、多问卷阻断等
    由 ``select_newvision_questionnaire_xlsx`` 在导入阶段抛出，避免此处误报
    ``unsupported_bundle``）。
    """
    norm = [p.replace("\\", "/") for p in extracted_relative_paths]
    for rel in norm:
        r = rel.lower()
        if ".dataset_vendor/acme_v1.marker" in r.replace("//", "/"):
            return "acme_vendor_v1"
    if any(p.lower().endswith(".xlsx") for p in norm):
        return "newvision_v1"
    return "unsupported_bundle"
