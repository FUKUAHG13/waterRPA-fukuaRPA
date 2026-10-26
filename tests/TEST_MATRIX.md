# 测试影响矩阵

本表说明各测试组保护的产品边界。本地测试包使用 `python scripts/run_impacted_tests.py`；正式发布仍执行完整发布门禁。

| 测试组 | 主要职责 | 常见变更路径 |
|---|---|---|
| `smoke` | 导入边界和延迟启动 | 程序入口、启动流程、常量 |
| `review` | 审查记录、测试有效结果、原字节备份与交付收尾 | 审查基准/模板、备份规范、AGENTS、`check_review_workflow.py`、开发依赖/CI |
| `ui` | Qt 布局、缩放、对话框、步骤行、长列表虚拟化和拖放 | `fukua_rpa/ui/`、界面资源 |
| `workflow` | 调度、校验、跳转、条件和预览 | 引擎、调度器、步骤与工作流模型 |
| `runner` | 进程内/独立 Runner、IPC、调试、弹窗与强制停止 | Runner、Worker、运行控制器 |
| `vision` | 截图、OpenCV/原生匹配、倍率记忆和画面变化唤醒 | 视觉模块、原生核心 |
| `input` | 鼠标/键盘动作和录制 | 动作与录制模块 |
| `mapping` | 全局热键、窗口绑定和 UI Automation | 映射、UIA、Win32 模块 |
| `config` | 方案、迁移、可配置备份保留、损坏恢复、存储路径和全量包导入导出 | 配置、方案、路径模块 |
| `logging` | 日志策略、诊断、运行轨迹和有界指标 | 日志与性能模块 |
| `security` | 凭据、隐私、完整性和压缩包边界 | 凭据、完整性、方案模块 |
| `plugins` | 插件清单、包、注册表、宿主契约和随仓库插件自身测试 | `fukua_rpa/plugin_*`、`plugins/*/` |
| `packaging` | spec、依赖裁剪、清单和冻结包启动 | spec、发布脚本、依赖清单 |
| `performance` | 回归阈值和有界资源行为 | 性能、视觉和原生调度 |

测试选择采用保守策略：

- 被修改的测试文件始终运行自身。
- 一个文件可以同时选中多个测试组。
- 无法映射的可执行源码变更会选择完整单元测试。
- 仅文档变更不选择运行时测试。
- 任何打包都必须执行冻结包启动、自检和运行时闭包检查，即使源码测试采用定向范围。
- 性能基准和耐久测试是正式发布的必测项；本地测试包只有在相关领域发生变化时才运行。
- 正式发布必须运行全部主程序测试和 `plugins/*/tests`，插件测试采用严格模式；缺少 `pytest`、插件测试
  超时或任一插件失败均阻断。普通开发环境可明确记录可选依赖缺失，但不能把跳过结果冒充通过。
- 共用一个 `QApplication` 的 Qt 测试必须对可能打开无父对话框的窗口使用 `tests.qt_test_support.dispose_widget`。只关闭宿主窗口并不够：必须在宿主仍有效时关闭相关顶层窗口，排空零延时回调，再执行延迟删除。

证据等级与运行成本：

| 标记 | 含义 |
|---|---|
| `E1/短` | 纯规则、静态或单模块测试，通常少于 30 秒 |
| `E2/中` | 跨模块、故障注入、真实 DLL/依赖测试，通常 30～180 秒 |
| `E3/长` | 冻结 EXE、真实窗口/输入、多屏、休眠或干净 Windows，通常超过 180 秒 |
| `E4/耐久` | 重复统计、P50/P95、长时间运行或多机器矩阵 |

时间只是选择测试的参考，不是超时承诺；准确耗时由本轮测试记录提供。Mock 通过不得替代同一条目明确
要求的 E3 实机门禁。

高价值测试模块标记：

| 测试模块 | 保护的约定 | 发生以下变更时运行 |
|---|---|---|
| `test_ui_performance` | 缩放节流、布局缓存、定时器暂停、虚拟步骤行、批量录制插入和脏状态自动保存 | 响应式布局、主窗口事件、步骤列表、方案或录制 |
| `test_ui_conventions` | `E1/短`：新增/修改 UI 行的主题、局部样式、固定几何、布局度量、字号和例外格式静态门禁 | `scripts/check_ui_conventions.py`、`docs/UI开发规范.md` 或任何 UI Python 修改 |
| `test_ui_modernization` | 设置显隐、缩放、对话框、热键、备注分栏、隐藏步骤整行淡化、简易模式工具栏和高倍率按钮尺寸 | 任何可见 Qt 控件或设置行为 |
| `test_ui_accessibility` | 键盘/读屏基础与混合 DPI 多显示器坐标回归 | UI 可访问性、屏幕坐标与覆盖层 |
| `test_dpi_awareness` | `E1/短`：Per-Monitor V2 早期初始化、冻结清单、DEVMODEW 物理模式解析，以及普通顶层窗口跨屏后的字体与视觉密度刷新契约 | 程序入口、DPI/坐标/主题模块、应用清单或 PyInstaller spec |
| `test_match_native_contract` | `E1/短`：原生 miss/取消/熔断契约、跨屏区域拆分，以及命中中心与宽高共同转换为 input-space | 原生模板匹配、DPI 坐标契约、图片内点击、确认补点或预览边界 |
| `test_task_settings_controllers` | 组合后的步骤/对话框控制器共享同一状态，动作显隐保留草稿，自定义点位批次、可拖预览与全局单预览，后台点击必须显式绑定窗口 | 步骤行、设置视图、坐标预览、绑定、控制器或对话框生命周期 |
| `test_drag_insert_preview` | 轻量内部拖动、约 30Hz 目标合并、取消/提交、窄带插入线和相同位置刷新去重 | 步骤列表拖放、插入动画或拖动性能 |
| `test_workflow_metadata` | 步骤备注、颜色、编辑器隐藏、组内粘贴连续性、空白粘贴落点、折叠标题整组删除、同路径重复 ID 三级保留、namespace 重映射幂等、恢复/索引幂等、重命名/折叠/取消分组、撤销重做、虚拟化与拖出修复 | 工作流编辑元数据、多选、分组、折叠、复制粘贴、快照恢复或步骤重排 |
| `test_bulk_task_settings` | 简易模式共有设置、高级模式全量显示、混合值保留和按动作能力过滤批量更新 | 批量步骤设置、动作能力表或步骤设置对话框 |
| `test_application_audit_log` | 窗口/UIA/按键/程序文件/截图目录/图片公开入口的单事务、删除步骤完整快照、界面/文件双通道与隐私开关，分组名双通道隐私、设置差异及秘密文本永不进入日志 | 主界面编辑日志、外部选择器、删除/分组、日志输出目标、设置审计或隐私边界 |
| `test_usage_notices` | 注意事项键唯一、类别与条目首字母排序、三级目录和关键执行边界持续可见 | 使用注意目录、步骤组织语义或新手兼容提示 |
| 使用 `qt_test_support` 的测试 | 在用例之间完整清理 Qt 顶层窗口 | 创建主窗口、设置窗口、步骤窗口或排队界面回调 |
| `test_engine_scheduler_boundaries` | 停止、超时、循环和调度边界 | 引擎、调度器、运行生命周期 |
| `test_scheduled_task_model` / `test_scheduled_task_store` / `test_scheduled_task_service` / `test_missed_run_detector` / `test_scheduled_task_logs` / `test_scheduled_task_ui_regressions` / `test_scheduled_coordinator_affinity` / `test_watchdog_drain_p1_3` | `E1～E2/中`：日历计算、有界摘要/详细日志、Runner 显式日志归属、退出尾日志、摘要秒级刷新、启用框绘制、独立日志窗口、右键操作列替代、稳定方案引用、错过策略（`MissedRunDetector` 一次性守卫、参照点回退、多次错过折叠为一次、skip/ask/catchup_once 决策表）、同线程约束、活动运行互斥和每计划最多一次待补 | 计划任务模型、服务、存储、错过检测、日志、管理界面、协调器、运行结束信号、休眠恢复或方案删除 |
| `test_step_runtime` | 类型化步骤策略、动作能力矩阵、重复目标、自定义点位批次、尝试快照和状态/失败语义 | 步骤设置、动作显隐、坐标序列、重试/重复行为或引擎分派 |
| `test_core_regressions` | 跨层执行回归 | 共用引擎、动作或视觉行为 |
| `test_advanced_image_actions` | 反色匹配、多图动作顺序、附属点击调度、嵌套图片迁移与高级模式显隐 | 高级图片匹配、多图动作、确认补点、延时二次点击或相关导入导出 |
| `test_scale_memory` | 学习倍率顺序和有界历史 | 倍率记忆或图片搜索顺序 |
| `test_scene_wake*` | 画面变化触发的图片搜索和回退 | 画面唤醒、截图区域、视觉循环 |
| `test_dodge_onboarding` | 避让设置显隐与取点回填、避让点击动作往返到引擎、双击避让收敛到终点单击、默认仅移动、后台点击失败不回退真实鼠标和非法动作拒绝、首次设置快速响应提示 | 避让设置、点击动作、引擎鼠标动作分派或新手引导提示 |
| `test_preview_and_mapping_models` | 坐标预览点位路径计数、禁用步骤跳过不重排标签、偏移点虚线分支、拖动代表点、随机预览线段索引有界，窗口映射后台点击 UIA 优先/PostMessage 回退/超时不重复、手动选窗与点击坐标分离、绑定坐标偏移和进程路径优先 | 坐标预览模型、点位序列、窗口映射后端或后台点击绑定 |
| `test_plugin_disable_persistence` | 插件禁用的本机持久语义：重启与升级保持禁用、已启用插件升级后保持启用、新安装默认禁用直至手动启用、卸载清理持久状态、状态不写入插件目录故不随插件包或方案导出 | `fukua_rpa/plugin_registry.py`、插件安装/卸载/升级路径或 PLG-004 禁用语义变更 |
| `test_credential_dpapi_integrity` | `E1～E2/短`：真实 DPAPI 密文篡改、截断与往返一致性守护（平台外自动跳过） | `fukua_rpa/credentials.py`、凭据库格式或 DPAPI 边界变更 |
| `test_start_task_from_step_once_guards` | `E1/短`：“从第 X 步开始执行”的启动守卫顺序（运行库就绪、连点器互斥、启动进行中、运行互斥与识别测试互斥） | `fukua_rpa/ui/controllers/execution_controller.py` 或运行入口互斥变更 |
| `test_input_hook_failure_visibility` | `E1/短`：低级钩子安装失败必须可见——按键映射与取点器安装失败上报原因、取点覆盖层自动关闭 | `fukua_rpa/ui/input_tools.py`、`hotkey_controller.py` 钩子安装路径或 INP-FR-001 语义变更 |
| `test_profile_package` | 完整方案/图片压缩包安全 | 方案导入导出和路径重写 |
| `test_profile_paths` | 受管 `fukua-data://` 引用、路径穿越拒绝和数据目录移动 | 模板框选、图片路径解析或便携存储 |
| `test_json_safety` / `test_bounded_runtime_inputs` | 字节/深度/节点上限、重复键和非有限 JSON 拒绝 | 任何用户可控 JSON 解析器或剪贴板/设置 JSON |
| `test_profile_validation` | 语义预检和配置全有或全无地应用到界面 | 配置结构、设置绑定、方案切换或工作流导入 |
| `test_stability_foundation` | 原子主提交、0/1/N 备份保留、历史恢复和损坏保护 | 配置存储、备份数量、恢复或方案版本 |
| `test_workflow_document` | 稳定步骤身份、增删改引用、组内插入边界、独立单步/批量复制及合并 namespace 互斥、自身合并、目的/来源分组身份隔离、片段/三级嵌套与内部跳转重映射 | 步骤 ID、绝对跳转、复制、删除或合并方案 |
| `test_workflow_security` | 导入能力审查、危险动作禁用和受保护导出 | 工作流导入导出或隐私规则 |
| `test_execution_cancellation` | 可中断输入、提示取消和可重试输入释放 | 停止、鼠标/键盘动作、脚本提示 |
| `test_auto_clicker_stop` | `E1～E2/短`：连点器本地停止、主脚本停止域、系统全局启停热键、注册失败轮询回退和输入互斥 | 连点器窗口、独立工具线程、热键控制器或输入所有权 |
| `test_auto_clicker_behavior` | `E1/短`：连点器三级循环、秒制自定义间隔、时间/空间集中程度、矩形/椭圆落点、窗口宽度、保持时长和首次中键提示 | 连点器界面、随机分布模型或连点器执行器 |
| `test_profile_hotkeys` | `E1/短`：槽位 JSON 有界归一化、稳定方案 ID、重命名保持绑定、独立快照启动和连点器互斥 | 多方案快捷启动、方案增删改、全局热键或本机设置 |
| `test_mouse_trail` | `E1/短`：普通脚本拖影全局/单步覆盖、即时移动、局部停止和单一有界覆盖层 | 鼠标移动、视觉反馈、步骤能力、设置或 Runner 指针事件 |
| `test_runner_isolation` | 命名管道、Job 守卫、版本化 JSON IPC、PID/能力/服务握手、心跳、终局分类、崩溃恢复、调试、弹窗、急停和缩放记忆回传 | Runner 协议、传输、子进程、Worker、原生服务目录或运行控制器 |
| `test_core_regressions` 中的录制用例 | F8/Esc 吞键、钩子失败可见性和中止语义 | 录制钩子或录制覆盖层生命周期 |
| `test_release_tooling` | 版本身份、清单、压缩包、许可证和构建器 | 打包、spec、发布元数据 |
| `test_private_release_inputs` | 公开源码/私有验证资料双指纹；资料缺失、漂移、目录切换与构建/质量身份错配均拒绝，正式编译前检查 | `scripts/private_release_inputs.py`、`create_build_record.py`、`create_quality_report.py`、`build_release.py`、`verify_release.py` |
| `test_startup_bytecode_annotation` | 启动冒烟字节码条件标注：cold/partial/warm/unknown 分类、补测溯源、判定值不被覆盖 | `scripts/verify_release.py`（启动段） |
| `test_startup_lazy_runtime_modules` | `E1/短`：启动不导入 `validation` 与 `ui.overlays`（子进程实证）、控制器仍可用、首次运行校验与浮层仍工作、浮层关闭后清空、导入失败记日志且可重试 | `ui/controllers/workflow_review_controller.py`、`ui/controllers/feedback_controller.py`（ADR 0041） |
| `test_recognition_completion_import_failure` | `E2/短`：真实完成回调遇浮层导入失败仍清理识别状态、按原就绪/脚本活动条件恢复按钮；错误与未命中不加载浮层、失败不先关闭旧浮层；恢复模块后同一控制器经真实识别入口、线程和 Qt 完成信号可重试；空/失效/销毁行引用不阻断收尾 | `ui/controllers/feedback_controller.py`；登记于实际命中的 `smoke` 组（FR-20261023-STARTUP-B2-001） |
| `test_startup_copy_safety` | `E2/短`：测量副本/真实 CLI 在任何写入前拒绝两臂、DLL、目录联接和已有证据路径碰撞；新输出仍保持白名单、缓存、配对和首开协议；所有危险样本为测试自建标记 | `scripts/run_startup_ab.py`；保留未映射脚本的保守全量选择，不为返修缩小测试组（FR-20261023-STARTUP-B-002） |
| `test_startup_lazy_step_dialog` | `E1/短`：启动不导入步骤设置对话框子树（子进程 `sys.modules` 实证）、摘要无 Qt 依赖且单一实现、小齿轮首开仍可用、导入失败只记日志且不留半初始化状态、批量设置导入失败不触碰方案快照 | `ui/task_row.py`、`ui/task_row_controllers/*`、`ui/lazy_dialogs.py`、`ui/controllers/workflow_group.py`、`workflow_summary.py`（ADR 0040） |
| `test_startup_measurement` | 启动测量协议：A/B/C 分类、B 类五条缓存校验、隔离副本、5 次采样与污染判据 | `scripts/startup_measurement.py` |
| `test_review_r14_guards` | R14/R16 审查守护：29.0 脱敏、`validate_cache` 空清单、关窗后置条件、导入映射判定、序号宽度、采样探针（关窗「运行中」分支无单测，由审查方实测覆盖） | `workflow_security.py`、`main_window.py`、`workflow_transfer_controller.py`、`run_startup_baseline.py` |
| `test_plugin_config_validation` / `test_plugin_manifest_package` / `plugins/*/tests` | `E1～E2/中`：清单 schema、包完整性、注册表状态、宿主信封和官方插件动作契约；正式发布必须严格全跑 | 插件协议、安装/卸载、引用守卫、宿主、插件源码或插件构建脚本 |
| `test_test_impact` | 已提交/未提交路径选择和质量范围映射 | 影响规则、本地打包门禁或新增测试模块 |
| `test_review_workflow` | `E1～E2/短`：无效/重复元数据、头尾冲突、漏登待办、零执行/空输出、原始换行/二进制备份、原 A 收尾与同树检查点；含自有临时 Git/CLI 子进程 | 审查基准/模板、备份规范、AGENTS、`scripts/check_review_workflow.py`、开发依赖/CI |
| `test_test_collection_gap` | `E1/短`：测试收集缺口门禁——顶层 `def test_*` 函数不得被 unittest 静默跳过，0 用例不得判绿 | 新增或移动测试模块、测试框架接线、`smoke` 套件的收集口径 |
| `test_batch6_launch_and_lifecycle_guards` | `E1/短`：启动互斥单一判据（含识别测试项）、插件数值参数越界不被静默改写、计划日志「待补跑」分组、删除步骤关闭取点器且生产路径在测试兜底清理前停止线程、运行期设置窗口只读 | `input_tools.py`、`main_window.py`、`controllers/workflow_step_edit.py` 经 UI 组触发生命周期守护；原 Runner 组登记保留；启动入口守卫、插件动作参数控件、计划日志筛选、运行期只读 |
| `test_crash_guard` | 崩溃看门狗判定、干净退出标记、面包屑有界与脱敏、崩溃报告格式与 Win32 等待 | 崩溃日志独立化、启动/退出生命周期或看门狗子进程 |
| `test_pyright_plugin_baseline` | `E1/短`：FR-015 插件数据层 pyright 基线棘轮——确定性复算、路径卫生、基线只减不增、`--write` 守卫 | `scripts/pyright_plugin_baseline.py`、`pyright_plugin_baseline.json` 或插件数据层 pyright 口径 |
| `test_smoke_win32_prototypes` | `E1～E2/短`：Win32 原型声明的烟雾守护（64 位句柄/结构体契约的源码级检查），**含 fresh-process UIA 清理路径行为探针（子进程）** | `fukua_rpa/win32_api.py` 原型声明、相关烟雾入口或 UIA 清理路径 |
| `test_runtime_diagnostics_checks` | `E1/短`：运行自检检查项名称唯一、五项原生检查结构完整（故障 DLL 不得产生重复/缺失行）（FR-20261008-102/906） | `fukua_rpa/diagnostics.py`、`--self-test` 自检报告结构变更 |
| `test_workflow_transfer_controller` | `E1/中`：JSON/全量包导入导出控制器的导出侧结构校验与错误转译 | `fukua_rpa/ui/controllers/workflow_transfer_controller.py` 或导出导入接线 |
| `test_main_window_lifecycle_callbacks` | `E1/短`：主窗口延迟回调随宿主销毁取消（上次会话提示与错过补跑询问），宿主存活时提示/询问路径正常 | `fukua_rpa/ui/main_window.py` 的 singleShot/生命周期接线或 `lifecycle.schedule_widget_callback` |
| `test_batch7_doc_alignment_guards` | `E1/短`：乱码步进方向只用固定别名、未知值原样保留并由运行前校验拒绝（旧前缀猜测不得复活）；UIA X64 助手 DLL 不得硬编码 VC runtime 标签 | `fukua_rpa/task_model.py` 方向解析、`fukua_rpa/validation.py` 步进校验或 `fukua_rpa/diagnostics.py` 冻结包自检 |
| `test_batch8_log_category_reverse` | `E1/短`：A5 日志类别**反向**守护——关闭全部普通类别后错误/急停/严重警告仍必须入队；普通消息在入队前被过滤；未标注类别与无策略时不得丢消息；满载不阻塞且丢弃与 sink 异常计数独立；隐私与诊断脱敏不受影响 | `fukua_rpa/logging_service.py` 的 `write_log`/入队/背压、`fukua_rpa/log_policy.py` 或日志类别过滤接线 |
| `test_drag_image_cache_key_scale` | `E1/短`：拖拽起/终点图片的倍率缓存键承载倍率、与 `scale_options_cache` 同键空间、预计算与运行期产出同一键（FR-20261005-005） | `vision/multi_image.py` 的 `drag_image_cache_key`、`vision/preload.py` 拖拽预计算分支或 `actions/mouse.py` 拖拽解析 |
| `test_runner_finished_outcome` | `E1/短`：已自报 `finished` 终局的子进程不被归类 `forced_stop`；用户急停/启动失败/真实崩溃三类分类不受影响（FR-20261005-006、DEC-20260930-002-02） | `runner/isolated.py` 的 `_terminate_live_process` / `_build_exit_report` 或 Runner 终局阶梯 |
| `test_risk_ack_scope` | `E1/短`：循环风险确认的去重键属于被检查方案（多方案快捷启动传目标 id，手动运行回退当前方案）（FR-20261005-001） | `ui/controllers/workflow_review_controller.py` 的 `confirm_loop_risks` 或 `ui/controllers/profile_hotkey_controller.py` 的启动路径 |
| `test_engine_vision.py` 的 `MultiTargetCollectorTests` / `MultiTargetSortingTests` | `E1/短`：全部匹配收集阶段的双缓冲并行计时、同目标重复命中不重置末位缓冲、数量不足处理、有限总收集时间，以及第二排序条件只在主条件相同时生效（随机顺序禁用） | `fukua_rpa/vision/collect.py`、`vision/match.py` 的 `_sort_targets_for_click`、多目标收集参数或排序设置 |
| `test_execution_cancellation.py` 的 `InputBackendCompatibilityTests` | `E1/短`：显式可选 SendInput 后端默认关闭、单一 INPUT ABI 身份与唯一签名绑定（FR-20261016-SUP-001）、两处打桩点仍被实际调用、虚拟桌面绝对坐标映射（负原点/越界夹取）、方向键扩展标记与扫描码标记、移动后等待可中断且默认立即返回、非法输入方式被拒绝 | `fukua_rpa/input_backend.py`、`fukua_rpa/text_input.py`、`fukua_rpa/win32_input.py`、`fukua_rpa/engine/logging.py` 的 `configure_input_backend`、`actions/mouse.py` 的 `_settle_after_move` 或输入方式设置 |
| `test_input_timeline.py` 的 `TimelineConversionTests` / `TimelineStorageTests` / `TimelinePlaybackTests` | `E1/短`：自动重复、多键重叠、键鼠重叠（C3）与**任何采集到移动的拖拽**（C5）触发时间线（相对时间、真实顺序）；纯点击与单键长按仍走既有步骤（未命中当前触发条件；点击按住时长等不在支持范围）；五类拖拽（匀速直线/先停顿后移动/直线变速/越过终点再折返/弯曲）在保存加载后回放仍保持顺序、相对时刻与坐标；孤立 repeat 补同刻 down；JSON 往返保真与校验（未释放键/按钮、无前导 down 的 repeat、乱序、越界时间、非法坐标均拒绝）；回放按事件顺序与时刻派发（含先定位再按下、滚轮符号）、按住期间其他事件不打断、停止时逆序释放按键与鼠标按钮；采集→转换→保存→加载→回放全链路 | `recording_model.py` 的 `recorded_events_to_timeline` / `build_recording_tasks`、`actions/input_timeline.py`、`validation.py` 的时序分支、`ui/input_tools.py` 的 `raw_input_events` 与拖拽采样 |
| `test_recording_hold_capture.py` 的 `RecordingKeyHoldCaptureTests` | `E1/短`：按键事件的 `ended` 由抬起回填（按住时长可测量）；录制结束时未抬起的键在结束点补齐释放记录且不触碰真实按键；录得的时长经转换进入步骤级 `key_hold_duration` 并可被执行器解析 | `ui/input_tools.py` 的录制 `keyboard_handler` / `truncate_pending_key_events`，或 `recording_model.py` 的 `recorded_events_to_tasks` |
| `test_key_mapping_tool.py` 的 `KeyMappingToolMigrationTests` | `E1/短`：设置页只保留入口链接且无映射编辑控件；支持链接三项留在设置页；工具窗口收揽映射区与裸键开关并显示当前方案名；关闭只隐藏、重开不重复连接；切换方案刷新方案名；行编辑（增行/启停）在窗口内可用 | `ui/key_mapping_tool.py`、`ui/settings_sections/mapping.py` 的拆分函数、`ui/settings_view.py`、`ui/plugin_helpers.py` 工具菜单、`ui/main_window.py` 的 `show_key_mapping_tool_window`，或方案切换路径 |
| `test_integrity` | 打包载荷路径和哈希边界 | 完整性清单或冻结载荷 |
| `test_performance_comparison` | 基准正确性断言和耗时阈值 | 调度/预览基准、OpenCV/原生匹配或性能门禁 |
| `test_benchmark_util_affinity` | `E1/短`：基准进程核心类别亲和的**运行时推导**（取机器自报 `EfficiencyClass` 最大类，掩码不得硬编码）与**读回自检**（任何不一致必须显式中止，不得静默退化到未绑定运行）；`all_flags` 只作原始字节、不参与判定 | `scripts/benchmark_util.py` 的类别推导/亲和自检，或 `benchmark_vision`/`benchmark_core`/`benchmark_native_scheduler` 入口的绑定调用 |

原生取消还有一项真实 DLL 烟雾门禁：`scripts/smoke_native_cancellation.py`。它会故意启动高负载搜索，从另一线程发出取消，并要求在 300ms 内返回结果码 `-3`。修改原生搜索循环或 ABI 时必须运行。`scripts/smoke_native_capture.py` 会在纯色真实窗口上核对原生 RGB 截图的尺寸、颜色与通道顺序；修改截图 ABI 时必须运行。

| `test_c1_settings_two_phase` | `E1/短`：设置对话框**延迟构建**后的两段式守护——无对话框时 capture 不写空映射、apply 只改宿主态、首开显示方案值而非控件默认值、高级模式不降级为简易、预置对话框被收养且只绑定一次、构建失败不留半成品且可重试 | `ui/main_window.py`、`ui/settings_binding.py`、`ui/settings_sections/*`、`ui/controllers/log_controller.py`、`controllers/settings_controller.py`、`tests/qt_test_support.py` 的延迟构建/两段式行为变更 |
