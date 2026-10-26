"""参数对象（parameter objects）的纯类型定义。

把跨多个模块传递的长参数列表收敛为单一聚合对象，降低“改一处漏一处”
的风险，并让调用方在将来可以整体传递/构造/校验这些参数。

这些 dataclass 只依赖标准库，不导入任何业务模块，因此可以被
``vision``、``engine_actions``、``ui/overlays``、``ui/controllers/run_controller``
等模块安全导入，避免循环依赖。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class TaskOnceParams:
    """``execute_task_once`` 一次动作分发所需的全部参数。

    作为 ``execute_task_once`` 公共入口与其内部实现
    ``_execute_task_once_impl`` 之间的聚合契约；内部实现再从这里取出字段
    构造更细的 ``StepActionContext`` 交给具体动作处理器。
    """

    cmd: Any
    val: Any
    retry: Any
    step_info: Any
    cache_key: Any
    task_conf: Any
    use_gray: bool
    point_limit_en: bool = False
    point_limit_count: int = 0
    coord_step_config: Mapping | None = None
    image_click_config: Mapping | None = None
    task: Mapping | None = None
    coord_sequence_config: Mapping | None = None
    search_regions: Sequence | None = None


@dataclass
class MatchOptions:
    """``NativeVisionFinder.find_template`` 一次搜索所需的全部参数。"""

    image_path: Any
    regions: Sequence | None
    min_scale: float
    max_scale: float
    scale_step: float
    use_gray: bool
    threshold: float
    find_all: bool = False
    max_matches: int = 512
    parallel_mode: str = "auto"
    preferred_scale: float | None = None
    preferred_scales: Sequence | None = None
    explicit_scale_only: bool = False


@dataclass
class OverlayOptions:
    """``Overlay`` 构造所需的全部展示参数。"""

    points: Sequence = field(default_factory=list)
    options: Mapping | None = None
    title: str | None = None
    auto_close_ms: int = 6000
    draw_lines: bool = True
    editable_indices: Sequence | None = None
    point_moved_callback: Any = None
    detail_text: str | None = None
    close_on_left_blank: bool = True
    marked_indices: Sequence | None = None
    point_labels: Sequence | None = None
    line_segments: Sequence | None = None


@dataclass
class RunConfig:
    """``RunController.start`` 一次运行所需的全部参数。"""

    tasks: Any = None
    log_callback: Any = None
    status_callback: Any = None
    click_callback: Any = None
    debug_callback: Any = None
    prompt_callback: Any = None
    backend_mode: str = "in_process"
    config: Any = None
    base_dir: str | None = None
    profile_name: str = ""
    logging_options: Any = None
    run_meta: Mapping | None = None


@dataclass(frozen=True, slots=True)
class FindAllMatchParams:
    """``find_all_targets_in_screenshot`` 一次多目标匹配所需的全部参数。

    调用方须持有 ``_vision_lock``；本对象仅聚合匹配参数，
    不涉及锁或截图获取。
    """

    img_path: Any
    cache_key: Any
    task_conf: float
    use_gray: bool
    screenshot_pil: Any
    offset_x: int
    offset_y: int
    search_regions: Sequence | None = None
    allow_inverted: bool = False
