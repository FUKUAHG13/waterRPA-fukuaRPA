"""轮询/等待间隔集中常量（health report §3.6）。

把散落在各后端的魔法数字集中到此处，避免同一语义出现多个不一致的字面量。
修改这些值时请同步评估所有调用点；新增轮询点应优先复用此处常量。
"""

# 通用轮询间隔：热键后端、原生冒烟、Runner 提示队列等。
POLL_INTERVAL_S = 0.05

# 快速轮询：日志刷新、原生冒烟的快轮询分支。
POLL_INTERVAL_FAST_S = 0.01

# 慢轮询：映射后端第二次点击间隔、原生冒烟的慢轮询分支。
POLL_INTERVAL_SLOW_S = 0.02

# 映射后端首次按下后的间隔（区别于通用 0.05）。
MAPPING_POLL_INTERVAL_S = 0.04

# 日志刷新间隔（与 POLL_INTERVAL_FAST_S 同值，但语义独立，便于单独调参）。
LOG_FLUSH_INTERVAL_S = 0.01

# 引擎可中断等待循环的最大轮询刻度：保证停止标志至少每 25 ms 被检查一次。
INTERRUPTIBLE_WAIT_TICK_S = 0.025

# 窗口查找轮询间隔：wait_for_window 在超时前每 100 ms 枚举一次窗口。
WINDOW_POLL_INTERVAL_S = 0.1
