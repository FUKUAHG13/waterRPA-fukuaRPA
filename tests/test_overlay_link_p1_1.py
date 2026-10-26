"""J-36 回归固化：P1-1（覆盖匹配小图 ``until_cond{index}_overlay_image`` 引用枚举）已修复。

背景
----
``task_images.iter_task_image_refs`` 此前只枚举 ``until_cond{index}_image`` 等
模板图字段，**漏掉** 了覆盖匹配小图 ``until_cond{index}_overlay_image``。P1-1 修复后，
overlay 小图与基础图被同等枚举（``kind="condition"``，容器为 ``task`` 自身），
使得以下消费者不再静默遗漏它：

* 可移植路径解析 / 重写（``profile_paths.rewrite_profile_image_paths``）；
* 完整包导出资产收集（``profile_package.collect_profile_image_paths``）；
* 隐私脱敏（``workflow_security.sanitize_profile_for_sharing``）。

本文件锁定的断言
----------------
  * 迭代器 **包含** overlay 引用，且紧跟其基础图（枚举顺序约束），kind 复用 ``condition``；
  * 导出资产收集 ``collect_profile_image_paths`` **包含** overlay 小图；
  * 可移植路径重写 ``rewrite_profile_image_paths`` 能改写 overlay 字段（验证 container/key
    回写正确），且为纯函数不改动原配置。

关于“脱敏清空 overlay”的已知缺口
---------------------------------
当前 ``sanitize_profile_for_sharing``（workflow_security.py:334-348）只清空
``until_cond{index}_image`` / ``drag_*_img`` / ``multi_image_actions``，**并未**清空
``until_cond{index}_overlay_image``。这属于 P1-1 的后续脱敏修复项，尚未落地，因此此处
**不**对其断言（避免把绿色基线写成红灯）。待该脱敏修复合入后，应在此补充
``test_redaction_clears_overlay``，与 R-1 的 cap 一样作为“未来扩展”基线。
"""

from __future__ import annotations

import unittest

from fukua_rpa.profile_package import (
    collect_profile_image_paths,
    rewrite_profile_image_paths,
)
from fukua_rpa.task_images import iter_task_image_paths, iter_task_image_refs


class OverlayLinkP1_1Tests(unittest.TestCase):
    def test_iterator_includes_overlay_refs(self):
        """迭代器包含 overlay 引用，且紧跟其基础图、kind 复用 condition。"""
        task = {
            "type": 15.0,
            "until_cond1_image": "base.png",
            "until_cond1_overlay_image": "overlay.png",
            "until_cond2_overlay_image": "overlay2.png",
        }
        refs = list(iter_task_image_refs(task))
        keys = [k for (_kind, _c, k, _p) in refs]

        self.assertIn("until_cond1_image", keys)
        self.assertIn("until_cond1_overlay_image", keys)
        self.assertIn("until_cond2_overlay_image", keys)

        # overlay 必须紧跟其基础图（枚举顺序不容错乱）
        self.assertEqual(
            keys.index("until_cond1_overlay_image"),
            keys.index("until_cond1_image") + 1,
        )

        # overlay 的 kind 复用 condition（与基础图一致，便于统一回写）
        kinds = {k: kind for (kind, _c, k, _p) in refs}
        self.assertEqual(kinds["until_cond1_overlay_image"], "condition")
        self.assertEqual(kinds["until_cond2_overlay_image"], "condition")

        # iter_task_image_paths 同样应产出 overlay 路径
        self.assertIn("overlay.png", list(iter_task_image_paths(task)))
        self.assertIn("overlay2.png", list(iter_task_image_paths(task)))

    def test_export_collection_includes_overlay(self):
        """完整包导出资产收集必须包含 overlay 小图（不遗漏）。"""
        config = {
            "tasks": [
                {
                    "type": 15.0,
                    "until_cond1_image": "base.png",
                    "until_cond1_overlay_image": "overlay.png",
                }
            ]
        }
        paths = collect_profile_image_paths(config)
        self.assertIn("base.png", paths)
        self.assertIn("overlay.png", paths)

    def test_rewrite_resolves_overlay_path(self):
        """可移植路径重写能改写 overlay 字段，且为纯函数不改动原配置。"""
        config = {
            "tasks": [
                {
                    "type": 15.0,
                    "until_cond1_image": "base.png",
                    "until_cond1_overlay_image": "overlay.png",
                }
            ]
        }

        def mapper(path: str) -> str:
            return "C:/assets/" + path

        out = rewrite_profile_image_paths(config, mapper)
        task = out["tasks"][0]
        self.assertEqual(task["until_cond1_image"], "C:/assets/base.png")
        self.assertEqual(task["until_cond1_overlay_image"], "C:/assets/overlay.png")

        # 原配置不被改动（纯函数语义）
        self.assertEqual(config["tasks"][0]["until_cond1_overlay_image"], "overlay.png")
        self.assertEqual(config["tasks"][0]["until_cond1_image"], "base.png")


if __name__ == "__main__":
    unittest.main()
