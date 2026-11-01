"""B03.3：真实 HTTP 适配与文件报告的离线验收，不以模拟命中率证明真实收益。"""

import json
from decimal import Decimal

import httpx
import pytest

from adam.config.settings import LLMConfig
from scripts import cache_eval
from tests.features.test_streaming import DONE, event, sse
from tests.features.test_tool_loop import answer

LLM = LLMConfig("https://cache-evaluation.invalid", "demo", "synthetic-secret", stream=False)


def reply(*, cache=True, stream=False):
    value = answer("已观察")
    value["usage"] = {"prompt_tokens": 100, "completion_tokens": 3}
    if cache:
        value["usage"].update(prompt_cache_hit_tokens=80, prompt_cache_miss_tokens=20)
    if stream:
        return sse([event({"content": "已观察"}), event(reason="stop", usage=value["usage"]), DONE])
    return httpx.Response(200, json=value)


@pytest.mark.parametrize("stream", [False, True])
def test_fixed_two_layouts_keep_units_usage_prices_and_observation_scopes_separate(
    tmp_path, stream
):
    from dataclasses import replace

    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        assert "tools" not in body
        return reply(stream=stream)

    root = tmp_path / "evaluation"
    result = cache_eval.run_suite(
        root,
        replace(LLM, stream=stream),
        rounds=2,
        prices=(Decimal("0.5"), Decimal("2"), Decimal("8")),
        transport=httpx.MockTransport(handle),
    )
    assert result["backend"] == "mock" and result["known_tokens"] == 412
    assert result["usage_unknown"] is False and len(requests) == 4
    rows = result["requests"]
    assert [r["arm"] for r in rows] == ["legacy", "stable", "stable", "legacy"]
    assert all(
        r["usage"] == [100, 3] and Decimal(r["cost_cny"]) == Decimal("0.000104") for r in rows
    )
    assert all(r["estimated_input_tokens"] > 100 for r in rows)
    assert all(r["input_prefix"]["input_bytes"] > r["estimated_input_tokens"] for r in rows)
    assert rows[0]["input_prefix"]["scope_id"] != rows[1]["input_prefix"]["scope_id"]
    assert rows[2]["input_prefix"]["previous_sha256"] == rows[1]["input_prefix"]["input_sha256"]
    assert rows[3]["input_prefix"]["previous_sha256"] == rows[0]["input_prefix"]["input_sha256"]
    assert (
        rows[2]["input_prefix"]["common_prefix_bytes"]
        > rows[3]["input_prefix"]["common_prefix_bytes"] + 1000
    )
    assert all(
        group["measurement_samples"] == 1 and group["elapsed_ms_mean"] >= 0
        for group in result["groups"].values()
    )
    for row, request in zip(rows, requests, strict=True):
        expected = cache_eval.request_context(row["arm"], row["round"], "demo")
        assert request["messages"] == expected.messages
    evidence = "".join(path.read_text(encoding="utf-8") for path in root.glob("*.json"))
    assert LLM.api_key not in evidence and LLM.base_url not in evidence


@pytest.mark.parametrize(
    "detail", [{}, {"prompt_cache_hit_tokens": 80}, {"prompt_cache_hit_tokens": True}]
)
def test_absent_partial_or_invalid_cache_does_not_invent_cost_or_erase_total(tmp_path, detail):
    def handle(request):
        value = answer("ok")
        value["usage"] = {"prompt_tokens": 100, "completion_tokens": 3, **detail}
        return httpx.Response(200, json=value)

    result = cache_eval.run_suite(
        tmp_path / "run",
        LLM,
        rounds=2,
        prices=(Decimal("1"), Decimal("2"), Decimal("3")),
        transport=httpx.MockTransport(handle),
    )
    assert result["known_tokens"] == 412 and not result["usage_unknown"]
    assert all(row["cost_cny"] is None for row in result["requests"])
    assert all(
        group["cache_tokens"]["miss_tokens"]["unknown_requests"] == 2
        and group["cost_cny_including_warmup"] is None
        for group in result["groups"].values()
    )


def test_api_failure_stops_remaining_samples_but_keeps_earlier_known_usage(tmp_path):
    calls = []

    def handle(request):
        calls.append(request)
        return reply() if len(calls) < 3 else httpx.Response(503, text=LLM.api_key)

    result = cache_eval.run_suite(
        tmp_path / "run", LLM, rounds=2, transport=httpx.MockTransport(handle)
    )
    assert len(calls) == 3 and result["known_tokens"] == 206 and result["usage_unknown"]
    assert [row["state"] for row in result["requests"]] == [
        "succeeded",
        "succeeded",
        "failed",
        "not_run",
    ]
    assert result["requests"][2]["error"] == "api_http"
    assert LLM.api_key not in json.dumps(result)


def test_missing_total_stops_without_treating_unknown_as_free_request(tmp_path):
    calls = []

    def handle(request):
        calls.append(request)
        value = answer("ok")
        del value["usage"]
        return httpx.Response(200, json=value)

    result = cache_eval.run_suite(
        tmp_path / "run", LLM, rounds=2, transport=httpx.MockTransport(handle)
    )
    assert len(calls) == 1 and result["usage_unknown"]
    assert result["requests"][0]["usage"] is None
    assert sum(r["state"] == "not_run" for r in result["requests"]) == 3


def test_incomplete_reply_keeps_reported_billable_usage_but_is_not_latency_success(tmp_path):
    def handle(request):
        value = json.loads(reply().content)
        value["choices"][0]["finish_reason"] = "length"
        return httpx.Response(200, json=value)

    result = cache_eval.run_suite(
        tmp_path / "run",
        LLM,
        rounds=2,
        prices=(Decimal("0.5"), Decimal("2"), Decimal("8")),
        transport=httpx.MockTransport(handle),
    )
    assert result["known_tokens"] == 103 and not result["usage_unknown"]
    assert result["requests"][0]["state"] == "failed"
    assert Decimal(result["requests"][0]["cost_cny"]) == Decimal("0.000104")
    assert result["groups"]["legacy"]["measurement_samples"] == 0
    assert result["groups"]["legacy"]["elapsed_ms_mean"] is None


def test_actual_usage_above_stop_line_is_preserved_and_prevents_more_calls(tmp_path):
    calls = []

    def handle(request):
        calls.append(request)
        value = answer("ok")
        value["usage"] = {"prompt_tokens": 50000, "completion_tokens": 3}
        return httpx.Response(200, json=value)

    result = cache_eval.run_suite(
        tmp_path / "run", LLM, rounds=2, transport=httpx.MockTransport(handle)
    )
    assert len(calls) == 1 and result["known_tokens"] == 50003
    assert not result["usage_unknown"]
    assert sum(r["state"] == "not_run" for r in result["requests"]) == 3


def test_native_cancel_keeps_original_exception_and_unknown_cost(tmp_path):
    failure = KeyboardInterrupt("private detail")

    def handle(request):
        raise failure

    root = tmp_path / "run"
    with pytest.raises(KeyboardInterrupt) as caught:
        cache_eval.run_suite(root, LLM, rounds=2, transport=httpx.MockTransport(handle))
    assert caught.value is failure
    result = cache_eval.report_suite(root)
    assert result["usage_unknown"] and result["requests"][0]["state"] == "interrupted"
    assert "private detail" not in json.dumps(result)


def test_cancel_record_write_failure_does_not_replace_original_interrupt(tmp_path, monkeypatch):
    failure = KeyboardInterrupt("private detail")
    original = cache_eval.write_record

    def handle(request):
        raise failure

    def write(path, data, **kwargs):
        if data.get("state") == "interrupted":
            raise OSError("synthetic disk failure")
        original(path, data, **kwargs)

    monkeypatch.setattr(cache_eval, "write_record", write)
    root = tmp_path / "run"
    with pytest.raises(KeyboardInterrupt) as caught:
        cache_eval.run_suite(root, LLM, rounds=2, transport=httpx.MockTransport(handle))
    assert caught.value is failure
    result = cache_eval.report_suite(root)
    assert result["usage_unknown"] and result["requests"][0]["state"] == "started"


@pytest.mark.parametrize("boundary", ["start", "finish"])
def test_evidence_write_failure_stops_calls_and_does_not_claim_unsaved_usage(
    tmp_path, monkeypatch, boundary
):
    original = cache_eval.write_record
    calls = []

    def write(path, data, **kwargs):
        # 故障分别发生于请求前开始记录、请求完成后的统计保存；不改变 HTTP 行为。
        if path.name == "01.json" and data["state"] == (
            "started" if boundary == "start" else "succeeded"
        ):
            raise OSError("synthetic disk failure")
        original(path, data, **kwargs)

    def handle(request):
        calls.append(request)
        return reply()

    monkeypatch.setattr(cache_eval, "write_record", write)
    root = tmp_path / "run"
    with pytest.raises(OSError):
        cache_eval.run_suite(root, LLM, rounds=2, transport=httpx.MockTransport(handle))
    assert len(calls) == (0 if boundary == "start" else 1)
    result = cache_eval.report_suite(root)
    assert result["requests"][0]["state"] == ("not_run" if boundary == "start" else "started")
    assert result["known_tokens"] == 0
    assert result["usage_unknown"] is (boundary == "finish")


def test_report_is_read_only_and_never_loads_config_or_creates_http_client(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "run"
    cache_eval.run_suite(root, LLM, rounds=2, transport=httpx.MockTransport(lambda _: reply()))
    before = {path.name: path.read_bytes() for path in root.iterdir()}

    def forbidden(*args, **kwargs):
        pytest.fail("report attempted mutation or network/config access")

    monkeypatch.setattr(cache_eval, "load_llm_config", forbidden)
    monkeypatch.setattr(cache_eval, "write_record", forbidden)
    monkeypatch.setattr(httpx, "Client", forbidden)
    assert cache_eval.main(["report", str(root)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["known_tokens"] == 412
    assert all(row["cost_cny"] is None for row in result["requests"])
    assert before == {path.name: path.read_bytes() for path in root.iterdir()}


@pytest.mark.parametrize("args", [[], ["--allow-paid-api", "--rounds", "9"]])
def test_cli_requires_explicit_payment_and_valid_plan_before_loading_credentials(
    tmp_path, monkeypatch, args
):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid run loaded credentials")

    monkeypatch.setattr(cache_eval, "load_llm_config", forbidden)
    command = ["run", str(tmp_path / "run"), "--llm-config", str(tmp_path / "llm.toml"), *args]
    if not args:
        with pytest.raises(SystemExit) as caught:
            cache_eval.main(command)
        assert caught.value.code == 2
    else:
        assert cache_eval.main(command) == 1
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("damage", ["cache", "elapsed", "source", "partial_json"])
def test_corrupt_sample_is_unknown_and_report_does_not_repair_it(tmp_path, damage):
    root = tmp_path / "run"
    cache_eval.run_suite(root, LLM, rounds=2, transport=httpx.MockTransport(lambda _: reply()))
    path = root / "03.json"
    row = json.loads(path.read_text(encoding="utf-8"))
    if damage == "cache":
        row["cache_usage"]["hit_tokens"] = True
    elif damage == "elapsed":
        row["elapsed_ms"] = -1
    elif damage == "source":
        row["arm"] = "legacy"
    path.write_text("{" if damage == "partial_json" else json.dumps(row), encoding="utf-8")
    before = path.read_bytes()
    result = cache_eval.report_suite(root)
    assert result["requests"][2]["state"] == "invalid"
    assert result["known_tokens"] == 309 and result["usage_unknown"]
    assert path.read_bytes() == before


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "100001", "not-price", True])
def test_price_rejects_unbounded_or_nonfinite_values(value):
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        cache_eval.price(value)
