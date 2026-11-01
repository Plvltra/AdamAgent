"""C04.2：实际文件/工具来源、单次视觉协议和统一 JSONL；语义质量另做真实本地验收。"""

import json
import threading
from contextlib import contextmanager
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from adam.application.image_questions import ImageQuestions
from adam.bootstrap import open_image_questions
from adam.cli.image_question import main
from adam.config.consumption import DEFAULT_CONSUMPTION, ConsumptionConfig
from adam.config.settings import AgentConfig, ContextConfig, LLMConfig
from adam.config.vision import LocalVisionConfig
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import (
    AgentError,
    BudgetError,
    ConfigError,
    LLMError,
    RequestInterrupted,
    StorageError,
    TurnSaveError,
)
from adam.contracts.media import decode_media_reference
from adam.llm.client import LLMClient
from adam.llm.local_vision import LlamaVisionClient
from adam.session.storage import SessionStore
from tests.features.test_image_input import image_app, image_config, load_then_answer
from tests.features.test_multimodal_input import wire_bytes
from tests.features.test_streaming import DONE, USAGE, event, sse
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response
from tests.image_samples import image_bytes


@contextmanager
def questions_at(
    root,
    respond=None,
    *,
    stream=False,
    limits=None,
    context=None,
    token=None,
    supported=True,
    consumption=DEFAULT_CONSUMPTION,
):
    """只替换远端 HTTP；真实加载与独立图问沿同一消费配置，共用已发生消费。"""
    (root / "sample.bin").write_bytes(image_bytes())
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        result = respond(request) if respond else answer("label and layout", tokens=20)
        return result if isinstance(result, httpx.Response) else httpx.Response(200, json=result)

    with image_app(root, load_then_answer, config=image_config(root, consumption=consumption)) as (
        app,
        store,
        media,
        original,
    ):
        app.send("load only, not a vision question")
        ref = decode_media_reference(outputs(original[-1])[-1]["media"])
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            local = LocalVisionConfig("adam-vision", stream=stream)
            client = (
                LlamaVisionClient(
                    local, http, lambda r: media.read(r.media_id, expected_version=r.version)
                )
                if supported
                else LLMClient(LLMConfig("http://127.0.0.1:8087/v1", "text", "unused"), http)
            )
            service = ImageQuestions(
                store,
                client,
                media,
                local.model,
                limits or AgentConfig(),
                context or ContextConfig(),
                token or CancellationToken(),
                consumption=consumption,
            )
            yield service, store, ref, bodies, app


def events(store, sid, kind):
    return [e["payload"] for e in store.load_events(sid) if e["type"] == kind]


@pytest.mark.parametrize("stream", [False, True])
def test_single_image_answer_binds_original_reference_and_records_unpriced_cost(tmp_path, stream):
    def respond(_):
        if stream:
            return sse(
                [
                    event({"role": "assistant", "content": "label and layout"}),
                    event(reason="stop", usage=USAGE),
                    DONE,
                ]
            )
        return answer("label and layout", tokens=20)

    with questions_at(tmp_path, respond, stream=stream) as (qa, store, ref, bodies, app):
        (tmp_path / "sample.bin").unlink()
        result = qa.ask("Which label is above the circle?", ref)
        assert result.text == "label and layout" and result.images == (ref,)
        assert result.session_id == app.session.session_id
        assert len(bodies) == 1 and bodies[0]["model"] == "adam-vision"
        assert wire_bytes(bodies[0]) == [image_bytes()]
        assert "tools" not in bodies[0] and len(bodies[0]["messages"]) == 2
        assert "load only" not in json.dumps(bodies[0])
        assert "system constraint" in bodies[0]["messages"][0]["content"]
        assert app.model == "test"
        started = events(store, result.session_id, "request_started")[-1]
        evidence = started["context"]["image_question"]
        assert evidence == {
            "version": 1,
            "backend": "llama_cpp",
            "images": [asdict(ref)],
            "cost": None,
            "currency": None,
            "cost_status": "unpriced",
        }
        assert started["request_id"] == result.request_id
        assert started["context"]["selected_turn_ids"] == []
        assert len(started["context"]["omitted_turn_ids"]) == 1
        finished = events(store, result.session_id, "request_finished")[-1]
        assert finished["status"] == "succeeded" and finished["duration_ms"] >= 0
        assert events(store, result.session_id, "turn_completed")[-1]["turn_id"] == result.turn_id
        messages = store.load_messages(result.session_id)
        assert messages[-2].raw["media"] == [asdict(ref)]
        assert messages[-1].content == result.text
        assert "base64," not in json.dumps(store.load_events(result.session_id))
        expected = 13 if stream else 22
        assert store.usage(result.session_id) == (24 + expected, False)


@pytest.mark.parametrize("question", ["", "  ", "x" * 16001, None])
def test_invalid_question_does_not_register_input_or_request(tmp_path, question):
    with questions_at(tmp_path) as (qa, store, ref, bodies, _):
        before = store.load_events(ref.source.session_id)
        with pytest.raises(AgentError):
            qa.ask(question, ref)
        assert bodies == [] and store.load_events(ref.source.session_id) == before


@pytest.mark.parametrize(
    "failure",
    [
        "unsupported",
        "forged_source",
        "changed_version",
        "other_session",
        "cancelled",
        "missing_bytes",
        "unsupported_mime",
    ],
)
def test_image_preflight_failure_never_falls_back_to_text(tmp_path, failure):
    token = CancellationToken()
    with questions_at(tmp_path, token=token, supported=failure != "unsupported") as (
        qa,
        store,
        ref,
        bodies,
        _,
    ):
        sid = ref.source.session_id
        before = store.load_events(sid)
        if failure == "forged_source":
            ref = replace(ref, source=replace(ref.source, tool_call_id="different"))
        if failure == "unsupported_mime":
            ref = replace(ref, mime_type="audio/wav")
        if failure == "changed_version":
            ref = replace(ref, version="0" * 64)
        if failure == "other_session":
            other = store.create_session("other", "system")
            store.select_session(other)
        if failure == "cancelled":
            token.cancel()
        if failure == "missing_bytes":
            for path in store.root.rglob("*.media"):
                path.unlink()
        with pytest.raises((AgentError, RequestInterrupted)):
            qa.ask("Describe layout", ref)
        assert bodies == [] and store.load_events(sid) == before


@pytest.mark.parametrize(
    "limits,context",
    [
        (AgentConfig(max_session_tokens=100), None),
        (AgentConfig(max_model_requests=0), None),
        (None, ContextConfig(max_input_tokens=100, safety_margin_tokens=0)),
    ],
)
def test_budget_rejection_has_no_new_input_or_http(tmp_path, limits, context):
    with questions_at(tmp_path, limits=limits, context=context) as (qa, store, ref, bodies, _):
        before = store.load_events(ref.source.session_id)
        with pytest.raises(BudgetError):
            qa.ask("Describe layout", ref)
        assert bodies == [] and store.load_events(ref.source.session_id) == before


@pytest.mark.parametrize(
    "failure,reason,unknown",
    [
        ("timeout", "api_timeout", True),
        ("connect", "api_not_sent", False),
        ("http", "api_http", True),
        ("usage", "usage_unknown", True),
        ("cancel", "interrupted", True),
        ("invalid", "invalid_response", False),
        ("tools", "invalid_response", False),
    ],
)
def test_request_failures_settle_once_and_do_not_retry_or_execute_tools(
    tmp_path, failure, reason, unknown
):
    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("private detail", request=request)
        if failure == "connect":
            raise httpx.ConnectError("private detail", request=request)
        if failure == "http":
            return httpx.Response(503, text="private detail")
        if failure == "cancel":
            raise RequestInterrupted()
        if failure == "invalid":
            data = answer("partial", tokens=20)
            data["choices"][0]["finish_reason"] = "length"
            return data
        if failure == "tools":
            return tool_response([call("write", "write_file", path="pwned", content="bad")])
        data = answer()
        del data["usage"]
        return data

    with questions_at(tmp_path, respond) as (qa, store, ref, bodies, _):
        sid = ref.source.session_id
        history = store.load_messages(sid)
        with pytest.raises((LLMError, RequestInterrupted)) as error:
            qa.ask("Describe layout", ref)
        actual_reason = (
            "interrupted" if isinstance(error.value, RequestInterrupted) else error.value.code.value
        )
        assert actual_reason == reason and "private detail" not in str(error.value)
        assert len(bodies) == 1 and not (tmp_path / "pwned").exists()
        assert len(events(store, sid, "request_started")) == 3
        assert len(events(store, sid, "request_usage")) == 3
        assert events(store, sid, "turn_finished")[-1]["reason"] == reason
        assert events(store, sid, "request_finished")[-1]["duration_ms"] >= 0
        assert store.usage(sid)[1] is unknown and store.load_messages(sid) == history
        if unknown:
            with pytest.raises(BudgetError):
                qa.ask("Do not retry automatically", ref)
            assert len(bodies) == 1


@pytest.mark.parametrize("boundary", ["start_request", "finish_request", "append_messages"])
def test_storage_failures_never_return_false_saved_answer_or_repeat_http(
    tmp_path, monkeypatch, boundary
):
    with questions_at(tmp_path) as (qa, store, ref, bodies, _):
        sid = ref.source.session_id
        old = store.load_messages(sid)

        def fail(*args, **kwargs):
            raise StorageError("injected local log boundary")

        monkeypatch.setattr(store, boundary, fail)
        with pytest.raises((StorageError, TurnSaveError)) as error:
            qa.ask("Describe layout", ref)
        assert len(bodies) == (0 if boundary == "start_request" else 1)
        assert store.load_messages(sid) == old
        if boundary != "start_request":
            assert error.value.partial_reply == "label and layout"
        if boundary == "append_messages":
            assert events(store, sid, "request_finished")[-1]["status"] == "succeeded"
            assert events(store, sid, "turn_finished")[-1]["reason"] == "answer_save_failed"


def test_actual_overrun_is_recorded_then_refused_without_second_call(tmp_path):
    with questions_at(tmp_path, lambda _: answer("overrun", tokens=60000)) as (
        qa,
        store,
        ref,
        bodies,
        _,
    ):
        with pytest.raises(BudgetError):
            qa.ask("Describe layout", ref)
        assert len(bodies) == 1 and store.usage(ref.source.session_id) == (60026, False)
        assert events(store, ref.source.session_id, "request_finished")[-1]["status"] == "succeeded"


@pytest.mark.parametrize(
    "field,value", [("cost", "0"), ("currency", "USD"), ("version", True), ("images", [])]
)
def test_persisted_question_evidence_rejects_forged_price_or_image(tmp_path, field, value):
    with questions_at(tmp_path) as (qa, store, ref, _, _):
        qa.ask("Describe layout", ref)
        sid = ref.source.session_id
        data = store.load_events(sid)
        started = next(e for e in reversed(data) if e["type"] == "request_started")
        started["payload"]["context"]["image_question"][field] = value
        from adam.session.log import parse_log

        with pytest.raises(ValueError):
            parse_log(("\n".join(json.dumps(e) for e in data) + "\n").encode(), sid)


@contextmanager
def local_server(*, wait=False, redirect=None):
    """真实 TCP 边界验证取消/超时/重定向；不启动模型，不把替身当语义验收。"""
    seen, release = [], threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            seen.append(json.loads(raw))
            if wait:
                release.wait(2)
            body = json.dumps(answer("real local HTTP", tokens=30)).encode()
            try:
                self.send_response(307 if redirect else 200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                if redirect:
                    self.send_header("Location", redirect)
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", seen
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def prepared(root):
    with questions_at(root) as (_, store, ref, _, _):
        return store.root, ref


@pytest.mark.parametrize("scenario", ["success", "timeout", "cancel", "redirect"])
def test_production_factory_real_tcp_lifecycle_and_reopen_without_automatic_request(
    tmp_path, scenario
):
    root, ref = prepared(tmp_path)
    token = CancellationToken()
    with local_server(
        wait=scenario in {"timeout", "cancel"},
        redirect="http://127.0.0.1:1/private" if scenario == "redirect" else None,
    ) as (endpoint, seen):
        local = LocalVisionConfig(
            "adam-vision",
            endpoint,
            timeout_seconds=0.15 if scenario == "timeout" else 5,
            stream=False,
        )
        timer = None
        if scenario == "cancel":

            def stop_when_sent():
                while not seen and not token.cancelled:
                    threading.Event().wait(0.01)
                token.cancel()

            timer = threading.Thread(target=stop_when_sent, daemon=True)
            timer.start()
        try:
            with open_image_questions(root, ref.source.session_id, local, cancel=token) as qa:
                if scenario == "success":
                    assert (
                        qa.ask_saved("Describe layout", ref.media_id, ref.version).text
                        == "real local HTTP"
                    )
                else:
                    with pytest.raises((LLMError, RequestInterrupted)):
                        qa.ask_saved("Describe layout", ref.media_id, ref.version)
        finally:
            token.cancel()
            if timer:
                timer.join(timeout=3)
        assert len(seen) == 1
        with SessionStore(root) as reopened:
            reopened.select_session(ref.source.session_id)
            assert len(seen) == 1
            assert (
                events(reopened, ref.source.session_id, "request_started")[-1]["context"][
                    "image_question"
                ]["cost"]
                is None
            )
            assert events(reopened, ref.source.session_id, "request_finished")[-1]["status"] == (
                "succeeded"
                if scenario == "success"
                else "interrupted"
                if scenario == "cancel"
                else "failed"
            )


@pytest.mark.parametrize("no_stream", [False, True])
def test_cli_uses_only_explicit_local_backend_and_prints_reference(
    tmp_path, monkeypatch, capsys, no_stream
):
    root, ref = prepared(tmp_path)
    with local_server() as (endpoint, seen):
        monkeypatch.setattr(
            "sys.argv",
            [
                "image-question",
                "--sessions-dir",
                str(root),
                "--session",
                ref.source.session_id,
                "--media-id",
                ref.media_id,
                "--version",
                ref.version,
                "--question",
                "Describe layout",
                "--model",
                "adam-vision",
                "--endpoint",
                endpoint,
                *(["--no-stream"] if no_stream else []),
            ],
        )
        # JSON 服务故意与默认 SSE 不同，命令必须受控失败，不降级重问。
        assert main() == (0 if no_stream else 1) and len(seen) == 1
        result = json.loads(capsys.readouterr().out)
        if no_stream:
            assert result["text"] == "real local HTTP" and result["images"] == [asdict(ref)]
        else:
            assert result["reason"] == "invalid_response"


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1:8087/v1",
        "http://localhost:8087/v1",
        "http://example.test:8087/v1",
        "http://127.0.0.1:80/v1",
        "http://127.0.0.1:65536/v1",
        "http://127.0.0.1:8087/v1?x=1",
        "http://user@127.0.0.1:8087/v1",
        "http://127.0.0.1:8087/v1/next",
    ],
)
def test_local_config_cannot_be_used_as_remote_upload_route(endpoint):
    with pytest.raises(ConfigError):
        LocalVisionConfig("adam-vision", endpoint)


@pytest.mark.parametrize(
    "changes",
    [
        {"model": ""},
        {"model": "\nprivate"},
        {"model": None},
        {"timeout_seconds": 0},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": 10**1000},
        {"timeout_seconds": True},
        {"stream": "true"},
        {"endpoint": None},
    ],
)
def test_invalid_direct_local_configuration_has_controlled_error(changes):
    with pytest.raises(ConfigError):
        LocalVisionConfig(**{"model": "adam-vision", **changes})


def test_non_decoded_media_provenance_is_not_upgraded_to_valid_picture(tmp_path):
    with questions_at(tmp_path) as (qa, store, ref, bodies, _):
        before = store.load_events(ref.source.session_id)
        ref = replace(ref, source=replace(ref.source, tool_name="download_browser"))
        with pytest.raises(AgentError, match="load_image"):
            qa.ask("Describe layout", ref)
        assert bodies == [] and store.load_events(ref.source.session_id) == before


@pytest.mark.parametrize("child_usage", [(42000, 0), None])
def test_restored_child_usage_or_unknown_cannot_be_bypassed_by_visual_entry(tmp_path, child_usage):
    from adam.contracts.delegation import ChildAgent
    from adam.session.delegation import DelegationStore
    from adam.session.outcome import ExecutionOutcome

    with questions_at(tmp_path) as (qa, store, ref, bodies, _):
        root = store.root / "_subagents" / ref.source.session_id
        index = DelegationStore(root)
        with SessionStore(root / "sessions") as children:
            child = children.create_session("prior helper", "child")
            children.select_session(child)
            turn = children.start_turn(child, "prior work")
            request = children.start_request(child, turn, "prior-model")
            children.finish_request(child, turn, request, child_usage)
            children.finish_turn(child, turn, ExecutionOutcome.failure(LLMError("prior failure")))
        index.put(ChildAgent(child, "prior helper", str(tmp_path), []))
        before = store.load_events(ref.source.session_id)
        with pytest.raises(BudgetError):
            qa.ask("Describe layout", ref)
        assert bodies == [] and store.load_events(ref.source.session_id) == before


@pytest.mark.parametrize("failure", ["missing", "corrupt", "busy"])
def test_unreadable_existing_child_stops_before_visual_input_and_http(tmp_path, failure):
    """在真实子日志缺失、完整坏行或写锁边界注入故障，不能用零消费继续请求。"""
    from adam.contracts.delegation import ChildAgent
    from adam.session.delegation import DelegationStore

    with questions_at(tmp_path) as (qa, store, ref, bodies, _):
        root = store.root / "_subagents" / ref.source.session_id
        index = DelegationStore(root)
        with SessionStore(root / "sessions") as children:
            child = children.create_session("prior helper", "child")
            index.put(ChildAgent(child, "prior helper", str(tmp_path), []))
            path = next(children.root.glob("*/*/*/" + child + ".jsonl"))
            if failure == "missing":
                path.unlink()
            elif failure == "corrupt":
                with path.open("ab") as file:
                    file.write(b"not-json\n")
            else:
                children.select_session(child)
            before = store.load_events(ref.source.session_id)
            original_index = index.path.read_bytes()
            with pytest.raises(AgentError):
                qa.ask("Describe layout", ref)
            assert bodies == [] and store.load_events(ref.source.session_id) == before
            assert index.path.read_bytes() == original_index


def test_repeated_explicit_questions_do_not_double_settle_previous_usage(tmp_path):
    with questions_at(tmp_path) as (qa, store, ref, bodies, _):
        one = qa.ask("First question", ref)
        two = qa.ask("Second question", ref)
        assert one.request_id != two.request_id and len(bodies) == 2
        assert store.usage(ref.source.session_id) == (68, False)
        assert len(events(store, ref.source.session_id, "request_usage")) == 4


def test_fixed_sample_generator_is_frozen_and_never_overwrites_existing_batch(tmp_path):
    import hashlib

    from scripts.vision_acceptance import generate

    root = tmp_path / "fixed"
    generate(root)
    metadata = json.loads((root / "fixed_cases.json").read_text(encoding="utf-8"))
    assert len(metadata["cases"]) == 4 and len(metadata["image_sha256"]) == 3
    for name, digest in metadata["image_sha256"].items():
        assert hashlib.sha256((root / "images" / name).read_bytes()).hexdigest() == digest
    before = (root / "fixed_cases.json").read_bytes()
    with pytest.raises(FileExistsError):
        generate(root)
    assert (root / "fixed_cases.json").read_bytes() == before


def test_unit_caps_cover_prior_load_and_repeat_question_without_new_input(tmp_path):
    consumption = ConsumptionConfig(enabled=True, max_calls=3, max_images=1)
    with questions_at(tmp_path, consumption=consumption) as (qa, store, ref, bodies, _):
        result = qa.ask("layout", ref)
        before = store.load_events(ref.source.session_id)
        usage = store.consumption_usage(ref.source.session_id)
        assert usage.known.calls == 3 and usage.known.images == 1
        assert usage.unknown_units == ()
        assert result.text == "label and layout"
        with pytest.raises(BudgetError):
            qa.ask("second question", ref)
        assert len(bodies) == 1 and store.load_events(ref.source.session_id) == before


def test_cli_independent_consumption_config_counts_real_tcp_without_default_key(
    tmp_path, monkeypatch, capsys
):
    with questions_at(tmp_path, consumption=ConsumptionConfig(enabled=True)) as (
        _,
        store,
        ref,
        _,
        _,
    ):
        root = store.root
    config = tmp_path / "explicit-consumption.toml"
    config.write_text("[consumption]\nenabled=true\nmax_images=1\nmax_calls=3\n", encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with local_server() as (endpoint, seen):
        arguments = [
            "image-question",
            "--sessions-dir",
            str(root),
            "--session",
            ref.source.session_id,
            "--media-id",
            ref.media_id,
            "--version",
            ref.version,
            "--question",
            "layout",
            "--model",
            "adam-vision",
            "--endpoint",
            endpoint,
            "--no-stream",
            "--consumption-config",
            str(config),
        ]
        monkeypatch.setattr("sys.argv", arguments)
        assert main() == 0
        assert json.loads(capsys.readouterr().out)["ok"]
        assert main() == 1
        assert json.loads(capsys.readouterr().out)["reason"] == "budget_exhausted"
        assert len(seen) == 1
    with SessionStore(root) as store:
        store.select_session(ref.source.session_id)
        usage = store.consumption_usage(ref.source.session_id)
        assert usage.known.calls == 3 and usage.known.images == 1 and not usage.unknown_units
