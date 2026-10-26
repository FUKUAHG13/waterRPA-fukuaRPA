# fukuaRPA 领域上下文

## 项目定位

- fukuaRPA 是面向非程序员的 Windows 桌面自动化工具，核心交互由图形界面、步骤列表、坐标操作和图片识别组成。
- 当前主线源码位于 `fukua_rpa/`，支持从同一套业务源码构建 onedir 与 onefile。其他开发者可以在明确记录功能和依赖边界的前提下探索轻量版、旧系统版或其他变体。
- GitHub Releases 保存已经走完发布验证的二进制包；公开仓库 `main` 只对应用户明确批准公开的稳定源码，未完成真实场景验证的开发改动保存在私有 `origin` 的开发分支。
- 当前主线程序自身始终不联网，不访问互联网或局域网；独立 Runner 只进行本机进程间通信。作者主页和项目链接只在用户主动点击后交给系统浏览器打开，网页访问由浏览器完成。

## 核心术语

- **方案（Profile）**：一组全局设置、按键映射和步骤的可保存配置。
- **凭据引用（Credential Reference）**：秘密文本步骤中保存的凭据名称。名称可以随普通方案和完整迁移
  包迁移，目标电脑据此重新创建同名凭据；DPAPI 密文和明文秘密值始终位于方案之外。
- **动作/指令类型（Command Type）**：由 `commands.py` 定义的稳定浮点编号和名称；编号标识动作种类，不标识工作流中的某个步骤实例。
- **步骤实例（Task/Step）**：工作流中一次具体的动作配置，以稳定 `step_id` 标识；界面步号只表示当前显示顺序。
- **运行请求（RunRequest）**：启动瞬间从界面数据深拷贝得到的不可变运行快照。
- **运行生命周期（RunLifecycle）**：管理唯一 `run_id`、准备/运行/停止状态和停止事件的线程安全对象。
- **原子备份（Atomic Backup）**：更新 `QSettings` 前完整替换的 `profiles_backup.json`。
- **历史备份（Profile History）**：原子备份被替换前保存的有限数量只读快照。
- **识别回退（Vision Fallback）**：原生 DLL 无法安全完成时改用 Python/OpenCV；原生成功但无结果时不回退。
- **物理坐标（Physical Coordinate）**：Windows 虚拟桌面像素坐标，可包含副屏负坐标。
- **Qt 坐标（Qt Coordinate）**：仅用于界面与覆盖层绘制的逻辑坐标。
- **显式窗口绑定（Explicit Window Binding）**：用户单击目标程序得到的窗口身份，与映射点击坐标分开保存和验证。
- **Hook 键状态（Hook Key State）**：由低级键盘 Hook 的按下/抬起事件维护的修饰键集合，不依赖异步状态读取时序。
- **全量包（Full Package）**：包含方案、图片资产、清单和完整性哈希的可移植 ZIP。
- **性能快照（Performance Snapshot）**：每项最多保留 256 个耗时样本的 P50/P95、计数器和缓存统计。
- **运行轨迹（Run Trace）**：最多 2000 项、不记录步骤值或路径的步骤结果与跳转序列。
- **窗口检查器（Window Inspector）**：读取已绑定根窗口、目标控件、子控件和完整性级别的诊断视图。
- **UIA 控件动作（UIA Control Action）**：在专用 COM 线程中对坐标处标准控件执行 Invoke/Selection/Toggle/DefaultAction。
- **调试会话（Debug Session）**：协调步骤前断点、暂停、继续、单步和停止唤醒的线程安全状态。
- **条件断点（Conditional Breakpoint）**：仅当安全表达式为真时在步骤执行前暂停的断点；求值错误也暂停并解释原因。
- **运行变量（Runtime Variable）**：仅在一次脚本运行内存在、由受限表达式读写的值。
- **发布物料（Release Materials）**：构建记录、SBOM、许可证、签名状态、PE 依赖闭包、包内哈希和包外清单的集合。
- **运行时闭包（Runtime Closure）**：发行目录中全部 EXE/DLL/PYD 的 AMD64 架构及非系统导入均可在包内解析的状态。
- **原生能力掩码（Native Capability Mask）**：DLL 声明的 **20 个**稳定能力位，当前覆盖截图、匹配、缓存、计时、工作预算、ABI 元数据、有界任务池、优先倍率回退与专搜、低分辨率画面指纹、DXGI 画面变化通知、可取消搜索、OpenCV 彩色语义和打包 RGB 截图；权威定义见 `fukua_rpa/vision/core.py` 的 `NATIVE_CAPABILITY_BITS`。位与 API 版本必须逐项区分：低分辨率画面指纹自 **API 11100** 起声明，DXGI 画面变化通知自 **API 11200** 起声明，虚拟桌面边界查询与打包 RGB 截图自 **API 11500** 起声明；**不得**把 11100 与 11200 合并表述。
- **原生任务池（Native Match Job Pool）**：一次 DLL 调用内统一调度缩放档位、区域和结果行块的最多 8 线程工作队列，不绑定具体 CPU 核心。
- **缩放记忆（Scale Memory）**：快速一个模式在本次进程内按频率、近期程度和质量学习多个常用倍率；手动项排在学习项之前，失败后继续完整范围。
- **画面变化唤醒（Scene Wake）**：只在自适应额外等待中用 DXGI 通知加分块指纹确认变化，确认后尝试常用倍率专搜，未命中仍立即完整搜索。
- **启动阶段（Startup Stage）**：轻量首帧、完整工作区可见、运行后端就绪三个可独立计时和验证的启动节点。
- **原生 ABI 契约（Native ABI Contract）**：DLL 与 Python 对指针位数、结构体大小和构建标志的机器可验证约定。
- **原生服务目录（Native Service Catalog）**：不含路径和错误正文的版本化接口，向 Runner 报告截图、视觉、输入与 Hook 的契约版本、能力、取消方式、超时和回退状态。
- **便携发行归档（Portable Release Archive）**：以构建目录为唯一根、按外部清单逐成员复核的完整 onedir ZIP。
- **双构建边界（Dual Package Boundary）**：同一套完整版源码分别生成 onedir 与 onefile；两种构建共享功能、版本和发行态烟雾测试。
- **OpenCV 线程预算（OpenCV Thread Budget）**：进程级最多 2 个 OpenCV 工作线程，与识别间隔等待相互独立。
- **日志策略（Log Policy）**：简易/详细/完全预设或用户自定义类别组成的运行快照；在异步入队前过滤，严重错误与急停始终输出。
- **动作设置能力（Action Setting Capability）**：由 `commands.py` 统一声明某种动作可使用的图片、坐标路径、截图或条件设置；原始草稿保留，运行副本屏蔽无关项。
- **Runner 会话（Runner Session）**：向主界面提供统一启动、停止和调试接口的执行后端；当前包括默认进程内 Worker 与默认关闭的独立子进程实验后端。
- **Runner IPC**：独立 Runner 使用的随机认证 Windows 命名管道和版本化、有大小上限的 JSON 消息协议，不打开 TCP 端口，不传递 pickle 或可执行 Python 对象。
- **Runner 能力握手**：协议 v3 在开始执行前核对实际子进程 PID、必需命令/事件能力、协议版本和四类服务目录，不兼容时明确失败且不静默回退。
- **Runner 心跳**：子进程定期回传不含脚本内容的阶段与运行状态；心跳迟到只标记暂时无响应，用户急停超时后才允许强制结束。
- **Runner Job 守卫（Runner Job Guard）**：主进程持有带退出清理标志的 Windows Job Object；主进程异常退出时结束 Runner，但允许工作流启动的外部程序脱离。
- **Runner 终局（Runner Outcome）**：一次运行明确归类为正常完成、用户停止、强制停止、启动失败或崩溃；崩溃不会自动重放脚本。
- **计划任务（Scheduled Task）**：通过稳定 `profile_id` 指向方案的本地触发规则；只在 fukuaRPA 运行且
  桌面会话可用时生效，不是 Windows 服务。
- **计划运行协调器（Scheduled Run Coordinator）**：复用唯一运行入口和输入互斥，处理跳过、每计划最多
  一次待补、运行结束排空与看门狗恢复的应用级对象。
- **插件清单（Plugin Manifest）**：声明插件 ID、版本、动作、参数和权限的有界静态契约，不允许携带任意
  Qt 控件或 Python 回调。
- **插件宿主（Plugin Host）**：通用插件动作使用的执行边界；当前主线仍是只校验请求的离线桩，未来可执行
  实现必须位于独立、版本化且可取消的宿主中。
- **独立输入工具（Standalone Input Tool）**：连点器等复用动作能力但拥有独立局部停止状态的工具；与主
  脚本互斥取得输入所有权，不能用工具停止污染之后的脚本运行。
- **方案快捷键槽位（Profile Hotkey Slot）**：本机全局设置中通过稳定 `profile_id` 绑定方案、系统级
  启动热键和可选停止热键的有界记录；触发时运行方案快照，不切换当前编辑页面，也不创建第二个主进程。
- **集中程度（Concentration）**：0～100 的随机分布密度参数；0 表示配置范围内均匀分布，数值越大越
  倾向中心。时间间隔和空间落点分别保存自己的值，但共享同一数值语义和预设。
- **鼠标移动拖影（Mouse Trail）**：普通脚本前台鼠标移动的短暂蓝色视觉轨迹；不改变实际运动路径，
  不应用于后台点击或用户手动移动。
- **输入时序（Input Timeline）**：由录制在自动重复、多键重叠、键鼠重叠或拖拽含移动采样时生成的
  步骤（29.0），保存相对时间与真实事件顺序；回放按事件时刻单线程派发并自行逆序释放。
- **全局按键映射（Shared Key Mapping）**：所有方案共用同一套按键映射，保存时以当前方案为权威镜像
  到共享键与全部方案，切换方案不改变映射内容。
- **默认设置模板（Default Settings Template）**：用户保存的全局设置快照，只用于新建方案时叠加在
  出厂默认之上；不含步骤列表、方案名与按键映射。

## 关系

```text
Profile -> RunRequest -> EngineRunConfig + detached Tasks
ScheduledTask -> ScheduledRunCoordinator -> shared RunController -> RunRequest
ProfileHotkeySlot -> shared HotkeyController -> detached Profile RunRequest
Tasks -> RunController -> RunnerBackend -> WorkerThread / IsolatedRunner -> RPAEngine
IsolatedRunner -> Authenticated Named Pipe -> RunnerProcess -> RPAEngine
RPAEngine -> Screenshot / Vision / Input / Hook Service Contracts
Profile -> atomic backup -> QSettings -> bounded history
MainWindow -> ProfileCollection / PreviewModel / WindowMappingBackend
Engine -> PerformanceMetrics / RunTrace
Engine -> LogPolicy -> LoggingService / LogTelemetry
Engine -> DebugSession / SafeExpression / ScaleMemoryStore / SceneWake / NativeVisionCore
PluginAction -> PluginRegistry / PluginHostClient -> bounded PluginHost
StandaloneInputTool -> shared action implementation + isolated stop predicate
WindowMappingBackend -> WindowInspector / UIAutomationBackend / PostMessage
ReleaseBuilder -> RuntimeClosure / BuildRecord / SBOM / PayloadHashes / ExternalManifest -> PortableArchive / SHA256SUMS
```

## 当前格式边界

- 当前主线构建目标为 Windows 10 1809+ x64 与 Windows 11 x64；其他系统需要独立依赖选择、构建目标和验证矩阵。
- 任意软件更新均不承诺兼容更早版本的方案，也不需要等到大版本才允许提高最低方案格式。当前仍存在的旧格式读取和迁移属于尽力支持，不构成后续版本义务。
- 不受支持的方案必须明确拒绝或进入只读保护，不能半加载、自动保存或覆盖原配置。若某次更新仍提供迁移，迁移前必须验证原始数据并保留原文件或备份。
- 当前主线的原生 DLL 是可选加速层，加载、能力或运行检查失败时使用 Python/OpenCV 回退。
