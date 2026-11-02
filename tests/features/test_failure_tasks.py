"""B35.1：三条实际构造失败、来源哈希及六个变体的独立终态验收。"""

import hashlib
import json

from scripts.failure_tasks import Blueprint, generate, prepare, verify


def test_constructed_failures_generate_variants_with_independent_verification(tmp_path):
    root = tmp_path / "dataset"
    dataset = generate(root)
    assert len(dataset["sources"]) == 3 and len(dataset["blueprints"]) == 6
    for source in dataset["sources"]:
        raw = (root / source["path"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == source["sha256"]
        trace = json.loads(raw)
        assert not trace["verification"]["passed"]
        assert trace["origin"] == "constructed_local_failure"
    for row in dataset["blueprints"]:
        blueprint = Blueprint(**row)
        workspace = tmp_path / blueprint.task_id
        prepare(blueprint, workspace)
        if blueprint.family != "preserve_correct":
            assert not verify(blueprint, workspace, "done")["passed"]
        for name, text in blueprint.expected_files.items():
            (workspace / name).write_text(text, encoding="utf-8")
        reply = json.dumps(blueprint.expected_reply) if blueprint.expected_reply else "done"
        assert verify(blueprint, workspace, reply)["passed"]
        (workspace / "extra.txt").write_text("unrequested")
        assert not verify(blueprint, workspace, reply)["passed"]
