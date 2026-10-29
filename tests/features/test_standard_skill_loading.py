"""T2.2：标准与 TOML 包统一加载，材料按需读取，实际请求和日志保留来源版本。"""

import json
from hashlib import sha256

import httpx
import pytest

from adam.bootstrap import build_application
from adam.contracts.errors import ToolError
from adam.contracts.skills import validate_catalog
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from tests.features.test_memory_context import config_at
from tests.features.test_skill_catalog import catalog, skill
from tests.features.test_standard_skill_metadata import standard
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response


def test_mixed_formats_share_catalog_and_keep_legacy_request_snapshots_valid(tmp_path):
    source, _ = standard(tmp_path, "检查代码", body="读取真实定义".encode())
    skill(tmp_path)
    c = catalog(tmp_path)
    entries = {item["id"]: item for item in c.summaries()}
    assert entries["检查代码"]["format"] == "standard"
    assert entries["verify-project"]["format"] == "toml"
    assert c.load("检查代码")["content"] == "读取真实定义"
    assert c.load("检查代码")["version"] == sha256(source.read_bytes()).hexdigest()
    validate_catalog(c.summaries())
    legacy = dict(entries["verify-project"])
    legacy.pop("format")
    validate_catalog([legacy])
    with pytest.raises(ValueError):
        validate_catalog([{**legacy, "format": "unknown"}])


def test_normalized_id_loads_actual_unicode_directory(tmp_path):
    standard(tmp_path, "ｉｎｓｐｅｃｔ-code", body=b"normalized body")
    assert catalog(tmp_path).load("inspect-code")["content"] == "normalized body"


def test_standard_material_inventory_reads_text_without_executing_scripts(tmp_path):
    source, _ = standard(tmp_path)
    materials = {
        "references/check.md": "检查真实输出",
        "scripts/check.py": "raise RuntimeError('must never execute on loading')",
        "assets/template.json": '{"result": "pending"}',
    }
    for relative, body in materials.items():
        path = source.parent / relative
        path.parent.mkdir(exist_ok=True)
        path.write_text(body, encoding="utf-8")
    c = catalog(tmp_path)
    loaded = c.load("inspect-code")
    assert loaded["resources"] == sorted(materials)
    assert "metadata" in loaded and "content" not in c.summaries()[0]
    for relative, body in materials.items():
        result = c.load("inspect-code", relative)
        assert result["content"] == body
        assert result["version"] == sha256((source.parent / relative).read_bytes()).hexdigest()
        assert result["metadata_version"] == loaded["metadata_version"]


def test_body_change_keeps_metadata_version_but_header_change_requires_restart(tmp_path):
    source, header = standard(tmp_path)
    c = catalog(tmp_path)
    old = c.load("inspect-code")
    source.write_bytes(header + b"updated body")
    new = c.load("inspect-code")
    assert new["version"] != old["version"]
    assert new["metadata_version"] == old["metadata_version"]
    source.write_bytes(header.replace(b"Read code", b"Review code") + b"updated body")
    with pytest.raises(ToolError, match="元数据已变化"):
        c.load("inspect-code")
    assert catalog(tmp_path).load("inspect-code")["metadata_version"] != old["metadata_version"]


@pytest.mark.parametrize("body", [b"\xffbinary", b"nul\0text"])
def test_unloaded_binary_material_does_not_enter_catalog_and_load_fails(tmp_path, body):
    source, _ = standard(tmp_path)
    (source.parent / "asset.bin").write_bytes(body)
    c = catalog(tmp_path)
    assert c.summaries()
    assert "asset.bin" in c.load("inspect-code")["resources"]
    with pytest.raises(ToolError, match="文本"):
        c.load("inspect-code", "asset.bin")


def test_material_inventory_and_body_result_have_explicit_limits(tmp_path):
    source, header = standard(tmp_path)
    source.write_bytes(header + b"x" * 3000)
    with pytest.raises(ToolError, match="结果上限"):
        catalog(tmp_path, max_result_chars=1000).load("inspect-code")
    for index in range(33):
        (source.parent / f"material-{index}.md").write_text("note", encoding="utf-8")
    with pytest.raises(ToolError, match="32"):
        catalog(tmp_path)


@pytest.mark.parametrize("resource", ["../outside.md", "/absolute.md", "missing.md", "skill.toml"])
def test_standard_loader_rejects_unregistered_or_external_material(tmp_path, resource):
    standard(tmp_path)
    with pytest.raises(ToolError, match="未登记"):
        catalog(tmp_path).load("inspect-code", resource)


def test_standard_tool_loop_loads_only_selected_material_and_records_versions(tmp_path):
    source, header = standard(tmp_path, body=b"CHOSEN_BODY: read references/check.md")
    reference = source.parent / "references" / "check.md"
    reference.parent.mkdir()
    reference.write_text("REFERENCE_BODY: verify observed output", encoding="utf-8")
    standard(tmp_path, "unused-guide", body=b"UNRELATED_BODY")
    (source.parent / "unread.md").write_text("UNREAD_MATERIAL", encoding="utf-8")
    config = config_at(tmp_path)
    requests = []

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        text = json.dumps(payload)
        assert "UNRELATED_BODY" not in text and "UNREAD_MATERIAL" not in text
        if len(requests) == 1:
            assert "CHOSEN_BODY" not in text and "REFERENCE_BODY" not in text
            batch = [call("guide", "load_skill", skill_id="inspect-code")]
        elif len(requests) == 2:
            assert outputs(payload)[-1]["content"].startswith("CHOSEN_BODY")
            assert "REFERENCE_BODY" not in text
            batch = [
                call(
                    "material",
                    "load_skill",
                    skill_id="inspect-code",
                    resource="references/check.md",
                )
            ]
        else:
            assert outputs(payload)[-1]["content"].startswith("REFERENCE_BODY")
            return httpx.Response(200, json=answer("已按指导核对"))
        return httpx.Response(200, json=tool_response(batch))

    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
    ):
        app = build_application(config, store, LLMClient(config.llm, http), tmp_path)
        try:
            assert app.send("按 inspect-code 指导核对") == "已按指导核对"
            sid = app.session.session_id
            events = store.load_events(sid)
            contexts = [e["payload"]["context"] for e in events if e["type"] == "request_started"]
            assert len(contexts) == 3
            assert all(item["format"] == "standard" for item in contexts[0]["skills"])
            loaded = [
                json.loads(e["payload"]["output"]) for e in events if e["type"] == "tool_finished"
            ]
            assert [item["resource"] for item in loaded] == ["SKILL.md", "references/check.md"]
            assert loaded[0]["version"] == sha256(source.read_bytes()).hexdigest()
            assert loaded[1]["version"] == sha256(reference.read_bytes()).hexdigest()
            assert all(item["metadata_version"] == sha256(header).hexdigest() for item in loaded)
        finally:
            app.close()
    source.write_bytes(header + b"NEW_SOURCE")
    with SessionStore(config.sessions_dir) as store:
        store.select_session(sid)
        saved = json.dumps(store.load_events(sid))
        assert "CHOSEN_BODY" in saved and "REFERENCE_BODY" in saved
        assert "NEW_SOURCE" not in saved and "UNREAD_MATERIAL" not in saved
