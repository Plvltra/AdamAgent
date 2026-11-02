"""B30.2.2：模拟HTTP走真实终端、CLI批准及持久输入来源。"""

import json
import sys
import time

import httpx
import pytest

from adam.bootstrap import build_application
from adam.cli.main import confirm_terminal
from adam.config.settings import AppConfig, LLMConfig, ReflectionConfig, ToolDiscoveryConfig
from adam.contracts.errors import ApprovalDeniedError
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response


@pytest.mark.parametrize("approve_input", [True, False])
def test_terminal_tools_approve_each_input_and_keep_sources(tmp_path, approve_input):
    config = AppConfig(
        LLMConfig("https://unused.test", "test", "secret", stream=False),
        tmp_path / "sessions",
        "system",
        reflection=ReflectionConfig(enabled=False),
        tool_discovery=ToolDiscoveryConfig(mode="full"),
    )
    displays, approvals = [], []
    count, identity = 0, None

    def approve(request):
        approvals.append(request)
        return confirm_terminal(
            request,
            lambda _: "y" if request.action == "start" or approve_input else "n",
            displays.append,
        )

    def handler(request):
        nonlocal count, identity
        count += 1
        body = json.loads(request.content)
        if count == 1:
            value = tool_response(
                [
                    call(
                        "open",
                        "open_terminal",
                        argv=[
                            sys.executable,
                            "-u",
                            "-c",
                            "import os,sys; from pathlib import Path; data=sys.stdin.read(); print(os.environ['TAG']+data,flush=True); Path('done').touch()",
                        ],
                        env={"TAG": "prefix:"},
                    )
                ]
            )
        elif count == 2:
            identity = outputs(body)[-1]["terminal_id"]
            value = tool_response(
                [call("send", "terminal_input", terminal_id=identity, text="hello\n", eof=True)]
            )
        elif count == 3:
            assert approve_input
            deadline = time.monotonic() + 5
            while not (tmp_path / "done").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert (tmp_path / "done").exists()
            value = tool_response([call("read", "read_terminal", terminal_id=identity)])
        elif count == 4:
            assert "prefix:hello" in outputs(body)[-1]["stdout"]["text"]
            value = tool_response([call("close", "close_terminal", terminal_id=identity)])
        else:
            assert count == 5
            value = answer("done")
        return httpx.Response(200, json=value)

    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
    ):
        app = build_application(
            config, store, LLMClient(config.llm, http), tmp_path, confirm_terminal=approve
        )
        try:
            if approve_input:
                assert app.send("interact") == "done"
            else:
                with pytest.raises(ApprovalDeniedError):
                    app.send("interact")
                assert count == 2 and not (tmp_path / "done").exists()
            assert [item.action for item in approvals] == ["start", "input"]
            assert any('"TAG": "prefix:"' in line for line in displays)
            assert any('"hello\\n"' in line for line in displays)
            path = store.root / "_terminals" / app.session.session_id / (identity + ".json")
            record = json.loads(path.read_text(encoding="utf-8"))
            assert record["inputs"][0]["source"]["turn_id"] == record["source"]["turn_id"]
            assert record["inputs"][0]["source"]["request_id"] != record["source"]["request_id"]
            assert record["inputs"][0]["status"] == ("closed" if approve_input else "denied")
        finally:
            app.close()
