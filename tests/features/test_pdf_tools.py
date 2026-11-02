"""PDF 工具通过真实路径授权、解析、模型工具循环与录制重放，远端聊天仅替换 HTTP。"""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from adam.config.loader import load_config
from adam.config.settings import PermissionConfig, ToolLimits
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import ConfigError, RequestInterrupted, ToolError
from adam.contracts.execution import ExecutionMode, ToolCapability
from adam.recording.runtime import replay_turn
from adam.tool_calls.factory import create_tool_registry
from adam.tool_calls.policy import PathPolicy
from tests.features.support.pdf_data import make_pdf
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app, forbid_external_actions
from tests.test_config import LLM


def read(registry, **args):
    return json.loads(registry.execute("read_pdf", json.dumps(args)))


def test_real_chat_continues_same_pdf_version_records_pages_and_replays_without_reopening(
    tmp_path, monkeypatch
):
    source = tmp_path / "guide.pdf"
    data = make_pdf([{"text": "Cover"}, {"text": "Configuration evidence"}])
    source.write_bytes(data)

    def respond(body, number):
        if number == 1:
            assert "read_pdf" in [tool["function"]["name"] for tool in body["tools"]]
            value = tool_response([call("first", "read_pdf", path="guide.pdf", page_count=1)])
        elif number == 2:
            page = outputs(body)[-1]
            assert page["ok"] and page["next_page"] == 2
            assert page["pages"][0]["text"] == "Cover"
            value = tool_response(
                [
                    call(
                        "second",
                        "read_pdf",
                        path="guide.pdf",
                        start_page=2,
                        expected_version=page["version"],
                    )
                ]
            )
        else:
            page = outputs(body)[-1]
            assert page["pages"][0]["page"] == 2 and page["next_page"] is None
            assert page["pages"][0]["text"] == "Configuration evidence"
            assert page["path"] == str(source) and not page["layout_verified"]
            value = answer("Evidence on physical page 2")
        return httpx.Response(200, json=value)

    with captured_app(tmp_path, respond) as (app, recorder, store, recording, _):
        assert recorder.run(lambda: app.send("task")) == "Evidence on physical page 2"
        events = store.load_events(app.session.session_id)
        assert hashlib.sha256(data).hexdigest() in json.dumps(events)
    source.write_bytes(make_pdf([{"text": "changed after capture"}]))
    forbid_external_actions(monkeypatch)
    monkeypatch.setattr(
        "adam.tool_calls.pdf_tools.extract_pdf", lambda *a, **k: pytest.fail("replay parsed PDF")
    )
    assert replay_turn(recording, tmp_path / "replay")["status"] == "matched"


@pytest.mark.parametrize("delegated", [False, True], ids=["parent", "child"])
def test_pdf_is_plan_read_only_and_inherits_parent_directory_without_new_approval(
    tmp_path, delegated
):
    (tmp_path / "guide.pdf").write_bytes(make_pdf([{"text": "visible"}]))
    registry = create_tool_registry(
        tmp_path,
        delegated=delegated,
        confirm_command=lambda _: pytest.fail("read asked command approval"),
        confirm_edit=lambda _: pytest.fail("read asked edit approval"),
    ).with_mode(lambda: ExecutionMode.PLAN)
    assert registry.capability("read_pdf") == ToolCapability.READ_ONLY
    assert registry.can_parallelize("read_pdf")
    assert read(registry, path="guide.pdf")["pages"][0]["text"] == "visible"


@pytest.mark.parametrize("location", ["outside", "protected", "credential"])
def test_unapproved_or_reserved_paths_are_rejected_before_file_open_and_pdf_parse(
    tmp_path, monkeypatch, location
):
    root = tmp_path / "workspace"
    root.mkdir()
    protected = root / "reserved"
    protected.mkdir()
    path = {
        "outside": tmp_path / "private.pdf",
        "protected": protected / "private.pdf",
        "credential": root / "llm.toml",
    }[location]
    path.write_bytes(make_pdf([{"text": "PRIVATE_DOCUMENT"}]))
    registry = create_tool_registry(root, protected_dirs=(protected,))
    original = Path.open

    def checked(file, *args, **kwargs):
        if file == path:
            pytest.fail("denied path was opened")
        return original(file, *args, **kwargs)

    monkeypatch.setattr(Path, "open", checked)
    monkeypatch.setattr(
        "adam.tool_calls.pdf_tools.extract_pdf", lambda *a, **k: pytest.fail("denied path parsed")
    )
    result = read(registry, path=str(path))
    assert not result["ok"] and "PRIVATE_DOCUMENT" not in json.dumps(result)


def test_explicit_additional_root_allows_reading_and_source_version_is_not_guessed(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    source = outside / "reference.pdf"
    source.write_bytes(make_pdf([{"text": "approved source"}]))
    registry = create_tool_registry(root, PermissionConfig(additional_read_dirs=(outside,)))
    first = read(registry, path=str(source))
    assert first["ok"] and first["path"] == str(source)
    source.write_bytes(make_pdf([{"text": "changed source"}]))
    rejected = read(registry, path=str(source), expected_version=first["version"])
    assert not rejected["ok"] and "已更新" in rejected["error"]
    assert read(registry, path=str(source))["version"] != first["version"]


@pytest.mark.parametrize("fault", ["modified", "revoked"])
def test_source_changed_or_permission_revoked_during_parse_discards_extracted_result(
    tmp_path, monkeypatch, fault
):
    import adam.tool_calls.pdf_tools as module

    source = tmp_path / "guide.pdf"
    source.write_bytes(make_pdf([{"text": "PRIVATE_OLD_TEXT"}]))
    parser = module.extract_pdf
    resolve = PathPolicy.resolve
    revoked = False

    def authorize(self, value, **kwargs):
        if revoked:
            raise ToolError("授权已撤回。")
        return resolve(self, value, **kwargs)

    def parse(*args, **kwargs):
        nonlocal revoked
        result = parser(*args, **kwargs)
        if fault == "modified":
            source.write_bytes(make_pdf([{"text": "NEW_LONGER_TEXT"}, {}]))
        else:
            revoked = True
        return result

    monkeypatch.setattr(PathPolicy, "resolve", authorize)
    monkeypatch.setattr(module, "extract_pdf", parse)
    result = read(create_tool_registry(tmp_path), path="guide.pdf")
    assert not result["ok"] and "PRIVATE_OLD_TEXT" not in json.dumps(result)


def test_output_limit_keeps_page_identity_and_accurate_resume_without_generic_preview(tmp_path):
    data = make_pdf([{"text": "A" * 5000}, {"text": "next page"}])
    (tmp_path / "guide.pdf").write_bytes(data)
    registry = create_tool_registry(tmp_path, limits=ToolLimits(max_result_chars=900))
    raw = registry.execute("read_pdf", '{"path":"guide.pdf"}')
    result = json.loads(raw)
    assert result["ok"] and len(raw) <= 900 and "preview" not in result
    assert len(result["pages"]) == 1 and result["next_page"] == 2
    page = result["pages"][0]
    assert page["truncated"] and page["extracted_chars"] == 5000 and len(page["text"]) < 5000
    next_page = read(registry, path="guide.pdf", start_page=2, expected_version=result["version"])
    assert next_page["pages"][0]["page"] == 2 and next_page["pages"][0]["text"] == "next page"
    tiny = create_tool_registry(tmp_path, limits=ToolLimits(max_result_chars=128))
    assert not read(tiny, path="guide.pdf")["ok"]


def test_file_limit_and_page_arguments_reject_before_returning_any_partial_page(tmp_path):
    source = tmp_path / "guide.pdf"
    data = make_pdf([{"text": "body"}])
    source.write_bytes(data)
    registry = create_tool_registry(tmp_path, limits=ToolLimits(max_pdf_file_bytes=len(data) - 1))
    assert not read(registry, path="guide.pdf")["ok"]
    registry = create_tool_registry(tmp_path, limits=ToolLimits(max_pdf_read_pages=1))
    assert read(registry, path="guide.pdf")["ok"]
    for kwargs in ({"page_count": 2}, {"start_page": True}, {"expected_version": "bad"}):
        assert not read(registry, path="guide.pdf", **kwargs)["ok"]
    for values in (
        {"max_pdf_read_pages": True},
        {"max_pdf_read_pages": 101},
        {"max_pdf_file_bytes": 33554433},
    ):
        with pytest.raises(ConfigError):
            replace(ToolLimits(), **values)


def test_io_failure_does_not_echo_os_error_body(tmp_path, monkeypatch):
    source = tmp_path / "guide.pdf"
    source.write_bytes(make_pdf([{"text": "text"}]))
    original = Path.open

    def fail(path, *args, **kwargs):
        if path == source:
            raise OSError("PRIVATE_OS_ERROR")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail)
    result = read(create_tool_registry(tmp_path), path="guide.pdf")
    assert not result["ok"] and "PRIVATE_OS_ERROR" not in json.dumps(result)


def test_file_changed_while_handle_is_open_is_not_parsed(tmp_path, monkeypatch):
    source = tmp_path / "guide.pdf"
    source.write_bytes(make_pdf([{"text": "old"}]))
    replacement = make_pdf([{"text": "new longer content"}, {}])
    original = Path.open

    class UpdatingReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, limit):
            data = self.stream.read(limit)
            with original(source, "wb") as target:
                target.write(replacement)
            return data

    def open_file(path, *args, **kwargs):
        stream = original(path, *args, **kwargs)
        return UpdatingReader(stream) if path == source and args == ("rb",) else stream

    monkeypatch.setattr(Path, "open", open_file)
    monkeypatch.setattr(
        "adam.tool_calls.pdf_tools.extract_pdf", lambda *a, **k: pytest.fail("unstable PDF parsed")
    )
    result = read(create_tool_registry(tmp_path), path="guide.pdf")
    assert not result["ok"] and "变化" in result["error"]


def test_cancel_and_collaborative_deadline_cannot_return_a_successful_page(tmp_path, monkeypatch):
    import adam.tool_calls.pdf_tools as module

    (tmp_path / "guide.pdf").write_bytes(make_pdf([{"text": "text"}]))
    registry = create_tool_registry(tmp_path)
    cancel = CancellationToken()
    cancel.cancel()
    with pytest.raises(RequestInterrupted):
        registry.execute("read_pdf", '{"path":"guide.pdf"}', cancel=cancel)
    parser = module.extract_pdf
    expired = False

    def parse(*args, **kwargs):
        nonlocal expired
        result = parser(*args, **kwargs)
        expired = True
        return result

    monkeypatch.setattr(module, "extract_pdf", parse)
    monkeypatch.setattr(module.time, "monotonic", lambda: 100 if expired else 0)
    result = read(registry, path="guide.pdf")
    assert not result["ok"] and "协作时间上限" in result["error"]


def test_toml_pdf_limits_reach_actual_registry_and_reject_unsupported_ranges(tmp_path):
    main = tmp_path / "config.toml"
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    main.write_text("[tool_limits]\nmax_pdf_file_bytes=100\nmax_pdf_read_pages=1", encoding="utf-8")
    config = load_config(main)
    (tmp_path / "guide.pdf").write_bytes(make_pdf([{"text": "too large"}]))
    registry = create_tool_registry(tmp_path, limits=config.tool_limits)
    assert not read(registry, path="guide.pdf")["ok"]
    for settings in ("max_pdf_read_pages=101", "max_pdf_file_bytes=33554433"):
        main.write_text("[tool_limits]\n" + settings, encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(main)
