"""C04.1：真实本地图片经原工具/来源/媒体/模型边界；不证明视觉语义质量。"""

import builtins
import hashlib
import json
import shutil
from contextlib import contextmanager
from dataclasses import replace

import httpx
import pytest

from adam.bootstrap import build_application
from adam.config.settings import (
    AgentConfig,
    PermissionConfig,
    ToolDiscoveryConfig,
    ToolLimits,
    VisionConfig,
)
from adam.contracts.errors import AgentError, BudgetError, RequestInterrupted, StorageError
from adam.contracts.execution import ExecutionMode
from adam.contracts.media import decode_media_reference
from adam.contracts.model_input import ModelCapabilities
from adam.llm.client import LLMClient
from adam.llm.message_encoding import ChatInputEncoder
from adam.recording.runtime import replay_turn
from adam.session.media import MediaStore
from adam.session.media_evidence import SessionMedia
from adam.session.storage import SessionStore
from adam.vision.adapters.pillow import PillowDecoder
from tests.features.test_memory_context import config_at
from tests.features.test_multimodal_input import wire_bytes
from tests.features.test_subagents import application as delegated_app
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_hooks import hooks_at
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app, forbid_external_actions
from tests.image_samples import image_bytes


def image_config(root, **changes):
    """显式启用图片，其他能力仍用临时默认配置，不自动启用任何视觉模型。"""
    return replace(config_at(root), vision=VisionConfig(enabled=True), **changes)


@contextmanager
def image_app(root, respond, *, config=None, supported=False, session_id=None):
    """仅远端使用 MockTransport，本地文件、解码、工具日志与媒体存储都真实执行。"""
    config = config or image_config(root)
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(200, json=respond(body, len(bodies)))

    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
    ):
        media = SessionMedia(store, MediaStore(store.root / "_media"))

        def read(reference):
            return media.read(reference.media_id, expected_version=reference.version)

        encoder = (
            ChatInputEncoder(
                ModelCapabilities(config.llm.model, ("image/png", "image/jpeg", "image/webp")), read
            )
            if supported
            else None
        )
        app = build_application(
            config, store, LLMClient(config.llm, http, input_encoder=encoder), root
        )
        if session_id:
            app.resume(session_id)
        try:
            yield app, store, media, bodies
        finally:
            app.close()


def load_then_answer(body, number):
    return (
        tool_response([call("load", "load_image", path="sample.bin")])
        if number == 1
        else answer("loaded, not viewed")
    )


def media_events(store, sid):
    return [e["payload"] for e in store.load_events(sid) if e["type"] == "media_saved"]


@pytest.mark.parametrize("image_format", ["PNG", "JPEG", "WEBP"])
def test_real_image_load_has_full_reference_metadata_and_no_implicit_model_attachment(
    tmp_path, image_format
):
    raw = image_bytes(image_format, orientation=6)
    original = tmp_path / "sample.bin"
    original.write_bytes(raw)
    with image_app(tmp_path, load_then_answer) as (app, store, media, bodies):
        app.select_mode("plan")
        assert app.send("load only, do not infer contents") == "loaded, not viewed"
        output = outputs(bodies[-1])[-1]
        ref = decode_media_reference(output["media"])
        assert output["status"] == "loaded" and output["model_input_attached"] is False
        assert output["image"] == {
            "format": image_format,
            "width": 3,
            "height": 2,
            "orientation": 6,
            "display_width": 2,
            "display_height": 3,
        }
        assert ref.version == hashlib.sha256(raw).hexdigest() and ref.size_bytes == len(raw)
        assert ref.source.session_id == app.session.session_id
        assert ref.source.tool_name == "load_image" and ref.source.tool_call_id == "load"
        assert media.read(ref.media_id, expected_version=ref.version).data == raw
        assert len(media_events(store, app.session.session_id)) == 1
        assert original.read_bytes() == raw
        assert not any(isinstance(m["content"], list) for body in bodies for m in body["messages"])
        assert all(
            "base64," not in json.dumps(e) for e in store.load_events(app.session.session_id)
        )
        assert app.mode is ExecutionMode.PLAN


def test_loaded_image_can_be_explicitly_attached_with_exact_bytes_after_original_deletion(tmp_path):
    raw = image_bytes()
    original = tmp_path / "sample.bin"
    original.write_bytes(raw)

    def respond(body, number):
        if number <= 2:
            return load_then_answer(body, number)
        assert wire_bytes(body) == [raw]
        return answer("mock transport received original bytes")

    with image_app(tmp_path, respond, supported=True) as (app, store, _, bodies):
        app.send("load only")
        ref = decode_media_reference(outputs(bodies[-1])[-1]["media"])
        original.unlink()
        assert (
            app.send("explicit image input", media=(ref,))
            == "mock transport received original bytes"
        )
        assert len(bodies) == 3
        assert media_events(store, app.session.session_id)[0] == outputs(bodies[1])[-1]["media"]


def test_loading_does_not_upgrade_text_model_and_failed_attach_creates_no_turn(tmp_path):
    (tmp_path / "sample.bin").write_bytes(image_bytes())
    with image_app(tmp_path, load_then_answer) as (app, store, _, bodies):
        app.send("load")
        ref = decode_media_reference(outputs(bodies[-1])[-1]["media"])
        before = store.load_events(app.session.session_id)
        with pytest.raises(AgentError, match="不支持|未声明"):
            app.send("look at this", media=(ref,))
        assert len(bodies) == 2 and store.load_events(app.session.session_id) == before


def test_rewrite_and_restart_keep_each_saved_image_version_and_dimensions(tmp_path):
    path = tmp_path / "sample.bin"
    first = image_bytes(size=(3, 2))
    second = image_bytes(size=(2, 4))
    path.write_bytes(first)

    def respond(body, number):
        return (
            tool_response([call(str(number), "load_image", path="sample.bin")])
            if number in (1, 3)
            else answer("loaded")
        )

    with image_app(tmp_path, respond) as (app, _, media, bodies):
        app.send("first")
        one = decode_media_reference(outputs(bodies[-1])[-1]["media"])
        path.write_bytes(second)
        app.send("second")
        two = decode_media_reference(outputs(bodies[-1])[-1]["media"])
        sid = app.session.session_id
        assert one.version != two.version and one.media_id != two.media_id
        assert media.read(one.media_id, expected_version=one.version).data == first
    path.unlink()
    with image_app(tmp_path, lambda *_: answer("unused"), session_id=sid) as (
        app,
        store,
        media,
        bodies,
    ):
        assert len(media_events(store, sid)) == 2 and bodies == []
        assert media.read(one.media_id, expected_version=one.version).data == first
        assert media.read(two.media_id, expected_version=two.version).data == second
        saved = [json.loads(m.content) for m in app.history() if m.role == "tool"]
        assert [(m["image"]["width"], m["image"]["height"]) for m in saved] == [(3, 2), (2, 4)]


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("corrupt", "unsupported_or_corrupt"),
        ("truncated", "corrupt_image"),
        ("animation", "animated_image"),
        ("bytes", "byte_limit"),
        ("pixels", "pixel_limit"),
        ("dimension", "dimension_limit"),
    ],
)
def test_bad_or_over_limit_inputs_are_explicit_and_never_publish_media(tmp_path, failure, expected):
    raw = image_bytes(animated=failure == "animation")
    if failure == "corrupt":
        raw = b"private file content not a picture"
    if failure == "truncated":
        raw = raw[:-12]
    limits = {
        "bytes": {"max_bytes": 1},
        "pixels": {"max_pixels": 5},
        "dimension": {"max_dimension": 2},
    }.get(failure, {})
    (tmp_path / "sample.bin").write_bytes(raw)
    config = replace(image_config(tmp_path), vision=VisionConfig(enabled=True, **limits))
    with image_app(tmp_path, load_then_answer, config=config) as (app, store, _, bodies):
        app.send("load bounded")
        result = outputs(bodies[-1])[-1]
        assert result["ok"] is False and result["status"] == expected
        assert "private file content" not in json.dumps(result)
        assert media_events(store, app.session.session_id) == []
        assert not list(store.root.rglob("*.media"))


@pytest.mark.parametrize(
    "target", ["directory", "missing", "credential", "retained", "mcp_retained", "outside"]
)
def test_image_file_permissions_are_checked_before_decoding(tmp_path, monkeypatch, target):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = image_config(workspace)
    path = workspace / "sample.bin"
    if target == "directory":
        path.mkdir()
    elif target == "missing":
        pass
    elif target == "credential":
        path = workspace / ".env.secret"
        path.write_bytes(image_bytes())
    elif target in {"retained", "mcp_retained"}:
        directory = "_media" if target == "retained" else "_mcp_media"
        path = config.sessions_dir / directory / "raw.png"
        path.parent.mkdir(parents=True)
        path.write_bytes(image_bytes())
    else:
        path = tmp_path / "foreign.png"
        path.write_bytes(image_bytes())
    monkeypatch.setattr(
        PillowDecoder, "decode", lambda *_args, **_kwargs: pytest.fail("unauthorized decode")
    )

    def respond(body, number):
        return (
            tool_response([call("load", "load_image", path=str(path))])
            if number == 1
            else answer("blocked")
        )

    with image_app(workspace, respond, config=config) as (app, store, _, bodies):
        app.send("load")
        assert outputs(bodies[-1])[-1]["ok"] is False
        assert media_events(store, app.session.session_id) == []


def test_extra_read_authorization_is_not_implied_by_write_authorization(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    path = external / "image.png"
    path.write_bytes(image_bytes())

    def respond(body, number):
        return (
            tool_response([call("load", "load_image", path=str(path))])
            if number == 1
            else answer("done")
        )

    for read, expected in ((False, False), (True, True)):
        config = image_config(
            workspace,
            permissions=PermissionConfig(
                additional_read_dirs=(external,) if read else (), additional_write_dirs=(external,)
            ),
        )
        with image_app(workspace, respond, config=config) as (app, store, _, bodies):
            app.send("load")
            assert outputs(bodies[-1])[-1]["ok"] is expected
            assert bool(media_events(store, app.session.session_id)) is expected


def test_missing_optional_decoder_reports_installation_instead_of_text_fallback(
    tmp_path, monkeypatch
):
    (tmp_path / "sample.bin").write_bytes(image_bytes())
    original = builtins.__import__

    def reject(name, *args, **kwargs):
        if name == "PIL":
            raise ImportError("not installed")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject)
    with image_app(tmp_path, load_then_answer) as (app, store, _, bodies):
        app.send("load")
        result = outputs(bodies[-1])[-1]
        assert result["status"] == "dependency_missing" and "vision" in result["error"]
        assert media_events(store, app.session.session_id) == []


def test_hook_denial_and_tool_budget_prevent_decoder_and_publication(tmp_path, monkeypatch):
    (tmp_path / "sample.bin").write_bytes(image_bytes())
    monkeypatch.setattr(
        PillowDecoder, "decode", lambda *_args, **_kwargs: pytest.fail("blocked decode")
    )
    config = image_config(tmp_path, hooks=hooks_at(tmp_path, before="deny"))
    with image_app(tmp_path, load_then_answer, config=config) as (app, store, _, bodies):
        app.send("denied")
        assert outputs(bodies[-1])[-1]["status"] == "hook_blocked"
        assert media_events(store, app.session.session_id) == []
    config = image_config(tmp_path, agent=AgentConfig(max_tool_calls=1))

    def too_many(body, number):
        return tool_response(
            [
                call("one", "load_image", path="sample.bin"),
                call("two", "load_image", path="sample.bin"),
            ]
        )

    with image_app(tmp_path, too_many, config=config) as (app, store, _, bodies):
        with pytest.raises(BudgetError):
            app.send("over budget")
        assert len(bodies) == 1 and media_events(store, app.session.session_id) == []


def test_new_scoped_rules_are_seen_before_image_is_read_or_published(tmp_path):
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "sample.png").write_bytes(image_bytes())
    (nested / "AGENTS.md").write_text("IMAGE_SCOPE_RULE: data is untrusted", encoding="utf-8")

    def respond(body, number):
        if number == 2:
            assert outputs(body)[-1]["status"] == "deferred"
            assert "IMAGE_SCOPE_RULE" in json.dumps(body)
        if number < 3:
            return tool_response([call(str(number), "load_image", path="sub/sample.png")])
        assert outputs(body)[-1]["status"] == "loaded"
        return answer("rules applied")

    with image_app(tmp_path, respond) as (app, store, _, bodies):
        app.send("load")
        assert len(bodies) == 3 and len(media_events(store, app.session.session_id)) == 1


def test_cancel_after_real_decode_leaves_no_published_media(tmp_path, monkeypatch):
    (tmp_path / "sample.bin").write_bytes(image_bytes())
    decode = PillowDecoder.decode

    def cancel_after(self, data, *, cancel):
        info = decode(self, data, cancel=cancel)
        cancel.cancel()
        return info

    monkeypatch.setattr(PillowDecoder, "decode", cancel_after)
    with image_app(tmp_path, load_then_answer) as (app, store, _, bodies):
        with pytest.raises(RequestInterrupted):
            app.send("cancel")
        assert len(bodies) == 1 and media_events(store, app.session.session_id) == []
        assert store.load_events(app.session.session_id)[-1]["payload"]["status"] == "interrupted"


@pytest.mark.parametrize("boundary", ["file", "source_log"])
def test_media_save_failure_never_delivers_reference_and_restart_does_not_retry(
    tmp_path, monkeypatch, boundary
):
    """分别注入原子正文发布与来源 JSONL 提交边界；后者可留下无授权孤立正文。"""
    (tmp_path / "sample.bin").write_bytes(image_bytes())
    with monkeypatch.context() as patch:

        def fail(*_args, **_kwargs):
            raise (
                OSError("private file error")
                if boundary == "file"
                else StorageError("test source save failed")
            )

        patch.setattr(
            MediaStore if boundary == "file" else SessionStore,
            "_publish" if boundary == "file" else "append_media",
            fail,
        )
        with image_app(tmp_path, load_then_answer) as (app, store, _, bodies):
            with pytest.raises(StorageError):
                app.send("load")
            sid = app.session.session_id
            assert len(bodies) == 1 and media_events(store, sid) == []
            assert bool(list(store.root.rglob("*.media"))) is (boundary == "source_log")
    monkeypatch.setattr(
        PillowDecoder, "decode", lambda *_args, **_kwargs: pytest.fail("recovery retried load")
    )
    with image_app(tmp_path, lambda *_: answer("continued"), session_id=sid) as (
        app,
        store,
        media,
        bodies,
    ):
        assert bodies == [] and media_events(store, sid) == []
        assert app.send("continue without upload") == "continued"
        assert len(bodies) == 1
        if boundary == "source_log":
            orphan = next(store.root.rglob("*.media")).stem
            with pytest.raises(StorageError, match="已提交"):
                media.read(orphan, expected_version="0" * 64)


def test_output_limit_is_checked_before_creating_any_media(tmp_path, monkeypatch):
    (tmp_path / "sample.bin").write_bytes(image_bytes())
    monkeypatch.setattr(
        PillowDecoder, "decode", lambda *_args, **_kwargs: pytest.fail("unreturnable image decoded")
    )
    config = image_config(tmp_path, tool_limits=ToolLimits(max_result_chars=1000))
    with image_app(tmp_path, load_then_answer, config=config) as (app, store, _, bodies):
        app.send("load")
        result = outputs(bodies[-1])[-1]
        assert result["status"] == "output_limit" and not result["ok"]
        assert media_events(store, app.session.session_id) == []


def test_child_loads_in_own_namespace_and_parent_cannot_read_its_reference(tmp_path):
    (tmp_path / "sample.bin").write_bytes(image_bytes())
    refs = []

    def child(request):
        body = json.loads(request.content)
        if not outputs(body):
            value = tool_response([call("child-load", "load_image", path="sample.bin")])
        else:
            refs.append(decode_media_reference(outputs(body)[-1]["media"]))
            value = answer("loaded separately")
        return httpx.Response(200, json=value)

    count = 0

    def parent(request):
        nonlocal count
        count += 1
        if count == 1:
            value = tool_response([call("spawn", "spawn_agent", name="loader", task="load only")])
        elif count == 2:
            value = tool_response([call("wait", "wait_agents", agent_ids=[])])
        else:
            value = answer("child done")
        return httpx.Response(200, json=value)

    with delegated_app(tmp_path, parent, child, config=image_config(tmp_path)) as (app, store):
        app.select_mode("plan")
        app.send("delegate read-only input")
        assert len(refs) == 1
        ref = refs[0]
        assert ref.source.session_id != app.session.session_id
        assert media_events(store, app.session.session_id) == []
        with pytest.raises(StorageError, match="已提交"):
            SessionMedia(store, MediaStore(store.root / "_media")).read(
                ref.media_id, expected_version=ref.version
            )
        assert list((store.root / "_media" / "children" / app.session.session_id).rglob("*.media"))


@pytest.mark.parametrize(
    "directory",
    ["_media", "_mcp_media", "_background", "_inputs", "_triggers", "_terminals", "_subagents"],
)
@pytest.mark.parametrize("rules_enabled", [True, False])
def test_child_image_loading_cannot_republish_reserved_media(tmp_path, directory, rules_enabled):
    """关闭可选规则也不能让真实子任务把宿主私有媒体重新发布为获准图片。"""
    config = image_config(tmp_path)
    config = replace(config, rules=replace(config.rules, enabled=rules_enabled))
    private = config.sessions_dir / directory / "private.bin"
    private.parent.mkdir(parents=True)
    raw = image_bytes()
    private.write_bytes(raw)
    results = []

    def child(request):
        body = json.loads(request.content)
        if not outputs(body):
            value = tool_response([call("child-load", "load_image", path=str(private))])
        else:
            results.extend(outputs(body))
            value = answer("child input checked")
        return httpx.Response(200, json=value)

    count = 0

    def parent(request):
        nonlocal count
        count += 1
        if count == 1:
            value = tool_response([call("spawn", "spawn_agent", name="loader", task="check input")])
        elif count == 2:
            value = tool_response([call("wait", "wait_agents", agent_ids=[])])
        else:
            assert outputs(json.loads(request.content))[-1]["agents"][0]["status"] == "completed"
            value = answer("child checked")
        return httpx.Response(200, json=value)

    with delegated_app(tmp_path, parent, child, config=config) as (app, store):
        app.select_mode("plan")
        assert app.send("check private input boundary") == "child checked"
        assert len(results) == 1
        assert results[0]["ok"] is False and "保留目录" in results[0]["error"]
        assert media_events(store, app.session.session_id) == []
        assert not list(store.root.rglob("*.media"))
        child_root = store.root / "_subagents" / app.session.session_id / "sessions"
        with SessionStore(child_root) as children:
            sessions = children.list_sessions()
            assert len(sessions) == 1
            for session in sessions:
                assert media_events(children, session.session_id) == []
    assert private.read_bytes() == raw


def test_recorded_load_fact_replays_without_source_file_decoder_or_network(tmp_path, monkeypatch):
    (tmp_path / "sample.bin").write_bytes(image_bytes())

    def respond(body, number):
        return httpx.Response(200, json=load_then_answer(body, number))

    with captured_app(tmp_path, respond, config=image_config(tmp_path)) as (
        app,
        recorder,
        store,
        source,
        _,
    ):
        assert recorder.run(lambda: app.send("task")) == "loaded, not viewed"
        assert len(media_events(store, app.session.session_id)) == 1
    (tmp_path / "sample.bin").unlink()
    media_root = (tmp_path / "sessions" / "_media").resolve()
    assert media_root.is_relative_to(tmp_path.resolve())
    shutil.rmtree(media_root)
    monkeypatch.setattr(
        PillowDecoder, "decode", lambda *_args, **_kwargs: pytest.fail("offline decoder called")
    )
    forbid_external_actions(monkeypatch)
    result = replay_turn(source, tmp_path / "offline")
    assert result["status"] == "matched" and result["reply"] == "loaded, not viewed"


def test_image_group_is_discovered_before_original_execution(tmp_path):
    (tmp_path / "sample.bin").write_bytes(image_bytes())
    config = image_config(tmp_path, tool_discovery=ToolDiscoveryConfig(mode="on_demand"))

    def respond(body, number):
        names = [item["function"]["name"] for item in body["tools"]]
        if number == 1:
            assert "load_image" not in names
            return tool_response([call("find", "discover_tools", groups=["vision"])])
        assert "load_image" in names
        if number == 2:
            return tool_response([call("load", "load_image", path="sample.bin")])
        assert outputs(body)[-1]["status"] == "loaded"
        return answer("discovered and loaded")

    with image_app(tmp_path, respond, config=config) as (app, store, _, bodies):
        assert app.send("load this file") == "discovered and loaded"
        assert len(bodies) == 3 and len(media_events(store, app.session.session_id)) == 1


def test_direct_registry_calls_cannot_fabricate_media_source(tmp_path, monkeypatch):
    """直接调用无执行器来源；即使文件可读也不能生成看似已提交的引用。"""
    from adam.tool_calls.factory import create_tool_registry
    from adam.tool_calls.policy import PathPolicy
    from adam.tool_calls.vision_tools import register_vision

    (tmp_path / "sample.bin").write_bytes(image_bytes())
    monkeypatch.setattr(
        PillowDecoder, "decode", lambda *_args, **_kwargs: pytest.fail("unbound decode")
    )
    with SessionStore(tmp_path / "sessions") as store:
        sid = store.create_session("no active tool", "")
        store.select_session(sid)
        registry, tools = register_vision(
            create_tool_registry(tmp_path),
            VisionConfig(enabled=True),
            PathPolicy(tmp_path, PermissionConfig()),
            SessionMedia(store, MediaStore(store.root / "_media")),
        )
        assert tools is not None
        result = json.loads(registry.execute("load_image", '{"path":"sample.bin"}'))
        assert not result["ok"] and "真实工具来源" in result["error"]
        tools.bind_source({"turn_id": "a" * 32, "request_id": "b" * 32, "tool_call_id": "invented"})
        with pytest.raises(StorageError):
            registry.execute("load_image", '{"path":"sample.bin"}')
        assert media_events(store, sid) == [] and not list(store.root.rglob("*.media"))
