"""B01.2 配置、输出额度和录制快照的适配边界。"""

import json
from dataclasses import asdict

import pytest

from adam.config.loader import load_config
from adam.config.settings import NoProgressConfig
from adam.contracts.errors import ConfigError
from adam.recording.snapshot import capture_seed, validate_seed
from adam.session.storage import SessionStore
from adam.tool_calls.progress import NoProgressGuard, ProgressAction
from adam.tool_calls.progress_output import encode_progress
from tests.features.test_memory_context import config_at
from tests.test_config import LLM


@pytest.mark.parametrize(
    "content,expected",
    [
        ("", NoProgressConfig()),
        ("[no_progress]\nenabled=false", NoProgressConfig(enabled=False)),
        ("[no_progress]\nwarn_after=3\nstop_after=5", NoProgressConfig(warn_after=3, stop_after=5)),
    ],
)
def test_toml_loads_explicit_or_default_policy(tmp_path, content, expected):
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")
    assert load_config(path).no_progress == expected


@pytest.mark.parametrize(
    "content",
    [
        "no_progress=[]",
        "[no_progress]\nunknown=1",
        "[no_progress]\nenabled=1",
        "[no_progress]\nwarn_after=true",
        "[no_progress]\nstop_after=2",
    ],
)
def test_invalid_toml_policy_is_rejected_before_startup(tmp_path, content):
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_feedback_retains_exit_code_reference_and_warning_with_minimum_output_budget():
    guard = NoProgressGuard()
    failed = {"ok": True, "status": "completed", "exit_code": 7}
    guard.observe("run_command", "{}", failed)
    decision = guard.observe("run_command", "{}", failed)
    artifact = {
        "artifact_id": "a" * 32,
        "version": "b" * 64,
        "saved_chars": 999999,
        "complete": False,
        "format": "json",
    }
    output = json.dumps({**failed, "artifact": artifact, "stderr": '中文"\\' * 2000})
    encoded = encode_progress(output, decision, 512)
    result = json.loads(encoded)
    assert len(encoded) <= 512 and result["truncated"]
    assert result["ok"] and result["exit_code"] == 7 and result["status"] == "completed"
    assert result["artifact"] == artifact
    assert result["no_progress"]["action"] == "warn" and "换策略" in result["no_progress"]["hint"]


def test_unbounded_extension_status_is_hashed_and_unknown_state_is_not_retry_failure():
    guard = NoProgressGuard()
    decision = guard.observe("extension", "{}", {"ok": False, "status": "x" * 100000})
    assert len(decision.category) < 100 and len(decision.fingerprint) == 64
    assert (
        guard.observe("extension", "{}", {"ok": False, "status": "unknown"}).consecutive_failures
        == 0
    )
    assert guard.decision.action is ProgressAction.CONTINUE


def test_recording_captures_policy_and_accepts_old_snapshot_without_reinventing_policy(tmp_path):
    config = config_at(tmp_path)
    with SessionStore(config.sessions_dir) as store:
        sid = store.create_session("snapshot", "")
        store.select_session(sid)
        seed = capture_seed(config, store, sid, "task", tmp_path)
    assert seed["controls"]["no_progress"] == asdict(config.no_progress)
    seed["controls"].pop("no_progress")
    validate_seed(seed)
    assert "no_progress" not in seed["controls"]
    seed["controls"]["no_progress"] = {"enabled": True, "warn_after": 3, "stop_after": 2}
    with pytest.raises(ValueError):
        validate_seed(seed)
