"""OCR 结果数据模型。

纯数据层，仅依赖标准库，不依赖任何 OCR 引擎或 fukuaRPA 运行时，
可独立导入与测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 单字/词的四点包围盒，每个点是 (x, y) 浮点坐标（屏幕物理像素）。
Box4 = list[tuple[float, float]]


@dataclass
class OcrWord:
    """一个被识别出的文本片段。"""

    text: str
    box: Box4
    score: float

    @property
    def xs(self) -> list[float]:
        return [p[0] for p in self.box]

    @property
    def ys(self) -> list[float]:
        return [p[1] for p in self.box]

    @property
    def left(self) -> float:
        return min(self.xs)

    @property
    def right(self) -> float:
        return max(self.xs)

    @property
    def top(self) -> float:
        return min(self.ys)

    @property
    def bottom(self) -> float:
        return max(self.ys)

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.bottom - self.top

    @property
    def center(self) -> tuple[float, float]:
        """包围盒几何中心（物理像素）。"""
        return (self.left + self.right) / 2.0, (self.top + self.bottom) / 2.0

    def point_at(self, fx: float, fy: float) -> tuple[float, float]:
        """返回包围盒内按比例 (fx, fy) 的点。

        (0,0)=左上，(1,1)=右下，(0.5,0.5)=中心。用于把"点击目标文字"
        映射成具体的点击坐标。
        """
        return (
            self.left + self.width * max(0.0, min(1.0, fx)),
            self.top + self.height * max(0.0, min(1.0, fy)),
        )


@dataclass
class OcrResult:
    """一次 OCR 的完整结果。"""

    words: list[OcrWord] = field(default_factory=list)
    backend: str = ""
    note: str = ""

    @property
    def text(self) -> str:
        """按从上到下、从左到右拼接的全文。"""
        return "\n".join(w.text for w in self.words)

    def to_dict(self) -> dict:
        return {
            "backend": self.backend,
            "note": self.note,
            "words": [
                {
                    "text": w.text,
                    "score": round(w.score, 4),
                    "box": [[round(x, 1), round(y, 1)] for (x, y) in w.box],
                    "center": [round(c, 1) for c in w.center],
                }
                for w in self.words
            ],
        }
