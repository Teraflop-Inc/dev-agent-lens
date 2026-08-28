"""
Unit tests for the OTLP push.

Both behaviours under test cost a real backfill. A span whose end precedes its
start makes the backend reject the whole insert batch while still answering
200, and its ingest queue is bounded, so a full queue answers 503 and the spans
are lost unless the push waits and retries.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from dev_agent_lens.export import otlp


class _Span:
    """Minimal stand-in for the OTel protobuf span fields we touch."""

    def __init__(self, start, end, trace_id=b"t", span_id=b"s"):
        self.start_time_unix_nano = start
        self.end_time_unix_nano = end
        self.trace_id = trace_id
        self.span_id = span_id


def test_clamp_times_fixes_only_negative_durations():
    spans = [_Span(100, 50), _Span(100, 100), _Span(100, 200)]

    clamped = otlp.clamp_times(spans)

    assert clamped == 1
    assert [s.end_time_unix_nano for s in spans] == [100, 100, 200]


def test_duplicate_span_ids_are_detected():
    spans = [
        (None, _Span(1, 2, b"trace", b"a")),
        (None, _Span(1, 2, b"trace", b"a")),
        (None, _Span(1, 2, b"trace", b"b")),
    ]

    assert otlp._count_duplicate_ids(spans) == 1


def test_no_duplicates_across_different_traces():
    spans = [
        (None, _Span(1, 2, b"trace-1", b"a")),
        (None, _Span(1, 2, b"trace-2", b"a")),
    ]

    assert otlp._count_duplicate_ids(spans) == 0


def _push_chunk(statuses, max_retries=5, pause=0.0):
    """Drive _push_chunk against a scripted sequence of HTTP statuses."""
    result = otlp.PushResult(spans_total=1)
    calls = iter(statuses)

    def fake_urlopen(request, timeout=None):
        status = next(calls)
        if status >= 400:
            import urllib.error

            raise urllib.error.HTTPError(request.full_url, status, "", None, None)

        class _Response:
            def __enter__(self_inner):
                return SimpleNamespace(status=status)

            def __exit__(self_inner, *exc):
                return False

        return _Response()

    with patch.object(otlp.urllib.request, "urlopen", fake_urlopen):
        otlp._push_chunk(
            "http://x/v1/traces", b"body", [(None, None)], 0, 1, pause, max_retries, result
        )
    return result


def test_503_is_retried_until_the_queue_drains():
    result = _push_chunk([503, 503, 200])

    assert result.spans_sent == 1
    assert result.retries_503 == 2
    assert result.requests == 3
    assert result.complete


def test_persistent_503_gives_up_and_is_reported_not_swallowed():
    result = _push_chunk([503] * 10, max_retries=3)

    assert result.spans_sent == 0
    assert result.exhausted_chunks == 1
    assert not result.complete


def test_other_http_errors_fail_the_chunk_immediately():
    result = _push_chunk([500, 200])

    assert result.spans_sent == 0
    assert result.failed_chunks == 1
    assert result.requests == 1, "a 500 is not retried"


def test_push_refuses_to_send_colliding_span_ids():
    spans = [(None, _Span(1, 2, b"trace", b"a")), (None, _Span(1, 2, b"trace", b"a"))]

    with patch.object(otlp, "_import_otel", return_value=(None,) * 6), patch.object(
        otlp, "build_spans", return_value=(object(), spans, 0)
    ):
        with pytest.raises(ValueError, match="trace_id, span_id"):
            otlp.push_trajectories("http://x", [{}], project="p")


def test_dry_run_sends_nothing():
    spans = [(None, _Span(1, 2, b"trace", b"a"))]

    with patch.object(otlp, "_import_otel", return_value=(None,) * 6), patch.object(
        otlp, "build_spans", return_value=(object(), spans, 3)
    ), patch.object(otlp.urllib.request, "urlopen") as urlopen:
        result = otlp.push_trajectories("http://x", [{}], project="p", dry_run=True)

    urlopen.assert_not_called()
    assert result.spans_total == 1
    assert result.clamped_spans == 3
    assert result.spans_sent == 0
