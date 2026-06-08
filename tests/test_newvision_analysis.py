from __future__ import annotations

import csv
from pathlib import Path

import pytest

from backend.app.parsers.newvision_analysis import parse_newvision_analysis_result_bytes


def test_newvision_analysis_result_matches_customer_csv_sample():
    repo = Path(__file__).resolve().parents[1]
    sample_dir = (
        repo
        / "test-data/upload-samples/newvision/样例数据/院外导入样例数据/患者原始数据示例"
    )
    expected_csv = (
        repo
        / "test-data/upload-samples/newvision/样例数据/院外导入样例数据/json解析结果/json_parse_result.csv"
    )

    fields: dict[str, object] = {}
    for path in sorted(sample_dir.glob("*_AnalysisRlt.json")):
        fields.update(parse_newvision_analysis_result_bytes(path.read_bytes(), source_path=path.name))

    rows = list(csv.DictReader(expected_csv.open("r", encoding="utf-8-sig", newline="")))
    assert len(rows) == 1
    expected = rows[0]

    # Compare the values that the customer-approved CSV actually populated.
    expected_non_empty = {
        key: value
        for key, value in expected.items()
        if key and str(value).strip()
    }
    assert expected_non_empty
    assert set(expected_non_empty).issubset(fields.keys())

    for key, value in expected_non_empty.items():
        if key.endswith("_json"):
            assert fields[key] == value
        else:
            assert fields[key] == pytest.approx(float(value))

