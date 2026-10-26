"""录制后端抽象与三种实现。

* ``MssOpencvBackend``（默认真实后端）：用 ``mss`` 抓屏、``opencv`` 在后台线程编码为
  视频。完全离线、可在 Python 层精确控制帧率与暂停/恢复，无需系统 ffmpeg。
  依赖 ``mss`` 与 ``opencv-python``（需用户 ``pip install``）。所有重导入均为**惰性**，
  因此本插件在没装这两个库时也能被 import、被 fukuaRPA 列出/校验，只是运行该动作时
  才会提示安装。
* ``FfmpegBackend``：调用系统 ``ffmpeg``（gdigrab）做高质量录屏，画质优于 opencv 软编码。
  需要系统 PATH 中存在 ``ffmpeg`` 可执行文件。由于 ffmpeg 通过子进程管线录制，暂停/
  恢复受外部程序限制，本后端仅实现 start/stop/snapshot；pause/resume 会返回明确的不
  支持错误。
* ``MockRecorderBackend``：内置状态机与占位文件，用于单元测试与无依赖环境的逻辑冒烟，
  完全不依赖任何第三方库。

信任边界：屏幕录制/截图为纯本地行为，不涉及任何对外网络通信，符合 fukuaRPA「不联网」
承诺；本插件不申请任何网络权限。
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from abc import ABC, abstractmethod

from .models import STATE_IDLE, STATE_PAUSED, STATE_RECORDING, RecorderStatus


def _region_to_mss_bbox(region: dict[str, int] | None, full: dict[str, int]) -> dict[str, int]:
    """把插件 region({x,y,width,height}) 规整为 mss 抓屏 bbox。

    ``full`` 为整屏 bbox（含 left/top，多显示器时可能非零）。未给 region 时返回整屏。
    """
    if not region:
        return dict(full)
    return {
        "left": int(region.get("x", full.get("left", 0))),
        "top": int(region.get("y", full.get("top", 0))),
        "width": int(region.get("width", full.get("width", 0))),
        "height": int(region.get("height", full.get("height", 0))),
    }


def _fourcc_for(fmt: str) -> str:
    fmt = (fmt or "mp4").lower()
    if fmt == "avi":
        return "XVID"
    # mp4 / mkv / webm 等统一回退到 mp4v（opencv 软编码兼容性最好）。
    return "mp4v"


# --------------------------------------------------------------------------
# 抽象后端
# --------------------------------------------------------------------------

class RecorderBackend(ABC):
    """录制动作的底层能力。实现层自行保证异常可被上层捕获。"""

    @abstractmethod
    def start(self, output_path: str, fps: int, region: dict[str, int] | None, audio: bool) -> RecorderStatus:
        """开始录制，返回状态快照。"""

    @abstractmethod
    def pause(self) -> RecorderStatus:
        """暂停录制。"""

    @abstractmethod
    def resume(self) -> RecorderStatus:
        """恢复录制。"""

    @abstractmethod
    def stop(self) -> RecorderStatus:
        """停止录制，返回最终状态（含时长/帧数/字节数）。"""

    @abstractmethod
    def snapshot(self, path: str, region: dict[str, int] | None) -> str:
        """抓一帧静态图保存到 path，返回实际保存路径。"""

    @abstractmethod
    def status(self) -> RecorderStatus:
        """当前状态快照。"""

    @abstractmethod
    def close(self) -> None:
        """释放资源（如仍在录制则先停止）。"""


# --------------------------------------------------------------------------
# 真实后端 1：mss + opencv（默认可控，支持暂停/恢复）
# --------------------------------------------------------------------------

class MssOpencvBackend(RecorderBackend):
    def __init__(self) -> None:
        self._status = RecorderStatus()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread: threading.Thread | None = None
        self._writer = None
        self._bbox: dict[str, int] | None = None
        self._capture_errors = 0  # 逐帧采集失败计数（用于取证完整性判断）
        self._capture_session = 0  # 录制会话 id，隔离新旧会话共享的 frames/status
        self._paused_at_ns: int | None = None  # 当前暂停开始时间戳
        self._total_paused_ns: int = 0  # 本会话累计暂停时长

    # ---- 内部：整屏 bbox ----
    def _full_bbox(self) -> dict[str, int]:
        import mss  # 惰性

        with mss.mss() as sct:
            # monitors[0] 是「所有显示器的合并边界」，monitors[1] 是主屏。
            m = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            return {
                "left": int(m["left"]),
                "top": int(m["top"]),
                "width": int(m["width"]),
                "height": int(m["height"]),
            }

    def start(self, output_path: str, fps: int, region: dict[str, int] | None, audio: bool) -> RecorderStatus:
        if audio:
            logging.getLogger(__name__).debug(
                "mss 后端不支持音频录制，audio=True 参数已忽略（仅 ffmpeg 后端支持音频）"
            )
        if self._status.state == STATE_RECORDING:
            raise RuntimeError("已在录制中，请先停止")
        # SAFETY: 拒绝覆盖已存在的输出文件，防止用户数据被静默截断（对齐 pdf 后端的
        # FileExistsError 语义）。自动命名路径在上层生成唯一文件名，不会触发此检查。
        if output_path and os.path.exists(output_path):
            raise RuntimeError(f"输出文件已存在，拒绝覆盖: {output_path}")
        import cv2  # 惰性
        import numpy as np  # 惰性

        self._bbox = _region_to_mss_bbox(region, self._full_bbox())
        if self._bbox["width"] <= 0 or self._bbox["height"] <= 0:
            raise ValueError("录制区域宽高必须为正数")

        # FR-20260812-D8-001: 记录本次会话是否自建了输出文件（start 前不存在）。
        # stop() 失败（0 帧/采集错误）时必须清理本方失败产物，否则同路径重试会
        # 命中上方 exists SAFETY 守卫被拒，用户须手动删除才能再录。只删本方自建，
        # 绝不触碰用户既有文件。
        self._created_output = bool(output_path) and not os.path.exists(output_path)

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        fourcc = cv2.VideoWriter_fourcc(*_fourcc_for(os.path.splitext(output_path)[1].lstrip(".")))
        self._writer = cv2.VideoWriter(
            output_path, fourcc, float(fps),
            (self._bbox["width"], self._bbox["height"]),
        )
        if not self._writer.isOpened():
            self._writer.release()
            self._writer = None
            raise RuntimeError(f"无法打开视频编码器（输出：{output_path}）")

        self._stop.clear()
        self._paused.clear()
        # 重置逐帧错误计数：该计数仅用于本会话的取证完整性判断，且 _capture_loop
        # 内部仅在 self._capture_session == my_session 时才累加，故跨会话不会污染
        # 新会话的 stop() 判断（修复潜在 P2 跨会话计数污染）。
        self._capture_errors = 0
        self._capture_session += 1
        self._paused_at_ns = None
        self._total_paused_ns = 0
        my_session = self._capture_session
        self._status = RecorderStatus(
            state=STATE_RECORDING,
            output_path=os.path.abspath(output_path),
            fps=int(fps),
            region=dict(self._bbox),
            started_at_ns=time.time_ns(),
            frames=0,
        )
        # 把 writer / bbox / session 以局部变量传入线程，使其与 self._writer 解耦：
        # 即便 stop() 后该线程仍存活（join 超时），它释放的也只是本次会话捕获的 writer，
        # 不会误伤新会话的 writer（修复 P3 待核实#2 的线程竞争）。
        self._thread = threading.Thread(
            target=self._capture_loop,
            args=(self._writer, my_session, dict(self._bbox), np),
            daemon=True,
        )
        self._thread.start()
        return self.status()

    def _capture_loop(self, writer, my_session: int, bbox: dict[str, int], np) -> None:
        import mss  # 惰性

        # DESIGN: fps 控帧——按帧间隔节流，避免 mss 以最快速度连续抓帧导致
        # 实际帧率远超 VideoWriter 元数据声明的 fps（修复 R-recorder-fps）。
        fps = self._status.fps
        frame_interval = 1.0 / fps if fps > 0 else 0.0
        next_frame_time = time.perf_counter()
        was_paused = False  # 跟踪暂停→恢复转换，恢复时重置计时避免帧爆发

        with mss.mss() as sct:
            while not self._stop.is_set():
                if self._paused.is_set():
                    was_paused = True
                    time.sleep(0.05)
                    continue
                # 从暂停恢复时重置节流计时，不补录暂停期间的帧（修复 D-10 帧爆发）。
                if was_paused:
                    next_frame_time = time.perf_counter()
                    was_paused = False
                now = time.perf_counter()
                if frame_interval > 0 and now < next_frame_time:
                    time.sleep(min(next_frame_time - now, 0.01))
                    continue
                try:
                    img = sct.grab(bbox)
                    frame = np.array(img)
                    # mss 返回 BGRA，opencv 需要 BGR。
                    frame = frame[:, :, :3]  # 去掉 alpha
                    writer.write(frame)
                    with self._lock:
                        # 仅当仍是同一录制会话时才累加帧数，避免 join 超时后误增新会话计数。
                        if self._capture_session == my_session:
                            self._status.frames += 1
                except Exception:  # noqa: BLE001 单帧失败不中断整段录制，但计入错误
                    self._capture_errors += 1
                    time.sleep(0.05)
                next_frame_time += frame_interval
        # 释放本会话捕获的 writer（局部变量，与新会话解耦）。
        with contextlib.suppress(Exception):
            writer.release()

    def pause(self) -> RecorderStatus:
        if self._status.state != STATE_RECORDING:
            raise RuntimeError("当前未在录制，无法暂停")
        self._paused.set()
        self._paused_at_ns = time.time_ns()
        self._status.state = STATE_PAUSED
        return self.status()

    def resume(self) -> RecorderStatus:
        if self._status.state != STATE_PAUSED:
            raise RuntimeError("当前未处于暂停状态，无法恢复")
        if self._paused_at_ns is not None:
            self._total_paused_ns += time.time_ns() - self._paused_at_ns
            self._paused_at_ns = None
        self._paused.clear()
        self._status.state = STATE_RECORDING
        return self.status()

    def stop(self) -> RecorderStatus:
        if self._status.state == STATE_IDLE:
            raise RuntimeError("尚未开始录制")
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        with self._lock:
            # 扣除暂停时长：若停止时仍处于暂停状态，也要计入未恢复的那段。
            paused_ns = self._total_paused_ns
            if self._paused_at_ns is not None:
                paused_ns += time.time_ns() - self._paused_at_ns
            elapsed_ns = time.time_ns() - self._status.started_at_ns - paused_ns
            self._status.duration_ms = int(elapsed_ns / 1_000_000)
            path = self._status.output_path
            if path and os.path.isfile(path):
                self._status.file_bytes = os.path.getsize(path)
            frames = self._status.frames
            errors = self._capture_errors
            self._status.state = STATE_IDLE
        # 取证完整性：未捕获到任何帧、或逐帧采集有失败，都不得伪装成成功。
        if frames == 0:
            self._cleanup_own_output(path)
            raise RuntimeError(
                "录制未捕获到任何帧（可能权限不足/锁屏/区域非法/显示器休眠），"
                "输出视频为空，请检查录制区域与权限后重试"
            )
        if errors > 0:
            self._cleanup_own_output(path)
            raise RuntimeError(
                f"录制过程中有 {errors} 帧采集失败（可能锁屏/显示器休眠/分辨率变更），"
                "生成的视频可能不完整，请确认后重试"
            )
        return self.status()

    def _cleanup_own_output(self, path: str | None) -> None:
        """FR-20260812-D8-001: 删除本次会话自建的空/残缺输出文件。

        仅当 start() 确认该路径在录制前不存在（本方自建）时才删除；用户既有
        文件永远不受影响。删除失败静默（不掩盖原始失败原因）。
        """
        if not getattr(self, "_created_output", False):
            return
        if not path:
            return
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass

    def snapshot(self, path: str, region: dict[str, int] | None) -> str:
        # SAFETY: 拒绝覆盖已存在的截图文件，与 start() 的录制路径保持一致。
        if path and os.path.exists(path):
            raise RuntimeError(f"SAFETY: 拒绝覆盖已存在的截图文件: {path}")
        import mss  # 惰性

        bbox = _region_to_mss_bbox(region, self._full_bbox())
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        try:
            with mss.mss() as sct:
                img = sct.grab(bbox)
                # mss 截图可直接保存为 .png（自带方法），无需 opencv 转码。
                mss.tools.to_png(img.rgb, img.size, output=path)
        except Exception:
            # FR-20260813-D9-001: 截图中途失败（显示器切换/权限/设备睡眠）会写出
            # 部分字节；清理残缺文件，避免 SAFETY 守卫永久阻止同路径重试。
            try:
                if path and os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass
            raise
        return os.path.abspath(path)

    def status(self) -> RecorderStatus:
        with self._lock:
            return RecorderStatus(
                state=self._status.state,
                output_path=self._status.output_path,
                fps=self._status.fps,
                region=dict(self._status.region) if self._status.region else None,
                started_at_ns=self._status.started_at_ns,
                frames=self._status.frames,
                duration_ms=self._status.duration_ms,
                file_bytes=self._status.file_bytes,
            )

    def close(self) -> None:
        if self._status.state in (STATE_RECORDING, STATE_PAUSED):
            with contextlib.suppress(Exception):
                self.stop()


# --------------------------------------------------------------------------
# 真实后端 2：系统 ffmpeg（gdigrab，画质高；暂停/恢复不支持）
# --------------------------------------------------------------------------

class FfmpegBackend(RecorderBackend):
    def __init__(self) -> None:
        self._status = RecorderStatus()
        self._proc = None

    @staticmethod
    def available() -> bool:
        import shutil

        return shutil.which("ffmpeg") is not None

    def _ffmpeg_cmd(self, output_path: str, fps: int, region: dict[str, int] | None, audio: bool) -> list:
        # -n：禁止覆盖已存在文件（start() 入口已拦截，此处为纵深防御）。
        cmd = ["ffmpeg", "-n", "-f", "gdigrab", "-framerate", str(int(fps)), "-i", "desktop"]
        if region:
            cmd += [
                "-offset_x", str(int(region.get("x", 0))),
                "-offset_y", str(int(region.get("y", 0))),
                "-video_size", f'{int(region.get("width", 0))}x{int(region.get("height", 0))}',
            ]
        if audio:
            cmd += ["-f", "dshow", "-i", "audio=virtual-audio-capturer"]
        cmd.append(output_path)
        return cmd

    def start(self, output_path: str, fps: int, region: dict[str, int] | None, audio: bool) -> RecorderStatus:
        if self._status.state == STATE_RECORDING:
            raise RuntimeError("已在录制中，请先停止")
        if not self.available():
            raise RuntimeError("未找到系统 ffmpeg（请安装并加入 PATH）")
        # SAFETY: 拒绝覆盖已存在的输出文件，防止用户数据被静默截断。
        if output_path and os.path.exists(output_path):
            raise RuntimeError(f"输出文件已存在，拒绝覆盖: {output_path}")

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        cmd = self._ffmpeg_cmd(output_path, fps, region, audio)
        import subprocess  # 标准库

        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._status = RecorderStatus(
            state=STATE_RECORDING,
            output_path=os.path.abspath(output_path),
            fps=int(fps),
            region=dict(region) if region else None,
            started_at_ns=time.time_ns(),
            frames=0,
        )
        return self.status()

    def pause(self) -> RecorderStatus:
        raise RuntimeError("ffmpeg 后端暂不支持暂停/恢复，请改用 mss 后端（默认）以使用暂停/恢复")

    def resume(self) -> RecorderStatus:
        raise RuntimeError("ffmpeg 后端暂不支持暂停/恢复，请改用 mss 后端（默认）以使用暂停/恢复")

    def stop(self) -> RecorderStatus:
        if self._proc is None:
            raise RuntimeError("尚未开始录制")
        # ffmpeg 通过关闭 stdin / terminate 正常收尾写出文件。
        try:
            self._proc.terminate()
            self._proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            with contextlib.suppress(Exception):
                self._proc.kill()
        self._proc = None
        elapsed_ns = time.time_ns() - self._status.started_at_ns
        self._status.duration_ms = int(elapsed_ns / 1_000_000)
        path = self._status.output_path
        if path and os.path.isfile(path):
            self._status.file_bytes = os.path.getsize(path)
        self._status.state = STATE_IDLE
        # 取证完整性（修复 P2）：ffmpeg 静默失败时可能产出 0 字节或缺失文件，
        # 不得伪装成成功。与 MssOpencvBackend.stop() 保持同一校验标准。
        if not path or not os.path.isfile(path):
            raise RuntimeError(
                "ffmpeg 录制产出缺失（可能因 audio=True 但无 virtual-audio-capturer、"
                "gdigrab 失败或被外部终止），输出视频无效，请检查录制环境后重试"
            )
        if self._status.file_bytes == 0:
            raise RuntimeError(
                "ffmpeg 录制产出为空文件（0 字节），输出视频无效，请检查录制环境后重试"
            )
        return self.status()

    def snapshot(self, path: str, region: dict[str, int] | None) -> str:
        if not self.available():
            raise RuntimeError("未找到系统 ffmpeg（请安装并加入 PATH）")
        # SAFETY: 拒绝覆盖已存在的截图文件，与 start() 的录制路径保持一致。
        if path and os.path.exists(path):
            raise RuntimeError(f"SAFETY: 拒绝覆盖已存在的截图文件: {path}")
        import subprocess  # 标准库

        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        # -n：禁止覆盖已存在文件（入口已拦截，此处为纵深防御，与 _ffmpeg_cmd 一致）。
        cmd = ["ffmpeg", "-n", "-f", "gdigrab", "-i", "desktop"]
        if region:
            cmd += [
                "-offset_x", str(int(region.get("x", 0))),
                "-offset_y", str(int(region.get("y", 0))),
                "-video_size", f'{int(region.get("width", 0))}x{int(region.get("height", 0))}',
            ]
        cmd += ["-vframes", "1", path]
        # FR-20260809-1203: ffmpeg 挂起（异常显示器/权限）时必须有界等待，
        # 否则 snapshot 调用线程无限阻塞、插件宿主线程被占。
        try:
            subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
        except subprocess.TimeoutExpired as exc:
            # FR-20260813-D9-001: 超时残留部分文件同样需清理，避免 SAFETY 守卫锁死重试。
            try:
                if path and os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass
            raise RuntimeError(
                "ffmpeg 截图超时（10 秒未完成），已中止"
            ) from exc
        except Exception:
            # 其它异常（权限/设备）：清理可能残留的部分文件。
            try:
                if path and os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass
            raise
        if not os.path.isfile(path):
            raise RuntimeError("ffmpeg 截图失败，未生成文件")
        return os.path.abspath(path)

    def status(self) -> RecorderStatus:
        return RecorderStatus(
            state=self._status.state,
            output_path=self._status.output_path,
            fps=self._status.fps,
            region=dict(self._status.region) if self._status.region else None,
            started_at_ns=self._status.started_at_ns,
            frames=self._status.frames,
            duration_ms=self._status.duration_ms,
            file_bytes=self._status.file_bytes,
        )

    def close(self) -> None:
        if self._proc is not None:
            with contextlib.suppress(Exception):
                self.stop()


# --------------------------------------------------------------------------
# 测试后端：Mock（无任何第三方依赖）
# --------------------------------------------------------------------------

class MockRecorderBackend(RecorderBackend):
    """内置状态机 + 占位文件，用于无依赖环境下的逻辑验证。"""

    def __init__(self) -> None:
        self._status = RecorderStatus()
        self._paused_at_ns: int | None = None  # 当前暂停开始时间戳
        self._total_paused_ns: int = 0  # 本会话累计暂停时长

    def start(self, output_path: str, fps: int, region: dict[str, int] | None, audio: bool) -> RecorderStatus:
        if self._status.state == STATE_RECORDING:
            raise RuntimeError("已在录制中，请先停止")
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        # 写一个空容器占位，证明输出路径链路可用（真实后端会写入编码后的视频）。
        with open(output_path, "wb") as handle:
            handle.write(b"mock-recording-placeholder")
        self._paused_at_ns = None
        self._total_paused_ns = 0
        self._status = RecorderStatus(
            state=STATE_RECORDING,
            output_path=os.path.abspath(output_path),
            fps=int(fps),
            region=dict(region) if region else None,
            started_at_ns=time.time_ns(),
            frames=0,
        )
        return self.status()

    def pause(self) -> RecorderStatus:
        if self._status.state != STATE_RECORDING:
            raise RuntimeError("当前未在录制，无法暂停")
        self._paused_at_ns = time.time_ns()
        self._status.state = STATE_PAUSED
        return self.status()

    def resume(self) -> RecorderStatus:
        if self._status.state != STATE_PAUSED:
            raise RuntimeError("当前未处于暂停状态，无法恢复")
        if self._paused_at_ns is not None:
            self._total_paused_ns += time.time_ns() - self._paused_at_ns
            self._paused_at_ns = None
        self._status.state = STATE_RECORDING
        return self.status()

    def stop(self) -> RecorderStatus:
        if self._status.state == STATE_IDLE:
            raise RuntimeError("尚未开始录制")
        # 扣除暂停时长：若停止时仍处于暂停状态，也要计入未恢复的那段。
        paused_ns = self._total_paused_ns
        if self._paused_at_ns is not None:
            paused_ns += time.time_ns() - self._paused_at_ns
        elapsed_ns = time.time_ns() - self._status.started_at_ns - paused_ns
        self._status.duration_ms = int(elapsed_ns / 1_000_000)
        path = self._status.output_path
        if path and os.path.isfile(path):
            self._status.file_bytes = os.path.getsize(path)
        self._status.state = STATE_IDLE
        return self.status()

    def snapshot(self, path: str, region: dict[str, int] | None) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"mock-snapshot-placeholder")
        return os.path.abspath(path)

    def status(self) -> RecorderStatus:
        return RecorderStatus(
            state=self._status.state,
            output_path=self._status.output_path,
            fps=self._status.fps,
            region=dict(self._status.region) if self._status.region else None,
            started_at_ns=self._status.started_at_ns,
            frames=self._status.frames,
            duration_ms=self._status.duration_ms,
            file_bytes=self._status.file_bytes,
        )

    def close(self) -> None:
        if self._status.state in (STATE_RECORDING, STATE_PAUSED):
            with contextlib.suppress(Exception):
                self.stop()


def create_backend(name: str = "mss") -> RecorderBackend:
    """按名称构造录制后端。

    * mock —— 测试后端（默认用于 CI/无依赖环境）。
    * mss / opencv / mss_opencv —— mss + opencv 软编码后端（默认真实后端，支持暂停/恢复）。
    * ffmpeg —— 系统 ffmpeg gdigrab 后端（画质高，不支持暂停/恢复）。
    """
    if name == "mock":
        return MockRecorderBackend()
    if name in ("mss", "opencv", "mss_opencv"):
        return MssOpencvBackend()
    if name == "ffmpeg":
        return FfmpegBackend()
    raise ValueError(f"未知录制后端：{name!r}（可选：mss / ffmpeg / mock）")


# --------------------------------------------------------------------------
# 进程级单例缓存与生命周期管理
# --------------------------------------------------------------------------

# 进程级单例缓存：一段录制会话（start→pause→resume→stop）跨多个动作调用，
# 必须在同进程内复用同一实例，否则状态会丢失。
_BACKEND_CACHE: dict[str, RecorderBackend] = {}


def get_backend(name: str = "mss") -> RecorderBackend:
    """按名字取录制后端，进程内复用同一实例（单例）。"""
    cached = _BACKEND_CACHE.get(name)
    if cached is None:
        cached = create_backend(name)
        _BACKEND_CACHE[name] = cached
    return cached


def close_all() -> None:
    """释放所有缓存的录制后端并清空缓存。

    生命周期：本模块持有进程级单例缓存；宿主（P3）退出时通过 ``atexit`` 自动调用
    本函数，停止仍在进行的录制并释放句柄。幂等——重复调用安全。
    """
    for backend in list(_BACKEND_CACHE.values()):
        with contextlib.suppress(Exception):
            backend.close()
    _BACKEND_CACHE.clear()


try:
    import atexit
    atexit.register(close_all)
except Exception:  # noqa: BLE001 极少数无 atexit 的环境（如受限嵌入式解释器）
    pass
