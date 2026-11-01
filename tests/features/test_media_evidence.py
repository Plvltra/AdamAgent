"""C00.2：真实 PNG/WAV 字节、来源 JSONL、失败提交、会话隔离与整目录恢复。"""

import hashlib
import io
import json
import math
import shutil
import struct
import wave
import zlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, replace
from threading import Barrier

import httpx
import pytest

import adam.session.media as media_module
from adam.contracts.artifacts import ArtifactSource
from adam.contracts.errors import StorageError
from adam.contracts.media import MediaSource
from adam.session.artifacts import ArtifactStore
from adam.session.media import MediaStore
from adam.session.media_evidence import SessionMedia
from adam.session.storage import SessionStore
from tests.features.test_subagents import application as delegated_application
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response


def png():
    """构造完整 2x2 RGBA PNG（含正确 CRC），只校验字节协议，不做看图或截图。"""

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    pixels = (
        b"\x00"
        + bytes([255, 0, 0, 255, 0, 255, 0, 255])
        + b"\x00"
        + bytes([0, 0, 255, 255, 255, 255, 255, 255])
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


def wav():
    """构造可由标准 wave 解码的 100ms 单声道 PCM，不录音、不播放。"""
    output = io.BytesIO()
    with wave.open(output, "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(8000)
        file.writeframes(
            b"".join(
                struct.pack("<h", int(5000 * math.sin(2 * math.pi * 440 * index / 8000)))
                for index in range(800)
            )
        )
    return output.getvalue()


@contextmanager
def active_media(root, *, kind="input", **limits):
    """真实临时会话建立输入/工具来源，不调用模型，不接触用户数据。"""
    with SessionStore(root / "sessions") as store:
        sid = store.create_session("media evidence", "")
        turn = store.start_turn(sid, "retain selected test media")
        source = MediaSource(sid, turn, "input")
        if kind == "tool":
            request = store.start_request(sid, turn, "test")
            store.finish_request(sid, turn, request, (1, 1))
            store.append_tool_event(
                sid,
                "tool_started",
                {
                    "turn_id": turn,
                    "request_id": request,
                    "tool_call_id": "download",
                    "name": "test_download",
                    "arguments": "{}",
                },
            )
            source = MediaSource(sid, turn, "tool", request, "download", "test_download")
        files = MediaStore(store.root / "_media", **limits)
        yield store, source, SessionMedia(store, files)


def media_path(media, reference):
    return media.files.root / reference.source.session_id / (reference.media_id + ".media")


@pytest.mark.parametrize("mime,make", [("image/png", png), ("audio/wav", wav)])
def test_real_binary_round_trip_restart_and_reference_only_jsonl(tmp_path, mime, make):
    raw = make()
    with active_media(tmp_path) as (store, source, media):
        reference = media.publish(source, raw, mime)
        assert reference.mime_type == mime and reference.size_bytes == len(raw)
        assert reference.version == hashlib.sha256(raw).hexdigest()
        assert media.read(reference.media_id, expected_version=reference.version).data == raw
        event = next(
            item for item in store.load_events(source.session_id) if item["type"] == "media_saved"
        )
        assert event["payload"] == asdict(reference)
        assert "data" not in event["payload"] and "path" not in event["payload"]
        assert "base64" not in json.dumps(event)
        expected_file = media_path(media, reference).read_bytes()
    with SessionStore(tmp_path / "sessions") as restored:
        restored.select_session(source.session_id)
        media = SessionMedia(restored, MediaStore(restored.root / "_media"))
        content = media.read(reference.media_id, expected_version=reference.version)
        assert content.reference == reference and content.data == raw
        assert media_path(media, reference).read_bytes() == expected_file
        if mime == "audio/wav":
            with wave.open(io.BytesIO(content.data), "rb") as sound:
                assert (
                    sound.getnchannels(),
                    sound.getsampwidth(),
                    sound.getframerate(),
                    sound.getnframes(),
                ) == (1, 2, 8000, 800)
                assert len(sound.readframes(800)) == 1600
        else:
            assert content.data.startswith(b"\x89PNG\r\n\x1a\n")
            position = 8
            while position < len(content.data):
                size = int.from_bytes(content.data[position : position + 4], "big")
                kind, data = (
                    content.data[position + 4 : position + 8],
                    content.data[position + 8 : position + 8 + size],
                )
                assert zlib.crc32(kind + data) == int.from_bytes(
                    content.data[position + 8 + size : position + 12 + size], "big"
                )
                if kind == b"IDAT":
                    assert len(zlib.decompress(data)) == 18
                position += 12 + size


def test_complete_directory_backup_restores_binary_and_existing_text_artifact(tmp_path):
    original = tmp_path / "original"
    with active_media(original, kind="tool") as (store, source, media):
        references = [
            (media.publish(source, data, mime), data)
            for mime, data in [("image/png", png()), ("audio/wav", wav())]
        ]
        text_source = ArtifactSource(
            source.session_id,
            source.turn_id,
            source.request_id,
            source.tool_call_id,
            source.tool_name,
        )
        text_files = ArtifactStore(store.root / "_artifacts")
        text = text_files.save(text_source, "existing text output")
        store.append_artifact(text)
        original_events = store.load_events(source.session_id)
    backup, restored_root = tmp_path / "backup", tmp_path / "restored"
    shutil.copytree(original / "sessions", backup)
    shutil.copytree(backup, restored_root)
    with SessionStore(restored_root) as store:
        store.select_session(source.session_id)
        media = SessionMedia(store, MediaStore(restored_root / "_media"))
        for reference, data in references:
            assert media.read(reference.media_id, expected_version=reference.version).data == data
        assert (
            ArtifactStore(restored_root / "_artifacts")
            .read(source.session_id, text.artifact_id, expected_version=text.version)
            .text
            == "existing text output"
        )
        assert store.load_media(source.session_id, references[0][0].media_id) == references[0][0]
    with SessionStore(original / "sessions") as store:
        assert store.load_events(source.session_id) == original_events


@pytest.mark.parametrize("failure", ["missing", "payload", "metadata"])
def test_missing_corrupt_or_relabelled_media_is_not_a_valid_reference(tmp_path, failure):
    with active_media(tmp_path) as (_, source, media):
        reference = media.publish(source, png(), "image/png")
        path = media_path(media, reference)
        if failure == "missing":
            path.unlink()
        elif failure == "payload":
            data = path.read_bytes()
            path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
        else:
            # 仅更改同长 MIME；正文哈希仍正确，但完整引用与宿主记录不符。
            data = path.read_bytes()
            assert b"image/png" in data
            path.write_bytes(data.replace(b"image/png", b"image/gif"))
        with pytest.raises(StorageError):
            media.read(reference.media_id, expected_version=reference.version)


@pytest.mark.parametrize("limit", ["single", "session", "items"])
def test_binary_limits_reject_whole_item_without_truncating_prior_media(tmp_path, limit):
    limits = (
        {"max_bytes": len(png()) - 1}
        if limit == "single"
        else {"max_session_bytes": len(png()) + 1}
        if limit == "session"
        else {"max_items": 1}
    )
    with active_media(tmp_path, **limits) as (store, source, media):
        if limit == "single":
            with pytest.raises(StorageError):
                media.publish(source, png(), "image/png")
            assert list(media.files.root.glob("*/*.media")) == []
        else:
            first = media.publish(source, png(), "image/png")
            before = media_path(media, first).read_bytes()
            with pytest.raises(StorageError):
                media.publish(source, wav(), "audio/wav")
            assert media_path(media, first).read_bytes() == before
            assert media.read(first.media_id, expected_version=first.version).data == png()
        assert len(
            [
                event
                for event in store.load_events(source.session_id)
                if event["type"] == "media_saved"
            ]
        ) == (0 if limit == "single" else 1)


def test_cross_session_and_copied_file_do_not_grant_access(tmp_path):
    with active_media(tmp_path) as (store, source, media):
        reference = media.publish(source, png(), "image/png")
        other = store.create_session("other owner", "")
        turn = store.start_turn(other, "another input")
        with pytest.raises(StorageError, match="来源"):
            media.read(reference.media_id, expected_version=reference.version)
        target = media.files.root / other / (reference.media_id + ".media")
        target.parent.mkdir(parents=True)
        target.write_bytes(media_path(media, reference).read_bytes())
        # 伪造本会话声明仍不能把另一个所有者的旁路文件变成有效媒体。
        forged = replace(reference, source=MediaSource(other, turn, "input"))
        store.append_media(forged)
        with pytest.raises(StorageError):
            media.read(reference.media_id, expected_version=reference.version)
        store.select_session(source.session_id)
        assert media.read(reference.media_id, expected_version=reference.version).data == png()


@pytest.mark.parametrize("boundary", ["write", "fsync", "publish", "source_log"])
def test_failed_commit_delivers_no_readable_reference_or_partial_item(
    tmp_path, monkeypatch, boundary
):
    with active_media(tmp_path) as (store, source, media):
        first = media.publish(source, png(), "image/png")
        before = media_path(media, first).read_bytes()
        events = store.load_events(source.session_id)

        def fail(*args, **kwargs):
            raise PermissionError("injected binary publish IO failure")

        def fail_log(reference):
            # 正文已经真实原子发布，仅故障注入来源记录提交边界。
            raise StorageError("injected media source log IO failure")

        original_temporary = media_module.tempfile.NamedTemporaryFile

        class PartialWrite:
            """在真实临时文件写入部分字节后失败，不替换原子发布算法。"""

            def __init__(self, file):
                self.file = file

            def __getattr__(self, name):
                return getattr(self.file, name)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.file.__exit__(*args)

            def write(self, data):
                self.file.write(data[: max(1, len(data) // 2)])
                raise PermissionError("injected partial binary write")

        with monkeypatch.context() as patch:
            if boundary == "write":
                patch.setattr(
                    media_module.tempfile,
                    "NamedTemporaryFile",
                    lambda **kwargs: PartialWrite(original_temporary(**kwargs)),
                )
            elif boundary == "source_log":
                patch.setattr(store, "append_media", fail_log)
            else:
                patch.setattr(media_module.os, "fsync" if boundary == "fsync" else "replace", fail)
            with pytest.raises(StorageError):
                media.publish(source, wav(), "audio/wav")
        assert store.load_events(source.session_id) == events
        assert media_path(media, first).read_bytes() == before
        published = list(media.files.root.glob("*/*.media"))
        assert len(published) == (2 if boundary == "source_log" else 1)
        assert list(media.files.root.glob("*/.media-*.tmp")) == []
        if boundary == "source_log":
            orphan = next(path for path in published if path != media_path(media, first))
            with pytest.raises(StorageError, match="来源"):
                media.read(orphan.stem, expected_version=hashlib.sha256(wav()).hexdigest())


def test_two_store_instances_cannot_overcommit_session_quota(tmp_path):
    with active_media(tmp_path, max_session_bytes=12) as (store, source, media):
        barrier = Barrier(2)

        def save():
            files = MediaStore(media.files.root, max_session_bytes=12)
            barrier.wait(timeout=3)
            try:
                return files.save(source, b"12345678", "application/octet-stream")
            except StorageError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            references = [ref for ref in pool.map(lambda _: save(), range(2)) if ref is not None]
        assert len(references) == 1
        store.append_media(references[0])
        assert (
            media.read(references[0].media_id, expected_version=references[0].version).data
            == b"12345678"
        )
        assert len(list(media.files.root.glob("*/*.media"))) == 1


def test_orphan_temporary_is_not_promoted_or_given_source_after_restart(tmp_path):
    with active_media(tmp_path) as (store, source, media):
        reference = media.publish(source, png(), "image/png")
        path = media_path(media, reference)
        temporary = path.parent / ".media-unpublished.tmp"
        temporary.write_bytes(path.read_bytes())
    with SessionStore(tmp_path / "sessions") as store:
        store.select_session(source.session_id)
        media = SessionMedia(store, MediaStore(store.root / "_media"))
        assert media.read(reference.media_id, expected_version=reference.version).data == png()
        assert (
            len(
                [
                    event
                    for event in store.load_events(source.session_id)
                    if event["type"] == "media_saved"
                ]
            )
            == 1
        )
        assert temporary.exists() and len(list(path.parent.glob("*.media"))) == 1


def test_regular_parent_and_child_tools_cannot_read_private_binary_directory(tmp_path):
    private = tmp_path / "sessions" / "_media" / "private.txt"
    private.parent.mkdir(parents=True)
    private.write_text("PRIVATE_MEDIA_BODY", encoding="utf-8")
    counts = {"parent": 0, "child": 0}

    def child(request):
        counts["child"] += 1
        body = json.loads(request.content)
        if counts["child"] == 1:
            result = tool_response([call("child-probe", "read_file", path=str(private))])
        else:
            assert outputs(body)[-1]["ok"] is False and "保留目录" in outputs(body)[-1]["error"]
            result = answer("child denied")
        return httpx.Response(200, json=result)

    def parent(request):
        counts["parent"] += 1
        body = json.loads(request.content)
        phase = counts["parent"]
        if phase == 1:
            result = tool_response(
                [
                    call("parent-probe", "read_file", path=str(private)),
                    call("spawn", "spawn_agent", name="reader", task="probe media"),
                ]
            )
        elif phase == 2:
            assert outputs(body)[-2]["ok"] is False and "保留目录" in outputs(body)[-2]["error"]
            result = tool_response([call("wait", "wait_agents", agent_ids=[])])
        else:
            assert outputs(body)[-1]["agents"][0]["reply"] == "child denied"
            result = answer("private media protected")
        assert "PRIVATE_MEDIA_BODY" not in json.dumps(body)
        return httpx.Response(200, json=result)

    with delegated_application(tmp_path, parent, child) as (app, _):
        assert app.send("inspect private boundaries") == "private media protected"
    assert counts == {"parent": 3, "child": 2}


@pytest.mark.parametrize("operation", ["publish", "read"])
def test_session_changed_during_file_io_does_not_publish_or_deliver_to_wrong_owner(
    tmp_path, monkeypatch, operation
):
    with active_media(tmp_path) as (store, source, media):
        first = media.publish(source, png(), "image/png")
        other = store.create_session("another owner", "")
        before = store.load_events(source.session_id)
        if operation == "publish":
            original = media.files.save

            def change_after_save(*args, **kwargs):
                reference = original(*args, **kwargs)
                store.select_session(other)
                return reference

            monkeypatch.setattr(media.files, "save", change_after_save)
            with pytest.raises(StorageError, match="当前会话"):
                media.publish(source, wav(), "audio/wav")
            assert store.load_events(source.session_id) == before
        else:
            original = media.files.read

            def change_after_read(*args, **kwargs):
                content = original(*args, **kwargs)
                store.select_session(other)
                return content

            monkeypatch.setattr(media.files, "read", change_after_read)
            with pytest.raises(StorageError, match="当前会话"):
                media.read(first.media_id, expected_version=first.version)
        assert store.active_session_id == other
        assert not any(event["type"] == "media_saved" for event in store.load_events(other))
