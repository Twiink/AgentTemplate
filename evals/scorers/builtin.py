"""内置评分器实现；每个函数接收评估记录，返回指标字典。"""

from __future__ import annotations

import json
from difflib import SequenceMatcher
from typing import Any


def exact_match(record: dict[str, Any]) -> dict[str, float]:
    """精确匹配评分器；输出与期望完全一致得分 1.0，否则 0.0。"""
    expected = record.get("expected")
    output = record.get("output")

    if expected is None:
        return {"exact_match": 0.0}

    # 规范化比较：转为字符串并去除首尾空白
    expected_str = (
        json.dumps(expected, sort_keys=True) if not isinstance(expected, str) else expected
    )
    output_str = json.dumps(output, sort_keys=True) if not isinstance(output, str) else str(output)

    expected_normalized = expected_str.strip()
    output_normalized = output_str.strip()

    score = 1.0 if expected_normalized == output_normalized else 0.0

    return {"exact_match": score}


def contains(record: dict[str, Any]) -> dict[str, float]:
    """包含评分器；期望内容是输出的子串得分 1.0，否则 0.0。

    支持字符串或列表形式的期望值：
    - 字符串：检查是否为输出的子串（不区分大小写）
    - 列表：检查所有项是否都在输出中
    """
    expected = record.get("expected")
    output = record.get("output")

    if expected is None or output is None:
        return {"contains": 0.0}

    output_str = json.dumps(output) if not isinstance(output, str) else str(output)
    output_lower = output_str.lower()

    if isinstance(expected, str):
        score = 1.0 if expected.lower() in output_lower else 0.0
    elif isinstance(expected, list):
        all_found = all(str(item).lower() in output_lower for item in expected)
        score = 1.0 if all_found else 0.0
    else:
        expected_str = str(expected).lower()
        score = 1.0 if expected_str in output_lower else 0.0

    return {"contains": score}


def similarity(record: dict[str, Any]) -> dict[str, float]:
    """相似度评分器；使用 SequenceMatcher 计算字符串相似度（0.0-1.0）。"""
    expected = record.get("expected")
    output = record.get("output")

    if expected is None or output is None:
        return {"similarity": 0.0}

    expected_str = (
        json.dumps(expected, sort_keys=True) if not isinstance(expected, str) else str(expected)
    )
    output_str = json.dumps(output, sort_keys=True) if not isinstance(output, str) else str(output)

    matcher = SequenceMatcher(None, expected_str, output_str)
    score = matcher.ratio()

    return {"similarity": score}


def combined(record: dict[str, Any]) -> dict[str, float]:
    """组合评分器；返回 exact_match、contains、similarity 三个指标。"""
    return {
        **exact_match(record),
        **contains(record),
        **similarity(record),
    }
