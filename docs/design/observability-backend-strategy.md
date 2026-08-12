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
the median input length observed during the outage).

**`session_id` was not affected.** It rides on `metadata.requester_metadata`
and is still present on `litellm_request` spans at 99.5%, unchanged from before
08-03. An earlier reading of this outage claimed `%redacted` and
`%null_session` were the same spans — they are not. `%null_session` rose only
because two span types that never carried `session_id`
(`Claude_Code_Final_Output_0`, `Claude_Code_Internal_Prompt_0`) first appear on
08-03 and are now two-thirds of all volume. That is dilution, not loss. Both
rates land near 66% because each of the three span types emitted per call is
roughly a third of the total, so any two-of-three subset does.

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

## 3. Resolved: there was no surviving-content population

The "~34% that kept content" was an artifact. Splitting by span name and
separating "no input field" from "real content" (24h window, 2026-08-12):

| span name | n | no input field | redacted | real content |
| -- | --: | --: | --: | --: |
| `litellm_request` | 2516 | 0 | 2448 | **68** |
| `Claude_Code_Final_Output_0` | 2448 | 2448 | 0 | 0 |
| `Claude_Code_Internal_Prompt_0` | 2448 | 0 | 2448 | 0 |

`Claude_Code_Final_Output_0` is an output span with **no `input.value` field at
all**. It scored as "not redacted" only because the marker string cannot appear
in a field that does not exist — not because content survived.

So of spans that actually carry an input field, **98.6% were redacted**, and
only **68 spans in 24 hours** held real content. ENG2-1510's original headline
figure of ~98% was correct; the ~66%/~34% split that appeared mid-investigation
was computed over a denominator that included a span type with no input field,
and should be disregarded.

The blackout was total, not partial. Nothing about §3 blocks closing ENG2-1510.

Discriminating query, kept for re-verification after deploy:

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

Post-deploy, `real_content` should dominate `litellm_request` and
`Claude_Code_Internal_Prompt_0`; `Claude_Code_Final_Output_0` stays `no_input`
either way, as it has no input field by construction.

## 4. Post-deploy verification (not yet run)

Config-only change; requires an `sf-litellm` deploy, which is gated on Adam.

- [ ] Deploy `sf-litellm` and confirm the running config has no
      `callback_settings.arize_phoenix.message_logging` key
- [ ] Burst reconciliation (ENG2-1494): send N tagged requests, confirm N spans
      land — quantifies any gRPC drop rather than assuming zero
- [ ] Confirm `%redacted` < 1% and median `input.value` back in the 400–600
      char range
- [ ] Confirm real content on `litellm_request` and
      `Claude_Code_Internal_Prompt_0` (not `%null_session`, which is unrelated
      to this bug — see §2)
- [ ] Run `uv run python scripts/span_size_sentinel.py --days 1` — the
      raw-request span returns with content, so bloat will regrow; this
      quantifies how fast and whether the fork patch is urgent
- [ ] Add an inverted check alongside the size sentinel: it only alarms when
      spans grow, so this outage — which made spans *smaller* — scored as a win
      and ran 9 days unnoticed. Needs a content-presence floor
      (`%redacted < 5%`) to catch the next one. Covers ENG2-1510's existing
      "add a monitor" acceptance criterion.

**2026-08-03 → fix is a permanent gap** in the Phoenix content record.
Redaction happens before export, so nothing recoverable is stored. The
`sandbox_agent_events` (AIT/ACP) path is unaffected and retains tool-level
detail for that window — that is the fallback for August analysis.
