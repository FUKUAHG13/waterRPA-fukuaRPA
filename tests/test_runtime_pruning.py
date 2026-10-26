import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.runtime_pruning import (
    PRUNE_OPENGL32SW,
    PYINSTALLER_EXCLUDES,
    apply_runtime_pruning,
    find_prunable_runtime_paths,
    pruning_group,
)


class _FakeAnalysis:
    def __init__(self, binaries, datas):
        self.binaries = list(binaries)
        self.datas = list(datas)


class RuntimePruningTests(unittest.TestCase):
    def test_high_confidence_payloads_are_classified(self):
        cases = {
            "cv2/opencv_videoio_ffmpeg4130_64.dll": "opencv_ffmpeg",
            "PySide6/Qt6Quick.dll": "qt_quick_qml_pdf_virtual_keyboard",
            "PySide6/plugins/imageformats/qpdf.dll": (
                "qt_quick_qml_pdf_virtual_keyboard"
            ),
            "PySide6/plugins/platforminputcontexts/qtvirtualkeyboardplugin.dll": (
                "qt_quick_qml_pdf_virtual_keyboard"
            ),
            "PySide6/translations/qtbase_de.qm": "non_chinese_qt_translations",
            "PIL/_avif.cp314-win_amd64.pyd": "pillow_optional_extensions",
            "PIL/_imagingft.cp314-win_amd64.pyd": "pillow_optional_extensions",
            "_tkinter.pyd": "tcl_tk",
            "_tcl_data/tcl8.6/init.tcl": "tcl_tk",
            "PySide6/plugins/platforms/qdirect2d.dll": "qt_unused_platform_plugins",
            "PySide6/plugins/platforms/qminimal.dll": "qt_unused_platform_plugins",
            "PySide6/plugins/imageformats/qgif.dll": "qt_unused_imageformat_plugins",
            "PySide6/plugins/imageformats/qtiff.dll": "qt_unused_imageformat_plugins",
            "PySide6/plugins/imageformats/qtga.dll": "qt_unused_imageformat_plugins",
            "PySide6/plugins/imageformats/qwbmp.dll": "qt_unused_imageformat_plugins",
            "PySide6/plugins/imageformats/qicns.dll": "qt_unused_imageformat_plugins",
            "PySide6/plugins/imageformats/qwebp.dll": "qt_unused_imageformat_plugins",
            "icuuc.dll": "foreign_icu_payloads",
            "_internal/icuin.dll": "foreign_icu_payloads",
            "_internal/icudt78.dll": "foreign_icu_payloads",
        }
        for destination, expected in cases.items():
            with self.subTest(destination=destination):
                self.assertEqual(
                    pruning_group((destination, "source", "BINARY")), expected
                )

    def test_required_and_compatibility_payloads_are_retained(self):
        retained = (
            "PySide6/Qt6Core.dll",
            "PySide6/Qt6Gui.dll",
            "PySide6/Qt6Widgets.dll",
            "PySide6/opengl32sw.dll",
            "PySide6/plugins/platforms/qwindows.dll",
            "PySide6/plugins/platforms/qoffscreen.dll",
            "PySide6/plugins/imageformats/qjpeg.dll",
            "PySide6/plugins/imageformats/qpng.dll",
            "PySide6/plugins/imageformats/qsvg.dll",
            "PySide6/plugins/imageformats/qico.dll",
            "PySide6/translations/qtbase_zh_CN.qm",
            "PySide6/translations/qtbase_zh_TW.qm",
            "PIL/_imaging.cp314-win_amd64.pyd",
            "PySide6/icuuc.dll",
        )
        for destination in retained:
            with self.subTest(destination=destination):
                self.assertIsNone(
                    pruning_group((destination, "source", "BINARY"))
                )

    def test_software_opengl_is_retained_pending_validation(self):
        """Mesa software OpenGL must stay until RDP/VM/legacy-GPU validation.

        opengl32sw.dll 是缺失/损坏 GPU 驱动、RDP 会话与虚拟机环境的回退路径。
        在未完成上述验证前，裁剪开关必须保持关闭，断言同时守护开关与判定结果。
        """
        self.assertFalse(
            PRUNE_OPENGL32SW,
            "软件 OpenGL 尚未完成 RDP/VM/旧 GPU 验证，不得提前开启裁剪",
        )
        for destination in ("PySide6/opengl32sw.dll", "_internal/opengl32sw.dll"):
            with self.subTest(destination=destination):
                self.assertIsNone(
                    pruning_group((destination, "source", "BINARY"))
                )

    def test_network_ssl_payloads_are_pruned(self):
        """SSL payloads must be pruned: the product never talks to the network.

        “本程序不联网”是本项目的既定设计（见 ARCHITECTURE 与 PYINSTALLER_EXCLUDES
        中的 `_ssl`、`PySide6.QtNetwork`）。因此 SSL 载荷若出现在分析中应被裁剪，
        而不是被保留——这与其对构建产物的“保留”方向相反。
        """
        for name in ("_ssl.pyd", "libssl-3-x64.dll", "libssl-3.dll"):
            with self.subTest(name=name):
                self.assertEqual(
                    pruning_group((name, "source", "BINARY")),
                    "network_ssl_payloads",
                )
        # 排除项必须持续覆盖网络栈，防止后续依赖无意引入联网能力。
        self.assertTrue({"_ssl", "PySide6.QtNetwork"}.issubset(PYINSTALLER_EXCLUDES))

    def test_analysis_payloads_are_pruned_without_reordering_survivors(self):
        analysis = _FakeAnalysis(
            [
                ("PySide6/Qt6Core.dll", "core", "BINARY"),
                ("PySide6/Qt6Pdf.dll", "pdf", "BINARY"),
                ("_tkinter.pyd", "tk", "BINARY"),
                ("PySide6/plugins/platforms/qwindows.dll", "win", "BINARY"),
                ("icuuc.dll", "agent-poppler", "BINARY"),
                ("icudt78.dll", "agent-poppler-data", "BINARY"),
            ],
            [
                ("PySide6/translations/qtbase_fr.qm", "fr", "DATA"),
                ("PySide6/translations/qtbase_zh_CN.qm", "zh", "DATA"),
                ("_tk_data/tk.tcl", "tk-data", "DATA"),
            ],
        )
        summary = apply_runtime_pruning(analysis)
        self.assertEqual(
            [entry[0] for entry in analysis.binaries],
            ["PySide6/Qt6Core.dll", "PySide6/plugins/platforms/qwindows.dll"],
        )
        self.assertEqual(
            [entry[0] for entry in analysis.datas],
            ["PySide6/translations/qtbase_zh_CN.qm"],
        )
        self.assertEqual(summary["qt_quick_qml_pdf_virtual_keyboard"], 1)
        self.assertEqual(summary["non_chinese_qt_translations"], 1)
        self.assertEqual(summary["tcl_tk"], 2)
        self.assertEqual(summary["foreign_icu_payloads"], 2)

    def test_tk_helpers_are_excluded_at_analysis_time(self):
        self.assertTrue(
            {"_tkinter", "mouseinfo", "pymsgbox", "tkinter"}.issubset(
                PYINSTALLER_EXCLUDES
            )
        )
        # PyScreeze imports ImageDraw -> ImageText -> ImageFont even when no
        # font rendering is requested. The optional _imagingft binary can still
        # be removed because ImageFont handles its absence gracefully.
        self.assertNotIn("PIL.ImageFont", PYINSTALLER_EXCLUDES)

    def test_frozen_directory_guard_finds_reintroduced_payloads(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            retained = root / "_internal" / "PySide6" / "plugins" / "platforms"
            retained.mkdir(parents=True)
            (retained / "qwindows.dll").write_bytes(b"required")
            removed = root / "_internal" / "cv2"
            removed.mkdir(parents=True)
            (removed / "opencv_videoio_ffmpeg4130_64.dll").write_bytes(b"unused")
            (root / "_internal" / "icuuc.dll").write_bytes(b"foreign-icu")
            self.assertEqual(
                find_prunable_runtime_paths(root),
                [
                    "_internal/cv2/opencv_videoio_ffmpeg4130_64.dll",
                    "_internal/icuuc.dll",
                ],
            )


if __name__ == "__main__":
    unittest.main()
