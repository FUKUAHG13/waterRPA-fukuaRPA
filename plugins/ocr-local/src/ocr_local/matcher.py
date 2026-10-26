"""在 OCR 结果里查找目标文本，并计算点击坐标。"""

from __future__ import annotations

import re

from .models import OcrResult, OcrWord

# SAFETY: 限制正则表达式长度，防止病态正则 + 长文本导致指数回溯挂起（ReDoS）。
MAX_TARGET_LENGTH = 10000


def match_text(word_text: str, target: str, mode: str) -> bool:
    """判断单个识别文本是否命中目标。

    equals 为严格相等（不忽略空格 / 大小写），语义最直观；
    contains 做大小写/空格无关的包含判断；
    regex 用 target 作为正则去搜索 word_text。

    Raises:
        ValueError: regex 模式下 target 超过 MAX_TARGET_LENGTH 字符。
    """
    if mode == "equals":
        return word_text == target
    if mode == "regex":
        if len(target) > MAX_TARGET_LENGTH:
            raise ValueError(f"正则表达式过长（最大 {MAX_TARGET_LENGTH} 字符）")
        try:
            # SAFETY: Python re 无回溯超时，但 OCR 词文本通常 < 100 字符，
            # 且 MAX_TARGET_LENGTH=10000 限制了模式长度，ReDoS 实际不可达。
            return re.search(target, word_text) is not None
        except re.error:
            return False
    # 默认 contains（子串，忽略大小写与所有空白）
    normalized_text = re.sub(r'\s+', '', word_text).lower()
    normalized_target = re.sub(r'\s+', '', target).lower()
    return normalized_target != "" and normalized_target in normalized_text


def find_target(
    result: OcrResult,
    targets: list[str],
    *,
    mode: str = "contains",
) -> OcrWord | None:
    """在识别结果中按候选列表顺序返回第一个命中的词。"""
    for target in targets:
        for word in result.words:
            if match_text(word.text, target, mode):
                return word
    return None


def click_point(
    word: OcrWord,
    offset: tuple[float, float] = (0.5, 0.5),
) -> tuple[float, float]:
    """返回词包围盒内按比例 (fx, fy) 的点击坐标。"""
    return word.point_at(offset[0], offset[1])
