"""An OTLP/HTTP trace receiver that lands spans in the span store. No Phoenix.

This is the producer path a Phoenix-free deployment needs: LiteLLM's OpenTelemetry
callback (the one surviving third-party dependency per decision 0003) posts
``ExportTraceServiceRequest`` protobufs to ``/v1/traces``; this server turns them into
raw-shape rows with the same converter ``dal ingest-sessions --to-store`` uses and
appends them to ``spans_raw`` under a ``source``. The sync loop's typed rebuild then
picks them up like any other batch.

Buffered on purpose. The proxy exports in batches of four spans (``OTEL_BSP_MAX_EXPORT_
BATCH_SIZE=4``, a whale-span mitigation), and one Parquet file per request would be
thousands of files a day. Rows accumulate and flush every ``flush_rows`` rows or
``flush_seconds`` seconds, whichever first, and on shutdown. A flush that fails keeps
the rows and retries on the next tick; a crash between receipt and flush loses at most
one window, which the exporter's own retry (it gets a 200 only after we buffered, not
after we wrote) does not cover. That is the trade for file counts the store can query.

Idempotent per (span_id, source) because ``append_frame`` is: an exporter retry of a
batch we already wrote lands nothing twice.

Point a producer at it:

    OTEL_EXPORTER_OTLP_ENDPOINT=http://<host>:4318
    OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
    OTEL_RESOURCE_ATTRIBUTES=openinference.project.name=<source>

The source is the resource's ``openinference.project.name``, else ``service.name``,
else ``--source-default``. gRPC is not served; the proxy speaks http/protobuf when told.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

log = logging.getLogger(__name__)

PROTOBUF_TYPES = {"application/x-protobuf", "application/protobuf"}
EVENT_BODY_LIMIT = 25 * 1024 * 1024  # bytes, GitHub's own webhook payload cap


def _import_proto():
    try:
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
            ExportTraceServiceResponse,
        )
    except ImportError as e:  # pragma: no cover - the otlp extra is a declared dependency
        raise ImportError("opentelemetry-proto is required: uv sync --extra otlp") from e
    return ExportTraceServiceRequest, ExportTraceServiceResponse


def _resource_source(resource: Any, default: str) -> str:
    attrs = {kv.key: kv.value for kv in resource.attributes}
    for key in ("openinference.project.name", "service.name"):
        v = attrs.get(key)
        if v is not None and v.HasField("string_value") and v.string_value:
            return v.string_value
    return default


def decode_request(body: bytes, default_source: str) -> dict[str, list[tuple[Any, Any]]]:
    """Protobuf bytes -> {source: [(scope, span), ...]}, grouped by resource source."""
    ExportTraceServiceRequest, _ = _import_proto()
    req = ExportTraceServiceRequest()
    req.ParseFromString(body)
    out: dict[str, list[tuple[Any, Any]]] = {}
    for rs in req.resource_spans:
        source = _resource_source(rs.resource, default_source)
        bucket = out.setdefault(source, [])
        for ss in rs.scope_spans:
            for span in ss.spans:
                bucket.append((ss.scope, span))
    return out


class SpanBuffer:
    """Rows per source, flushed to the store in one append per source."""

    def __init__(self, store: Any, con: Any, *, flush_rows: int, flush_seconds: float) -> None:
        self.store, self.con = store, con
        self.flush_rows, self.flush_seconds = flush_rows, flush_seconds
        self._rows: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._last_flush = time.monotonic()
        self.received = 0
        self.written = 0
        self.flushes = 0
        self.failed_flushes = 0

    def add(self, by_source: dict[str, list[tuple[Any, Any]]]) -> int:
        from dev_agent_lens.export.store_ingest import spans_to_rows

        n = 0
        with self._lock:
            for source, spans in by_source.items():
                rows = spans_to_rows(spans)
                self._rows.setdefault(source, []).extend(rows)
                n += len(rows)
            self.received += n
            pending = sum(len(v) for v in self._rows.values())
        log.debug("[otlp] buffered %d rows (%d pending)", n, pending)
        if pending >= self.flush_rows:
            self.flush("rows")
        return n

    def add_rows(self, source: str, rows: list[dict[str, Any]]) -> int:
        """Rows already in the raw shape (a webhook event), same buffer, same flush."""
        with self._lock:
            self._rows.setdefault(source, []).extend(rows)
            self.received += len(rows)
            pending = sum(len(v) for v in self._rows.values())
        if pending >= self.flush_rows:
            self.flush("rows")
        return len(rows)

    def due(self) -> bool:
        return (time.monotonic() - self._last_flush) >= self.flush_seconds and any(
            self._rows.values()
        )

    def flush(self, reason: str = "timer") -> int:
        # Requests and the timer share one DuckDB connection and raw writer.
        # Keep intake independent, but serialize the entire append operation.
        with self._flush_lock:
            return self._flush_locked(reason)

    def _flush_locked(self, reason: str) -> int:
        import pandas as pd

        with self._lock:
            batch, self._rows = self._rows, {}
            self._last_flush = time.monotonic()
        if not batch:
            return 0
        t0 = time.perf_counter()
        total = 0
        try:
            for source, rows in batch.items():
                n = self.store.append_frame(
                    self.con, pd.DataFrame(rows), "spans_raw", source=source
                )
                total += n
                log.info(
                    "[otlp] flush(%s) source=%s rows=%d written=%d", reason, source, len(rows), n
                )
        except Exception as e:  # noqa: BLE001 - keep the rows, report, retry next tick
            self.failed_flushes += 1
            with self._lock:
                for source, rows in batch.items():
                    self._rows.setdefault(source, [])[:0] = rows
            log.error(
                "[otlp] flush failed, %d rows kept for retry: %s", sum(map(len, batch.values())), e
            )
            return 0
        self.flushes += 1
        self.written += total
        log.info("[otlp] flushed %d rows in %.0fms", total, (time.perf_counter() - t0) * 1000)
        return total


def make_handler(buffer: SpanBuffer, default_source: str, events_secret: str | None = None):
    _, ExportTraceServiceResponse = _import_proto()
    ok_body = ExportTraceServiceResponse().SerializeToString()

    class Handler(BaseHTTPRequestHandler):
        server_version = "dal-otlp/1"
        # A client that stops sending gets 30 s, not a thread forever (the events door is
        # public through the router relay).
        timeout = 30

        def log_message(self, fmt: str, *args: Any) -> None:  # route to logging, debug level
            log.debug("[otlp] " + fmt, *args)

        def _reply(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if self.path in ("/health", "/healthz", "/"):
                text = (
                    f"ok received={buffer.received} written={buffer.written} "
                    f"flushes={buffer.flushes} failed={buffer.failed_flushes}\n"
                ).encode()
                self._reply(200, text, "text/plain")
            else:
                self._reply(404, b"not found\n", "text/plain")

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            if self.path.startswith("/v1/events/"):
                self._post_event(self.path[len("/v1/events/") :].strip("/"))
                return
            if self.path != "/v1/traces":
                self._reply(404, b"only /v1/traces and /v1/events/<producer>\n", "text/plain")
                return
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype not in PROTOBUF_TYPES:
                self._reply(
                    415,
                    b"send application/x-protobuf (OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf)\n",
                    "text/plain",
                )
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            t0 = time.perf_counter()
            try:
                by_source = decode_request(body, default_source)
            except Exception as e:  # noqa: BLE001 - a bad body is the client's problem
                log.warning("[otlp] undecodable request (%d bytes): %s", length, e)
                self._reply(400, f"bad protobuf: {e}\n".encode(), "text/plain")
                return
            n = buffer.add(by_source)
            log.debug(
                "[otlp] accepted %d spans (%d bytes) in %.1fms",
                n,
                length,
                (time.perf_counter() - t0) * 1000,
            )
            self._reply(200, ok_body, "application/x-protobuf")

        def _post_event(self, producer: str) -> None:
            """A git host's webhook: one JSON document, one span (ENG2-1622)."""
            from dev_agent_lens.webhook_events import PRODUCER, event_to_row, verify_signature

            producer = producer.split("?", 1)[0]
            if not PRODUCER.fullmatch(producer):
                self._reply(404, b"use /v1/events/<producer>\n", "text/plain")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._reply(400, b"bad Content-Length\n", "text/plain")
                return
            if length < 0 or length > EVENT_BODY_LIMIT:
                # Refused before a byte is read; GitHub itself caps a payload at 25 MB.
                log.warning("[events] %s: body of %d bytes refused", producer, length)
                self._reply(413, b"body too large\n", "text/plain")
                return
            body = self.rfile.read(length)
            if events_secret and not verify_signature(self.headers, body, events_secret):
                log.warning("[events] %s: bad or missing signature (%d bytes)", producer, length)
                self._reply(401, b"bad signature\n", "text/plain")
                return
            try:
                row = event_to_row(producer, self.headers, body)
            except ValueError as e:
                log.warning("[events] %s: bad delivery (%d bytes): %s", producer, length, e)
                self._reply(400, f"{e}\n".encode(), "text/plain")
                return
            buffer.add_rows(producer, [row])
            log.info("[events] %s: %s (%d bytes)", producer, row["name"], length)
            self._reply(200, b"ok\n", "text/plain")

    return Handler


class Receiver:
    """Serve until `stop()`. `serve_forever` runs the flush timer alongside the server."""

    def __init__(
        self,
        store: Any,
        con: Any,
        *,
        host: str = "127.0.0.1",
        port: int = 4318,
        default_source: str = "otlp",
        flush_rows: int = 500,
        flush_seconds: float = 30.0,
        events_secret: str | None = None,
    ) -> None:
        self.buffer = SpanBuffer(store, con, flush_rows=flush_rows, flush_seconds=flush_seconds)
        # The hook secret for /v1/events/<producer>. Explicit argument, else the env; empty
        # means unsigned deliveries are accepted, which is only right on a private network.
        if events_secret is None:
            events_secret = os.environ.get("DAL_EVENTS_SECRET") or None
        self.events_secret = events_secret
        self.httpd = ThreadingHTTPServer(
            (host, port), make_handler(self.buffer, default_source, events_secret)
        )
        self.httpd.daemon_threads = True
        self._stop = threading.Event()

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def serve_forever(self) -> None:
        t = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True
        )
        t.start()
        log.info(
            "[otlp] listening on %s:%d, flush every %d rows or %.0fs",
            *self.httpd.server_address,
            self.buffer.flush_rows,
            self.buffer.flush_seconds,
        )
        # Say which door /v1/events is. An open door is right on a private network and
        # wrong behind the public relay; the log line is how a deploy tells the difference.
        log.info(
            "[events] signature %s",
            "required" if self.events_secret else "NOT required (DAL_EVENTS_SECRET unset)",
        )
        try:
            while not self._stop.wait(1.0):
                if self.buffer.due():
                    self.buffer.flush("timer")
        finally:
            self.httpd.shutdown()
            self.buffer.flush("shutdown")
            log.info(
                "[otlp] stopped: received=%d written=%d flushes=%d failed=%d",
                self.buffer.received,
                self.buffer.written,
                self.buffer.flushes,
                self.buffer.failed_flushes,
            )

    def stop(self) -> None:
        self._stop.set()
