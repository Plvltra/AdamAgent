"""B12.2.3：真实工具/CLI、来源与知识目录；仅替代远端模型和终端输入。"""

import copy
import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import httpx
import pytest

from adam.config.loader import load_config
from adam.config.settings import DocumentConfig, PermissionConfig, SkillsConfig, ToolLimits
from adam.contracts.errors import ConfigError, ToolError
from adam.contracts.execution import ExecutionMode, ToolCapability
from adam.documents.knowledge_access import KnowledgeAccess
from adam.documents.knowledge_lifecycle import KnowledgeLifecycle
from adam.documents.knowledge_proposals import KnowledgeProposals
from adam.documents.models import DocumentError
from adam.knowledge import main
from adam.recording.runtime import replay_turn
from adam.tool_calls.document_tools import register_documents
from adam.tool_calls.factory import create_tool_registry
from tests.features.test_context_prefix_evaluation import response_for
from tests.features.test_knowledge_candidates import proposal_fixture
from tests.features.test_knowledge_publication import Reviewer
from tests.features.test_knowledge_review import opinion_for
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import application, call, outputs
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app, forbid_external_actions
from tests.test_config import LLM


def submission(proposal):
    value = copy.deepcopy(proposal)
    for role in ("support", "counterevidence"):
        value[role] = [{"chunk_id": c["chunk_id"], "quote": c["quote"]} for c in value[role]]
    return value


@pytest.fixture
def knowledge(tmp_path):
    index, proposal = proposal_fixture(tmp_path)
    proposal["valid_from"] = None
    store = KnowledgeProposals(tmp_path / "knowledge", index)
    return KnowledgeAccess(KnowledgeLifecycle(store)), submission(proposal)


def adopt(access, value):
    identity = access.propose(value)["candidate_id"]
    access.lifecycle.review(identity, Reviewer())
    access.lifecycle.publish(identity, lambda view: True)
    return identity


def test_short_references_become_actual_positions_and_only_approved_facts_are_returned(knowledge):
    access, value = knowledge
    submitted = access.propose(value)
    identity = submitted["candidate_id"]
    assert submitted["status"] == "pending" and access.query()["items"] == []
    summary = access.lifecycle.candidates()
    assert len(summary) == 1 and summary[0]["status"] == "pending"
    assert summary[0]["source_status"] == "not_checked"
    frozen = access.lifecycle.inspect(identity)["candidate"]
    for role in ("support", "counterevidence"):
        citation = frozen["proposal"][role][0]
        assert citation["quote"] in Path(citation["path"]).read_text(encoding="utf-8")
        assert citation["end"] - citation["start"] == len(citation["quote"])
    access.lifecycle.review(identity, Reviewer())
    assert access.query()["items"] == []
    access.lifecycle.publish(identity, lambda view: True)
    result = access.query("重试")
    assert result["items"][0]["candidate_id"] == identity
    assert result["items"][0]["counterevidence"] == frozen["proposal"]["counterevidence"]
    assert result["source_status"] == "current_snapshot"
    assert result["semantic_status"] == "not_verified"
    assert access.query("ZXYunknown")["status"] == "no_match"


@pytest.mark.parametrize(
    "fault", ["approved", "path", "missing_counter", "bad_quote", "stale", "ambiguous"]
)
def test_proposal_rejects_forgery_or_unlocatable_sources_without_creating_candidate(
    knowledge, tmp_path, fault
):
    access, value = knowledge
    if fault == "approved":
        value["approved"] = True
    elif fault == "path":
        value["support"][0]["path"] = "C:/secret"
    elif fault == "missing_counter":
        del value["counterevidence"]
    elif fault == "bad_quote":
        value["support"][0]["quote"] = "伪造原文"
    elif fault == "stale":
        (tmp_path / "docs/new.md").write_text("规则更新了。", encoding="utf-8")
    else:
        (tmp_path / "docs/new.md").write_text("retry retry", encoding="utf-8")
        corpus = access.lifecycle.proposals.index.refresh()
        chunk = next(c for c in corpus.chunks if c.text == "retry retry")
        value["source_generation"] = corpus.generation
        value["support"] = [{"chunk_id": chunk.chunk_id, "quote": "retry"}]
    with pytest.raises(DocumentError):
        access.propose(value)
    assert not list(access.lifecycle.proposals.directory.glob("*.json"))


@pytest.mark.parametrize("change", ["withdraw", "source", "query"])
def test_published_paging_rejects_changed_decisions_sources_or_query(knowledge, tmp_path, change):
    access, value = knowledge
    identities = [adopt(access, value), adopt(access, value)]
    first = access.query(limit=1)
    assert first["next_offset"] == 1
    second = access.query(offset=1, limit=1, version=first["version"])
    assert {first["items"][0]["candidate_id"], second["items"][0]["candidate_id"]} == set(
        identities
    )
    if change == "withdraw":
        access.lifecycle.withdraw(identities[0], "不再采用")
    elif change == "source":
        (tmp_path / "docs/new.md").write_text("来源已变化。", encoding="utf-8")
    with pytest.raises(DocumentError, match="发生变化"):
        access.query("different" if change == "query" else "", offset=1, version=first["version"])


def test_output_limit_preserves_whole_evidence_and_continuation_position(knowledge):
    access, value = knowledge
    adopt(access, value)
    result = KnowledgeAccess(access.lifecycle, max_chars=800).query()
    assert result["status"] == "output_limit" and result["items"] == []
    assert result["next_offset"] == 0 and result["total"] == 1
    assert len(json.dumps(result, ensure_ascii=False)) <= 800
    full = access.query(version=result["version"])
    assert full["items"][0]["support"] and full["items"][0]["counterevidence"]
    with pytest.raises(DocumentError, match="元数据"):
        KnowledgeAccess(access.lifecycle, max_chars=512).query("x" * 1000)


def test_registered_tools_are_lazy_and_children_only_query_published_facts(tmp_path):
    def registry(child):
        return register_documents(
            create_tool_registry(tmp_path, limits=ToolLimits()),
            tmp_path,
            DocumentConfig(roots=(Path("missing"),)),
            tmp_path / "cache",
            PermissionConfig(),
            knowledge_dir=tmp_path / "knowledge",
            allow_knowledge_proposals=not child,
        ).with_mode(lambda: ExecutionMode.PLAN)

    parent, child = registry(False), registry(True)
    assert parent.capability("propose_knowledge") is ToolCapability.SESSION_STATE
    assert child.capability("propose_knowledge") is ToolCapability.UNKNOWN
    assert child.capability("query_published_knowledge") is ToolCapability.READ_ONLY
    assert not child.can_parallelize("query_published_knowledge")
    assert not (tmp_path / "knowledge").exists() and not (tmp_path / "cache").exists()
    failed = json.loads(parent.execute("propose_knowledge", '{"approved":true}'))
    assert not failed["ok"] and not (tmp_path / "knowledge").exists()
    disabled = register_documents(
        create_tool_registry(tmp_path),
        tmp_path,
        DocumentConfig(),
        tmp_path / "cache",
        PermissionConfig(),
        knowledge_dir=tmp_path / "knowledge",
    )
    assert disabled.capability("query_published_knowledge") is ToolCapability.UNKNOWN


def test_model_submits_pending_then_queries_and_replay_does_not_repeat_storage(
    knowledge, tmp_path, monkeypatch
):
    access, value = knowledge
    config = replace(
        config_at(tmp_path),
        documents=DocumentConfig(roots=(Path("docs"),)),
        documents_dir=tmp_path / "cache",
        knowledge_dir=tmp_path / "knowledge",
    )

    def respond(body, number):
        if number == 1:
            names = {t["function"]["name"] for t in body["tools"]}
            assert {"propose_knowledge", "query_published_knowledge"} <= names
            assert not {"publish_knowledge", "review_knowledge", "approve_knowledge"} & names
            result = tool_response([call("propose", "propose_knowledge", **value)])
        elif number == 2:
            assert outputs(body)[-1]["status"] == "pending"
            result = tool_response([call("query", "query_published_knowledge")])
        else:
            assert outputs(body)[-1]["items"] == []
            result = answer("候选已保存，等待独立审查和确认。")
        return httpx.Response(200, json=result)

    with captured_app(tmp_path, respond, config=config) as (app, recorder, store, path, bodies):
        assert recorder.run(lambda: app.send("task")) == "候选已保存，等待独立审查和确认。"
        assert len(bodies) == 3
    before = {p: p.read_bytes() for p in access.lifecycle.proposals.directory.glob("*.json")}
    assert len(before) == 1
    forbid_external_actions(monkeypatch)
    monkeypatch.setattr(
        KnowledgeAccess, "propose", lambda *_: pytest.fail("must not repeat proposal")
    )
    monkeypatch.setattr(
        KnowledgeLifecycle, "current", lambda *_: pytest.fail("must not reread facts")
    )
    assert replay_turn(path, tmp_path / "replay")["status"] == "matched"
    assert {p: p.read_bytes() for p in before} == before


def test_main_and_real_child_cannot_read_raw_records_but_can_query_published_facts(
    knowledge, tmp_path
):
    access, value = knowledge
    identity = adopt(access, value)
    record = access.lifecycle.proposals.directory / (identity + ".json")
    (tmp_path / "knowledge/AGENTS.md").write_text("PROTECTED_RULE_MUST_NOT_LOAD", encoding="utf-8")
    config = replace(
        config_at(tmp_path),
        documents=DocumentConfig(roots=(Path("docs"),)),
        documents_dir=tmp_path / "cache",
        knowledge_dir=tmp_path / "knowledge",
    )
    parent_calls, child_calls = [], []

    def parent(request):
        body = json.loads(request.content)
        assert "PROTECTED_RULE_MUST_NOT_LOAD" not in json.dumps(body)
        parent_calls.append(body)
        if len(parent_calls) == 1:
            result = tool_response(
                [
                    call("raw", "read_file", path=str(record)),
                    call(
                        "edit",
                        "replace_text",
                        path=str(record),
                        old_text="pending",
                        new_text="published",
                        expected_version=sha256(record.read_bytes()).hexdigest(),
                    ),
                    call(
                        "review",
                        "request_review",
                        goal="inspect record",
                        criteria="read evidence",
                        paths=[str(record)],
                    ),
                ]
            )
        elif len(parent_calls) == 2:
            assert len(outputs(body)) == 3
            assert all(not item["ok"] and "保留目录" in item["error"] for item in outputs(body))
            result = tool_response(
                [call("spawn", "spawn_agent", name="reader", task="查询已采用知识")]
            )
        elif len(parent_calls) == 3:
            result = tool_response([call("wait", "wait_agents", agent_ids=[])])
        else:
            assert outputs(body)[-1]["agents"][0]["status"] == "completed"
            result = answer("已读取经确认的知识，原始记录不可直接访问。")
        return httpx.Response(200, json=result)

    def child(request):
        body = json.loads(request.content)
        child_calls.append(body)
        if len(child_calls) == 1:
            names = {t["function"]["name"] for t in body["tools"]}
            assert "propose_knowledge" not in names
            result = tool_response(
                [
                    call("raw", "read_file", path=str(record)),
                    call("facts", "query_published_knowledge", query="重试"),
                ]
            )
        else:
            found = outputs(body)
            assert not found[-2]["ok"]
            assert found[-1]["items"][0]["candidate_id"] == identity
            result = answer("仅使用了已发布知识。")
        return httpx.Response(200, json=result)

    before = record.read_bytes()
    with application(tmp_path, parent, child, config=config) as (app, _):
        assert app.send("协助查询知识") == "已读取经确认的知识，原始记录不可直接访问。"
    assert len(child_calls) == 2 and record.read_bytes() == before


@pytest.mark.parametrize("entry", ["documents", "skills"])
def test_other_material_loaders_cannot_use_knowledge_directory_as_a_source(tmp_path, entry):
    reserved = tmp_path / "knowledge"
    reserved.mkdir()
    (reserved / "private.md").write_text("PRIVATE_KNOWLEDGE_RECORD", encoding="utf-8")
    if entry == "documents":
        registry = register_documents(
            create_tool_registry(tmp_path),
            tmp_path,
            DocumentConfig(roots=(reserved,)),
            tmp_path / "cache",
            PermissionConfig(),
            knowledge_dir=reserved,
        )
        result = json.loads(registry.execute("search_documents", '{"query":"PRIVATE"}'))
        assert not result["ok"] and "保留目录" in result["error"]
    else:
        config = replace(
            config_at(tmp_path), knowledge_dir=reserved, skills=SkillsConfig(user_dir=reserved)
        )
        with (
            pytest.raises(ToolError, match="保留目录"),
            application(
                tmp_path,
                lambda _: pytest.fail("must not request"),
                lambda _: pytest.fail("must not spawn"),
                config=config,
            ),
        ):
            pytest.fail("reserved Skill directory must not load")


def cli_config(root):
    (root / "config.toml").write_text(
        '[storage]\ndocuments_dir="cache"\nknowledge_dir="knowledge"\n[documents]\nroots=["docs"]\n',
        encoding="utf-8",
    )
    (root / "llm.toml").write_text(LLM, encoding="utf-8")
    return ["--config", str(root / "config.toml"), "--workspace", str(root)]


def test_cli_propose_review_confirm_query_and_withdraw_use_same_records(
    knowledge, tmp_path, monkeypatch, capsys
):
    access, value = knowledge
    options = cli_config(tmp_path)
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(json.dumps(value), encoding="utf-8")
    calls = []
    original_client = httpx.Client

    def handle(request):
        calls.append(request)
        return response_for(request, content=json.dumps(opinion_for(request)))

    monkeypatch.setattr(
        "adam.knowledge.httpx.Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    assert main([*options, "propose", str(proposal_path)]) == 0
    identity = json.loads(capsys.readouterr().out)["candidate_id"]
    assert main([*options, "list"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["status"] == "pending"
    assert main([*options, "publish", identity]) == 1
    assert "尚未通过" in capsys.readouterr().out and not calls
    assert main([*options, "review", identity]) == 0
    output = capsys.readouterr().out
    assert "单独计费" in output and '"usage"' in output and len(calls) == 1
    assert "secret" not in output
    confirmations = []
    monkeypatch.setattr("builtins.input", lambda prompt: confirmations.append(prompt) or "yes")
    assert main([*options, "publish", identity]) == 0
    output = capsys.readouterr().out
    assert "counterevidence" in output and "reviewed" in output and "published" in output
    assert len(confirmations) == 1
    assert main([*options, "current", "重试"]) == 0
    assert json.loads(capsys.readouterr().out)["items"][0]["candidate_id"] == identity
    assert main([*options, "withdraw", identity, "用户撤回"]) == 0
    capsys.readouterr()
    assert main([*options, "show", identity]) == 0
    assert json.loads(capsys.readouterr().out)["state"]["status"] == "withdrawn"
    assert main([*options, "history", identity]) == 0
    assert [e["status"] for e in json.loads(capsys.readouterr().out)] == [
        "reviewed",
        "published",
        "withdrawn",
    ]
    assert len(calls) == 1 and access.query()["items"] == []


@pytest.mark.parametrize("input_mode", ["no", "eof"])
def test_cli_denial_or_missing_terminal_input_never_publishes(
    knowledge, tmp_path, monkeypatch, input_mode
):
    access, value = knowledge
    options = cli_config(tmp_path)
    identity = access.propose(value)["candidate_id"]
    access.lifecycle.review(identity, Reviewer())

    def deny(prompt):
        if input_mode == "eof":
            raise EOFError()
        return "no"

    monkeypatch.setattr("builtins.input", deny)
    monkeypatch.setattr(
        "adam.knowledge.httpx.Client", lambda **kwargs: pytest.fail("no HTTP expected")
    )
    assert main([*options, "publish", identity]) == 0
    assert access.lifecycle.inspect(identity)["state"]["status"] == "rejected"


@pytest.mark.parametrize("invalid", ['""', '"\\u0000"', "1", "[]"])
def test_knowledge_storage_config_defaults_relative_paths_and_invalid_values(tmp_path, invalid):
    path = tmp_path / "config.toml"
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    path.write_text("", encoding="utf-8")
    assert load_config(path).knowledge_dir == tmp_path / "knowledge"
    path.write_text('[storage]\nknowledge_dir="persistent/knowledge"', encoding="utf-8")
    assert load_config(path).knowledge_dir == tmp_path / "persistent/knowledge"
    path.write_text("[storage]\nknowledge_dir=" + invalid, encoding="utf-8")
    with pytest.raises(ConfigError, match="knowledge_dir"):
        load_config(path)
