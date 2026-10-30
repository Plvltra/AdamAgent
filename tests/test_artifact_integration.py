"""T8.2 配置与宿主日志契约；业务执行与磁盘故障在 features 中验证。"""

import json
from dataclasses import asdict, replace

import pytest

from adam.config.loader import load_config
from adam.config.settings import ArtifactConfig
from adam.contracts.artifacts import ArtifactRef, ArtifactSource
from adam.contracts.errors import ConfigError
from adam.session.log import decode_records, parse_log
from adam.session.storage import record
from tests.test_config import LLM
from tests.test_hook_log import started_tool


@pytest.mark.parametrize(
    "content",
    [
        "enabled=1",
        "max_bytes=0",
        "max_bytes=true",
        "max_bytes=100000001",
        "max_session_bytes=-1",
        "max_items=4097",
        "max_read_chars=1000001",
        "unknown=true",
    ],
)
def test_invalid_artifact_config_is_rejected(tmp_path, content):
    main = tmp_path / "config.toml"
    main.write_text("[artifacts]\n" + content, encoding="utf-8")
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(main)


def test_artifact_toml_and_direct_configuration_agree(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "[artifacts]\nenabled=false\nmax_bytes=1234\nmax_read_chars=77\n", encoding="utf-8"
    )
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    assert load_config(path).artifacts == ArtifactConfig(
        enabled=False, max_bytes=1234, max_read_chars=77
    )


@pytest.mark.parametrize(
    "change",
    [
        {"session_id": "f" * 8},
        {"turn_id": "f" * 32},
        {"request_id": "f" * 32},
        {"tool_call_id": "absent"},
        {"tool_name": "run_command"},
    ],
)
def test_artifact_log_rejects_forged_source(tmp_path, change):
    with started_tool(tmp_path) as (store, sid, link, _):
        reference = ArtifactRef(
            "a" * 32, ArtifactSource(sid, tool_name="read_file", **link), "b" * 64, 3, 3, 3, True
        )
        forged = replace(reference, source=replace(reference.source, **change))
        raw = b"".join(record(item["type"], item["payload"]) for item in store.load_events(sid))
        log = parse_log(raw, sid)
        with pytest.raises(ValueError):
            log.extend(decode_records(record("artifact_saved", asdict(forged))))
        assert log.artifacts == {}


def test_attachment_source_can_only_be_recorded_once_before_tool_finishes(tmp_path):
    with started_tool(tmp_path) as (store, sid, link, _):
        reference = ArtifactRef(
            "a" * 32, ArtifactSource(sid, tool_name="read_file", **link), "b" * 64, 3, 3, 3, True
        )
        store.append_artifact(reference)
        before = store.load_events(sid)
        for duplicate in (reference, replace(reference, artifact_id="c" * 32)):
            with pytest.raises(ValueError):
                store.append_artifact(duplicate)
            assert store.load_events(sid) == before
        store.append_tool_event(sid, "tool_finished", {**link, "output": json.dumps({"ok": True})})
        with pytest.raises(ValueError):
            store.append_artifact(replace(reference, artifact_id="d" * 32))
        assert store.load_artifact(sid, reference.artifact_id) == reference
