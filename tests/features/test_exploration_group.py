"""B39.1：先到错误证据不能胜出，真实定位经过核验且只结算一次。"""

import hashlib
import json

import pytest

from adam.application.subagents.exploration import ExplorationGroup
from adam.contracts.errors import StorageError
from adam.verification.citations import CitationVerifier


def test_false_first_result_then_verified_winner_survives_restart(tmp_path):
    source = tmp_path / "source.txt"
    source.write_bytes(b"intro\nneedle here\n")
    verifier = CitationVerifier(tmp_path, ("source.txt",), "needle")
    evidence = {
        "path": "source.txt",
        "line": 2,
        "content": "needle here",
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }
    for field, value in [
        ("path", "../outside"),
        ("line", 1),
        ("content", "fake"),
        ("sha256", "old"),
    ]:
        assert verifier.check(json.dumps({**evidence, field: value})).status == "failed"
    members = (("a", "r1"), ("b", "r2"), ("c", "r3"))
    path = tmp_path / "group.json"
    group = ExplorationGroup(path, members, verifier)
    assert group.submit("a", "r1", "I succeeded")["winner"] is None
    state = group.submit("b", "r2", json.dumps(evidence))
    assert state["winner"] == ["b", "r2"]
    assert len(state["checks"]) == 2
    assert group.submit("c", "r3", json.dumps(evidence)) == state
    assert ExplorationGroup(path, members, verifier).snapshot() == state
    source.write_bytes(b"changed\nneedle here\n")
    assert verifier.check(json.dumps(evidence)).status == "failed"


def test_save_failure_does_not_publish_winner(tmp_path, monkeypatch):
    source = tmp_path / "source.txt"
    source.write_bytes(b"needle")
    verifier = CitationVerifier(tmp_path, ("source.txt",), "needle")
    group = ExplorationGroup(tmp_path / "group.json", (("a", "r1"),), verifier)
    evidence = json.dumps(
        {
            "path": "source.txt",
            "line": 1,
            "content": "needle",
            "sha256": hashlib.sha256(b"needle").hexdigest(),
        }
    )

    def fail_replace(*args):
        raise OSError("injected atomic replacement failure")

    monkeypatch.setattr("adam.application.subagents.exploration.os.replace", fail_replace)
    with pytest.raises(StorageError):
        group.submit("a", "r1", evidence)
    assert group.snapshot()["winner"] is None
    assert json.loads(group.path.read_text())["winner"] is None
