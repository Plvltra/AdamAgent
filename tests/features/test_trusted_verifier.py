"""B38.1：实际终态独立判据与宿主选择，不接受模型自封成功。"""

import pytest

from adam.verification.files import FileStateVerifier, VerifierCatalog


def test_host_snapshot_cannot_be_replaced_by_reply_or_mutable_input(tmp_path):
    expected = {"result.txt": b"correct"}
    verifier = FileStateVerifier(tmp_path, expected)
    catalog = VerifierCatalog({"task": verifier})
    expected["result.txt"] = b"wrong"
    (tmp_path / "result.txt").write_bytes(b"wrong")
    result = catalog.select("task").check('success=true; expected="wrong"')
    assert result.status == "failed" and result.observed_files[0][1]
    (tmp_path / "result.txt").write_bytes(b"correct")
    passed = verifier.check("没有改好")
    assert passed.status == "passed" and passed.version == result.version
    with pytest.raises(ValueError, match="unknown"):
        catalog.select("model_created_verifier")


def test_missing_file_and_read_failure_are_distinct(tmp_path, monkeypatch):
    verifier = FileStateVerifier(tmp_path, {"result.txt": b"correct"})
    assert verifier.check("done").status == "failed"
    path = tmp_path / "result.txt"
    path.write_bytes(b"correct")

    def fail_read(self):
        raise PermissionError("injected read failure")

    monkeypatch.setattr(type(path), "read_bytes", fail_read)
    assert verifier.check("done").status == "unknown"
    with pytest.raises(ValueError, match="invalid"):
        FileStateVerifier(tmp_path, {"../outside": b"correct"})
