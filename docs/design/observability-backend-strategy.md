# Observability backend strategy: LiteLLM → Phoenix

Status: current as of 2026-08-12 (ENG2-1494, ENG2-1510)

This document is cited by ENG2-1486 and ENG2-1494 as the source of the
"`arize_phoenix` is HTTP-only" rule. **It never existed in the repo** — both
tickets cite a file that was never committed, and the claim survived only as a
comment in `docker-compose.yml`. This file now exists so the rule has one
home, and the rule itself is corrected below.

## 1. Transport: protocol and endpoint

**Both OTLP/HTTP and OTLP/gRPC work. There is no HTTP-only constraint.**

| Environment | `PHOENIX_COLLECTOR_ENDPOINT` | Resolved protocol |
| -- | -- | -- |
| Local (`docker-compose.yml`) | `http://phoenix:6006` | `otlp_http` → `http://phoenix:6006/v1/traces` |
| Prod (`fly/litellm.fly.toml`) | `http://sf-phoenix.internal:4317` | `otlp_grpc` |

The callback picks the exporter itself, in
`ArizePhoenixLogger.get_arize_phoenix_config()`:

```python
if collector_endpoint.startswith("grpc://") or (
    ":4317" in collector_endpoint and "/v1/traces" not in collector_endpoint
):
    endpoint = collector_endpoint
    protocol = "otlp_grpc"
```

`PHOENIX_COLLECTOR_HTTP_ENDPOINT` is checked first and wins when set — which is
why `litellm_config_phoenix.yaml` and `litellm_config_test_phoenix.yaml` end up
on HTTP despite also setting a `:4317` value.

### What the stale claim got wrong

The retired comment said the callback "uses requests/HTTP under the hood" and
that `:4317` caused silent drops with `Custom Logger Error`. Three independent
lines of evidence contradict it:

1. The config resolver above explicitly branches to `otlp_grpc` on `:4317`.
2. `fly/phoenix.fly.toml:29-33` documents `4317` as the deliberate path and
   notes it "auto-dual-stacks"; the actual IPv6 defect was on **6006**, fixed
   with `PHOENIX_HOST="::"`.
3. Prod has been exporting over gRPC continuously and spans land
   (`sf-workspaces`).

The likely origin of the myth: an early misconfiguration pointed at `:4317`
*with* a `/v1/traces` path, which the resolver classifies as `otlp_http` and
then POSTs to a gRPC listener — that genuinely fails. The port was blamed;
the path was the problem.

**Rule:** point at a bare `host:4317` for gRPC, or any endpoint ending
`/v1/traces` for HTTP. Never mix — a `:4317` endpoint *with* a `/v1/traces`
suffix silently selects HTTP and breaks.

## 2. Content: the `message_logging` landmine

`callback_settings.arize_phoenix.message_logging` is **not** a per-span toggle.
It is the callback-wide content kill-switch, and setting it to `false` caused a
9-day capture outage (ENG2-1510).

Chain, all in upstream `litellm`:

1. `OpenTelemetry._resolve_capture_mode()` →
   `SPAN_AND_EVENT if self.message_logging else NO_CONTENT`
2. `NO_CONTENT` makes `_capture_in_span()` false, which gates **both**
   `_maybe_log_raw_request()` and the content on the spans DAL reads.
3. `perform_redaction()` rewrites messages to
   `[{"role": "user", "content": "redacted-by-litellm"}]`.
4. Arize `set_messages()` maps last-message content → `SpanAttributes.INPUT_VALUE`.

Result: `attributes.input.value == "redacted-by-litellm"` (19 chars — exactly
the median input length observed during the outage), and
`metadata.requester_metadata` (which carries `session_id`) stripped alongside.
`%redacted` and `%null_session` tracked each other to within 0.1% for 9
consecutive days: one cause, two symptoms.

Onset was **2026-08-03**, the deploy of `7aaa658` — not "~08-01" as originally
filed; 08-01 and 08-02 were a weekend with zero rows.

### Why the fix is a revert, not a retune

ENG2-1461 set the flag to kill the raw-request child span (~1.3MB/span of
`llm.anthropic.*`, the 30GB TOAST). That part worked. But upstream offers no
config-only way to keep it dead while restoring content —
`_maybe_log_raw_request()` early-returns on exactly two conditions:

- `not self._capture_in_span()` — kills content too (the bug we just hit)
- `self._gen_ai_semconv_latest_experimental` — renames every attribute to
  `gen_ai.*`, breaking DAL's normalizer and Phoenix's openinference rendering

Decoupling them requires a fork patch exposing a separate `raw_request_logging`
setting. Until that lands, span bloat is contained by the ENG2-1476 size
sentinel and the ENG2-1469 trim script — not by this flag.

## 3. Unresolved: the ~34% that kept content

Every day of the outage, ~66% of spans were redacted and ~34% were not, stable
to within 0.4% for 9 days. That stability is not explained yet, and it should
be understood before anyone concludes the fix is complete.

Note a discrepancy worth resolving first: ENG2-1510's own monthly table implies
only **623** August LLM spans (1.5%) carry real content, while 13,812 (32.6%)
have **no** `input.value` at all. 32.6% is suspiciously close to the reported
34% — so the "survivors" may be spans that never had an input value (parent
`litellm_proxy_request` SERVER spans, guardrail spans), counted as
"not redacted" because the marker string is absent rather than because content
survived.

Discriminating query — split the non-redacted bucket by project and span name,
and separate "real content" from "no content":

```sql
SELECT p.name AS project,
       s.name AS span_name,
       CASE WHEN s.attributes #>> '{input,value}' IS NULL      THEN 'no_input'
            WHEN s.attributes #>> '{input,value}' =
                 'redacted-by-litellm'                          THEN 'redacted'
            ELSE 'real_content' END AS bucket,
       count(*)
FROM phoenix.spans s
JOIN phoenix.traces t   ON t.id = s.trace_rowid
JOIN phoenix.projects p ON p.id = t.project_rowid
WHERE s.start_time >= '2026-08-04' AND s.start_time < '2026-08-11'
GROUP BY 1, 2, 3
ORDER BY 4 DESC;
```

If `real_content` collapses to a single project (e.g. `dev-agent-lens` rather
than `sf-workspaces`), the survivors are simply traffic from a proxy that never
received the flag, and the prod outage was total rather than partial.

## 4. Post-deploy verification (not yet run)

Config-only change; requires an `sf-litellm` deploy, which is gated on Adam.

- [ ] Deploy `sf-litellm` and confirm the running config has no
      `callback_settings.arize_phoenix.message_logging` key
- [ ] Burst reconciliation (ENG2-1494): send N tagged requests, confirm N spans
      land — quantifies any gRPC drop rather than assuming zero
- [ ] Confirm `%redacted` < 1% and median `input.value` back in the 400–600
      char range
- [ ] Confirm `%null_session` falls with it (same-span hypothesis)
- [ ] Run `uv run python scripts/span_size_sentinel.py --days 1` — the
      raw-request span returns with content, so bloat will regrow; this
      quantifies how fast and whether the fork patch is urgent
- [ ] Resolve §3 before closing ENG2-1510

**2026-08-03 → fix is a permanent gap** in the Phoenix content record.
Redaction happens before export, so nothing recoverable is stored. The
`sandbox_agent_events` (AIT/ACP) path is unaffected and retains tool-level
detail for that window — that is the fallback for August analysis.
