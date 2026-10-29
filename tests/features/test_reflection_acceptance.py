"""E4.6：同任务开销与新任务复用的工程对照；脚本模型不证明真实模型学习效果。"""

import json
import sys
from dataclasses import replace

import httpx
import pytest

from adam.config.settings import ReflectionConfig
from scripts.reflection_eval import CHECKER, prepare, report
from tests.features.test_memory_context import config_at, memory_records
from tests.features.test_streaming import DONE, event, sse
from tests.features.test_subagents import call
from tests.features.test_task_planning import plan_app
from tests.features.test_tool_loop import answer, tool_response

LESSON = {
    "problem": "加法检查返回非零",
    "hypothesis": "加法代码可能误用了减法",
    "action": "读取运算符并仅修正需要加法的位置",
    "verification": "运行独立检查脚本",
    "lesson": "加法异常时先核对运算符，再用独立检查验证，不能把所有减号改成加号",
    "applies_when": "当前项目的加法任务",
    "limits": "未验证为通用规则；差值和其他任务不适用",
}


@pytest.mark.parametrize("enabled", [False, True])
def test_real_repair_transfer_and_counterexample_with_scripted_model(tmp_path, enabled):
    prepare(tmp_path / "eval")
    arm = tmp_path / "eval" / ("reflection" if enabled else "baseline")
    workspace = arm / "workspace"
    cfg = config_at(workspace)
    cfg = replace(cfg, sessions_dir=arm / "sessions", reflection=ReflectionConfig(enabled=enabled))
    bodies, stages, metrics = [], {}, {}

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "tools" not in body:
            return httpx.Response(200, json=answer(json.dumps(LESSON)))
        target = stages["target"]
        observed = {
            m["tool_call_id"]: json.loads(m["content"])
            for m in body["messages"]
            if m["role"] == "tool"
        }

        def command(ident):
            return call(ident, "run_command", argv=[sys.executable, "-B", "check.py", target])

        if not observed:
            calls = [
                call(
                    "read",
                    "read_file",
                    path={"counterexample": "difference.py"}.get(target, target + ".py"),
                )
            ]
            # 这是假模型的受控策略：相关经验可跳过一次已知会失败的检查。
            # 此差异仅验证程序能传入经验并改变执行路径，不宣称真实模型也会节省。
            if target != "transfer" or not memory_records(body):
                calls.append(command("first_check"))
            data = tool_response(calls)
        elif target == "counterexample":
            assert observed["first_check"]["exit_code"] == 0
            data = answer("差值保持减法")
        elif "edit" not in observed:
            if "first_check" in observed:
                assert observed["first_check"]["exit_code"] != 0
            data = tool_response(
                [
                    call(
                        "edit",
                        "replace_text",
                        path=target + ".py",
                        old_text="return a - b" if target == "train" else "result -= value",
                        new_text="return a + b" if target == "train" else "result += value",
                        expected_version=observed["read"]["version"],
                    )
                ]
            )
        elif "verified" not in observed:
            data = tool_response([command("verified")])
        else:
            assert observed["verified"]["exit_code"] == 0
            data = answer("独立检查通过")
        return httpx.Response(200, json=data)

    with plan_app(workspace, handler, config=cfg, confirm=lambda _: True) as (app, store):
        for target in ("train", "transfer", "counterexample"):
            stages["target"] = target
            app.new(target)
            before = len(bodies)
            app.send(
                "加法任务 " + target
                if target != "counterexample"
                else "加法经验是否适用差值？差值必须保持减法"
            )
            sid = app.session.session_id
            metrics[target] = len(bodies) - before
            codes = [e["exit_code"] for e in store.load_executions(sid)]
            if target == "train":
                assert codes == [1, 0]
                assert metrics[target] == (5 if enabled else 4)
                if enabled:
                    candidate = app.experience_candidates()[0]
                    app.decide_experience(candidate["reflection_id"], "accept")
            elif target == "transfer":
                assert codes == ([0] if enabled else [1, 0])
                assert metrics[target] == 4  # 少一次失败工具，但同批读文件使模型请求数没有减少。
                if enabled:
                    assert memory_records(bodies[before])
            else:
                assert codes == [0] and store.load_edits(sid) == []
        assert "a - b" in (workspace / "difference.py").read_text()
        assert (workspace / "check.py").read_text(encoding="utf-8") == CHECKER
    summary = report(tmp_path / "eval")["arms"]["reflection" if enabled else "baseline"]
    assert summary["checker_unchanged"]
    assert all(t["status"] == "succeeded" and not t["unknown_usage"] for t in summary["turns"])
    assert sum(t["reflection_requests"] for t in summary["turns"]) == (1 if enabled else 0)
    assert sum(t["requests"] for t in summary["turns"]) == sum(metrics.values())


def test_manual_evaluation_prepare_is_isolated_and_report_does_not_execute(tmp_path):
    root = tmp_path / "eval"
    prepare(root)
    state = report(root)
    assert all(a["state"] == "not_run" for a in state["arms"].values())
    assert all(a["checker_unchanged"] for a in state["arms"].values())
    with pytest.raises(FileExistsError):
        prepare(root)
    assert not list(root.rglob("llm.toml"))
    assert not list(root.rglob("*.jsonl"))


@pytest.mark.parametrize("stream", [False, True])
def test_command_parameter_error_can_reflect_without_fabricating_execution(tmp_path, stream):
    bodies, output = [], []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            data = tool_response([call("bad", "run_command", argv=[])])
        elif len(bodies) == 2:
            assert "tools" not in body
            facts = json.loads(body["messages"][-1]["content"])
            assert facts["tools"][0]["execution"] == {}
            data = answer(json.dumps(LESSON))
        else:
            data = answer("参数无效，命令未执行")
        if stream:
            delta = dict(data["choices"][0]["message"])
            if calls := delta.get("tool_calls"):
                delta["tool_calls"] = [{"index": index, **item} for index, item in enumerate(calls)]
            return sse(
                [
                    event(delta),
                    event(
                        reason="tool_calls" if delta.get("tool_calls") else "stop",
                        usage=data["usage"],
                    ),
                    DONE,
                ]
            )
        return httpx.Response(200, json=data)

    cfg = replace(config_at(tmp_path), reflection=ReflectionConfig())
    cfg = replace(cfg, llm=replace(cfg.llm, stream=stream))
    with plan_app(
        tmp_path,
        handler,
        config=cfg,
        confirm=lambda _: pytest.fail("invalid arguments need no approval"),
    ) as (app, store):
        app.send("检查参数", on_output=output.append)
        if stream:
            assert "".join(item.text for item in output) == "参数无效，命令未执行"
        assert len(bodies) == 3
        assert store.load_executions(app.session.session_id) == []
        assert len(store.load_reflections(app.session.session_id)) == 1


def test_interrupt_during_reflection_stops_without_retry_or_success_history(tmp_path):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(
                200, json=tool_response([call("read", "read_file", path="absent")])
            )
        raise KeyboardInterrupt

    cfg = replace(config_at(tmp_path), reflection=ReflectionConfig())
    with plan_app(tmp_path, handler, config=cfg) as (app, store):
        with pytest.raises(KeyboardInterrupt):
            app.send("读取并修正")
        assert len(bodies) == 2 and app.usage().unknown
        assert not store.load_turns(app.session.session_id)
        events = store.load_events(app.session.session_id)
        end = [e["payload"] for e in events if e["type"] == "turn_finished"][-1]
        assert end["status"] == "interrupted"
