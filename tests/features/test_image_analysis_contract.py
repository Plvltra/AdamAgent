"""图片分析扩展只验收来源和边界；合成字节与替身分析器不证明实际视觉效果。"""

import hashlib
import json

import pytest

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import BudgetError, RequestInterrupted, StorageError
from adam.contracts.perception import ImageReply
from adam.documents.image_analysis import inspect_image
from adam.documents.models import DocumentError


class Analyzer:
    """只捕获扩展契约的输入，返回预设文本；不解码像素或调用模型。"""

    def __init__(self, reply=None, failure=None):
        self.reply = reply or ImageReply("unverified observation", "fixture-model", (10, 5))
        self.failure = failure
        self.requests = []

    def analyze(self, request, check_cancel):
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        check_cancel()
        return self.reply


def test_missing_backend_has_explicit_capability_result_and_never_creates_network_client(
    monkeypatch,
):
    def forbidden(*args, **kwargs):
        pytest.fail("unconfigured perception performed external work")

    monkeypatch.setattr("httpx.Client", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    result = inspect_image(b"opaque fixture bytes", "image/png", "what can be read?")
    assert result.status == "unavailable" and result.reason == "image_backend_not_configured"
    assert result.reply is None and not result.independently_verified
    assert result.source_version == hashlib.sha256(b"opaque fixture bytes").hexdigest()
    assert "opaque fixture bytes" not in json.dumps(result.to_dict())


@pytest.mark.parametrize("usage", [(20, 7), None])
def test_explicit_backend_receives_exact_bytes_once_and_source_is_assigned_by_host(usage):
    analyzer = Analyzer(ImageReply("model interpretation", "fixture-model", usage))
    data = b"bounded original input"
    result = inspect_image(data, "image/webp", "describe only", analyzer=analyzer)
    assert len(analyzer.requests) == 1
    request = analyzer.requests[0]
    assert request.data is data and request.source_version == hashlib.sha256(data).hexdigest()
    assert request.question == "describe only" and request.max_output_chars == 4000
    assert "bounded original input" not in repr(request)
    assert result.status == "analyzed" and result.reply.usage == usage
    assert not result.independently_verified
    assert (
        inspect_image(data + b"changed", "image/webp", "describe only").source_version
        != result.source_version
    )


@pytest.mark.parametrize(
    "invalid",
    [
        {"data": b""},
        {"data": bytearray(b"x")},
        {"mime_type": "application/pdf"},
        {"question": " "},
        {"question": "x" * 2001},
        {"question": "\ud800"},
        {"max_input_bytes": True},
        {"max_input_bytes": 1},
        {"max_output_chars": 8001},
    ],
)
def test_invalid_input_is_rejected_before_backend_invocation(invalid):
    analyzer = Analyzer()
    args = {"data": b"source", "mime_type": "image/jpeg", "question": "describe", **invalid}
    with pytest.raises(DocumentError):
        inspect_image(**args, analyzer=analyzer)
    assert analyzer.requests == []


@pytest.mark.parametrize(
    "reply",
    [
        ImageReply("", "model", (1, 2)),
        ImageReply("text", "", (1, 2)),
        ImageReply("too long", "model", (1, 2)),
        ImageReply("ok", "model", (True, 0)),
        ImageReply("ok", "model", (-1, 0)),
        ImageReply("\ud800", "model", (1, 0)),
    ],
)
def test_invalid_backend_reply_fails_without_returning_partial_or_retrying(reply):
    analyzer = Analyzer(reply)
    with pytest.raises(DocumentError, match="返回"):
        inspect_image(b"input", "image/png", "describe", analyzer=analyzer, max_output_chars=4)
    assert len(analyzer.requests) == 1


@pytest.mark.parametrize(
    "failure", [BudgetError("budget"), StorageError("log"), RequestInterrupted()]
)
def test_original_budget_log_and_cancel_failures_propagate_without_conversion(failure):
    analyzer = Analyzer(failure=failure)
    with pytest.raises(type(failure)) as caught:
        inspect_image(b"input", "image/png", "describe", analyzer=analyzer)
    assert caught.value is failure and len(analyzer.requests) == 1


def test_unknown_backend_error_is_sanitized_and_not_retried():
    analyzer = Analyzer(failure=RuntimeError("PRIVATE_PROVIDER_BODY"))
    with pytest.raises(DocumentError) as caught:
        inspect_image(b"input", "image/png", "describe", analyzer=analyzer)
    assert "PRIVATE_PROVIDER_BODY" not in str(caught.value) and len(analyzer.requests) == 1


def test_already_cancelled_analysis_never_calls_backend():
    token = CancellationToken()
    token.cancel()
    analyzer = Analyzer()
    with pytest.raises(RequestInterrupted):
        inspect_image(
            b"input", "image/png", "describe", analyzer=analyzer, check_cancel=token.check
        )
    assert analyzer.requests == []


def test_cancel_after_backend_finishes_does_not_return_result(monkeypatch):
    token = CancellationToken()
    analyzer = Analyzer()

    def cancel_after(request, check_cancel):
        analyzer.requests.append(request)
        token.cancel()
        return ImageReply("finished but cancelled", "fixture-model", (3, 2))

    monkeypatch.setattr(analyzer, "analyze", cancel_after)
    with pytest.raises(RequestInterrupted):
        inspect_image(
            b"input", "image/png", "describe", analyzer=analyzer, check_cancel=token.check
        )
    assert len(analyzer.requests) == 1
