"""E7.2：真实 stdio 服务通过现有模型工具循环运行，验证完整 Schema 与日志。"""

import json
import sys
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from adam.bootstrap import build_application
from adam.config.mcp import MCPConfig, MCPServerConfig
from adam.contracts.errors import ToolError
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from adam.tool_calls.factory import create_tool_registry
from adam.tool_calls.mcp_tools import register_mcp
from tests.features.test_mcp_connection import SERVICE, assert_stopped
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response

DEMO = Path(__file__).resolve().parents[2] / "examples" / "mcp_readonly.py"


def test_local_and_mcp_query_share_agent_loop_and_persist_result(tmp_path):
    (tmp_path / "note.txt").write_text("same evidence\n", encoding="utf-8")
    config = replace(
        config_at(tmp_path),
        mcp=MCPConfig(
            True,
            (
                MCPServerConfig(
                    "files",
                    sys.executable,
                    (str(DEMO), "--workspace", str(tmp_path)),
                    ("read_project_text",),
                ),
            ),
        ),
    )
    requests = []

    def model(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            assert "mcp_files_read_project_text" in {t["function"]["name"] for t in body["tools"]}
            batch = [call("local", "read_file", path="note.txt")]
        elif len(requests) == 2:
            batch = [call("remote", "mcp_files_read_project_text", path="note.txt")]
        else:
            local, remote = outputs(body)
            assert remote["ok"]
            assert remote["structured_content"]["content"].strip() == local["lines"][0]["text"]
            assert remote["structured_content"]["version"] == local["version"]
            return httpx.Response(200, json=answer("两条路径的依据一致"))
        return httpx.Response(200, json=tool_response(batch))

    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(model)) as http,
    ):
        app = build_application(config, store, LLMClient(config.llm, http), tmp_path)
        try:
            assert app.send("比较两种查询") == "两条路径的依据一致"
            events = store.load_events(app.session.session_id)
            names = [e["payload"]["name"] for e in events if e["type"] == "tool_started"]
            assert names == ["read_file", "mcp_files_read_project_text"]
            assert app.usage().tokens == 36
        finally:
            app.close()


def test_nested_schema_is_validated_and_failed_registration_cleans_process(tmp_path):
    conf = MCPConfig(
        True,
        (
            MCPServerConfig(
                "sample",
                sys.executable,
                (str(SERVICE), "--pid-file", str(tmp_path / "server.pid")),
                ("probe",),
            ),
        ),
    )
    base = create_tool_registry(tmp_path)
    with ExitStack() as resources:
        registered = register_mcp(base, conf, tmp_path, resources)
        invalid = json.loads(registered.execute("mcp_sample_probe", '{"data":{"value":"wrong"}}'))
        assert not invalid["ok"] and "Schema" in invalid["error"]
        valid = json.loads(registered.execute("mcp_sample_probe", '{"data":{"value":3}}'))
        assert valid["structured_content"]["data"] == {"value": 3}
        pid = valid["structured_content"]["pid"]
    assert_stopped(pid)
    # 第二次目录注册冲突：上下文退出须回收本次和此前连接，不覆盖 base。
    with pytest.raises(ToolError, match="冲突"), ExitStack() as resources:
        once = register_mcp(base, conf, tmp_path, resources)
        register_mcp(once, conf, tmp_path, resources)
    assert_stopped(int((tmp_path / "server.pid").read_text(encoding="utf-8")))
    assert not any(t["function"]["name"].startswith("mcp_") for t in base.definitions())
