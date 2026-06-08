from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


ETDRS_9_ORDER: tuple[str, ...] = (
    "Central_Subfield",
    "OuterTemp",
    "OuterInf",
    "OuterNas",
    "OuterSup",
    "InnerTemp",
    "InnerInf",
    "InnerNas",
    "InnerSup",
)

GCC_6_ORDER: tuple[str, ...] = (
    "Temporal_Inferior",
    "Inferior",
    "Nasal_Inferior",
    "Nasal_Superior",
    "Superior",
    "Temporal_Superior",
)

RNFL12_LABELS: tuple[str, ...] = ("T", "", "", "S", "", "", "N", "", "", "I", "", "")

RNFL_AVG_FALLBACK_NAMES: tuple[str, ...] = (
    "平均RNFL厚度(um)",
    "RNFL颞侧厚度",
    "RNFL上部厚度",
    "RNFL鼻侧厚度",
    "RNFL下部厚度",
)

DISCINFO_FALLBACK_EN: tuple[str, ...] = (
    "Cup Area",
    "Disc Area",
    "Rim Area",
    "Cup Volume",
    "CDR Vertical CDR",
    "AvgCDR",
)


def is_newvision_analysis_result_path(path: str | Path) -> bool:
    return Path(str(path).replace("\\", "/")).name.lower().endswith("_analysisrlt.json")


def parse_newvision_analysis_result_bytes(raw: bytes, *, source_path: str = "") -> dict[str, Any]:
    """Parse NewVision ``*_AnalysisRlt.json`` into customer-facing result columns."""
    data = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, dict):
        return {}

    prefix = _eye_prefix_from_path(source_path)
    if not prefix:
        return {}

    fields: dict[str, Any] = {}
    has_disc = isinstance(data.get("DISCINFO"), dict)
    has_macular = isinstance(data.get("ETDRS"), dict) or isinstance(data.get("GCC"), dict)

    if has_disc:
        fields.update(_collect_disc_fields(prefix, data))
        fields[f"{prefix}_disc_json"] = Path(source_path).name if source_path else None
    if has_macular:
        fields.update(_collect_macular_fields(prefix, data))
        fields[f"{prefix}_macular_json"] = Path(source_path).name if source_path else None

    return fields


def _eye_prefix_from_path(source_path: str) -> str:
    name = Path(str(source_path).replace("\\", "/")).name.lower()
    if name.startswith("od-"):
        return "od_"
    if name.startswith("os-"):
        return "os_"
    return ""


def _get_nested(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    cur: Any = data
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def _sanitize_name(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[^\w\u4e00-\u9fff]+", "_", text)
    return text.strip("_")


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _set_scalar(fields: dict[str, Any], key: str, value: Any) -> None:
    fields[key] = value if value is not None else None


def _set_ordered_values(
    fields: dict[str, Any],
    *,
    prefix: str,
    base: str,
    values: Any,
    order_names: Any,
    expected_names: tuple[str, ...],
) -> None:
    value_list = _as_list(values)
    name_list = _as_list(order_names)
    mapping: dict[str, Any] = {}
    for value, name in zip(value_list, name_list):
        key = _sanitize_name(name)
        if key:
            mapping[key] = value

    for name in expected_names:
        key = _sanitize_name(name)
        fields[f"{prefix}{base}_{key}"] = mapping.get(key)


def _set_rnfl12(fields: dict[str, Any], prefix: str, values: Any) -> None:
    value_list = _as_list(values)
    for idx, label in enumerate(RNFL12_LABELS, start=1):
        suffix = f"{idx:02d}_{label}" if label else f"{idx:02d}"
        fields[f"{prefix}DiscRNFL12Clock_{suffix}"] = (
            value_list[idx - 1] if idx - 1 < len(value_list) else None
        )


def _set_named_values(
    fields: dict[str, Any],
    *,
    prefix: str,
    base: str,
    values: Any,
    names: Any,
    fallback_names: tuple[str, ...],
) -> None:
    value_list = _as_list(values)
    name_list = _as_list(names) or list(fallback_names)

    mapping: dict[str, Any] = {}
    for value, name in zip(value_list, name_list):
        key = _sanitize_name(name)
        if key:
            mapping[key] = value

    for name in name_list:
        key = _sanitize_name(name)
        if key:
            fields[f"{prefix}{base}_{key}"] = mapping.get(key)


def _collect_disc_fields(prefix: str, data: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    _set_rnfl12(fields, prefix, _get_nested(data, "DISCINFO", "DiscRNFL12Clock"))
    _set_named_values(
        fields,
        prefix=prefix,
        base="DiscRNFLClockAvg",
        values=_get_nested(data, "DISCINFO", "DiscRNFLClockAvg"),
        names=_get_nested(data, "DISCINFO", "DiscRNFLClockAvgName"),
        fallback_names=RNFL_AVG_FALLBACK_NAMES,
    )
    fields[f"{prefix}DiscRNFLTkickness"] = _get_nested(
        data,
        "DISCINFO",
        "DiscRNFLTkickness",
        default=None,
    )
    names_en = _get_nested(data, "DISCINFO", "DiscinfoValueNameEN")
    names_cn = _get_nested(data, "DISCINFO", "DiscinfoValueNameCN")
    names = names_en if isinstance(names_en, list) else names_cn
    _set_named_values(
        fields,
        prefix=prefix,
        base="DiscinfoValue",
        values=_get_nested(data, "DISCINFO", "DiscinfoValue"),
        names=names,
        fallback_names=DISCINFO_FALLBACK_EN,
    )
    return fields


def _collect_macular_fields(prefix: str, data: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    _set_scalar(
        fields,
        f"{prefix}Macular_Average_Volume",
        _get_nested(data, "ETDRS", "Macular_Average_Volume"),
    )
    _set_scalar(
        fields,
        f"{prefix}Macular_Avg_Thickness",
        _get_nested(data, "ETDRS", "Macular_Avg_Thickness"),
    )
    _set_scalar(
        fields,
        f"{prefix}Macular_Central_Subfield",
        _get_nested(data, "ETDRS", "Macular_Central_Subfield"),
    )
    _set_ordered_values(
        fields,
        prefix=prefix,
        base="Macular_Avg_Thickness_Nine",
        values=_get_nested(data, "ETDRS", "Macular_Avg_Thickness_Nine"),
        order_names=_get_nested(data, "ETDRS", "Macular_Avg_Thickness_Nine_Order"),
        expected_names=ETDRS_9_ORDER,
    )
    _set_scalar(
        fields,
        f"{prefix}MacularGCC_Avg_Thickness",
        _get_nested(data, "GCC", "MacularGCC_Avg_Thickness"),
    )
    _set_scalar(
        fields,
        f"{prefix}MacularGCC_Min_Thickness",
        _get_nested(data, "GCC", "MacularGCC_Min_Thickness"),
    )
    _set_ordered_values(
        fields,
        prefix=prefix,
        base="MacularGCC_Avg_Thickness_Six",
        values=_get_nested(data, "GCC", "MacularGCC_Avg_Thickness_Six"),
        order_names=_get_nested(data, "GCC", "MacularGCC_Avg_Thickness_Six_Order"),
        expected_names=GCC_6_ORDER,
    )
    return fields
