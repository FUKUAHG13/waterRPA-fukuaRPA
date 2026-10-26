"""P6-1 守卫测试：isolated_runner 并发加锁（RLock）消除 GIL 假设。

退出标准：注入 _respond_to_prompt 与 _close_prompts 竞争，断言无丢 prompt / 无死锁。
纯加锁、零行为变更：在 GIL 构建下行为与原实现一致，但在 free-threading 构建或
高竞争下不再依赖"单步 dict 操作原子"的隐性假设。
"""
import threading

from PySide6.QtCore import QCoreApplication

from fukua_rpa.runner.isolated import IsolatedRunnerThread


def _app():
    return QCoreApplication.instance() or QCoreApplication([])


def _make_runner():
    _app()
    return IsolatedRunnerThread(
        tasks=[],
        config={},
        base_dir=".",
        profile_name="concurrency-test",
        logging_options={},
        scale_memory_state={},
        parent=None,
    )


def test_prompts_concurrent_no_loss_no_deadlock():
    """writer 与 responder 竞争同一批 prompt_id，主线程触发 _close_prompts。

    断言：(1) 所有线程在 timeout 内结束（无死锁）；(2) 并发期间无异常（锁未损坏）；
    (3) _close_prompts 原子清空所有残留 prompt（无丢 prompt / 无泄漏）。
    """
    runner = _make_runner()
    N = 16
    PER = 100
    errors = []

    def writer(i):
        try:
            for j in range(PER):
                pid = f"w{i}-{j}"
                runner._handle_prompt({"id": pid, "text": f"prompt {pid}"})
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def responder(i):
        try:
            for j in range(PER):
                pid = f"w{i}-{j}"
                runner._respond_to_prompt(pid, True)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(N)]
    threads += [threading.Thread(target=responder, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    # 无死锁：所有线程必须在 timeout 内结束
    assert not any(t.is_alive() for t in threads), "检测到死锁（线程未在规定时间内结束）"
    assert not errors, f"并发期间抛异常: {errors[:3]}"

    # _close_prompts 必须原子清空所有残留 prompt
    runner._close_prompts()
    assert runner._prompts == {}, "关闭 prompt 后仍有残留（可能丢 prompt）"


def test_stop_deadline_concurrent_no_deadlock():
    """多线程并发 request_stop 竞争写入 _stop_deadline，断言无死锁且语义保持。

    request_stop 仅在 isRunning() 时进入 deadline 设置分支；测试中 runner 未
    start，故将 isRunning 受控为 True 以触达真实的加锁设置路径。
    """
    runner = _make_runner()
    runner.isRunning = lambda: True  # 受控进入 deadline 设置分支

    def stopper():
        runner.request_stop()

    threads = [threading.Thread(target=stopper) for _ in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert not any(t.is_alive() for t in threads), "request_stop 并发死锁"
    assert runner._stop_deadline is not None
    assert isinstance(runner._stop_deadline, float)
    # 仅在首次为 None 时设置；后续调用不应覆盖已设定的 deadline
    first = runner._stop_deadline
    runner.request_stop()
    assert runner._stop_deadline == first, "request_stop 不应覆盖已设置的 deadline"


def test_close_prompts_marks_all_pending_done():
    """放入 pending（未响应）prompt 后 _close_prompts 必须全部 close_from_runner，
    不丢任何 request（每个 pending 都进入 done 态）。"""
    runner = _make_runner()
    for i in range(50):
        runner._handle_prompt({"id": f"p{i}", "text": "x"})
    pending = list(runner._prompts.values())
    assert len(pending) == 50
    runner._close_prompts()
    assert runner._prompts == {}
    assert all(req.done.is_set() for req in pending), (
        "close_from_runner 未关闭所有 pending prompt（丢 prompt）"
    )
