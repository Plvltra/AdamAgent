"""T6.1：真实读取的并发、串行屏障及失败清理；聊天接入在 T6.2 验收。"""

from threading import Barrier, Event, Lock, Thread, current_thread, get_ident

import pytest

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted
from adam.contracts.execution import ToolCapability
from adam.tool_calls.scheduler import ToolCleanupError, ToolScheduler, ToolTask


def task(call_id, action, *, parallel=True, capability=ToolCapability.READ_ONLY):
    return ToolTask(call_id, lambda: action, capability, parallel)


def test_file_queries_overlap_refill_slots_and_deliver_original_order(tmp_path):
    """第二项先结束就能派发第三项，无需等待第一项；日志回调仍按原顺序。"""
    files = [tmp_path / f"{i}.txt" for i in range(4)]
    for index, path in enumerate(files):
        path.write_text(f"file-{index}", encoding="utf-8")
    coordinator = get_ident()
    pair = Barrier(2)
    third_started = Event()
    mutex = Lock()
    active = peak = 0
    finished, prepared, delivered, worker_threads = [], [], [], []

    def prepare(index):
        prepared.append((index, get_ident()))

        def read(cancel):
            nonlocal active, peak
            with mutex:
                active += 1
                peak = max(peak, active)
                worker_threads.append(get_ident())
            try:
                if index < 2:
                    pair.wait(timeout=3)
                if index == 0:
                    assert third_started.wait(3)
                if index == 2:
                    third_started.set()
                cancel.check()
                return files[index].read_text(encoding="utf-8")
            finally:
                with mutex:
                    finished.append(index)
                    active -= 1

        return read

    jobs = [
        ToolTask(f"call-{i}", lambda i=i: prepare(i), ToolCapability.READ_ONLY, True)
        for i in range(4)
    ]
    results = ToolScheduler().run(
        jobs, on_result=lambda result: delivered.append((result.call_id, get_ident()))
    )
    assert peak == 2 and active == 0
    assert all(thread != coordinator for thread in worker_threads)
    assert prepared == [(i, coordinator) for i in range(4)]
    assert delivered == [(f"call-{i}", coordinator) for i in range(4)]
    assert [(result.call_id, result.value) for result in results] == [
        (f"call-{i}", f"file-{i}") for i in range(4)
    ]
    assert finished.index(1) < finished.index(0)


@pytest.mark.parametrize("capability", list(ToolCapability))
def test_undeclared_or_stateful_action_is_a_serial_barrier(capability):
    """包括只读但未承诺线程安全的扩展：不能与前后读取重叠。"""
    pair = Barrier(2)
    coordinator = get_ident()
    mutex = Lock()
    events = []
    active = 0

    def read(label, cancel):
        nonlocal active
        with mutex:
            active += 1
        try:
            pair.wait(timeout=3)
            cancel.check()
            return label
        finally:
            with mutex:
                active -= 1
                events.append(label)

    def serial(_cancel):
        assert get_ident() == coordinator
        with mutex:
            assert active == 0 and set(events) == {"a", "b"}
            events.append("serial")
        return "serial"

    jobs = [
        task("a", lambda cancel: read("a", cancel)),
        task("b", lambda cancel: read("b", cancel)),
        task("serial", serial, parallel=False, capability=capability),
        task("c", lambda cancel: read("c", cancel)),
        task("d", lambda cancel: read("d", cancel)),
    ]
    results = ToolScheduler().run(jobs)
    assert events[2] == "serial" and set(events[3:]) == {"c", "d"}
    assert [result.value for result in results] == ["a", "b", "serial", "c", "d"]


def test_business_error_is_a_result_and_does_not_cancel_other_queries(tmp_path):
    target = tmp_path / "present.txt"
    target.write_text("evidence", encoding="utf-8")
    pair = Barrier(2)

    def read(path, cancel):
        pair.wait(timeout=3)
        cancel.check()
        try:
            return {"ok": True, "text": path.read_text(encoding="utf-8")}
        except FileNotFoundError:
            return {"ok": False, "error": "missing"}

    results = ToolScheduler().run(
        [
            task("missing", lambda cancel: read(tmp_path / "missing.txt", cancel)),
            task("present", lambda cancel: read(target, cancel)),
        ]
    )
    assert [result.value for result in results] == [
        {"ok": False, "error": "missing"},
        {"ok": True, "text": "evidence"},
    ]


@pytest.mark.parametrize("failure_at", ["worker", "prepare", "result", "start", "interrupt"])
def test_fatal_boundary_stops_pending_work_and_joins_running_reader(failure_at, monkeypatch):
    """注入程序、预检、结果落盘回调或线程创建失败，实际读取线程必须退出。"""
    entered, stopped = Event(), Event()
    error = KeyboardInterrupt() if failure_at == "interrupt" else RuntimeError(failure_at)
    prepared, delivered, live = [], [], []
    parent = CancellationToken()
    scheduler = ToolScheduler(cancel=parent)

    def wait_for_cancel(cancel):
        live.append(current_thread())
        entered.set()
        try:
            cancel.wait(5)
            pytest.fail("running reader was not cancelled")
        finally:
            stopped.set()

    def fail():
        assert entered.wait(3)
        raise error

    if failure_at == "worker":
        jobs = [task("a", lambda _: fail()), task("b", wait_for_cancel)]
    elif failure_at == "prepare":
        jobs = [
            task("a", wait_for_cancel),
            ToolTask("b", fail, ToolCapability.READ_ONLY, True),
        ]
    elif failure_at == "start":
        original_start = Thread.start

        def start(thread):
            if thread.name == "adam-read-b":
                fail()
            original_start(thread)

        monkeypatch.setattr(Thread, "start", start)
        jobs = [task("a", wait_for_cancel), task("b", lambda _: "never")]
    else:

        def first(_cancel):
            assert entered.wait(3)
            return "a"

        jobs = [task("a", first), task("b", wait_for_cancel)]

    def never_prepare():
        prepared.append("unexpected")
        return lambda _: "never"

    jobs.append(ToolTask("pending", never_prepare, ToolCapability.READ_ONLY, True))

    def save(result):
        delivered.append(result.call_id)
        if failure_at in {"result", "interrupt"}:
            raise error

    with pytest.raises(type(error)) as raised:
        scheduler.run(jobs, on_result=save)
    assert raised.value is error
    assert stopped.is_set() and prepared == []
    assert live and all(not thread.is_alive() for thread in live)
    assert not parent.cancelled
    assert delivered == (["a"] if failure_at in {"result", "interrupt"} else [])
    assert scheduler.run([task("next", lambda _: "reusable")])[0].value == "reusable"


@pytest.mark.parametrize("parallel", [True, False])
def test_parent_cancellation_stops_and_joins_current_tasks(parallel):
    cancel = CancellationToken()
    participants = 2 if parallel else 1
    ready = Barrier(participants + 1)
    stopped = []
    threads = []
    pending = []
    mutex = Lock()

    def wait(token):
        threads.append(get_ident())
        try:
            ready.wait(timeout=3)
            token.wait(5)
            pytest.fail("cancel not delivered")
        finally:
            with mutex:
                stopped.append(get_ident())

    def interrupt():
        ready.wait(timeout=3)
        cancel.cancel()

    controller = Thread(target=interrupt)
    controller.start()
    jobs = [task(str(i), wait, parallel=parallel) for i in range(participants)]
    jobs.append(
        ToolTask("pending", lambda: pending.append("wrong"), ToolCapability.READ_ONLY, parallel)
    )
    try:
        with pytest.raises(RequestInterrupted):
            ToolScheduler(cancel=cancel).run(jobs)
    finally:
        cancel.cancel()
        controller.join(timeout=3)
    assert not controller.is_alive()
    assert sorted(stopped) == sorted(threads) and len(stopped) == participants
    assert pending == []


def test_pre_cancelled_batch_performs_no_preparation():
    cancel = CancellationToken()
    cancel.cancel()
    prepared = []
    with pytest.raises(RequestInterrupted):
        ToolScheduler(cancel=cancel).run([ToolTask("a", lambda: prepared.append("wrong"))])
    assert prepared == []


def test_cleanup_timeout_reports_survivor_and_refuses_new_work():
    """故意不检查令牌，确认未把 join 超时当作已退出；测试自身最终释放并回收线程。"""
    entered, release, stopped = Event(), Event(), Event()
    worker_threads = []
    primary = RuntimeError("prepare failed")
    scheduler = ToolScheduler(cleanup_timeout_seconds=0.03)

    def uncooperative(_cancel):
        worker_threads.append(current_thread())
        entered.set()
        try:
            assert release.wait(5)
            return "late"
        finally:
            stopped.set()

    def fail():
        assert entered.wait(3)
        raise primary

    try:
        with pytest.raises(ToolCleanupError) as raised:
            scheduler.run(
                [
                    task("stuck", uncooperative),
                    ToolTask("failure", fail, ToolCapability.READ_ONLY, True),
                ]
            )
        assert raised.value.call_ids == ("stuck",)
        assert raised.value.__context__ is primary
        assert worker_threads[0].is_alive() and not stopped.is_set()
        with pytest.raises(ToolCleanupError):
            scheduler.run([task("new", lambda _: pytest.fail("must not start"))])
    finally:
        release.set()
        for thread in worker_threads:
            thread.join(timeout=3)
    assert stopped.is_set() and not worker_threads[0].is_alive()


def test_second_interrupt_during_cleanup_preserves_original_failure(monkeypatch):
    """在线程 join 边界注入再次中断，仍须回收真实线程并保留最初的预检故障。"""
    entered = Event()
    live, interruptions = [], []
    primary = RuntimeError("precheck failed")
    original_join = Thread.join

    def wait(cancel):
        live.append(current_thread())
        entered.set()
        cancel.wait(5)

    def fail():
        assert entered.wait(3)
        raise primary

    def join(thread, timeout=None):
        if thread.name == "adam-read-running" and not interruptions:
            interruptions.append(True)
            raise KeyboardInterrupt()
        return original_join(thread, timeout)

    monkeypatch.setattr(Thread, "join", join)
    with pytest.raises(RuntimeError) as raised:
        ToolScheduler().run(
            [
                task("running", wait),
                ToolTask("failure", fail, ToolCapability.READ_ONLY, True),
            ]
        )
    assert raised.value is primary
    assert interruptions == [True]
    assert live and all(not thread.is_alive() for thread in live)


def test_single_slot_preserves_order_and_reentrant_run_is_rejected():
    scheduler = ToolScheduler(max_concurrent=1)
    actions = []

    def action(index, cancel):
        cancel.check()
        actions.append(index)
        with pytest.raises(RuntimeError, match="重入"):
            scheduler.run([])
        return index

    results = scheduler.run([task(str(i), lambda c, i=i: action(i, c)) for i in range(3)])
    assert actions == [0, 1, 2]
    assert [result.value for result in results] == actions
