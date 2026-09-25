"""`attributes_rest` must keep json_merge_patch's values (ENG2-1650).

The typed layout replaced `json_merge_patch(attributes, '{"llm":{"anthropic":null}}')` with a
Python function because the DuckDB call exhausted memory on large live spans. These cases pin
the replacement to the DuckDB result, compared as JSON values: Python may write a float in a
different form, which is the only permitted difference.
"""

import json

import duckdb
import pytest

from dev_agent_lens.storage.layouts.typed import _REST_PATCH, _register_functions, attributes_rest

CASES = [
    '{"a": 1, "llm": {"x": 2, "anthropic": {"messages": "[]"}}}',
    '{"llm": {"anthropic": null}}',
    '{"llm": {}}',
    "{}",
    '{"llm": "not an object"}',
    '{"llm": null, "z": [{"llm": 1}]}',
    '{"a": 1.5e-05, "b": "\\u00e9/\\u2028", "c": 10000000000000000001, "f": 1.0}',
    '{"llm": {"model_name": "claude", "anthropic": {"tools": "[1]"}, "token_count": {"prompt": 5}},'
    ' "input": {"value": "x"}}',
    "[1, 2]",
    '"string"',
    "null",
]


@pytest.mark.parametrize("doc", CASES)
def test_matches_json_merge_patch(doc):
    con = duckdb.connect()
    expected = con.execute("SELECT json_merge_patch(?, ?)", [doc, _REST_PATCH]).fetchone()[0]
    assert json.loads(attributes_rest(doc)) == json.loads(expected)


def test_null_and_malformed():
    assert attributes_rest(None) is None
    with pytest.raises(ValueError):
        attributes_rest("{not json")


def test_registered_function_on_a_vector():
    con = duckdb.connect()
    _register_functions(con)
    _register_functions(con)  # re-registering on the same connection is safe
    rows = con.execute(
        "SELECT dal_attributes_rest(a) FROM (VALUES (?), (NULL), (?)) t(a)",
        [CASES[0], CASES[3]],
    ).fetchall()
    assert [json.loads(r[0]) if r[0] else None for r in rows] == [
        {"a": 1, "llm": {"x": 2}},
        None,
        {"llm": {}},
    ]
