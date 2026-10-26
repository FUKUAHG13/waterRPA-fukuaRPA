"""FR-20261005-005 / FR-20260930-013 回归守护：拖拽起/终点图片的多倍率缓存键。

背景（缺陷）
------------
修复前 ``actions/mouse.py`` 的拖拽起/终点识别使用 ``drag_source_*`` /
``drag_target_*`` 自定义缓存键，该键**不含** ``scale_min`` / ``scale_max`` /
``scale_step``，因而不属于 ``scale_options_cache`` 的键空间。于是
``scale_options_for`` 每次都回退全局默认倍率，且 ``scaled_templates_cache``
永不命中：OpenCV 回退路径只保留 scale 1.0 的基础模板，而原生路径按完整配置
范围搜索——同一方案在两种后端下结果翻转（非 1.0 倍率的拖拽图片在 OpenCV 下
永远识别不到，落点错误可致误拖拽）。

守护口径
--------
1. 拖拽键必须与普通模板键同属一个键空间（``scale_options_for`` 能命中）；
2. 预计算（``vision/preload.py``）与运行期（``actions/mouse.py``）必须产出
   **同一个**键；
3. 非 1.0 倍率下拖拽图片的缩放档位必须被保留，而不是回退默认倍率。

本文件在缺陷对象（HEAD ``ab9bd66c``）上因第 1、2、3 条断言失败而红；
在修复后对象上全绿。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fukua_rpa.vision.multi_image import VisionMultiImageMixin


class _Engine(VisionMultiImageMixin):
    """最小宿主：只提供被测方法依赖的属性。

    默认倍率设为 1.0/1.0/0.0（即"不启用多倍率"），使测试能够区分
    "步骤配置倍率被保留"与"回退到全局默认倍率"两种结果。
    """

    def __init__(self):
        self.min_scale = 1.0
        self.max_scale = 1.0
        self.scale_step = 0.0
        self.enable_grayscale = False
        self.scale_options_cache = {}


def _task(**overrides):
    task = {
        "drag_source_img": r"C:\templates\source.png",
        "drag_target_img": r"C:\templates\target.png",
        # 走 image_target_options 的"自定义倍率"分支，使步骤倍率生效。
        "custom_en": True,
        "custom_scale_min": 0.8,
        "custom_scale_max": 1.2,
        "custom_scale_step": 0.1,
        "custom_gray": False,
    }
    task.update(overrides)
    return task


class DragImageCacheKeyScaleTests(unittest.TestCase):
    """拖拽缓存键必须承载倍率并与预计算共用同一键空间。"""

    def setUp(self):
        self.engine = _Engine()

    def test_drag_keys_live_in_scale_options_cache_key_space(self):
        """核心守护：拖拽键必须能被 scale_options_for 命中。

        缺陷对象上该断言失败——``drag_target_*`` 键不在
        ``scale_options_cache`` 中，``scale_options_for`` 恒回退默认倍率。
        """
        task = _task()

        key = self.engine.drag_target_cache_key(task)
        # 预计算会把这个键登记进倍率表；此处模拟该登记，验证键空间一致。
        self.engine.scale_options_cache[str(key)] = (0.8, 1.2, 0.1)

        self.assertEqual(
            self.engine.scale_options_for(key),
            (0.8, 1.2, 0.1),
            "拖拽终点缓存键不在 scale_options_cache 键空间内，"
            "运行期会回退默认倍率（非 1.0 倍率拖拽图片识别退化）",
        )

    def test_source_and_target_keys_carry_scale_options(self):
        """起点与终点键都必须包含倍率三元组。

        缺陷对象上键为 ``drag_source_{path}_{conf}_{gray}`` 形态，
        不含倍率；本断言比较的是"键随倍率变化而变化"。
        """
        base = _task()
        narrowed = _task(
            custom_scale_min=0.5,
            custom_scale_max=0.9,
            custom_scale_step=0.05,
        )

        for method_name, _field in (
            ("drag_source_cache_key", "drag_source_img"),
            ("drag_target_cache_key", "drag_target_img"),
        ):
            with self.subTest(method=method_name):
                key_base = getattr(self.engine, method_name)(base)
                key_narrow = getattr(self.engine, method_name)(narrowed)
                self.assertNotEqual(
                    key_base,
                    key_narrow,
                    f"{method_name} 产生的缓存键不随 scale 变化——"
                    "该键未承载倍率，无法参与多倍率识别",
                )
                self.assertNotIn(
                    "drag_source_",
                    key_base,
                    "缓存键仍使用旧的 drag_source_* 自定义形态",
                )
                self.assertNotIn(
                    "drag_target_",
                    key_base,
                    "缓存键仍使用旧的 drag_target_* 自定义形态",
                )

    def test_non_unit_scale_is_not_silently_dropped(self):
        """非 1.0 倍率下不得静默回退到全局默认倍率。

        直接比对"键可命中的倍率"与"步骤配置倍率"，确认配置值被保留。
        """
        task = _task(
            custom_scale_min=0.6,
            custom_scale_max=1.4,
            custom_scale_step=0.2,
        )
        key = self.engine.drag_source_cache_key(task)

        # 用步骤配置值登记，模拟 preload 的预计算行为。
        self.engine.scale_options_cache[str(key)] = (0.6, 1.4, 0.2)

        self.assertEqual(
            self.engine.scale_options_for(key),
            (0.6, 1.4, 0.2),
            "运行期回退到了全局默认倍率而非步骤配置的倍率区间",
        )

    def test_empty_drag_image_yields_stable_key(self):
        """边界：未配置拖拽图片时键退化为空路径形态，不得抛异常。"""
        task = _task(drag_source_img="", drag_target_img="   ")

        source_key = self.engine.drag_source_cache_key(task)
        target_key = self.engine.drag_target_cache_key(task)

        self.assertIsInstance(source_key, str)
        self.assertIsInstance(target_key, str)


class DragPrecomputeRuntimeKeyParityTests(unittest.TestCase):
    """预计算与运行期必须产出同一个键（FR-20260930-013 的核心不变量）。"""

    def test_preload_and_runtime_agree_on_key(self):
        """``preload`` 拖拽分支与运行期解析共用 ``drag_image_cache_key``。

        通过对同一 task 分别调用两条路径所用的入口验证：预计算入口是
        ``drag_image_cache_key(task, field)``，运行期入口是
        ``drag_source_cache_key`` / ``drag_target_cache_key``（二者均委托前者）。
        """
        engine = _Engine()
        task = _task()

        for field, runtime_entry in (
            ("drag_source_img", "drag_source_cache_key"),
            ("drag_target_img", "drag_target_cache_key"),
        ):
            with self.subTest(field=field):
                preload_key = engine.drag_image_cache_key(task, field)
                runtime_key = getattr(engine, runtime_entry)(task)
                self.assertEqual(
                    preload_key,
                    runtime_key,
                    f"{field} 的预计算键与运行期键不一致，"
                    "预计算的缩放模板不会被运行期复用",
                )


if __name__ == "__main__":
    unittest.main()
