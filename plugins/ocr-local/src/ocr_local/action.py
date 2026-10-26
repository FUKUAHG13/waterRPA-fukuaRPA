"""插件动作入口（fukuaRPA 插件契约实现）。

这是 fukuaRPA 宿主进程（P3）加载插件后调用的入口：

* ``run(request)`` 接收 PluginProtocol 的 request 信封，解析出
  ``payload``（即步骤里的 ``plugin_config``），派发到具体动作，
  返回 PluginResponse 形状的字典 ``{"ok", "result", "error"}``。
* ``execute_action(...)`` 是纯逻辑核心，不依赖宿主，便于单元测试与
  独立 CLI 复用。

权限模型（见 manifest.json）：
* ``screen.capture_region`` —— 截图由宿主注入到 payload（P3）。
* ``input.mouse`` —— 真正的点击由宿主按返回的 click_x/click_y 执行，
  以保证「可被急停取消、不残留输入」的约束；独立 CLI 才本地点击。
* 本插件**没有任何网络权限**，OCR 完全离线。
"""

from __future__ import annotations

from typing import Any

from .backend import _BACKEND_CACHE, _BACKEND_LOCK, OcrBackend, create_backend
from .engine import ocr_image
from .matcher import click_point, find_target

# 支持的动作 id 白名单（与 manifest 的 actions 对齐）。
_ACTION_IDS = ("ocr_find_click", "ocr_read_region")


# INVARIANT: 后端缓存受 backend._BACKEND_LOCK 保护，P3 并发宿主下安全。
# FR-20260812-OCR-2: 锁统一收口到 backend.py，此处复用同一把锁——此前
# backend.get_backend() 无锁读写缓存、action._get_backend() 另持锁，两套
# 保护不一致。
# 单例后端本身仍非线程安全，P3 若需真正并发执行同一插件的多个步骤，须改为每请求独立后端。
def _get_backend(name: str = "rapidocr") -> OcrBackend:
    with _BACKEND_LOCK:
        cached = _BACKEND_CACHE.get(name)
        if cached is None:
            cached = create_backend(name)
            _BACKEND_CACHE[name] = cached
        return cached


def _collect_targets(payload: dict[str, Any]) -> list[str]:
    targets: list[str] = []
    for t in payload.get("targets") or []:
        if t:
            targets.append(str(t))
    if payload.get("target_text"):
        targets.append(str(payload["target_text"]))
    return targets


def _resolve_image(payload: dict[str, Any]) -> Any | None:
    """解析图像来源。

    独立模式：``image_path``（本地文件）或 ``image_b64``（base64 PNG）。
    P3 集成模式：宿主把截取到的区域以 ``image_b64`` 注入 payload。
    """
    if payload.get("image_path"):
        return payload["image_path"]
    if payload.get("image_b64"):
        return payload["image_b64"]
    return None


def _response(ok: bool, result: Any, error: str | None = None,
              run_id: str | None = None, step_id: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": ok, "result": result, "error": error}
    # 协议强制字段 run_id/step_id：仅当调用方（run）从信封取出并提供时才回显，
    # 以兼容不携带信封的逻辑核心 execute_action / CLI（对齐 office-docs 行为）。
    if run_id is not None:
        out["run_id"] = run_id
    if step_id is not None:
        out["step_id"] = step_id
    return out


def execute_action(
    plugin_id: str,
    action_id: str,
    payload: dict[str, Any],
    *,
    backend: OcrBackend | None = None,
    backend_name: str = "rapidocr",
    run_id: str | None = None,
    step_id: str | None = None,
) -> dict[str, Any]:
    """执行一个 OCR 动作，返回 PluginResponse 形状的字典。"""

    if action_id not in _ACTION_IDS:
        return _response(False, None, f"未知动作：{action_id}", run_id=run_id, step_id=step_id)

    image = _resolve_image(payload)
    if image is None:
        return _response(
            False,
            None,
            "未提供图像：独立模式请传 image_path/image_b64；P3 集成模式由宿主注入截图",
            run_id=run_id, step_id=step_id,
        )

    score_threshold = float(payload.get("score_threshold", 0.5))
    if backend is None:
        try:
            backend = _get_backend(backend_name)
        except Exception as exc:  # noqa: BLE001 后端构造失败不应让宿主崩溃
            return _response(False, None, f"无法创建 OCR 后端：{exc}", run_id=run_id, step_id=step_id)
    try:
        result = ocr_image(
            image,
            backend=backend,
            backend_name=backend_name,
            score_threshold=score_threshold,
        )
    except Exception as exc:  # 推理失败不应让宿主/运行崩溃
        return _response(False, None, f"OCR 推理失败：{exc}", run_id=run_id, step_id=step_id)

    words_dict = result.to_dict()["words"]

    # 动作：OCR 读取区域文字（不点击）
    if action_id == "ocr_read_region":
        return _response(
            True,
            {"found": bool(result.words), "text": result.text, "words": words_dict},
            run_id=run_id, step_id=step_id,
        )

    # 动作：OCR 找字并点击（或仅定位）
    targets = _collect_targets(payload)
    if not targets:
        return _response(
            True,
            {
                "found": False,
                "words": words_dict,
                "note": "未指定查找目标，仅返回识别结果",
            },
            run_id=run_id, step_id=step_id,
        )

    mode = payload.get("match_mode", "contains")
    word = find_target(result, targets, mode=mode)
    if word is None:
        return _response(
            True,
            {"found": False, "words": words_dict, "note": "未找到目标文本"},
            run_id=run_id, step_id=step_id,
        )

    offset = payload.get("click_offset") or [0.5, 0.5]
    if not isinstance(offset, (list, tuple)) or len(offset) < 2:
        offset = [0.5, 0.5]
    cx, cy = click_point(word, (float(offset[0]), float(offset[1])))
    out: dict[str, Any] = {
        "found": True,
        "matched_text": word.text,
        "click_x": round(cx, 1),
        "click_y": round(cy, 1),
        "score": round(word.score, 4),
        "words": words_dict,
    }
    # 坐标契约（修复潜在 P1）：click_x/click_y 默认是输入图像（P3 模式为区域截图）
    # 的局部坐标。若宿主在 payload 提供区域屏幕原点 region_origin([x, y])，则叠加为
    # 绝对屏幕坐标（向前兼容 P3 宿主）；否则标注坐标为局部，宿主须自行叠加区域原点。
    region_origin = payload.get("region_origin")
    if isinstance(region_origin, (list, tuple)) and len(region_origin) == 2:
        try:
            ox, oy = float(region_origin[0]), float(region_origin[1])
            out["click_x"] = round(cx + ox, 1)
            out["click_y"] = round(cy + oy, 1)
            out["coordinate_space"] = "absolute"
        except (TypeError, ValueError):
            out["coordinate_space"] = "local"
            out["note"] = "region_origin 非法，坐标保持为区域局部坐标，宿主须叠加区域原点"
    else:
        out["coordinate_space"] = "local"
        out["note"] = "坐标为区域局部坐标，宿主须叠加区域原点后点击"
    var = payload.get("set_variable")
    if var:
        # P3 宿主据此把匹配文本写入声明过的变量；独立模式仅回显。
        out["variable_value"] = word.text
    return _response(True, out, run_id=run_id, step_id=step_id)


def run(request: dict[str, Any]) -> dict[str, Any]:
    """宿主进程（P3）调用入口。

    入参为 PluginProtocol 的 request 信封；返回 PluginResponse 形状字典。
    """
    # 信封里的 run_id/step_id 需回显到响应（plugin_protocol 响应分支强制），
    # 与 office-docs 保持一致：优先取信封根（payload），回退取请求根。
    run_id = None
    step_id = None
    try:
        payload = request.get("payload") or {}
        run_id = payload.get("run_id") or request.get("run_id")
        step_id = payload.get("step_id") or request.get("step_id")
        # 契约标准信封（plugin_protocol.make_request）把 action_id / plugin_id
        # 嵌套在 request["payload"] 内；真正的 plugin_config 在 payload.payload。
        # 扁平形态（无嵌套 payload）则参数直接位于 payload / 请求根，去掉协议元数据键。
        plugin_id = payload.get("plugin_id") or request.get("plugin_id", "")
        action_id = payload.get("action_id") or request.get("action_id", "")
        config = payload.get("payload")
        if not isinstance(config, dict):
            _META = {"run_id", "step_id", "plugin_id", "action_id", "deadline_ns",
                     "protocol", "version", "kind"}
            source = payload if isinstance(payload, dict) and payload else request
            config = {k: v for k, v in source.items() if k not in _META}
        if action_id not in _ACTION_IDS:
            return _response(False, None, f"未知动作：{action_id}", run_id=run_id, step_id=step_id)
        return execute_action(plugin_id, action_id, config, run_id=run_id, step_id=step_id)
    except Exception as exc:  # 绝不向外抛出，统一转成 error 响应
        return _response(False, None, f"动作执行异常：{exc}", run_id=run_id, step_id=step_id)
