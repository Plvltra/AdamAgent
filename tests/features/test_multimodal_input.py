"""C00.3：真实媒体引用、模型传输、持久化与冻结字节离线重放；非视觉质量验收。"""

import base64
import json
import shutil
from contextlib import contextmanager
from dataclasses import replace

import httpx
import pytest

from adam.bootstrap import build_application
from adam.config.settings import ContextConfig, ReflectionConfig
from adam.contracts.errors import AgentError, BudgetError, FailureCode, StorageError, TurnSaveError
from adam.contracts.history import COMPACTION_VERSION
from adam.contracts.media import MediaSource
from adam.contracts.model_input import MediaPart, ModelCapabilities, TextPart, media_records
from adam.llm.client import LLMClient
from adam.llm.message_encoding import ChatInputEncoder
from adam.recording.capture import RecordingModel, TurnRecorder
from adam.recording.format import RecordingError
from adam.recording.journal import RecordingWriter, parse_recording, read_recording
from adam.recording.replay import ReplayTape
from adam.recording.replay_store import ReplayStore
from adam.recording.runtime import CaptureRuntime, build_replay, replay_turn
from adam.recording.sharing import export_share
from adam.recording.snapshot import capture_seed
from adam.session.media import MediaStore
from adam.session.media_evidence import SessionMedia
from adam.session.storage import SessionStore
from tests.features.test_media_evidence import media_path, png
from tests.features.test_memory_context import config_at
from tests.features.test_reflection_request import SUGGESTION
from tests.features.test_streaming import DONE, USAGE, event, sse
from tests.features.test_subagents import call
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import forbid_external_actions


@contextmanager
def multimodal_app(
    root, respond, *, config=None, capabilities=None, capture=False, reader=True, child_handler=None
):
    """真实本地业务只替换远端 HTTP；输入先有 C00.2 来源，不隐式上传路径。"""
    config = config or config_at(root)
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        result = respond(body, len(bodies))
        return result if isinstance(result, httpx.Response) else httpx.Response(200, json=result)

    @contextmanager
    def child_client(cancel):
        with httpx.Client(transport=httpx.MockTransport(child_handler)) as child_http:
            yield LLMClient(config.llm, child_http)

    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
    ):
        sid = store.create_session("media source", config.system_prompt)
        turn = store.start_turn(sid, "save original input")
        media = SessionMedia(store, MediaStore(store.root / "_media"))
        reference = media.publish(MediaSource(sid, turn, "input"), png(), "image/png")
        store.append_messages(
            sid,
            turn,
            [
                {"role": "user", "content": "save original input"},
                {"role": "assistant", "content": "stored"},
            ],
        )

        def read(ref):
            return media.read(ref.media_id, expected_version=ref.version)

        encoder = ChatInputEncoder(
            capabilities or ModelCapabilities(config.llm.model, ("image/png",)), read
        )
        client = LLMClient(config.llm, http, input_encoder=encoder)
        path = root / "capture.jsonl"
        writer = (
            RecordingWriter(path, capture_seed(config, store, sid, "task", root))
            if capture
            else None
        )
        recorder = (
            TurnRecorder(
                writer, lambda: store.load_events(sid), media_reader=read if reader else None
            )
            if writer
            else None
        )
        runtime = CaptureRuntime(recorder) if recorder else None
        app = build_application(
            config,
            store,
            RecordingModel(client, recorder) if recorder else client,
            root,
            decorate_tools=runtime.tools if runtime else None,
            observe_conversation=runtime.bind if runtime else None,
            child_client_factory=child_client if child_handler else None,
        )
        app.resume(sid)
        try:
            yield app, store, reference, media, bodies, recorder, path
        finally:
            app.close()
            if writer:
                writer.close()


def wire_bytes(body):
    """只解开实际 HTTP 编码比较字节，不解码或显示图片。"""
    images = [
        part["image_url"]["url"]
        for msg in body["messages"]
        if isinstance(msg["content"], list)
        for part in msg["content"]
        if part["type"] == "image_url"
    ]
    assert all(value.startswith("data:image/png;base64,") for value in images)
    return [base64.b64decode(value.split(",", 1)[1], validate=True) for value in images]


def test_real_png_tool_loop_and_restart_keep_reference_text_identity_and_reasoning(tmp_path):
    (tmp_path / "note.txt").write_text("evidence=42", encoding="utf-8")

    def respond(body, number):
        assert wire_bytes(body) == [png()]
        if number == 1:
            return tool_response([call("stable-id", "read_file", path="note.txt")])
        assert body["messages"][-2]["tool_calls"][0]["id"] == "stable-id"
        assert body["messages"][-2]["reasoning_content"] == "provider field"
        assert body["messages"][-1]["tool_call_id"] == "stable-id"
        assert "evidence=42" in body["messages"][-1]["content"]
        return answer("engineering transport verified")

    with multimodal_app(tmp_path, respond) as (app, store, ref, _, bodies, *_):
        assert app.send("task", media=(ref,)) == "engineering transport verified"
        message = app.history()[2]
        assert message.content == "task" and message.parts == (TextPart("task"), MediaPart(ref))
        assert message.raw["media"] == media_records((ref,))
        sid = app.session.session_id
        events = store.load_events(sid)
        assert [
            e["payload"]["media"]
            for e in events
            if e["type"] == "turn_started" and e["payload"]["user_text"] == "task"
        ] == [media_records((ref,))]
        assert not any("base64" in json.dumps(e) for e in events)
        assert len(bodies) == 2
    with SessionStore(tmp_path / "sessions") as restored:
        restored.select_session(sid)
        message = restored.load_messages(sid)[2]
        assert message.parts == (TextPart("task"), MediaPart(ref))


@pytest.mark.parametrize(
    "capability,change",
    [
        (ModelCapabilities("test"), None),
        (ModelCapabilities("test", ("image/png",)), "different"),
        (ModelCapabilities("test", ("image/jpeg",)), None),
        (ModelCapabilities("test", ("image/png",), max_media_bytes=1), None),
        (ModelCapabilities("test", ("image/png",), max_media_parts=1), "two"),
    ],
)
def test_unsupported_or_over_limit_inputs_never_create_turn_or_text_fallback(
    tmp_path, capability, change
):
    with multimodal_app(
        tmp_path, lambda *_: pytest.fail("must not call HTTP"), capabilities=capability
    ) as (app, store, ref, _, bodies, *_):
        before = store.load_events(ref.source.session_id)
        if change == "different":
            app.select_model(change)
        with pytest.raises(AgentError):
            app.send("task", media=(ref, ref) if change == "two" else (ref,))
        assert bodies == [] and store.load_events(ref.source.session_id) == before
        assert len(app.history()) == 2


@pytest.mark.parametrize("failure", ["missing", "corrupt", "changed-source", "foreign"])
def test_unreadable_replaced_or_foreign_media_is_not_sent(tmp_path, failure):
    with multimodal_app(tmp_path, lambda *_: pytest.fail("must not call HTTP")) as (
        app,
        store,
        ref,
        media,
        bodies,
        *_,
    ):
        before = store.load_events(ref.source.session_id)
        if failure == "missing":
            media_path(media, ref).unlink()
        elif failure == "corrupt":
            path = media_path(media, ref)
            data = path.read_bytes()
            path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
        elif failure == "changed-source":
            ref = replace(ref, source=replace(ref.source, turn_id="f" * 32))
        else:
            ref = replace(ref, source=replace(ref.source, session_id="f" * 8))
        with pytest.raises(AgentError):
            app.send("task", media=(ref,))
        assert bodies == [] and store.load_events(app.session.session_id) == before


def test_media_owner_changed_while_reading_is_rejected_without_implicit_switch(
    tmp_path, monkeypatch
):
    with multimodal_app(tmp_path, lambda *_: pytest.fail("must not call HTTP")) as (
        app,
        store,
        ref,
        media,
        bodies,
        *_,
    ):
        original = MediaStore.read
        owners = []

        def switch(files, *args, **kwargs):
            content = original(files, *args, **kwargs)
            owners.append(store.create_session("injected owner change", ""))
            store.select_session(owners[-1])
            return content

        monkeypatch.setattr(MediaStore, "read", switch)
        with pytest.raises(AgentError):
            app.send("task", media=(ref,))
        assert len(owners) == 1 and store.active_session_id == owners[0] and bodies == []


def test_reference_size_does_not_bypass_context_budget(tmp_path):
    config = replace(
        config_at(tmp_path), context=ContextConfig(auto_compact=False, max_input_tokens=4000)
    )
    with multimodal_app(
        tmp_path, lambda *_: pytest.fail("budget must stop before HTTP"), config=config
    ) as (app, store, ref, _, bodies, *_):
        with pytest.raises(BudgetError):
            app.send("task", media=(ref,))
        event = [
            e for e in store.load_events(ref.source.session_id) if e["type"] == "turn_finished"
        ][-1]
        assert event["payload"]["reason"] == FailureCode.CONTEXT_INPUT_LIMIT
        assert bodies == [] and len(app.history()) == 2


def test_commit_failure_keeps_input_media_and_request_usage_without_success_history(
    tmp_path, monkeypatch
):
    with multimodal_app(tmp_path, lambda *_: answer("unsaved")) as (
        app,
        store,
        ref,
        media,
        bodies,
        *_,
    ):

        def fail(*args, **kwargs):
            raise StorageError("injected final message block append failure")

        monkeypatch.setattr(store, "append_messages", fail)
        with pytest.raises(TurnSaveError) as error:
            app.send("task", media=(ref,))
        assert error.value.partial_reply == "unsaved"
        assert len(bodies) == 1 and len(app.history()) == 2
        assert store.usage(ref.source.session_id) == (12, False)
        assert media.read(ref.media_id, expected_version=ref.version).data == png()
        assert [e for e in store.load_events(ref.source.session_id) if e["type"] == "turn_started"][
            -1
        ]["payload"]["media"] == media_records((ref,))


def test_interruption_preserves_original_media_and_does_not_retry_or_commit(tmp_path):
    def stop(*_):
        raise KeyboardInterrupt()

    with multimodal_app(tmp_path, stop) as (app, store, ref, media, bodies, *_):
        with pytest.raises(KeyboardInterrupt):
            app.send("task", media=(ref,))
        assert len(bodies) == 1 and len(app.history()) == 2
        assert store.usage(ref.source.session_id)[1]
        assert media.read(ref.media_id, expected_version=ref.version).data == png()


def test_streaming_media_uses_same_canonical_input_and_persists_only_complete_answer(tmp_path):
    config = config_at(tmp_path)
    config = replace(config, llm=replace(config.llm, stream=True))

    def respond(body, _):
        assert body["stream"] and wire_bytes(body) == [png()]
        return sse(
            [
                event({"role": "assistant", "content": "streamed"}),
                event(reason="stop", usage=USAGE),
                DONE,
            ]
        )

    output = []
    with multimodal_app(tmp_path, respond, config=config) as (app, _, ref, _, bodies, *_):
        assert app.send("task", media=(ref,), on_output=output.append) == "streamed"
        assert [part.text for part in output if part.kind == "text"] == ["streamed"]
        assert app.history()[2].parts[-1] == MediaPart(ref) and len(bodies) == 1


def test_recording_copies_full_bytes_and_standalone_replay_survives_original_removal(
    tmp_path, monkeypatch
):
    with multimodal_app(tmp_path, lambda *_: answer("frozen evidence"), capture=True) as (
        app,
        _,
        ref,
        media,
        bodies,
        recorder,
        path,
    ):
        assert recorder.run(lambda: app.send("task", media=(ref,))) == "frozen evidence"
        assert wire_bytes(bodies[0]) == [png()]
        original_root = media.files.root
    recording = read_recording(path)
    assert recording.complete and recording.media == (ref,)
    original = path.read_bytes()
    shutil.rmtree(original_root.parent)
    forbid_external_actions(monkeypatch)
    report = replay_turn(path, tmp_path / "offline")
    assert report["status"] == "matched", report
    assert report["reply"] == "frozen evidence" and path.read_bytes() == original
    assert not original_root.parent.exists()
    archive = MediaStore(path.with_name(path.name + ".media"))
    assert archive.read_frozen(ref).data == png()


@pytest.mark.parametrize("failure", ["missing", "corrupt", "metadata"])
def test_replay_refuses_missing_corrupt_or_rebound_sidecar_before_creating_destination(
    tmp_path, failure
):
    with multimodal_app(tmp_path, lambda *_: answer("frozen"), capture=True) as (
        app,
        _,
        ref,
        _,
        _,
        recorder,
        path,
    ):
        recorder.run(lambda: app.send("task", media=(ref,)))
    archive = MediaStore(path.with_name(path.name + ".media"))
    file = archive.root / ref.source.session_id / (ref.media_id + ".media")
    if failure == "missing":
        file.unlink()
    elif failure == "corrupt":
        data = file.read_bytes()
        file.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    else:
        file.write_bytes(file.read_bytes().replace(b'"kind": "input"', b'"kind": "wrong"'))
    snapshot = {str(p): p.read_bytes() for p in archive.root.rglob("*") if p.is_file()}
    with pytest.raises(RecordingError, match="媒体"):
        replay_turn(path, tmp_path / "offline")
    assert not (tmp_path / "offline").exists()
    assert snapshot == {str(p): p.read_bytes() for p in archive.root.rglob("*") if p.is_file()}


def test_recording_without_reader_fails_before_turn_or_physical_request(tmp_path):
    with multimodal_app(
        tmp_path, lambda *_: pytest.fail("no physical request"), capture=True, reader=False
    ) as (app, store, ref, _, bodies, recorder, path):
        before = store.load_events(ref.source.session_id)
        with pytest.raises(RecordingError):
            recorder.run(lambda: app.send("task", media=(ref,)))
        assert bodies == [] and store.load_events(ref.source.session_id) == before
    assert read_recording(path).complete


def test_capture_publish_failure_leaves_no_input_or_request(tmp_path, monkeypatch):
    with multimodal_app(tmp_path, lambda *_: pytest.fail("no physical request"), capture=True) as (
        app,
        store,
        ref,
        _,
        bodies,
        recorder,
        _,
    ):
        before = store.load_events(ref.source.session_id)

        def fail(*args, **kwargs):
            raise OSError("injected archive atomic publish failure")

        monkeypatch.setattr(MediaStore, "_publish", fail)
        with pytest.raises(RecordingError):
            recorder.run(lambda: app.send("task", media=(ref,)))
        assert bodies == [] and store.load_events(ref.source.session_id) == before


def test_persistent_user_message_cannot_change_or_drop_diagnostic_media(tmp_path):
    with multimodal_app(tmp_path, lambda *_: answer()) as (app, store, ref, _, bodies, *_):
        sid = ref.source.session_id
        turn = store.start_turn(sid, "task", media=(ref,))
        before = store.load_events(sid)
        for message in [
            {"role": "user", "content": "task"},
            {
                "role": "user",
                "content": "task",
                "media": media_records((replace(ref, created_at="2026-11-01T01:00:00+00:00"),)),
            },
        ]:
            with pytest.raises(ValueError):
                store.append_messages(
                    sid, turn, [message, {"role": "assistant", "content": "forged"}]
                )
        assert store.load_events(sid) == before and len(app.history()) == 2 and bodies == []


@pytest.mark.parametrize("failure", [False, True])
def test_summary_attaches_original_media_not_just_reference_json_and_keeps_history(
    tmp_path, failure
):
    config = replace(
        config_at(tmp_path), context=ContextConfig(auto_compact=False, retain_recent_turns=0)
    )

    def respond(body, number):
        assert wire_bytes(body) == [png()]
        if number == 1:
            return answer("original answer")
        assert "tools" not in body
        material = json.loads(body["messages"][1]["content"][0]["text"])
        assert material["turns"][-1]["messages"][0]["media"]
        return answer(" " if failure else "summary from actual evidence")

    with multimodal_app(tmp_path, respond, config=config) as (app, store, ref, media, bodies, *_):
        app.send("task", media=(ref,))
        original = app.history()
        if failure:
            with pytest.raises(AgentError):
                app.compact()
            assert store.load_summary(ref.source.session_id) is None
        else:
            assert app.compact().text == "summary from actual evidence"
        assert app.history() == original and len(bodies) == 2
        assert media.read(ref.media_id, expected_version=ref.version).data == png()


@pytest.mark.parametrize("enough_budget", [False, True])
def test_reflection_attaches_source_media_or_explicitly_skips_for_budget(tmp_path, enough_budget):
    config = replace(
        config_at(tmp_path),
        reflection=ReflectionConfig(max_input_tokens=20000 if enough_budget else 4096),
    )

    def respond(body, number):
        assert wire_bytes(body) == [png()]
        if number == 1:
            return tool_response([call("missing", "read_file", path="not-present.txt")])
        if enough_budget and number == 2:
            assert "tools" not in body
            source = json.loads(body["messages"][-1]["content"][0]["text"])
            assert source["source_media"]
            return answer(json.dumps(SUGGESTION))
        return answer("not verified")

    with multimodal_app(tmp_path, respond, config=config) as (app, store, ref, _, bodies, *_):
        assert app.send("task", media=(ref,)) == "not verified"
        records = store.load_reflections(ref.source.session_id)
        assert len(records) == 1
        assert records[0]["status"] == ("completed" if enough_budget else "skipped")
        assert records[0]["reason"] == ("completed" if enough_budget else "input_budget")
        assert len(bodies) == (3 if enough_budget else 2)


def test_compaction_current_task_cannot_drop_original_media(tmp_path):
    with multimodal_app(tmp_path, lambda *_: pytest.fail("no HTTP")) as (
        app,
        store,
        ref,
        _,
        bodies,
        *_,
    ):
        sid = ref.source.session_id
        prior = store.load_turns(sid)[0].turn_id
        turn = store.start_turn(sid, "task", media=(ref,))
        before = store.load_events(sid)
        with pytest.raises(ValueError, match="media"):
            store.append_compaction_event(
                sid,
                "compaction_started",
                {
                    "operation_id": "operation",
                    "turn_id": turn,
                    "model": "test",
                    "source_turn_ids": [prior],
                    "previous_summary_id": None,
                    "max_output_tokens": 100,
                    "estimated_input_tokens": 100,
                    "compaction_version": COMPACTION_VERSION,
                    "current_task": {
                        "turn_id": turn,
                        "messages": [{"role": "user", "content": "task"}],
                    },
                },
            )
        assert store.load_events(sid) == before and bodies == [] and len(app.history()) == 2


def test_late_media_corruption_stops_before_http_and_settles_local_request_at_zero(
    tmp_path, monkeypatch
):
    with multimodal_app(tmp_path, lambda *_: pytest.fail("no HTTP after version change")) as (
        app,
        store,
        ref,
        media,
        bodies,
        *_,
    ):
        original = store.start_request

        def corrupt(*args, **kwargs):
            result = original(*args, **kwargs)
            path = media_path(media, ref)
            data = path.read_bytes()
            path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
            return result

        monkeypatch.setattr(store, "start_request", corrupt)
        with pytest.raises(AgentError) as failure:
            app.send("task", media=(ref,))
        assert failure.value.code == FailureCode.MODEL_INPUT_INVALID
        assert (
            bodies == []
            and len(app.history()) == 2
            and store.usage(ref.source.session_id) == (0, False)
        )
        events = store.load_events(ref.source.session_id)
        assert len([event for event in events if event["type"] == "request_started"]) == 1
        assert [event for event in events if event["type"] == "request_finished"][-1]["payload"][
            "reason"
        ] == FailureCode.MODEL_INPUT_INVALID


def test_parent_image_is_not_implicitly_granted_to_actual_child(tmp_path):
    children = []

    def child(request):
        body = json.loads(request.content)
        children.append(body)
        assert wire_bytes(body) == [] and "base64" not in json.dumps(body)
        assert "media" not in json.dumps(body["messages"])
        return httpx.Response(200, json=answer("child has text only"))

    def parent(body, number):
        assert wire_bytes(body) == [png()]
        if number == 1:
            return tool_response(
                [call("spawn", "spawn_agent", name="helper", task="text-only child task")]
            )
        if number == 2:
            return tool_response([call("wait", "wait_agents", agent_ids=[])])
        return answer("parent retains its image")

    with multimodal_app(tmp_path, parent, child_handler=child) as (app, _, ref, _, bodies, *_):
        assert app.send("task", media=(ref,)) == "parent retains its image"
        assert len(children) == 1 and len(bodies) == 3


def test_text_only_followup_cannot_silently_drop_image_in_history_after_model_change(tmp_path):
    with multimodal_app(tmp_path, lambda *_: answer("saved")) as (app, store, ref, _, bodies, *_):
        app.send("task", media=(ref,))
        app.select_model("undeclared-new-model")
        with pytest.raises(AgentError) as failure:
            app.send("follow up")
        assert failure.value.code == FailureCode.MODEL_INPUT_INVALID
        assert (
            len(bodies) == 1
            and len(app.history()) == 4
            and store.usage(ref.source.session_id) == (12, False)
        )


@pytest.mark.parametrize("media", [[], ["image.png"], None])
def test_host_input_rejects_noncanonical_reference_collection_without_side_effect(tmp_path, media):
    with multimodal_app(tmp_path, lambda *_: pytest.fail("invalid host input")) as (
        app,
        store,
        ref,
        _,
        bodies,
        *_,
    ):
        before = store.load_events(ref.source.session_id)
        with pytest.raises(AgentError):
            app.send("task", media=media)
        assert store.load_events(ref.source.session_id) == before and bodies == []


@pytest.mark.parametrize("missing", [False, True])
def test_json_share_is_not_binary_verification_and_cannot_enter_media_replay(
    tmp_path, monkeypatch, missing
):
    with multimodal_app(tmp_path, lambda *_: answer("frozen"), capture=True) as (
        app,
        _,
        ref,
        _,
        _,
        recorder,
        path,
    ):
        recorder.run(lambda: app.send("task", media=(ref,)))
    verified = read_recording(path)
    assert verified.media == (ref,) and verified.media_verified
    archive = path.with_name(path.name + ".media")
    if missing:
        shutil.rmtree(archive)
    raw = path.read_bytes()
    forbid_external_actions(monkeypatch)
    monkeypatch.setattr(
        "adam.recording.journal.MediaArchive.verify",
        lambda *_: pytest.fail("pure parsing/sharing must not access binaries"),
    )
    parsed = parse_recording(raw)
    assert parsed.complete and parsed.media == (ref,) and not parsed.media_verified
    tape = ReplayTape(parsed)
    with ReplayStore(tmp_path / "manual-offline", tape) as store:
        with pytest.raises(RecordingError, match="尚未验证"):
            build_replay(tape, store)
        assert store.offset == len(parsed.seed["session_events"])
    summary = export_share(path, tmp_path / "human-only.json")
    shared = json.loads((tmp_path / "human-only.json").read_text(encoding="utf-8"))
    assert summary["replayable"] is False and shared["purpose"] == "human_review_only"
    assert "data:image/" not in json.dumps(shared) and path.read_bytes() == raw
    assert archive.exists() is not missing
