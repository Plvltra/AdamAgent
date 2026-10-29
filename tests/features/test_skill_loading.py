"""E6.2：目录进入真实请求，正文通过工具按需读取，历史保留加载时的版本。"""

import json

import httpx
import pytest

from adam.bootstrap import build_application
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from tests.features.test_memory_context import config_at
from tests.features.test_skill_catalog import skill
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response


@pytest.mark.parametrize("matched", [True, False])
def test_load_only_relevant_guidance_and_preserve_request_snapshot(tmp_path, matched):
    chosen = skill(tmp_path, body="CHOSEN_BODY：核对实际检查入口")
    skill(tmp_path, "diagnose-failure", body="OTHER_BODY：收集错误")
    payloads = []
    config = config_at(tmp_path)

    def handle(request):
        body = json.loads(request.content)
        payloads.append(body)
        if len(payloads) == 1:
            encoded = json.dumps(body, ensure_ascii=False)
            assert "verify-project" in encoded and "diagnose-failure" in encoded
            assert "CHOSEN_BODY" not in encoded and "OTHER_BODY" not in encoded
            if matched:
                return httpx.Response(
                    200,
                    json=tool_response(
                        [
                            call("load", "load_skill", skill_id="verify-project"),
                        ]
                    ),
                )
        if matched:
            assert outputs(body)[-1]["content"].startswith("CHOSEN_BODY")
            assert "OTHER_BODY" not in json.dumps(body)
        return httpx.Response(200, json=answer("已按实际情况回答"))

    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
    ):
        app = build_application(config, store, LLMClient(config.llm, http), tmp_path)
        preview = app.preview_context("验证项目" if matched else "解释希伯来字母")
        assert preview.estimate.skill_tokens > 0
        assert app.send("验证项目" if matched else "解释希伯来字母") == "已按实际情况回答"
        sid = app.session.session_id
        events = store.load_events(sid)
        contexts = [e["payload"]["context"] for e in events if e["type"] == "request_started"]
        assert len(contexts[0]["skills"]) == 2
        assert len(payloads) == 2 if matched else len(payloads) == 1
        loaded = [e for e in events if e["type"] == "tool_finished"]
        assert len(loaded) == int(matched)
        if matched:
            assert json.loads(loaded[0]["payload"]["output"])["version"]
        chosen.joinpath("SKILL.md").write_text("NEW_BODY", encoding="utf-8")
    with SessionStore(config.sessions_dir) as store:
        store.select_session(sid)
        previous = json.dumps(store.load_events(sid), ensure_ascii=False)
        assert ("CHOSEN_BODY" in previous) == matched
        assert "NEW_BODY" not in previous
