"""识别/匹配失败的统一分类与错误类型。

作为“统一失败类别枚举”（架构级重构路线 §3.9）的落地基础：把原先仅靠
``None`` / ``[]`` 隐式区分的失败语义，显式归类为可枚举的类别，供调用方
据此决策。

本阶段只引入类型与最小内省（在 ``find_template`` 的返回路径上记录
``last_failure_category``），**不改变任何对外返回语义**；调用方据类别分支
的切换留到 P4（engine_vision 重构）在触碰调用点时统一处理。
"""

from __future__ import annotations

from enum import Enum


class MatchFailure(Enum):
    """原生模板匹配的失败/结果类别。

    注意：``MATCHED`` 与 ``NO_MATCH`` 是成功路径的分类，``UNAVAILABLE`` /
    ``CANCELLED`` / ``MISSING_RESOURCE`` / ``CORRUPT_RESOURCE`` 是失败路径。
    ``None`` 返回值本身仍表示“后端不可用/异常/取消”，``[]`` 仍表示权威未命中，
    本阶段保持这一返回契约不变。
    """

    MATCHED = "matched"
    NO_MATCH = "no_match"
    UNAVAILABLE = "unavailable"
    CANCELLED = "cancelled"
    MISSING_RESOURCE = "missing_resource"
    CORRUPT_RESOURCE = "corrupt_resource"


class ImageResourceError(Exception):
    """图像资源缺失或损坏，无法执行识别。

    当前阶段 ``find_template`` 仍对缺失/损坏返回 ``None``（保持向后兼容），
    待 P4 在调用方统一切换为抛出/分类处理时再启用本异常。
    """
