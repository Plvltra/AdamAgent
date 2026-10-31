"""T10.2：真实源码经只读工具查询，模拟模型 HTTP 验证来源与实际 JSONL。"""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from adam.code_index.index import CodeIndex
from adam.code_index.models import CodeIndexError, CodeLimits
from adam.code_index.query import ReferenceIndex
from adam.config.settings import PermissionConfig, ToolLimits
from adam.contracts.execution import ExecutionMode, ToolCapability
from adam.session.storage import SessionStore
from adam.tool_calls.factory import create_tool_registry
from adam.tool_calls.policy import PathPolicy
from tests.features.test_tool_loop import answer, make_conversation, tool_response


def write_sources(root, sources):
    for name, text in sources.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())
    return CodeIndex(root, PathPolicy(root, PermissionConfig()).resolve).refresh()


def query(registry, tool, **args):
    return json.loads(registry.execute(tool, json.dumps({"path": ".", **args})))


def test_import_aliases_relative_reexports_and_source_versions(tmp_path):
    snapshot = write_sources(
        tmp_path,
        {
            "pkg/core.py": "def run():\n    return 1\n",
            "pkg/__init__.py": "from .core import run as exported\n",
            "main.py": "import pkg.core as api\nfrom pkg import exported as go\nimport pkg.core\n"
            "api.run()\ngo()\npkg.core.run()\n",
            "pkg/client.py": "from . import core\nfrom .core import run\ncore.run()\nrun()\n",
        },
    )
    target = snapshot.find_definitions("run")[0]
    references = list(ReferenceIndex(snapshot, lambda: None).references())
    found = [item for item in references if item.definition_id == target.identifier]
    assert sorted(item.expression for item in found) == [
        "api.run",
        "core.run",
        "go",
        "pkg.core.run",
        "run",
    ]
    assert {item.start_line for item in found} == {3, 4, 5, 6}
    for item in found:
        assert item.version == hashlib.sha256(Path(item.path).read_bytes()).hexdigest()
        assert item.reason == "static_binding"


@pytest.mark.parametrize(
    "tail,expected",
    [
        ("def use(target):\n    target()\n", "dynamic_binding"),
        ("def use():\n    target()\n    target = 1\n", "dynamic_binding"),
        ("def use():\n    global target\n    target()\n", "dynamic_binding"),
        ("def use():\n    target()\n", "static_binding"),
        (
            "class C:\n    def target(self): pass\n    def use(self):\n        target()\n",
            "static_binding",
        ),
        ("class C:\n    target()\n", "class_namespace"),
        ("def use():\n    target.attr()\n", "dynamic_attribute"),
        ("f = lambda target: target()\n", "unsupported_scope"),
        ("values = [target() for target in ()]\n", "unsupported_scope"),
        ("if True:\n    def target(): pass\ntarget()\n", "dynamic_binding"),
        ("try:\n    1 / 0\nexcept Exception as target:\n    target()\n", "dynamic_binding"),
        ("match 1:\n    case target:\n        target()\n", "dynamic_binding"),
        ("from somewhere import *\ntarget()\n", "dynamic_binding"),
        ("def target(): pass\ntarget()\n", "ambiguous_binding"),
        ("target = 3\ntarget()\n", "dynamic_binding"),
    ],
)
def test_shadowing_and_dynamic_cases_are_not_guessed(tmp_path, tail, expected):
    snapshot = write_sources(tmp_path, {"app.py": "def target(): pass\n" + tail})
    references = list(ReferenceIndex(snapshot, lambda: None).references())
    match = [item for item in references if item.expression.startswith("target")]
    assert len(match) == 1
    assert match[0].reason == expected
    if expected == "static_binding":
        assert match[0].definition_id == snapshot.find_definitions("target")[0].identifier
    else:
        assert match[0].definition_id is None


def test_defaults_decorators_bases_and_nested_closures_use_correct_scope(tmp_path):
    snapshot = write_sources(
        tmp_path,
        {
            "app.py": "def factory(): pass\n"
            "@factory\ndef outer(value=factory()):\n"
            "    def local(): pass\n"
            "    class Inner(factory):\n"
            "        def use(self):\n"
            "            local()\n"
            "    return local\n"
        },
    )
    references = list(ReferenceIndex(snapshot, lambda: None).references())
    assert [(item.expression, item.reason) for item in references] == [
        ("factory", "static_binding"),
        ("factory", "static_binding"),
        ("factory", "static_binding"),
        ("local", "static_binding"),
        ("local", "static_binding"),
    ]


def test_import_cycles_and_module_name_collisions_are_unknown(tmp_path):
    snapshot = write_sources(
        tmp_path,
        {
            "a.py": "from b import f\nf()\n",
            "b.py": "from a import f\n",
            "pkg.py": "def run(): pass\n",
            "pkg/__init__.py": "def run(): pass\n",
            "client.py": "import pkg\npkg.run()\n",
        },
    )
    refs = list(ReferenceIndex(snapshot, lambda: None).references())
    assert {item.reason for item in refs} == {"import_cycle_or_depth", "ambiguous_module"}
    assert all(item.definition_id is None for item in refs)


def test_reference_count_is_bounded_per_file_and_globally(tmp_path):
    write_sources(tmp_path, {"a.py": "print(1)\nprint(2)\n"})
    policy = PathPolicy(tmp_path, PermissionConfig())
    limited = replace(CodeLimits(), max_references=1)
    assert (
        CodeIndex(tmp_path, policy.resolve, limited).refresh().modules[0].issue.reason
        == "resource_limit"
    )
    (tmp_path / "a.py").write_bytes(b"print(1)\n")
    (tmp_path / "b.py").write_bytes(b"print(2)\n")
    with pytest.raises(CodeIndexError, match="引用"):
        CodeIndex(tmp_path, policy.resolve, limited).refresh()


def test_tool_ambiguity_selection_refresh_and_diagnostics(tmp_path):
    write_sources(
        tmp_path,
        {
            "a.py": "def run(): pass\nrun()\n",
            "b.py": "def run(): pass\nrun()\n",
        },
    )
    tools = create_tool_registry(tmp_path)
    ambiguous = query(tools, "find_references", name="run")
    assert ambiguous["status"] == "ambiguous"
    candidates = ambiguous["results"]
    assert len(candidates) == 2 and all(item["kind"] == "definition" for item in candidates)
    selected = query(tools, "find_references", name=candidates[0]["identifier"])
    assert len(selected["results"]) == 1
    assert selected["results"][0]["definition_id"] == candidates[0]["identifier"]
    old_generation = selected["generation"]
    (tmp_path / "a.py").write_bytes(b"def broken(\n")
    (tmp_path / "b.py").unlink()
    failed = query(tools, "find_definitions", name="run")
    assert failed["status"] == "incomplete"
    assert failed["generation"] != old_generation
    assert failed["diagnostics"] == 1
    assert failed["results"][0]["kind"] == "diagnostic"


def test_whole_items_paginate_and_small_outputs_stay_parseable(tmp_path):
    write_sources(tmp_path, {"app.py": "\n".join(f"def f{i}(): pass" for i in range(12))})
    tools = create_tool_registry(tmp_path, limits=replace(ToolLimits(), max_result_chars=1800))
    offset, rows, generations = 0, [], set()
    while True:
        raw = tools.execute(
            "code_structure", json.dumps({"path": ".", "offset": offset, "limit": 2})
        )
        assert len(raw) <= 1800
        page = json.loads(raw)
        rows.extend(page["results"])
        generations.add(page["generation"])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert len(rows) == 13 and len({row["identifier"] for row in rows}) == 13
    assert len(generations) == 1
    small = create_tool_registry(tmp_path, limits=replace(ToolLimits(), max_result_chars=512))
    raw = small.execute("code_structure", '{"path":"."}')
    assert len(raw) <= 512
    assert json.loads(raw)["status"] == "output_limit"


@pytest.mark.parametrize(
    "arguments",
    [
        {"offset": -1},
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"name": ""},
        {"name": "x" * 2049},
        {"unexpected": True},
    ],
)
def test_invalid_query_arguments_fail_before_read(tmp_path, monkeypatch, arguments):
    def must_not_read(self):
        pytest.fail("invalid arguments must not scan")

    monkeypatch.setattr(CodeIndex, "refresh", must_not_read)
    result = query(
        create_tool_registry(tmp_path), "find_definitions", **{"name": "run", **arguments}
    )
    assert result["ok"] is False


def test_model_tools_use_real_sources_history_budget_and_plan_mode(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    write_sources(workspace, {"code.py": "def run(): pass\nrun()\n"})
    registry = create_tool_registry(workspace).with_mode(lambda: ExecutionMode.PLAN)
    names = {"code_structure", "find_definitions", "find_references"}
    assert names <= {spec["function"]["name"] for spec in registry.definitions()}
    assert all(registry.capability(name) is ToolCapability.READ_ONLY for name in names)
    assert all(registry.can_parallelize(name) for name in names)
    assert query(registry, "find_references", name="run")["ok"]
    payloads = []
    replies = iter(
        [
            tool_response(
                [
                    ("structure", "code_structure", '{"path":"."}'),
                    ("definition", "find_definitions", '{"path":".","name":"run"}'),
                    ("references", "find_references", '{"path":".","name":"run"}'),
                ]
            ),
            answer("code.py defines run at line 1 and references it at line 2"),
        ]
    )

    def respond(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=next(replies))

    with (
        SessionStore(tmp_path / "sessions") as store,
        httpx.Client(transport=httpx.MockTransport(respond)) as http,
    ):
        conversation = make_conversation(store, http, workspace)
        reply = conversation.send("Locate run and its references.")
        sid = conversation.session.session_id
        assert "line 2" in reply
        actual = [message for message in payloads[1]["messages"] if message["role"] == "tool"]
        assert {message["tool_call_id"] for message in actual} == {
            "structure",
            "definition",
            "references",
        }
        reference = json.loads(actual[-1]["content"])["results"][0]
        assert reference["start_line"] == 2 and reference["version"]
        assert store.usage(sid) == (24, False)
        assert len(store.load_messages(sid)) == 6
        assert {
            event["payload"]["name"]
            for event in store.load_events(sid)
            if event["type"] == "tool_started"
        } == names


@pytest.mark.parametrize(
    "source,reason",
    [
        ("import lib\nlib.run = lambda: 1\nlib.run()\n", "dynamic_binding"),
        ("import lib as lib\nlib.run()\n", "static_binding"),
    ],
)
def test_module_attribute_rebinding_is_not_a_proven_target(tmp_path, source, reason):
    snapshot = write_sources(tmp_path, {"lib.py": "def run(): pass\n", "main.py": source})
    matches = [
        item
        for item in ReferenceIndex(snapshot, lambda: None).references()
        if item.expression == "lib.run"
    ]
    assert len(matches) == 1 and matches[0].reason == reason


def test_non_package_file_is_not_treated_as_namespace_package(tmp_path):
    snapshot = write_sources(
        tmp_path,
        {
            "pkg.py": "",
            "pkg/mod.py": "def run(): pass\n",
            "main.py": "import pkg.mod as m\nm.run()\n",
        },
    )
    matches = list(ReferenceIndex(snapshot, lambda: None).references())
    assert matches[0].reason == "ambiguous_package"
    assert matches[0].definition_id is None
