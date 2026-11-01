"""B16.1：真实注册目录的语义候选、权限过滤与缓存失效；确定向量不代表模型效果。"""

from dataclasses import asdict, replace

import pytest

from adam.config.loader import load_config
from adam.config.settings import ToolDiscoveryConfig, ToolLimits
from adam.contracts.errors import ConfigError, ToolError
from adam.contracts.execution import ExecutionMode, ToolCapability
from adam.tool_calls.catalog import schema
from adam.tool_calls.discovery import ToolVisibility
from adam.tool_calls.registry import ToolRegistry
from adam.tool_calls.semantic_discovery import SemanticToolRanker
from tests.test_config import LLM


class Vectors:
    """用固定向量验证排序数据流，记录实际编码输入；不替代真实语义质量对照。"""

    def __init__(self):
        self.identity = "fixture-model-v1"
        self.batches, self.queries = [], []
        self.fault = None

    def passages(self, texts):
        self.batches.append(list(texts))
        values = [[1.0, 0.0] if "read_file" in text else [0.0, 1.0] for text in texts]
        if self.fault == "partial":
            return values[:-1]
        if self.fault == "nan":
            return [[float("nan"), 1.0]] * len(texts)
        return values

    def query(self, text):
        self.queries.append(text)
        if self.fault == "error":
            raise RuntimeError("private backend error")
        if self.fault == "cancel":
            raise KeyboardInterrupt
        if self.fault == "dimension":
            return [1.0, 0.0, 0.0]
        if self.fault == "identity":
            self.identity = "changed-during-encoding"
        return [-1.0, 0.0] if text == "unrelated" else [1.0, 0.0]


def directory(*, detail="read a file", extra=False):
    specs = {
        "read_file": schema(detail, {"path": ("string", "SCHEMA_NOT_FOR_ENCODER")}, ["path"]),
        "git_log": schema("inspect history", {}, []),
        "replace_text": schema("write contents", {}, []),
    }
    if extra:
        specs["read_file"]["parameters"]["properties"]["offset"] = {"type": "integer"}
    return ToolRegistry(
        specs,
        {name: lambda *_: pytest.fail("ranking executed a tool") for name in specs},
        ToolLimits(),
        capabilities={
            "read_file": ToolCapability.READ_ONLY,
            "git_log": ToolCapability.READ_ONLY,
            "replace_text": ToolCapability.EDIT_FILE,
        },
        groups={"read_file": "files", "git_log": "git", "replace_text": "files"},
    ).directory()


def semantic(backend=None, **options):
    return SemanticToolRanker(ToolDiscoveryConfig(ranking="semantic", **options), backend)


def test_semantic_candidates_do_not_select_schemas_or_execute_and_filter_before_encoding():
    catalog, backend = directory(), Vectors()
    selection = ToolVisibility(catalog, ExecutionMode.PLAN)
    result = semantic(backend).rank(catalog, ExecutionMode.PLAN, "查看内容")
    assert [entry.name for entry in result.entries] == ["read_file"]
    assert result.strategy == "semantic_v1" and result.scores == (("read_file", 1.0),)
    assert result.directory_version == catalog.version and len(catalog.version) == 64
    assert selection.snapshot().names == ()
    assert all(
        "replace_text" not in text and "SCHEMA_NOT_FOR_ENCODER" not in text
        for text in backend.batches[0]
    )
    with pytest.raises(ToolError, match="尚未提供"):
        selection.snapshot().require("read_file")


def test_exact_name_empty_query_and_group_remain_usable_without_semantic_authority():
    catalog, backend = directory(), Vectors()
    ranker = semantic(backend)
    assert ranker.rank(catalog, ExecutionMode.PLAN, "git_log").entries[0].name == "git_log"
    assert [e.name for e in ranker.rank(catalog, ExecutionMode.PLAN, "").entries] == [
        "read_file",
        "git_log",
    ]
    group = ranker.rank(catalog, ExecutionMode.PLAN, "", group="files")
    assert [e.name for e in group.entries] == ["read_file"] and len(backend.queries) == 1
    unrelated = ranker.rank(catalog, ExecutionMode.PLAN, "unrelated")
    assert unrelated.entries == () and unrelated.strategy == "semantic_v1"
    assert unrelated.fallback_reason is None


def test_cache_reuses_current_catalog_but_not_changed_schema_model_mode_or_group():
    catalog, backend = directory(), Vectors()
    ranker = semantic(backend)
    ranker.rank(catalog, ExecutionMode.PLAN, "查看")
    ranker.rank(catalog, ExecutionMode.PLAN, "读取")
    assert len(backend.batches) == 1 and len(backend.queries) == 2
    changed = directory(extra=True)
    assert changed.version != catalog.version
    ranker.rank(changed, ExecutionMode.PLAN, "查看")
    assert len(backend.batches) == 2
    backend.identity = "fixture-model-v2"
    ranker.rank(changed, ExecutionMode.PLAN, "查看")
    ranker.rank(changed, ExecutionMode.ACT, "查看")
    assert len(backend.batches) == 4 and "replace_text" in " ".join(backend.batches[-1])
    ranker.rank(changed, ExecutionMode.PLAN, "查看", group="files")
    assert len(backend.batches) == 5 and len(backend.batches[-1]) == 1
    renamed = directory(detail="changed description")
    assert renamed.version != catalog.version and directory().version == catalog.version


@pytest.mark.parametrize("fault", ["partial", "nan", "dimension", "identity", "error"])
def test_bad_batch_falls_back_without_leaking_backend_details_or_caching_partial(fault):
    catalog, backend = directory(), Vectors()
    ranker = semantic(backend)
    backend.fault = fault
    failed = ranker.rank(catalog, ExecutionMode.PLAN, "read_file")
    assert (
        failed.strategy == "keyword_fallback_v1"
        and failed.fallback_reason == "embedding_unavailable"
    )
    assert [e.name for e in failed.entries] == ["read_file"]
    assert failed.embedding is None and failed.scores == ()
    assert "private backend" not in repr(asdict(failed))
    backend.fault = None
    recovered = ranker.rank(catalog, ExecutionMode.PLAN, "查看")
    assert recovered.strategy == "semantic_v1" and len(backend.batches) == 2


def test_cancel_is_not_a_keyword_success_and_disabled_or_oversized_index_never_encodes():
    catalog, backend = directory(), Vectors()
    keyword = SemanticToolRanker(ToolDiscoveryConfig(), backend)
    assert keyword.rank(catalog, ExecutionMode.PLAN, "read_file").strategy == "keyword_v1"
    limited = semantic(backend, max_index_tools=1).rank(catalog, ExecutionMode.PLAN, "read")
    assert limited.fallback_reason == "candidate_limit" and limited.entries[0].name == "read_file"
    assert backend.batches == [] and backend.queries == []
    missing = semantic().rank(catalog, ExecutionMode.PLAN, "read")
    assert missing.fallback_reason == "embedding_unavailable"
    backend.fault = "cancel"
    with pytest.raises(KeyboardInterrupt):
        semantic(backend).rank(catalog, ExecutionMode.PLAN, "read")


@pytest.mark.parametrize(
    "kwargs", [{"query": "x" * 201}, {"query": None}, {"query": "read", "group": "hidden"}]
)
def test_invalid_query_does_not_enter_encoder(kwargs):
    backend = Vectors()
    with pytest.raises(ToolError):
        semantic(backend).rank(directory(), ExecutionMode.PLAN, **kwargs)
    assert not backend.batches and not backend.queries


@pytest.mark.parametrize(
    "options",
    [
        {"ranking": "remote"},
        {"max_index_tools": True},
        {"max_index_tools": 0},
        {"max_index_tools": 1025},
        {"semantic_min_score": float("nan")},
        {"semantic_min_score": True},
        {"semantic_min_score": 1.1},
        {"embedding_model": ""},
        {"embedding_model": "model\n"},
    ],
    ids=[
        "strategy",
        "bool_count",
        "zero",
        "large_count",
        "nan",
        "bool_score",
        "large_score",
        "empty_model",
        "newline_model",
    ],
)
def test_config_rejects_unbounded_or_ambiguous_semantic_options(options):
    with pytest.raises(ConfigError):
        ToolDiscoveryConfig(**options)


def test_toml_loads_ranking_separately_from_schema_loading_without_model_io(tmp_path):
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    path = tmp_path / "config.toml"
    path.write_text(
        '[tool_discovery]\nmode="full"\nranking="semantic"\nmax_index_tools=20\nsemantic_min_score=0.7\n',
        encoding="utf-8",
    )
    config = load_config(path).tool_discovery
    assert config.mode == "full" and config.ranking == "semantic" and config.max_index_tools == 20
    assert (
        config.semantic_min_score == 0.7 and replace(config, mode="on_demand").ranking == "semantic"
    )
