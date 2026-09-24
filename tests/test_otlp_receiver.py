"""The OTLP/HTTP receiver: real protobuf in, rows in the store out, no Phoenix anywhere."""

from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request

import duckdb
import pytest

pytest.importorskip("opentelemetry.proto")

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (  # noqa: E402
    ExportTraceServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue  # noqa: E402

from dev_agent_lens.otlp_receiver import Receiver, decode_request  # noqa: E402
from dev_agent_lens.storage.spanstore import open_store  # noqa: E402


def _request(project: str | None, span_ids: list[bytes], *, name="litellm_request") -> bytes:
    req = ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    if project:
        rs.resource.attributes.append(
            KeyValue(key="openinference.project.name", value=AnyValue(string_value=project))
        )
    ss = rs.scope_spans.add()
    ss.scope.name = "litellm"
    now = time.time_ns()
    for sid in span_ids:
        sp = ss.spans.add()
        sp.trace_id = b"\x01" * 16
        sp.span_id = sid
        sp.name = name
        sp.start_time_unix_nano = now
        sp.end_time_unix_nano = now + 1_000_000
        sp.attributes.append(KeyValue(key="input.value", value=AnyValue(string_value="hello")))
        sp.attributes.append(KeyValue(key="llm.model_name", value=AnyValue(string_value="m")))
    return req.SerializeToString()


def _post(url: str, body: bytes, ctype: str = "application/x-protobuf") -> int:
    r = urllib.request.Request(url, data=body, headers={"Content-Type": ctype}, method="POST")
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.fixture
def receiver(tmp_path):
    store = open_store(f"file://{tmp_path}/s")
    store.ensure()
    con = duckdb.connect()
    r = Receiver(
        store,
        con,
        host="127.0.0.1",
        port=0,
        default_source="fallback",
        flush_rows=3,
        flush_seconds=999,
    )
    t = threading.Thread(target=r.serve_forever, daemon=True)
    t.start()
    time.sleep(0.2)
    yield r, store, con
    r.stop()
    t.join(timeout=5)


def _count(con, store, source=None):
    sql = f"SELECT count(*) FROM read_parquet('{store.read_glob('spans_raw')}', hive_partitioning=true, union_by_name=true)"  # noqa: E501
    if source:
        sql += f" WHERE source = '{source}'"
    try:
        return con.execute(sql).fetchone()[0]
    except duckdb.IOException:
        return 0


def test_decode_groups_by_resource_source():
    by = decode_request(_request("proj-a", [b"\x01" * 8, b"\x02" * 8]), "fallback")
    assert list(by) == ["proj-a"] and len(by["proj-a"]) == 2
    by = decode_request(_request(None, [b"\x03" * 8]), "fallback")
    assert list(by) == ["fallback"]


def test_spans_land_under_their_source_after_the_row_flush(receiver):
    r, store, con = receiver
    url = f"http://127.0.0.1:{r.port}/v1/traces"
    assert _post(url, _request("proj-a", [b"\x01" * 8, b"\x02" * 8])) == 200
    assert _count(con, store) == 0  # buffered, below flush_rows
    assert _post(url, _request("proj-a", [b"\x03" * 8])) == 200  # third row triggers the flush
    time.sleep(0.3)
    assert _count(con, store, "proj-a") == 3
    row = con.execute(
        f"SELECT span_id, name, json_extract_string(attributes,'$.input.value') FROM read_parquet('{store.read_glob('spans_raw')}', hive_partitioning=true, union_by_name=true) ORDER BY 1 LIMIT 1"  # noqa: E501
    ).fetchone()
    assert row == ("0101010101010101", "litellm_request", "hello")


def test_an_exporter_retry_lands_nothing_twice(receiver):
    r, store, con = receiver
    url = f"http://127.0.0.1:{r.port}/v1/traces"
    body = _request("proj-b", [b"\x0a" * 8, b"\x0b" * 8, b"\x0c" * 8])
    assert _post(url, body) == 200
    assert _post(url, body) == 200
    time.sleep(0.3)
    assert _count(con, store, "proj-b") == 3
    assert r.buffer.received == 6 and r.buffer.written == 3


def test_shutdown_flushes_what_the_timer_had_not(tmp_path):
    store = open_store(f"file://{tmp_path}/s")
    store.ensure()
    con = duckdb.connect()
    r = Receiver(store, con, host="127.0.0.1", port=0, flush_rows=1000, flush_seconds=999)
    t = threading.Thread(target=r.serve_forever, daemon=True)
    t.start()
    time.sleep(0.2)
    assert _post(f"http://127.0.0.1:{r.port}/v1/traces", _request("late", [b"\x0f" * 8])) == 200
    r.stop()
    t.join(timeout=5)
    assert _count(con, store, "late") == 1


def test_wrong_content_type_and_path_are_refused_and_health_answers(receiver):
    r, _, _ = receiver
    base = f"http://127.0.0.1:{r.port}"
    assert _post(f"{base}/v1/traces", b"{}", "application/json") == 415
    assert _post(f"{base}/v1/metrics", b"") == 404
    assert _post(f"{base}/v1/traces", b"\xff\xfe garbage") in (
        400,
        200,
    )  # protobuf is lenient on junk; 400 when it is not
    with urllib.request.urlopen(f"{base}/health", timeout=5) as resp:
        assert resp.status == 200 and resp.read().startswith(b"ok received=")
