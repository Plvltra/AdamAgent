"""B38.3：验收通过也不代表回答已保存，故障不重试或形成成功历史。"""

from dataclasses import replace

import httpx
import pytest

from adam.bootstrap import build_application
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import AgentError, FailureCode, RequestInterrupted, StorageError
from adam.llm.client import LLMClient
from adam.session.diagnostics import read_report
from adam.session.storage import SessionStore
from adam.verification.files import FileStateVerifier
from tests.features.test_task_eval_runner import config
from tests.features.test_tool_loop import answer


@pytest.mark.parametrize(
    "boundary", ["correct", "cancel", "verification_save", "answer_save", "budget"]
)
def test_final_verification_faults_preserve_actual_terminal_state(tmp_path, monkeypatch, boundary):
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "result.txt").write_bytes(b"wrong" if boundary == "budget" else b"correct")
    actual = FileStateVerifier(workspace, {"result.txt": b"correct"})
    token = CancellationToken()

    class Verifier:
        def check(self, reply):
            result = actual.check(reply)
            if boundary == "cancel":
                token.cancel()
            return result

    calls, emitted = [], []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=answer("完成"))

    cfg = config(tmp_path)
    if boundary == "budget":
        cfg = replace(cfg, agent=replace(cfg.agent, max_model_requests=1))
    with (
        SessionStore(tmp_path / "sessions") as store,
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
    ):

        def fail_save(*args, **kwargs):
            raise StorageError("injected persistence failure")

        if boundary == "verification_save":
            monkeypatch.setattr(store, "append_verification", fail_save)
        elif boundary == "answer_save":
            monkeypatch.setattr(store, "append_messages", fail_save)
        app = build_application(
            cfg,
            store,
            LLMClient(cfg.llm, http),
            workspace,
            verifier=Verifier(),
            cancel=token,
            live_input=True,
        )
        try:
            if boundary == "correct":
                assert app.send("完成文件任务", on_output=emitted.append) == "完成"
            elif boundary == "cancel":
                with pytest.raises(RequestInterrupted):
                    app.send("完成文件任务", on_output=emitted.append)
            else:
                with pytest.raises(AgentError) as raised:
                    app.send("完成文件任务", on_output=emitted.append)
                assert (
                    raised.value.code
                    == {
                        "verification_save": FailureCode.STORAGE_ERROR,
                        "answer_save": FailureCode.ANSWER_SAVE_FAILED,
                        "budget": FailureCode.REQUEST_LIMIT,
                    }[boundary]
                )
            sid = app.session.session_id
            assert bool(store.load_messages(sid)) == (boundary == "correct")
            assert len(calls) == 1 and not emitted
        finally:
            app.close()
    report = read_report(next((tmp_path / "sessions").glob(f"*/*/*/{sid}.jsonl")))
    turn = report["turns"][0]
    assert turn["status"] == (
        "succeeded"
        if boundary == "correct"
        else "interrupted"
        if boundary == "cancel"
        else "failed"
    )
    if boundary in {"correct", "answer_save"}:
        assert turn["verifications"][0]["status"] == "passed"
    elif boundary in {"cancel", "verification_save"}:
        assert turn["verifications"] == []
