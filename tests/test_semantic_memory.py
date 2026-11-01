"""语义记忆配置与持久化证据的边界，不把非法向量来源当成有效检索。"""

from copy import deepcopy

import pytest

from adam.config.loader import load_config
from adam.config.settings import MemoryConfig
from adam.contracts.errors import ConfigError
from adam.contracts.memory import MemoryCommand, validate_selection
from adam.recording.environment import decode_memory
from adam.recording.snapshot import capture_seed, validate_seed
from adam.session.storage import SessionStore
from tests.features.test_memory_context import config_at
from tests.features.test_semantic_memory import LocalVectors, service_at
from tests.test_config import LLM


@pytest.mark.parametrize(
    "values",
    [
        {"method": "remote"},
        {"method": []},
        {"embedding_model": ""},
        {"semantic_min_score": True},
        {"semantic_min_score": float("nan")},
        {"semantic_min_score": 1.1},
        {"max_semantic_records": True},
        {"max_semantic_records": 0},
        {"max_semantic_records": 1025},
    ],
)
def test_semantic_config_rejects_invalid_strategy_model_and_limits(values):
    with pytest.raises(ConfigError):
        MemoryConfig(**values)


def test_toml_semantic_settings_and_legacy_recording_controls(tmp_path):
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    path = tmp_path / "config.toml"
    path.write_text(
        '[memory]\nmethod="semantic"\nsemantic_min_score=0.7\nmax_semantic_records=24\n',
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.memory.method == "semantic" and config.memory.max_semantic_records == 24
    assert config.memory.semantic_min_score == 0.7
    with SessionStore(tmp_path / "sessions") as store:
        sid = store.create_session("test", "system")
        store.select_session(sid)
        seed = capture_seed(config_at(tmp_path), store, sid, "task", tmp_path)
    legacy = deepcopy(seed)
    legacy["controls"]["memory"] = {
        key: value
        for key, value in seed["controls"]["memory"].items()
        if key in {"enabled", "max_entries", "max_input_tokens"}
    }
    validate_seed(legacy)
    seed["controls"]["memory"]["embedding_model"] = {"unexpected": "secret"}
    with pytest.raises(ValueError):
        validate_seed(seed)


@pytest.mark.parametrize(
    "fault", ["model", "score", "boolean", "id", "missing", "fallback", "extra"]
)
def test_bad_semantic_provenance_cannot_be_loaded(tmp_path, fault):
    service, _ = service_at(tmp_path, LocalVectors())
    service.manage(MemoryCommand("add", "单元测试"))
    selected = service.select("怎样检验？", 2000)
    value = selected.to_dict()
    assert decode_memory(value) == selected
    detail = value["retrieval"]
    if fault == "model":
        detail["embedding"] = ""
    elif fault == "score":
        detail["scores"][0][1] = float("nan")
    elif fault == "boolean":
        detail["scores"][0][1] = True
    elif fault == "id":
        detail["scores"][0][0] = "other"
    elif fault == "missing":
        value.pop("retrieval")
    elif fault == "fallback":
        detail["fallback_reason"] = "embedding_unavailable"
    else:
        detail["arbitrary"] = "not allowed"
    with pytest.raises(ValueError):
        validate_selection(value)
