"""Product identity, compatibility baseline, and defensive resource limits."""

PRODUCT_NAME = "fukuaRPA"
APP_VERSION = "v1.1.0"
BUILD_NAME = f"{PRODUCT_NAME}_{APP_VERSION}"
ONEFILE_BUILD_NAME = f"{BUILD_NAME}_single"
PROJECT_REPOSITORY_URL = "https://github.com/FUKUAHG13/waterRPA-fukuaRPA"
PROJECT_RELEASES_URL = f"{PROJECT_REPOSITORY_URL}/releases"
AUTHOR_BILIBILI_URL = "https://space.bilibili.com/95794432/dynamic"
NATIVE_CORE_DLL_NAME = "fukua_rpa_core.dll"
SUPPORTED_WINDOWS_TEXT = "Windows 10 1809 及以上版本（仅 x64）和 Windows 11"

# Persistent data versions are independent from the marketing version. Bump
# these only when the stored structure becomes incompatible; never infer data
# compatibility from APP_VERSION.
PROFILE_SCHEMA_VERSION = 4
# Old profiles are supported only on a best-effort basis. Raise this floor when
# an incompatible release intentionally drops an old layout (see ADR 0032).
MIN_SUPPORTED_PROFILE_SCHEMA_VERSION = 0
PROFILE_BACKUP_VERSION = 3
PROFILE_COMMIT_VERSION = 1
DEFAULT_KEY_MAPPING_COUNT = 3
DEFAULT_PROFILE_BACKUP_COUNT = 3
MAX_PROFILE_BACKUP_COUNT = 50
PROFILE_HISTORY_LIMIT = 12
PROFILE_HISTORY_MIN_INTERVAL_SECONDS = 60

# Command type 9.0 (截图保存); the task-row file dialog uses it to trigger a
# save-folder selection. Named to avoid a magic number in ui/task_row.py.
TASK_TYPE_DIRECTORY_SELECT = 9.0
# INVARIANT: these numeric command codes are persisted in profiles. Keep them
# synchronized with commands.py COMMAND_SPECS; never renumber or reuse an old
# value for a different action merely to make the table look contiguous.
TASK_TYPE_WAIT = 5.0
TASK_TYPE_SCROLL = 6.0
TASK_TYPE_SCREENSHOT_SAVE = 9.0
TASK_TYPE_LEFT_DRAG = 10.0
TASK_TYPE_RIGHT_DRAG = 11.0
TASK_TYPE_POPUP_NOTIFY = 12.0
TASK_TYPE_STOP_RUN = 13.0
TASK_TYPE_SOUND_ALERT = 14.0
TASK_TYPE_UNTIL = 15.0
TASK_TYPE_SET_VARIABLE = 16.0
TASK_TYPE_EXPRESSION = 17.0
TASK_TYPE_LAUNCH_APP = 18.0
TASK_TYPE_WAIT_WINDOW = 19.0
TASK_TYPE_ACTIVATE_WINDOW = 20.0
TASK_TYPE_CLOSE_WINDOW = 21.0
TASK_TYPE_SECRET_TEXT = 22.0
TASK_TYPE_UIA_CLICK = 23.0
TASK_TYPE_UIA_SET_VALUE = 24.0
TASK_TYPE_UIA_READ_VALUE = 25.0
TASK_TYPE_MULTI_IMAGE_ACTION = 26.0
# One fixed, generic plugin-action step type (design §5.4). Do NOT allocate a
# new per-plugin code — every plugin action shares this single code and carries
# its concrete plugin/action identity in the step's plugin_id / plugin_action_id
# fields instead. Never renumber or reuse this value.
TASK_TYPE_PLUGIN_ACTION = 27.0
# 连点器（原生指令）：左/中/右键重复点击，支持随机点击间隔、按下抬起间隔、
# 以当前鼠标或固定坐标为中心 + H/V 抖动、点击次数限制，以及大循环/小循环嵌套。
TASK_TYPE_AUTO_CLICKER = 28.0
# 录制时序（批次 2）：相对时间的事件流，用于还原按下/释放顺序、自动重复与多键重叠。
TASK_TYPE_INPUT_TIMELINE = 29.0
# 输入时序步骤的事件数上限（与录制事件上限一致，防止异常数据撑爆回放）。
MAX_TIMELINE_EVENTS = 50_000
# 单个输入时序步骤允许的最大相对时间（秒）。与「等待」步骤的 86400 秒上限一致，
# 回放侧与校验侧必须使用同一数值，否则会出现「校验通过但回放被丢弃」。
MAX_TIMELINE_EVENT_SECONDS = 86_400.0
UNTIL_CONDITION_MODES = ["图片出现", "图片消失", "区域发生变化", "区域变成指定图片", "覆盖匹配", "区域不变"]
UNTIL_CONDITION_LOGICS = ["全部满足", "任一满足"]
# FR-20261003-609:「按失败处理」没有任何引擎侧实现，选了也不会执行，因此界面
# 不把它作为可选项。保留在列表里是为了让校验继续接受旧方案里存的值。
UNREACHABLE_UNTIL_LIMIT_ACTION = "按失败处理"
UNTIL_LIMIT_ACTIONS = ["继续下一步", "停止脚本", UNREACHABLE_UNTIL_LIMIT_ACTION]

NATIVE_CORE_MIN_VERSION = 10400
NATIVE_CORE_MAX_VERSION = 19999
NATIVE_CORE_RELEASE_VERSION = 11500

MAX_PROFILES = 100
MAX_TASKS_PER_PROFILE = 5000
MAX_KEY_MAPPINGS = 256
MAX_PROFILE_NAME_LENGTH = 200
MAX_PROFILE_TOP_LEVEL_FIELDS = 256
MAX_TASK_FIELDS = 256
MAX_KEY_MAPPING_FIELDS = 64
MAX_PROFILE_JSON_BYTES = 64 * 1024 * 1024
MAX_PROFILE_STORE_JSON_BYTES = 128 * 1024 * 1024
MAX_PROFILE_STORE_FILE_BYTES = 192 * 1024 * 1024
MAX_PACKAGE_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_JSON_NESTING_DEPTH = 64
MAX_JSON_NODES = 2_000_000
MAX_SCALES_PER_TEMPLATE = 61
MAX_TEMPLATE_SOURCE_PIXELS = 25_000_000
MAX_NATIVE_CAPTURE_PIXELS = 64_000_000
MAX_TEMPLATE_CACHE_BYTES = 512 * 1024 * 1024
MAX_SINGLE_TEMPLATE_CACHE_BYTES = 128 * 1024 * 1024
MAX_CLICK_INDICATOR_OVERLAYS = 24
MAX_MULTI_IMAGE_ACTION_RULES = 32
MAX_AUXILIARY_CLICK_REQUESTS = 256
MAX_PACKAGE_FILES = 5000
MAX_PACKAGE_FILE_BYTES = 256 * 1024 * 1024
MAX_PACKAGE_TOTAL_BYTES = 1024 * 1024 * 1024

PROFILE_BACKUP_FORMAT = "fukuaRPA_profiles_backup"
LEGACY_PROFILE_BACKUP_FORMAT = "waterRPA_profiles_backup"
PROFILE_COMMIT_FORMAT = "fukuaRPA_profiles_commit"
FULL_PACKAGE_FORMAT = "fukuaRPA_full_package"
LEGACY_FULL_PACKAGE_FORMAT = "waterRPA_full_package"
PROFILE_COLLECTION_FORMAT = "fukuaRPA_profile_collection"
PROFILE_COLLECTION_PACKAGE_FORMAT = "fukuaRPA_profile_collection_package"
TASK_CLIPBOARD_KEY = "fukuaRPA_task"
LEGACY_TASK_CLIPBOARD_KEY = "waterRPA_task"

# --- 验证边界（集中管理，避免散落硬编码） ---
CONFIDENCE_MIN = 0.05
CONFIDENCE_MAX = 1.0
SCALE_MIN = 0.01
SCALE_MAX = 5.0
JITTER_MAX_SECONDS = 10.0
MAX_WAIT_SECONDS = 86_400.0
UI_SCALE_TOLERANCE = 0.001

# --- 视觉匹配默认值（集中管理，避免散落硬编码） ---
DEFAULT_CONFIDENCE = 0.8  # engine 默认置信度，亦为漏检回退置信度上限（engine_vision.py:97）
CLICK_ANCHOR_RATIO = 0.5  # 图片点击锚点默认比例（中心 0.5）
INDICATOR_RADIUS_RATIO = 0.55  # 命中指示半径相对匹配框宽高的比例

# --- 插件类型（集中管理，避免散落硬编码与成员不一致） ---
PLUGIN_TYPES = ("declarative", "python_action")

# DESIGN: 点击空间抖动和移动曲线用于模拟人类操作特征。空间抖动让每次点击在目标
# 周围随机偏移若干像素，避免机器人式的像素级精确重复点击；曲线强度控制鼠标移动
# 路径的弯曲程度（二次贝塞尔），0 为直线，1 为大幅曲线。两者默认关闭（=0）以保证
# 像素级精确行为的向后兼容；用户可在全局设置或小齿轮中按需开启。
CLICK_SPATIAL_JITTER_DEFAULT = 0  # 默认点击空间抖动 ±0 像素（关闭）
MOVE_CURVE_STRENGTH_DEFAULT = 0.0  # 默认移动曲线强度（关闭，直线）
KEY_HOLD_DURATION_DEFAULT = 0.0  # 默认系统按键按住时长（秒，0 表示不按住）

# --- 「直到条件成立」覆盖小图匹配参数（单一来源） ---
# 取值域必须与 engine/conditions.py 的 clamp 口径一致：把常量放在这里，两侧都引用
# 同一份定义，避免任一侧被单独改动而漂移（docs/adr/0025）。
UNTIL_OVERLAY_SCALE_MIN = 0.1
UNTIL_OVERLAY_SCALE_MAX = 5.0
UNTIL_OVERLAY_SCALE_DEFAULT = 1.0
UNTIL_OVERLAY_THRESHOLD_DEFAULT = 0.8

# --- 连点器次数上限（界面可设范围与执行器钳制共用，FR-SCOPE009-001） ---
# 执行器的静默钳制是实际生效的口径，因此以它为准；界面不再提供会被截断的值。
AUTO_CLICKER_COUNT_MAX = 1_000_000
