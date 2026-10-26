"""OCR 后端抽象与本地实现。

设计要点：
* ``OcrBackend`` 是稳定接口，fukuaRPA 集成层（P3 宿主进程）与独立 CLI
  都只依赖这个接口，不关心底层引擎。
* 默认实现 ``RapidOcrBackend`` 使用 rapidocr-onnxruntime，纯本地推理、
  模型随 pip 包一并安装，安装后完全离线，不需要任何网络请求——
  这与 fukuaRPA「本程序不联网」的产品边界一致（v1 插件清单也没有网络权限）。
* ``MockBackend`` 仅供单元测试，避免测试依赖重型的 OCR 运行时。
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Union

from .models import OcrResult, OcrWord

if TYPE_CHECKING:  # 仅类型检查期可见；运行期不导入，numpy 保持为可选依赖
    import numpy

# numpy 改为惰性导入：仅在实际加载图像 / 构造真实后端时才需要，
# 这样 `import ocr_local` 不强制依赖 numpy，便于在轻量环境导入与测试。
ImageInput = Union[str, "numpy.ndarray"]


class OcrBackend(ABC):
    """识别后端统一接口。"""

    name: str = "abstract"

    @abstractmethod
    def recognize(self, image: ImageInput) -> OcrResult:
        """对图像做检测+识别，返回 ``OcrResult``。"""
        raise NotImplementedError


class RapidOcrBackend(OcrBackend):
    """基于 rapidocr-onnxruntime 的本地 OCR 后端（中英文，离线）。"""

    name = "rapidocr_onnxruntime"

    def __init__(self, **engine_kwargs) -> None:
        from rapidocr_onnxruntime import RapidOCR

        # RapidOCR 默认会把模型放在包内，首次构造即加载，之后纯本地推理。
        self._engine = RapidOCR(**engine_kwargs)

    def recognize(self, image: ImageInput) -> OcrResult:
        boxes, txts, scores = self._engine(image)
        words: list[OcrWord] = []
        if boxes is not None and txts is not None and scores is not None:
            for box, text_list, score_list in zip(boxes, txts, scores, strict=False):
                text = text_list[0] if text_list else ""
                score = float(score_list[0]) if score_list else 0.0
                # box 可能是 np.ndarray，统一成 [(x,y), ...] 浮点列表。
                pts = [(float(p[0]), float(p[1])) for p in box]
                words.append(OcrWord(text=text, box=pts, score=score))
        return OcrResult(words=words, backend=self.name)


class MockBackend(OcrBackend):
    """测试用后端：返回固定结果，不加载任何模型。"""

    name = "mock"

    def __init__(self, words: list[OcrWord] | None = None) -> None:
        self._words = words or [
            OcrWord(text="确定", box=[(100, 100), (200, 100), (200, 140), (100, 140)], score=0.98),
            OcrWord(text="取消", box=[(300, 100), (400, 100), (400, 140), (300, 140)], score=0.97),
            OcrWord(text="用户名", box=[(100, 200), (220, 200), (220, 240), (100, 240)], score=0.95),
        ]

    def recognize(self, image: ImageInput) -> OcrResult:
        return OcrResult(words=list(self._words), backend=self.name)


_BACKEND_REGISTRY = {
    "rapidocr": RapidOcrBackend,
    "mock": MockBackend,
}


def create_backend(name: str = "rapidocr", **kwargs) -> OcrBackend:
    """按名字构造后端，便于将来接入 tesseract / paddle 等其它本地引擎。"""
    cls = _BACKEND_REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"未知 OCR 后端：{name!r}（可选：{sorted(_BACKEND_REGISTRY)}）")
    return cls(**kwargs)


# --------------------------------------------------------------------------
# 进程级单例缓存与生命周期管理
# --------------------------------------------------------------------------

# 进程级单例缓存：RapidOCR 首次构造会加载模型，避免每次动作都重载。
_BACKEND_CACHE: dict[str, OcrBackend] = {}

# FR-20260812-OCR-2: 缓存唯一并发控制锁。此前 backend.get_backend() 无锁
# 读写 _BACKEND_CACHE，action._get_backend() 另持 _BACKEND_LOCK 读写同一变量，
# 两套保护不一致（单例后端本身仍非线程安全，锁只保证构造/读写缓存原子）。
_BACKEND_LOCK = threading.Lock()

# 缓存统计：量化命中/未命中与首次加载耗时，便于诊断性能问题。
# first_load_ms 只记录第一次真正构造后端（缓存未命中）的耗时，0.0 表示尚未发生。
_CACHE_STATS = {"hits": 0, "misses": 0, "first_load_ms": 0.0}


def get_backend(name: str = "rapidocr") -> OcrBackend:
    """按名字取后端，进程内复用同一实例（单例）。

    FR-20260812-OCR-2: 与 action._get_backend 共用 _BACKEND_LOCK，保证
    _BACKEND_CACHE 的读写受同一把锁保护。
    """
    with _BACKEND_LOCK:
        cached = _BACKEND_CACHE.get(name)
        if cached is not None:
            _CACHE_STATS["hits"] += 1
            return cached
        _CACHE_STATS["misses"] += 1
        t0 = time.perf_counter()
        backend = create_backend(name)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if _CACHE_STATS["first_load_ms"] == 0.0:
            _CACHE_STATS["first_load_ms"] = elapsed_ms
        _BACKEND_CACHE[name] = backend
        return backend


def get_cache_stats() -> dict[str, float]:
    """返回缓存统计（供诊断/测试使用）。"""
    return dict(_CACHE_STATS)


def close_all() -> None:
    """释放所有缓存的后端实例并清空缓存。

    生命周期：本模块持有进程级单例缓存；宿主（P3）退出时通过 ``atexit`` 自动调用
    本函数，释放模型/句柄以避免资源泄漏。函数幂等——重复调用安全（第二次为空操作）。

    每个后端若实现 ``close()`` 则调用之（如将来新增需要显式释放的后端），否则仅丢弃
    引用交由 GC 回收。
    """
    for backend in list(_BACKEND_CACHE.values()):
        try:
            close = getattr(backend, "close", None)
            if callable(close):
                close()
        except Exception:  # noqa: BLE001 单个后端释放失败不影响其它
            pass
    _BACKEND_CACHE.clear()


try:
    import atexit
    atexit.register(close_all)
except Exception:  # noqa: BLE001 极少数无 atexit 的环境（如受限嵌入式解释器）
    pass
