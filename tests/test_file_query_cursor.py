"""B21.1.1：续读游标只绑定查询和版本，不能伪造授权或混用不同查询结果。"""

import pytest

from adam.contracts.file_query import QueryCursor, decode_cursor, query_digest


def test_cursor_roundtrip_and_canonical_query_key():
    query = query_digest({"path": "目录", "mode": "literal", "query": "needle"})
    assert query == query_digest({"query": "needle", "mode": "literal", "path": "目录"})
    cursor = QueryCursor(query, "b" * 64, 50)
    assert decode_cursor(cursor.encode(), query, "b" * 64).offset == 50
    assert "目录" not in cursor.encode() and len(cursor.encode()) <= 144
    assert QueryCursor(query, "b" * 64, 1000000000).encode().endswith(".1000000000")


@pytest.mark.parametrize("value", [None, {}, "", "x" * 145, "fq2." + "a" * 64, "fq1.a.b.0"])
def test_bad_cursor_is_rejected(value):
    with pytest.raises(ValueError):
        decode_cursor(value, "a" * 64, "b" * 64)


@pytest.mark.parametrize("offset", ["-1", "01", "1000000001", "1.0", "True"])
def test_offset_is_bounded_and_canonical(offset):
    with pytest.raises(ValueError):
        decode_cursor(f"fq1.{'a' * 64}.{'b' * 64}.{offset}", "a" * 64, "b" * 64)


def test_changed_query_and_generation_have_distinct_recovery_instructions():
    value = QueryCursor("a" * 64, "b" * 64, 50).encode()
    with pytest.raises(ValueError, match="不属于"):
        decode_cursor(value, "c" * 64, "b" * 64)
    with pytest.raises(ValueError, match="来源已变化"):
        decode_cursor(value, "a" * 64, "c" * 64)
