"""E2.4：受控向量验证缓存、排序和失败；真实 embedding 效果由固定题集另行测量。"""

from dataclasses import replace

import pytest

from adam.documents.embedding import FastEmbedBackend, unit_vector
from adam.documents.models import DocumentError
from adam.documents.search import DocumentSearch
from tests.features.test_document_index import make_index


class FakeEmbedding:
    """只模拟向量生成边界，不把这个替身当成语义模型效果证明。"""

    identity = "fake-model-v1"

    def __init__(self):
        self.inputs = []

    def passages(self, texts):
        self.inputs.extend(texts)
        return [[1.0, 0.0] if "网络" in text else [0.0, 1.0] for text in texts]

    def query(self, text):
        return [1.0, 0.0]


def test_semantic_cache_reuse_update_delete_and_model_change(tmp_path):
    index = make_index(tmp_path)
    index.config = replace(index.config, method="semantic")
    source = tmp_path / "docs" / "network.md"
    source.write_text("网络规则旧版本", encoding="utf-8")
    backend = FakeEmbedding()
    search = DocumentSearch(index, backend)
    first = search.search("断线怎么办", 3)
    assert first.method == "semantic" and len(first.hits) == 1
    assert len(backend.inputs) == 1
    assert search.search("断线怎么办", 3) == first
    assert len(backend.inputs) == 1
    assert DocumentSearch(index, backend).search("断线怎么办", 3) == first
    assert len(backend.inputs) == 1
    source.write_text("网络规则新版本", encoding="utf-8")
    second = search.search("断线怎么办", 3)
    assert second.hits[0].chunk.version != first.hits[0].chunk.version
    assert len(backend.inputs) == 2
    backend.identity = "fake-model-v2"
    search.search("断线怎么办", 3)
    assert len(backend.inputs) == 3
    source.unlink()
    assert search.search("断线怎么办", 3).hits == ()


def test_bad_vector_dimension_never_returns_a_false_match(tmp_path):
    index = make_index(tmp_path)
    index.config = replace(index.config, method="semantic")
    (tmp_path / "docs" / "guide.txt").write_text("网络配置", encoding="utf-8")
    backend = FakeEmbedding()
    backend.query = lambda _: [1.0, 0.0, 0.0]
    with pytest.raises(DocumentError, match="维度"):
        DocumentSearch(index, backend).search("问题", 3)


@pytest.mark.parametrize("value", [[True], [float("nan")], [0.0, 0.0], [], ["a"]])
def test_invalid_embeddings_are_rejected(value):
    with pytest.raises(ValueError):
        unit_vector(value)


def test_model_loader_never_downloads_during_normal_search(tmp_path, monkeypatch):
    options = []

    class Model:
        def __init__(self, **kwargs):
            options.append(kwargs)

    class Module:
        TextEmbedding = Model

    monkeypatch.setattr("adam.documents.embedding.importlib.metadata.version", lambda _: "test")
    monkeypatch.setattr("adam.documents.embedding.importlib.import_module", lambda _: Module)
    FastEmbedBackend(tmp_path, "model").prepare()
    FastEmbedBackend(tmp_path, "model", allow_download=True).prepare()
    assert [value["local_files_only"] for value in options] == [True, False]


def test_missing_package_reports_actionable_error(tmp_path, monkeypatch):
    import importlib.metadata

    def absent(_):
        raise importlib.metadata.PackageNotFoundError("fastembed")

    monkeypatch.setattr("adam.documents.embedding.importlib.metadata.version", absent)
    with pytest.raises(DocumentError, match="可选依赖"):
        FastEmbedBackend(tmp_path, "model").prepare()
