"""
Push ATIF trajectories to an OTLP backend (Phoenix).

`harbor-atif2otel` turns an ATIF trajectory into OTel spans; this module gets
those spans into Phoenix without losing any, which takes more care than it
sounds like.

Two failure modes, both silent, both measured on a real 10,348-span backfill:

  * **A span whose ``end_time`` precedes its ``start_time`` kills the whole
    insert batch.** harbor-atif2otel closes a ``turn-N`` span on the last step
    of the turn, and a later step can carry an earlier timestamp than the one
    that opened it. 9 such spans were enough for Phoenix to answer ``200 OK``
    twice and persist 98 of 10,348, with nothing in its log. :func:`clamp_times`
    is not cosmetic.
  * **The ingest queue is bounded.** Phoenix enqueues and returns before it
    writes, so the response code says nothing about persistence. Once the
    timestamps were valid it started answering ``503 Server is at capacity``
    instead: the queue drains only as fast as the backing Postgres accepts, and
    a single 29 MB request never had a chance. Hence small chunks and backoff.

So: **never treat a 200 as proof.** Verify by counting rows in the backing
store afterwards.

Re-running a push is safe. Trace ids are seeded from ``session_id`` and span ids
from step index, both deterministic, so Phoenix's insert dedup makes spans that
already landed no-ops.
"""

from __future__ import annotations

import logging
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

logger = logging.getLogger(__name__)

#: Phoenix reads its project name off this resource attribute.
PROJECT_ATTRIBUTE = "openinference.project.name"

#: OpenInference's user attribution attribute.
USER_ATTRIBUTE = "user.id"
#: The branch a session worked on, from the session file. The typed layout reads the
#: ticket id out of it (ENG2-1540).
BRANCH_ATTRIBUTE = "git.branch"

DEFAULT_CHUNK_SIZE = 100
DEFAULT_PAUSE_SECONDS = 1.0
DEFAULT_MAX_RETRIES = 12
MAX_BACKOFF_SECONDS = 30.0
BACKOFF_FACTOR = 1.7


@dataclass
class PushResult:
    """Outcome of a push. `spans_sent` counts spans Phoenix ACCEPTED, not stored."""

    spans_total: int = 0
    spans_sent: int = 0
    requests: int = 0
    retries_503: int = 0
    failed_chunks: int = 0
    exhausted_chunks: int = 0
    clamped_spans: int = 0
    stats: Counter = field(default_factory=Counter)

    @property
    def complete(self) -> bool:
        return (
            self.spans_sent == self.spans_total
            and not self.failed_chunks
            and not self.exhausted_chunks
        )


def _import_otel():
    """Import the optional OTel/harbor stack, with an actionable error."""
    try:
        from harbor_atif2otel import convert_trajectory
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )
        from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
        from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans
    except ImportError as exc:  # pragma: no cover - depends on optional extras
        import sys

        version = f"{sys.version_info.major}.{sys.version_info.minor}"
        hint = (
            f" harbor-atif2otel requires Python >= 3.12 and this interpreter is "
            f"{version}, so the extra installs without it."
            if sys.version_info < (3, 12)
            else ""
        )
        raise ImportError(
            "OTLP export needs harbor-atif2otel and opentelemetry-proto. "
            f"Install with: uv pip install 'dev_agent_lens[otlp]'.{hint}"
        ) from exc
    return (
        convert_trajectory,
        ExportTraceServiceRequest,
        AnyValue,
        KeyValue,
        ResourceSpans,
        ScopeSpans,
    )


def clamp_times(spans: Iterable[Any]) -> int:
    """
    Force ``end_time >= start_time`` on every span, returning how many moved.

    Phoenix rejects an entire insert batch on a negative duration and reports
    neither an error code nor a log line, so one bad span silently costs
    thousands of good ones.
    """
    clamped = 0
    for span in spans:
        if span.end_time_unix_nano < span.start_time_unix_nano:
            span.end_time_unix_nano = span.start_time_unix_nano
            clamped += 1
    return clamped


CACHE_WRITE_ATTRIBUTE = "llm.token_count.prompt_details.cache_write"


def _cache_writes_by_span(
    trajectory: dict[str, Any], trace_seed: str | None = None
) -> dict[bytes, int]:
    """Cache-write tokens keyed by the LLM span id harbor-atif2otel gives each step.

    harbor maps a step's prompt, completion and cache-read tokens but has no field for
    Anthropic's cache writes, so `session_to_atif` keeps them in the step's
    `metrics.extra` and they are stamped on the matching span here. The ids follow
    harbor's scheme: `<span seed>:llm:<index among non-copied steps>`, and an embedded
    subagent is converted under the seed `<trace seed>:subagent:<tool span>:<ref>:<id>`.
    """
    from harbor_atif2otel.ids import (
        sha256_span_id,
        sha256_trace_id,
        trajectory_span_seed,
        trajectory_trace_seed,
    )

    seed = trace_seed or trajectory_trace_seed(trajectory)
    base = trajectory_span_seed(trajectory, sha256_trace_id(seed).hex())
    steps = trajectory.get("steps") or []
    active = [s for s in steps if not s.get("is_copied_context")] or steps
    subagents = {
        sub.get("trajectory_id"): sub
        for sub in trajectory.get("subagent_trajectories") or []
        if sub.get("trajectory_id")
    }
    results: dict[str, dict[str, Any]] = {}
    for step in active:
        observation = step.get("observation")
        if isinstance(observation, dict):
            for result in observation.get("results") or []:
                if isinstance(result, dict) and result.get("source_call_id"):
                    results[result["source_call_id"]] = result
    writes: dict[bytes, int] = {}
    for i, step in enumerate(active):
        written = ((step.get("metrics") or {}).get("extra") or {}).get(
            "cache_creation_input_tokens"
        )
        if written:
            writes[sha256_span_id(f"{base}:llm:{i}")] = int(written)
        for j, call in enumerate(step.get("tool_calls") or []):
            refs = results.get(call.get("tool_call_id", ""), {}).get("subagent_trajectory_ref", [])
            if not isinstance(refs, list):
                refs = [refs]
            tool_span = sha256_span_id(f"{base}:tool:{i}:{j}").hex()
            for k, ref in enumerate(refs):
                ref_id = ref.get("trajectory_id") if isinstance(ref, dict) else None
                if ref_id in subagents:
                    writes.update(
                        _cache_writes_by_span(
                            subagents[ref_id], f"{seed}:subagent:{tool_span}:{k}:{ref_id}"
                        )
                    )
    return writes


def build_spans(
    trajectories: list[dict[str, Any]],
    project: str,
    user_id: str | None = None,
    service_name: str | None = None,
) -> tuple[Any, list[tuple[Any, Any]], int]:
    """
    Convert trajectories to OTel spans, tagged and clamped.

    Returns:
        ``(resource, spans, clamped)`` where `resource` carries the project
        routing attribute and `spans` is a list of ``(scope, span)`` pairs.
    """
    convert_trajectory, _, AnyValue, KeyValue, ResourceSpans, _ = _import_otel()

    def kv(key: str, value: str):
        return KeyValue(key=key, value=AnyValue(string_value=value))

    resource = None
    spans: list[tuple[Any, Any]] = []
    for trajectory in trajectories:
        started = time.perf_counter()
        # The harness that wrote the session names the service, so Codex and Claude Code
        # spans stay distinguishable in one store (ENG2-402). An explicit name still wins.
        name = service_name or (trajectory.get("agent") or {}).get("name") or "claude-code"
        resource_spans = convert_trajectory(trajectory, service_name=name)
        if resource is None:
            resource = ResourceSpans()
            resource.resource.CopyFrom(resource_spans.resource)
            resource.resource.attributes.append(kv(PROJECT_ATTRIBUTE, project))
        before = len(spans)
        cache_writes = _cache_writes_by_span(trajectory)
        for scope_span in resource_spans.scope_spans:
            for span in scope_span.spans:
                if user_id:
                    # harbor-atif2otel builds a fixed root attribute list with no
                    # passthrough for ATIF's root `extra`, so attribution is
                    # stamped here. Moving it upstream is the durable fix.
                    span.attributes.append(kv(USER_ATTRIBUTE, user_id))
                if trajectory.get("git_branch"):
                    span.attributes.append(kv(BRANCH_ATTRIBUTE, trajectory["git_branch"]))
                written = cache_writes.pop(bytes(span.span_id), None)
                if written:
                    span.attributes.append(
                        KeyValue(key=CACHE_WRITE_ATTRIBUTE, value=AnyValue(int_value=written))
                    )
                spans.append((scope_span.scope, span))
        if cache_writes:
            # The span ids below are recomputed from harbor-atif2otel's own seeds. A
            # harbor release that changes them would drop cache writes silently; say so.
            logger.warning(
                "[otlp] session=%s: %d cache-write counts matched no LLM span",
                trajectory.get("session_id"),
                len(cache_writes),
            )
        logger.info(
            "[otlp] converted session=%s spans=%d in %.0fms",
            trajectory.get("session_id"),
            len(spans) - before,
            (time.perf_counter() - started) * 1000,
        )

    clamped = clamp_times(span for _, span in spans)
    if clamped:
        logger.warning("[otlp] clamped %d spans whose end preceded their start", clamped)
    return resource, spans, clamped


def push_trajectories(
    endpoint: str,
    trajectories: list[dict[str, Any]],
    project: str,
    user_id: str | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    pause: float = DEFAULT_PAUSE_SECONDS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    service_name: str | None = None,
    dry_run: bool = False,
) -> PushResult:
    """
    Convert and push trajectories to an OTLP HTTP endpoint in small chunks.

    Args:
        endpoint: Base URL, e.g. ``http://127.0.0.1:6006``. ``/v1/traces`` is
            appended.
        dry_run: Convert and validate, send nothing.

    A completed push means every chunk was ACCEPTED. Confirm persistence by
    counting rows in the backing store; see the module docstring.
    """
    _, ExportTraceServiceRequest, _, _, ResourceSpans, ScopeSpans = _import_otel()

    resource, spans, clamped = build_spans(trajectories, project, user_id, service_name)
    result = PushResult(spans_total=len(spans), clamped_spans=clamped)
    if not spans:
        logger.warning("[otlp] nothing to push")
        return result

    duplicates = _count_duplicate_ids(spans)
    result.stats["duplicate_span_ids"] = duplicates
    if duplicates:
        raise ValueError(
            f"{duplicates} spans share a (trace_id, span_id); the backend would drop "
            "them. Give every subagent trajectory a distinct trajectory_id."
        )

    if dry_run:
        logger.info("[otlp] dry run: %d spans ready, nothing sent", len(spans))
        return result

    url = endpoint.rstrip("/") + "/v1/traces"
    for start in range(0, len(spans), chunk_size):
        batch = spans[start : start + chunk_size]
        payload = ResourceSpans()
        payload.resource.CopyFrom(resource.resource)
        scope_spans = ScopeSpans()
        scope_spans.scope.CopyFrom(batch[0][0])
        scope_spans.spans.extend(span for _, span in batch)
        payload.scope_spans.append(scope_spans)
        body = ExportTraceServiceRequest(resource_spans=[payload]).SerializeToString()

        _push_chunk(url, body, batch, start, len(spans), pause, max_retries, result)
        time.sleep(pause)

    logger.info(
        "[otlp] done accepted=%d/%d requests=%d retries_503=%d failed=%d",
        result.spans_sent,
        result.spans_total,
        result.requests,
        result.retries_503,
        result.failed_chunks + result.exhausted_chunks,
    )
    return result


def _push_chunk(
    url: str,
    body: bytes,
    batch: list[tuple[Any, Any]],
    start: int,
    total: int,
    pause: float,
    max_retries: int,
    result: PushResult,
) -> None:
    delay = pause
    for attempt in range(max_retries):
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/x-protobuf"}
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except urllib.error.URLError as exc:
            result.requests += 1
            result.failed_chunks += 1
            logger.error("[otlp] %d/%d transport error: %s", start + len(batch), total, exc.reason)
            return
        result.requests += 1
        elapsed = (time.perf_counter() - started) * 1000

        if status == 200:
            result.spans_sent += len(batch)
            logger.info(
                "[otlp] %d/%d bytes=%d in %.0fms retries=%d",
                start + len(batch),
                total,
                len(body),
                elapsed,
                attempt,
            )
            return
        if status == 503:
            # Bounded ingest queue. Wait for it to drain rather than lose spans.
            result.retries_503 += 1
            delay = min(delay * BACKOFF_FACTOR, MAX_BACKOFF_SECONDS)
            logger.info("[otlp] %d/%d at capacity, waiting %.1fs", start + len(batch), total, delay)
            time.sleep(delay)
            continue
        result.failed_chunks += 1
        logger.error("[otlp] %d/%d -> HTTP %s", start + len(batch), total, status)
        return

    result.exhausted_chunks += 1
    logger.error("[otlp] %d/%d gave up after %d retries", start + len(batch), total, max_retries)


def _count_duplicate_ids(spans: list[tuple[Any, Any]]) -> int:
    """Count spans sharing a (trace_id, span_id); the backend keeps only one."""
    seen: Counter = Counter()
    for _, span in spans:
        seen[(bytes(span.trace_id), bytes(span.span_id))] += 1
    return sum(count - 1 for count in seen.values() if count > 1)
