"""Typed layout: ~30 typed columns + verbatim overflow + content-addressed cold payload.

ENG2-1589's proposal, ported out of a scratch script so it runs from a clean checkout at
pinned settings against any span store.

Two design points worth keeping in view while reading the SQL:

* **Identity is stamped at ingest, not resolved at query time.** `account_uuid` and
  `session_id` live only on the `litellm_request` parent (99.9-100% there, 0% on children),
  so they are extracted once per trace and propagated. That deliberately *disagrees* with
  Phoenix, which is why "correctness is a diff against Phoenix" breaks at cutover and needs
  a two-stage oracle.
* **`thinking_*` is parsed out of a double-encoded JSON string.** That field is the one
  measured drift event in four months: `llm.invocation_parameters` went from ~90%
  unparseable to ~96% parseable JSON in August, swapping `temperature` for `thinking`.
  Structural type checks are blind to it, so these columns are exactly where a producer
  change lands as silent NULLs. Keep `attributes_rest` so the raw value survives.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from dev_agent_lens.storage.layouts.base import BuildResult, Layout
from dev_agent_lens.storage.spanstore.base import quote_literal

log = logging.getLogger(__name__)

# Tool names are producer-specific too: LiteLLM writes claude_code_tool_name, the OTLP
# path from Claude Code session JSONL writes OpenInference tool.name (found by the
# five-minute story, ENG2-1612: 5,583 Bash spans with tool_name NULL).
# Identity is producer-specific, and this is where a producer we have not met bites first.
# LiteLLM puts both ids inside a JSON string at metadata.user_api_key_end_user_id; the
# Claude Code hooks producer puts the session at claude.session_id and has no account;
# OpenInference's convention is session.id and user.id. The ENG2-1610 proof landed 3,941
# hooks spans with every one of them carrying a session id in attributes_rest and none in
# the typed column. Each path is tried in order; adding a producer here is additive.
_IDENT = """
CREATE OR REPLACE TEMP TABLE _ident_trace AS
SELECT trace_key,
        COALESCE(
          max(CASE WHEN euid LIKE '{%' THEN json_extract_string(euid,'$.account_uuid') END),
          max(account_alt), max(account_rx)) AS account_uuid,
        COALESCE(
          max(CASE WHEN euid LIKE '{%' THEN json_extract_string(euid,'$.device_id') END),
          max(device_rx)) AS device_id,
        COALESCE(
          max(CASE WHEN euid LIKE '{%' THEN json_extract_string(euid,'$.session_id') END),
          max(session_alt), max(session_rx)) AS session_id,
        mode(ticket_rx) AS ticket,
        max(agent_rx) AS agent
FROM _keys
GROUP BY 1
"""

# A trace with a session but no account (the hooks producer, a child-only trace) takes the
# account and device that any other trace of the same session carries. A session belongs
# to one person; that is the one join that closes the hooks gap without touching the
# producer.
_IDENT_SESSION = """
CREATE OR REPLACE TEMP TABLE _ident_session AS
SELECT session_id, max(account_uuid) AS account_uuid, max(device_id) AS device_id,
       mode(ticket) AS ticket
FROM _ident_trace WHERE session_id IS NOT NULL
GROUP BY 1
"""

# `person` is the only column that needs a file: a people list mapping each id a producer
# stamps (account, device, the `--user` name) to a name. Unknown ids leave it NULL, and the
# build log names them so the operator can add them.
_IDENT_FINAL = """
CREATE OR REPLACE TEMP TABLE _ident AS
SELECT t.trace_key,
       COALESCE(t.account_uuid, s.account_uuid) AS account_uuid,
       COALESCE(t.device_id, s.device_id)       AS device_id,
       t.session_id,
       COALESCE(t.ticket, s.ticket)             AS ticket,
       t.agent,
       COALESCE(pa.name, pd.name, pu.name)      AS person
FROM _ident_trace t
LEFT JOIN _ident_session s USING (session_id)
LEFT JOIN _people pa
       ON pa.kind = 'account' AND pa.key = lower(COALESCE(t.account_uuid, s.account_uuid))
LEFT JOIN _people pd
       ON pd.kind = 'device' AND pd.key = lower(COALESCE(t.device_id, s.device_id))
LEFT JOIN _people pu
       ON pu.kind = 'user' AND pu.key = lower(COALESCE(t.account_uuid, s.account_uuid))
"""

# Identity text as producers actually wrote it. Older Phoenix exports carry the request
# body under input.value with metadata as a Python repr ('user_id': '{"device_id": ...'),
# and the first proxy wrote the legacy string user_<hash>_account_<uuid>_session_<uuid>.
# The regexes read either, on root and request spans only, and only when no structured
# path answered; a child span quoting a session id in its content never counts.
_UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_RX_ACCOUNT = f"(?:account_uuid[^0-9a-f]{{1,16}}|_account_)({_UUID})"
_RX_DEVICE = "(?:device_id[^0-9a-f]{1,16}|[^a-z]user_)([0-9a-f]{64})"
_RX_SESSION = f"(?:session_id[^0-9a-f]{{1,16}}|_session_)({_UUID})"
_ROOTISH = "(parent_id IS NULL OR name IN ('litellm_request','raw_gen_ai_request'))"
# The ticket a call was working on (ENG2-1540, cost per outcome). Three places say it:
# a branch attribute the folder ingest stamps, the git branch Claude Code puts in its own
# context (`Current branch: adam/eng2-1572-...`), and the first ticket id in the opening of
# the conversation, which is the task prompt for a rollout and the person's first message
# on a laptop. Tool results later in a session quote dozens of ticket ids, so only the
# first 4,000 characters of the input count.
_RX_TICKET = "(ENG2-[0-9]{3,5})"
_RX_BRANCH_TICKET = r"Current branch: [^\n\"]{0,120}?(eng2-[0-9]{3,5})"
# Every attribute path the typed layout reads. `_prepare_raw` parses them once per row into
# the list `_j`. One json_extract per path parsed each ~262 KB `raw_gen_ai_request` attribute
# string about twenty times, and 12k live spans exhausted a 12 GB cap (2026-09-25,
# ENG2-1650). Append new paths at the end.
_PATHS = (
    "$.metadata.user_api_key_end_user_id",
    "$.claude.session_id",
    "$.session.id",
    "$.user.id",
    "$.agent.name",
    "$.git.branch",
    "$.input.value",
    "$.llm.model_name",
    "$.llm.provider",
    "$.llm.system",
    "$.llm.is_streaming",
    "$.llm.invocation_parameters",
    "$.metadata.usage_object.cache_read_input_tokens",
    "$.llm.token_count.prompt_details.cache_read",
    "$.metadata.usage_object.cache_creation_input_tokens",
    "$.llm.token_count.prompt_details.cache_write",
    "$.claude_code_tool_name",
    "$.tool.name",
    "$.metadata.requester_metadata.sandbox_id",
    "$.output.value",
    "$.llm.anthropic.messages",
    "$.llm.anthropic.tools",
    "$.llm.anthropic.system",
)


def _attr(path: str, row: str = "r.") -> str:
    """The value of one attribute path, read from `_j` (1-based) as `_raw` provides it."""
    return f"{row}_j[{_PATHS.index(path) + 1}]"


_TICKET_EXPR = (
    "upper(COALESCE("
    f"nullif(regexp_extract({_attr('$.git.branch', '')}, '(?i){_RX_TICKET}', 1), ''), "
    f"nullif(regexp_extract(attributes, '{_RX_BRANCH_TICKET}', 1), ''), "
    f"nullif(regexp_extract(substr({_attr('$.input.value', '')}, 1, 4000), "
    f"'{_RX_TICKET}', 1), '')))"
)

# invocation_parameters is double-encoded JSON, and on real corpora it is sometimes a trimmed
# marker such as "[dal_trim: ..." rather than an object. DuckDB's json_extract throws on
# malformed input even inside a CASE branch that is not taken, so the guard has to hand the
# parser NULL rather than skip it. Found by the ENG2-1609 rehearsal on the dev-agent-lens
# project: 2 of 36,052 spans, enough to fail the whole build.
_INV_RAW = _attr("$.llm.invocation_parameters")
_INV = f"(CASE WHEN {_INV_RAW} LIKE '{{%' THEN {_INV_RAW} END)"

# What a tool call DID, the same word for every harness (ENG2-402), so "how often does the
# agent run shell commands" means the same thing for Claude Code (`Bash`) and Codex
# (`exec_command`). Names not listed keep their own name, lowercased; MCP tools collapse to
# `mcp`. Add a harness by adding its names here.
_TOOL_NAME = f"COALESCE({_attr('$.claude_code_tool_name')}, {_attr('$.tool.name')})"
_TOOL_KIND = f"""CASE
    WHEN nullif({_TOOL_NAME}, '') IS NULL THEN NULL
    WHEN {_TOOL_NAME} IN ('Bash', 'bash', 'BashOutput', 'KillShell', 'TaskOutput', 'TaskStop',
                          'Monitor', 'exec_command', 'shell', 'local_shell_call',
                          'write_stdin', 'container.exec') THEN 'shell'
    WHEN {_TOOL_NAME} IN ('Edit', 'MultiEdit', 'Write', 'NotebookEdit', 'apply_patch') THEN 'edit'
    WHEN {_TOOL_NAME} IN ('Read', 'view_image') THEN 'read'
    WHEN {_TOOL_NAME} IN ('Grep', 'Glob', 'LS') THEN 'search'
    WHEN {_TOOL_NAME} IN ('WebSearch', 'web_search') THEN 'web_search'
    WHEN {_TOOL_NAME} IN ('WebFetch') THEN 'web_fetch'
    WHEN {_TOOL_NAME} IN ('Task', 'Agent', 'spawn_agent') THEN 'subagent'
    WHEN {_TOOL_NAME} IN ('TodoWrite', 'update_plan', 'ExitPlanMode', 'EnterPlanMode',
                          'TaskCreate', 'TaskUpdate', 'TaskList', 'TaskGet') THEN 'plan'
    WHEN {_TOOL_NAME} IN ('AskUserQuestion', 'request_user_input') THEN 'ask_user'
    WHEN {_TOOL_NAME} IN ('Skill') THEN 'skill'
    WHEN {_TOOL_NAME} IN ('ToolSearch') THEN 'tool_search'
    WHEN {_TOOL_NAME} IN ('CronCreate', 'CronDelete', 'CronList') THEN 'schedule'
    WHEN starts_with({_TOOL_NAME}, 'mcp__') OR ends_with({_TOOL_NAME}, '(MCP)')
         OR {_TOOL_NAME} IN ('ListMcpResourcesTool', 'ReadMcpResourceTool') THEN 'mcp'
    ELSE lower({_TOOL_NAME})
  END"""

_TYPED_SELECT = f"""
SELECT
  r.span_id, r.trace_key AS trace_id, r.parent_id AS parent_span_id,
  r.name, r.span_kind, r.start_time, r.end_time,
  CAST(date_diff('microsecond', r.start_time, r.end_time)/1000.0 AS DOUBLE) AS duration_ms,
  r.status_code,
  i.account_uuid, i.device_id, i.session_id, i.person, i.ticket,
  -- The harness that wrote the session (`claude-code`, `codex`), from the ATIF root
  -- span's agent.name (ENG2-402). NULL for proxy-captured traffic, which has no root
  -- agent span; that traffic is Claude Code today.
  i.agent,
  {_attr('$.llm.model_name')}  AS model_name,
  {_attr('$.llm.provider')}    AS provider,
  {_attr('$.llm.system')}      AS llm_system,
  (lower({_attr('$.llm.is_streaming')})='true') AS is_streaming,
  json_extract_string({_INV},'$.thinking.type')                        AS thinking_type,
   TRY_CAST(json_extract_string({_INV},'$.thinking.budget_tokens') AS INTEGER)
       AS thinking_budget_tokens,
  json_extract_string({_INV},'$.thinking.display')                     AS thinking_display,
  TRY_CAST(json_extract_string({_INV},'$.max_tokens') AS INTEGER)      AS max_tokens,
  r.llm_token_count_prompt      AS tokens_prompt,
  r.llm_token_count_completion  AS tokens_completion,
   -- The proxy writes LiteLLM's usage_object; the session ingest (ATIF, Claude or Codex)
   -- writes the OpenInference key. Either counts (ENG2-402).
   COALESCE(
     TRY_CAST({_attr('$.metadata.usage_object.cache_read_input_tokens')} AS BIGINT),
     TRY_CAST({_attr('$.llm.token_count.prompt_details.cache_read')} AS BIGINT))
       AS tokens_cache_read,
   COALESCE(
     TRY_CAST({_attr('$.metadata.usage_object.cache_creation_input_tokens')} AS BIGINT),
     TRY_CAST({_attr('$.llm.token_count.prompt_details.cache_write')} AS BIGINT))
       AS tokens_cache_write,
  {_TOOL_NAME}                                                AS tool_name,
  {_TOOL_KIND}                                                AS tool_kind,
  {_attr('$.metadata.requester_metadata.sandbox_id')} AS sandbox_id,
  {_attr('$.input.value')}  AS input_value,
  {_attr('$.output.value')} AS output_value,
  -- md5(NULL) is NULL, so a span without `llm.anthropic` gets no refs. The former
  -- `CASE WHEN json_extract(..., '$.llm.anthropic') IS NOT NULL` guard added nothing but a
  -- fourth parse of each payload.
  md5({_attr('$.llm.anthropic.messages')}) AS payload_messages_ref,
  md5({_attr('$.llm.anthropic.tools')})    AS payload_tools_ref,
  md5({_attr('$.llm.anthropic.system')})   AS payload_system_ref,
  dal_attributes_rest(r.attributes) AS attributes_rest,
  r.events,
  __SOURCE__ AS source,
  r.day
FROM _raw r LEFT JOIN _ident i USING (trace_key)
"""
# `source` is the DAL trace source a row was synced from, and it is the only per-project
# scoping the store has: Phoenix's project name is not on the span. The ENG2-1609 rehearsal
# found the typed select dropped it, so a store holding sf-workspaces plus the dead
# dev-agent-lens project could not scope typed queries per project, and the cookbook already
# documents that blending those two flips the autonomy headline. Rows landed before the
# stamp existed, and fixtures that never had one, get NULL rather than a failed build.

# One scan of `_raw`: the three payloads come from the attributes parsed once into `_j`.
_BLOB_ROWS = f"""
  SELECT day, md5(body) AS ref, kind, body FROM (
    SELECT day,
           unnest(['messages', 'tools', 'system']) AS kind,
           unnest([{_attr('$.llm.anthropic.messages', '')},
                   {_attr('$.llm.anthropic.tools', '')},
                   {_attr('$.llm.anthropic.system', '')}]) AS body
    FROM _raw)
  WHERE body IS NOT NULL
"""

# One row per ref, written in the month it first appears. `_refs` is (ref, first day):
# small, and what lets the blob write go month by month without repeating a blob.
_REFS = (
    "CREATE OR REPLACE TEMP TABLE _refs AS "
    f"SELECT ref, min(day) AS first_day FROM ({_BLOB_ROWS}) GROUP BY ref"
)

_BLOBS_MONTH = f"""
SELECT b.ref, any_value(b.kind) AS kind, any_value(b.body) AS body
FROM ({_BLOB_ROWS}) b JOIN _refs f ON f.ref = b.ref AND f.first_day = b.day
WHERE b.day >= DATE '__LO__' AND b.day < DATE '__HI__'
GROUP BY b.ref
"""


def _windows(lo: Any, hi: Any, days: int) -> list[tuple[Any, Any]]:
    """Half-open [lo, hi) day windows covering lo..hi inclusive."""
    import datetime as _dt

    if lo is None or hi is None:
        return []
    out, step = [], _dt.timedelta(days=max(1, days))
    cur = lo
    while cur <= hi:
        out.append((cur, cur + step))
        cur = cur + step
    return out


_REST_PATCH = '{"llm":{"anthropic":null}}'


def attributes_rest(attributes: str | None) -> str | None:
    """A span's attributes without `llm.anthropic`, whose payloads go to the blobs table.

    Same result as DuckDB's `json_merge_patch(attributes, '{"llm":{"anthropic":null}}')`:
    `llm` becomes `{}` when it is missing or not an object, and a non-object document
    returns the patch itself. Numbers keep their value, but Python may write a float in a
    different form (`1.5e-05` rather than `0.000015`). Malformed JSON raises, as the
    DuckDB function does.

    This runs in Python because json_merge_patch builds two mutable trees for every row
    of a 2,048-row vector. With ~262 KB `raw_gen_ai_request` attributes, that exhausted a
    12 GB cap on 12k live spans even on one thread (2026-09-25, ENG2-1650). Here each
    row's tree is freed before the next one is parsed.
    """
    import json

    if attributes is None:
        return None
    doc = json.loads(attributes)
    if not isinstance(doc, dict):
        return _REST_PATCH
    llm = doc.get("llm")
    if isinstance(llm, dict):
        llm.pop("anthropic", None)
    else:
        doc["llm"] = {}
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False)


def _register_functions(con: Any) -> None:
    """Register `dal_attributes_rest` on a connection, once."""
    import duckdb
    import pyarrow as pa

    def rest(values: Any) -> Any:
        return pa.array((attributes_rest(v.as_py()) for v in values), type=pa.string())

    try:
        con.remove_function("dal_attributes_rest")
    except Exception:  # noqa: BLE001 - not registered yet
        pass
    con.create_function(
        "dal_attributes_rest", rest, [duckdb.sqltype("VARCHAR")], duckdb.sqltype("VARCHAR"),
        type="arrow", null_handling="special", side_effects=False,
    )


def _bound_memory(con: Any) -> None:
    """Cap DuckDB at DAL_DUCKDB_MEMORY (default: half of RAM) and give it a spill
    directory, so a large build slows down instead of eating the machine."""
    import os
    import tempfile

    limit = os.environ.get("DAL_DUCKDB_MEMORY", "").strip()
    if not limit:
        try:
            total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
            limit = f"{max(1, int(total / 2 / 1e9))}GB"
        except (ValueError, OSError, AttributeError):
            limit = "8GB"
    tmp = os.environ.get("DAL_DUCKDB_TMP") or os.path.join(tempfile.gettempdir(), "dal-duckdb")
    os.makedirs(tmp, exist_ok=True)
    con.execute(f"SET memory_limit='{limit}'")
    # Incremental dependency scans may already have spilled on this connection.
    # DuckDB cannot relocate an active spill directory; keep the configured one.
    if con.execute("SELECT current_setting('temp_directory')").fetchone()[0] != tmp:
        con.execute(f"SET temp_directory={quote_literal(tmp)}")
    con.execute("SET preserve_insertion_order=false")
    log.info("[layout:typed] duckdb memory_limit=%s temp=%s", limit, tmp)


def identity_path() -> str:
    """Where the people file is: DAL_IDENTITY, else identity.yaml next to the DAL config,
    else the package's own identity.yaml (the one core/identity.py reads)."""
    from dev_agent_lens.config import get_config_path
    from dev_agent_lens.core.identity import IDENTITY_FILE

    if os.environ.get("DAL_IDENTITY"):
        return os.environ["DAL_IDENTITY"]
    beside = get_config_path().parent / "identity.yaml"
    return str(beside if beside.exists() else IDENTITY_FILE)


def _load_people(path: str | None = None) -> list[tuple[str, str, str]]:
    """Read identity.yaml into (kind, key, name) rows.

    Same file core/identity.py reads: a `people` list, each with `name`, `email` and
    `accounts: [{uuid: ...}]`. Two optional lists extend it for producers that carry no
    account: `devices` (the 64-hex device hash Claude Code sends) and `users` (the name
    given to `dal ingest-sessions --user`). Each person's `email` also counts as a user id.
    Keys are lowercased. A missing file is not an
    error: every span then has person NULL and the build log lists the ids it saw. A
    malformed file is logged and treated as empty rather than failing a build that took an
    hour, for the same reason.
    """
    import yaml

    path = path or identity_path()
    if not os.path.exists(path):
        log.info("[layout:typed] no identity file at %s; person stays NULL", path)
        return []
    try:
        with open(path) as fh:
            doc = yaml.safe_load(fh) or {}
    except Exception as exc:  # noqa: BLE001 - a bad file must not kill a build
        log.warning("[layout:typed] identity file %s unreadable (%s); person stays NULL", path, exc)
        return []
    rows: list[tuple[str, str, str]] = []
    for entry in doc.get("people") or []:
        name = str(entry.get("name") or entry.get("email") or "").strip()
        if not name:
            continue
        for acct in entry.get("accounts") or []:
            uuid = acct.get("uuid") if isinstance(acct, dict) else acct
            if uuid:
                rows.append(("account", str(uuid).strip().lower(), name))
        for kind, key in (("device", "devices"), ("user", "users")):
            ids = entry.get(key) or []
            if isinstance(ids, str):
                ids = [ids]
            rows.extend((kind, str(v).strip().lower(), name) for v in ids if str(v).strip())
        # A person's email is also a user id: `dal ingest-sessions --agent codex` stamps the
        # laptop's git email when --user is not given, because Codex session files carry no
        # account id (ENG2-402).
        email = str(entry.get("email") or "").strip().lower()
        if email:
            rows.append(("user", email, name))
    log.info(
        "[layout:typed] identity file %s: %d ids for %d people",
        path,
        len(rows),
        len({r[2] for r in rows}),
    )
    return rows


def _prepare_raw(con: Any, source_glob: str | list[str]) -> tuple[str, set[str]]:
    """Shared producer parsing for typed identity and incremental dependency tracking."""
    src = (
        "[" + ",".join(quote_literal(p) for p in source_glob) + "]"
        if isinstance(source_glob, list) else quote_literal(source_glob)
    )
    source_sql = f"read_parquet({src}, hive_partitioning=true, union_by_name=true)"
    cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {source_sql}").fetchall()}
    day_expr = "" if "day" in cols else ", CAST(start_time AS DATE) AS day"
    # The fixture (a phoenix.spans dump) carries the integer trace_rowid; the direct
    # clients carry the string trace_id and no rowid. Identity is keyed on whichever
    # exists, as text, so both shapes stamp the same way.
    if {"trace_rowid", "trace_id"} <= cols:
        trace_key = "COALESCE(CAST(trace_rowid AS VARCHAR), trace_id)"
    elif "trace_rowid" in cols:
        trace_key = "CAST(trace_rowid AS VARCHAR)"
    elif "trace_id" in cols:
        trace_key = "trace_id"
    else:
        raise ValueError("source has neither trace_rowid nor trace_id")
    # Memory is bounded per 2,048-row vector, and live spans carry attributes of up to
    # ~262 KB (`raw_gen_ai_request`). Two things made that exhaust a 12 GB cap on 12k live
    # spans (2026-09-25, ENG2-1650): seven separate json_extract calls each parsed every
    # attribute string, and `CASE WHEN rootish` around the ticket expression narrowed
    # vectors to exactly those large root rows while extracting `input.value`. Parsing
    # the paths once into `_j`, and gating only the small ticket result, keeps the same
    # output within 2.5 GB.
    paths = ", ".join(quote_literal(p) for p in _PATHS)
    con.execute(f"""CREATE OR REPLACE TEMP VIEW _raw AS
        SELECT * EXCLUDE (_rootish, _ticket_any),
               CASE WHEN _rootish THEN _ticket_any END AS ticket_rx
        FROM (
        SELECT *,
               {_attr('$.metadata.user_api_key_end_user_id', '')} AS euid,
               COALESCE({_attr('$.claude.session_id', '')},
                        {_attr('$.session.id', '')}) AS session_alt,
               {_attr('$.user.id', '')} AS account_alt,
               CASE WHEN {_ROOTISH}
                    THEN nullif(regexp_extract(attributes, '{_RX_ACCOUNT}', 1), '')
               END AS account_rx,
               CASE WHEN {_ROOTISH}
                    THEN nullif(regexp_extract(attributes, '{_RX_DEVICE}', 1), '')
               END AS device_rx,
               CASE WHEN {_ROOTISH}
                    THEN nullif(regexp_extract(attributes, '{_RX_SESSION}', 1), '')
               END AS session_rx,
               {_ROOTISH} AS _rootish,
               {_TICKET_EXPR} AS _ticket_any,
               CASE WHEN parent_id IS NULL THEN {_attr('$.agent.name', '')} END AS agent_rx
        FROM (
        SELECT *{day_expr}, {trace_key} AS trace_key,
               json_extract_string(attributes, [{paths}]) AS _j
        FROM {source_sql}))""")
    return source_sql, cols


class TypedLayout(Layout):
    name = "typed"

    def build(
        self, con: Any, source_glob: str | list[str], store: Any, *,
        zstd_level: int, deterministic: bool = True,
        identity_files: list[str] | None = None,
    ) -> BuildResult:
        from dev_agent_lens.storage.snapshots import SnapshotIO
        if SnapshotIO(store).read_current()[0] is not None:
            raise ValueError("published typed snapshot exists; use dal store rebuild --full")
        t0 = time.perf_counter()
        store.attach_duckdb(con)
        prior = None
        try:
            # DAL_BUILD_DETERMINISTIC=0 keeps every core busy at the cost of a
            # byte-identical layout; the content is the same either way. Set it for a
            # one-off rebuild of a big store on a big box.
            prior = con.execute("SELECT current_setting('threads')").fetchone()[0]
            if deterministic and os.environ.get("DAL_BUILD_DETERMINISTIC", "1") != "0":
                con.execute("SET threads=1")
            else:
                # Memory scales with threads x open partitions; 48 threads on lambda1
                # blew a 120 GB cap on one month. Eight is plenty for a scan-bound job.
                con.execute(f"SET threads={int(os.environ.get('DAL_BUILD_THREADS', '8'))}")
            _bound_memory(con)
            _register_functions(con)

            # `_raw` is a VIEW, not a table. Materializing the raw set held every
            # attribute string in memory: 16.5M spans on lambda1 grew past 190 GB and
            # would have been killed (2026-09-11). Each pass now streams from Parquet;
            # only the small identity columns are materialized, in `_keys`.
            ts = time.perf_counter()
            source_sql, cols = _prepare_raw(con, source_glob)
            from .incremental import parquet_sql
            identity_source = parquet_sql(identity_files) if identity_files else "_raw"
            con.execute(f"""CREATE OR REPLACE TEMP TABLE _keys AS
                SELECT trace_key, euid, session_alt, account_alt, account_rx, device_rx,
                       session_rx, ticket_rx, agent_rx
                FROM {identity_source}
                WHERE euid IS NOT NULL OR session_alt IS NOT NULL OR account_alt IS NOT NULL
                   OR account_rx IS NOT NULL OR device_rx IS NOT NULL OR session_rx IS NOT NULL
                   OR ticket_rx IS NOT NULL OR agent_rx IS NOT NULL""")
            rows = con.execute(f"SELECT count(*) FROM {source_sql}").fetchone()[0]
            log.info(
                "[layout:typed] scanned %d rows for identity in %.1fs",
                rows,
                time.perf_counter() - ts,
            )

            ts = time.perf_counter()
            people = _load_people()
            con.execute(
                "CREATE OR REPLACE TEMP TABLE _people (kind VARCHAR, key VARCHAR, name VARCHAR)"
            )
            if people:
                con.executemany("INSERT INTO _people VALUES (?, ?, ?)", people)
            con.execute(_IDENT)
            con.execute(_IDENT_SESSION)
            con.execute(_IDENT_FINAL)
            traces = con.execute("SELECT count(*) FROM _ident").fetchone()[0]
            stamped = con.execute(
                "SELECT count(*) FROM _raw r JOIN _ident i USING (trace_key) "
                "WHERE i.account_uuid IS NOT NULL"
            ).fetchone()[0]
            multi = con.execute(
                "SELECT count(*) FROM (SELECT trace_key FROM _keys WHERE euid IS NOT NULL "
                "GROUP BY 1 HAVING count(DISTINCT euid) > 1)"
            ).fetchone()[0]
            if multi:
                log.warning(
                    "[layout:typed] %d trace(s) carry conflicting identities; max() picked one",
                    multi,
                )
            log.info(
                "[layout:typed] identity stamped traces=%d spans=%d (%.1f%%) in %.1fs",
                traces,
                stamped,
                100.0 * stamped / max(rows, 1),
                time.perf_counter() - ts,
            )
            named = con.execute(
                "SELECT count(*) FROM _raw r JOIN _ident i USING (trace_key) "
                "WHERE i.person IS NOT NULL"
            ).fetchone()[0]
            unknown = con.execute(
                "SELECT account_uuid, device_id, count(*) AS n "
                "FROM _raw r JOIN _ident i USING (trace_key) "
                "WHERE i.person IS NULL "
                "AND (i.account_uuid IS NOT NULL OR i.device_id IS NOT NULL) "
                "GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 10"
            ).fetchall()
            log.info(
                "[layout:typed] person named on spans=%d (%.1f%%) from %d people; "
                "%d unnamed ids listed below",
                named,
                100.0 * named / max(rows, 1),
                len({n for _, _, n in people}),
                len(unknown),
            )
            ticketed = con.execute(
                "SELECT count(*) FROM _raw r JOIN _ident i USING (trace_key) "
                "WHERE i.ticket IS NOT NULL"
            ).fetchone()[0]
            log.info(
                "[layout:typed] ticket stamped on spans=%d (%.1f%%)",
                ticketed,
                100.0 * ticketed / max(rows, 1),
            )
            for account, device, n in unknown:
                log.info(
                    "[layout:typed]   unnamed account=%s device=%s spans=%d", account, device, n
                )

            store.clear("spans_typed")
            store.clear("blobs_typed")
            hot = quote_literal(store.write_target("spans_typed"))
            cold = quote_literal(store.write_target("blobs_typed"))
            typed_select = _TYPED_SELECT.replace(
                "__SOURCE__", "r.source" if "source" in cols else "NULL::VARCHAR"
            )
            # One window of days per COPY (DAL_BUILD_CHUNK_DAYS, default 7). A single
            # partitioned write over every day of a large store pins a block per
            # partition per thread and ran out of memory inside its own cap on lambda1
            # (16.5M spans, 111.7 GiB used); a month was still too much for one source
            # that wrote 16 GB in a week. The working set is one window's rows.
            lo_day, hi_day = con.execute("SELECT min(day), max(day) FROM _raw").fetchone()
            chunk = int(os.environ.get("DAL_BUILD_CHUNK_DAYS", "7"))
            windows = _windows(lo_day, hi_day, chunk)
            ts = time.perf_counter()
            con.execute(_REFS)
            log.info(
                "[layout:typed] %d windows of %d days, %d distinct blobs, refs in %.1fs",
                len(windows),
                chunk,
                con.execute("SELECT count(*) FROM _refs").fetchone()[0],
                time.perf_counter() - ts,
            )
            # Rows carry whole prompts and payloads. The default 122,880-row group buffers a
            # day of them per partition before the first flush; a byte-sized group keeps the
            # writer's buffer bounded (ENG2-1650). Row grouping is layout, not content.
            opts = (
                f"FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL {int(zstd_level)}, "
                f"ROW_GROUP_SIZE_BYTES '{os.environ.get('DAL_ROW_GROUP_BYTES', '64MB')}', "
                "OVERWRITE_OR_IGNORE"
            )
            t_hot = t_cold = 0.0
            for lo, hi in windows:
                tag = quote_literal(f"{lo:%Y%m%d}_{{i}}")
                ts = time.perf_counter()
                con.execute(
                    f"""COPY ({typed_select} WHERE r.day >= DATE '{lo}' AND r.day < DATE '{hi}')
                        TO {hot} ({opts}, PARTITION_BY (day), FILENAME_PATTERN {tag})"""
                )
                t_hot += time.perf_counter() - ts
                ts = time.perf_counter()
                blobs_sql = _BLOBS_MONTH.replace("__LO__", str(lo)).replace("__HI__", str(hi))
                con.execute(
                    f"""COPY ({blobs_sql}) TO {cold}
                        ({opts}, PARTITION_BY (kind), FILENAME_PATTERN {tag})"""
                )
                t_cold += time.perf_counter() - ts
                log.debug("[layout:typed] window %s..%s written", lo, hi)
            log.info("[layout:typed] wrote hot in %.1fs, blobs in %.1fs", t_hot, t_cold)
        finally:
            if prior is not None:
                con.execute(f"SET threads={int(prior)}")
            for t in ("_ident", "_ident_session", "_ident_trace", "_people"):
                con.execute(f"DROP TABLE IF EXISTS {t}")
            con.execute("DROP TABLE IF EXISTS _keys")
            con.execute("DROP TABLE IF EXISTS _refs")
            con.execute("DROP VIEW IF EXISTS _raw")
        bh, bc = store.size_bytes("spans_typed"), store.size_bytes("blobs_typed")
        r = BuildResult(
            layout=self.name,
            rows=rows,
            bytes_hot=bh,
            bytes_cold=bc,
            elapsed_ms=(time.perf_counter() - t0) * 1000,
            zstd_level=zstd_level,
            detail={
                "traces": traces,
                "spans_with_identity": stamped,
                "spans_with_person": named,
                "spans_with_ticket": ticketed,
                "identity_pct": round(100.0 * stamped / max(rows, 1), 1),
            },
        )
        log.info(
            "[layout:typed] built rows=%d hot=%.1fMB cold=%.1fMB level=%d in %.1fs",
            rows,
            bh / 1e6,
            bc / 1e6,
            zstd_level,
            r.elapsed_ms / 1000,
        )
        return r

    def update(self, con: Any, store: Any, *, zstd_level: int, full: bool = False) -> BuildResult:
        from .incremental import update
        return update(self, con, store, zstd_level=zstd_level, full=full)

    def attach(self, con: Any, store: Any) -> None:
        from dev_agent_lens.linear_tickets import attach_tickets

        store.attach_duckdb(con)
        for v in ("spans", "blobs"):
            con.execute(f"DROP VIEW IF EXISTS {v}")
        # The tracker's tickets (dal linear-sync), joinable on spans.ticket; an empty
        # view with the same columns when nobody has synced them (ENG2-1540).
        attach_tickets(con, store)
        from dev_agent_lens.storage.snapshots import SnapshotIO

        from .incremental import attach

        manifest, _ = SnapshotIO(store).read_current()
        if manifest is not None:
            attach(con, manifest)
            return
        con.execute(f"""CREATE VIEW spans AS SELECT * FROM read_parquet(
            {quote_literal(store.read_glob("spans_typed"))},
            hive_partitioning=true, union_by_name=true)""")
        if store.size_bytes("blobs_typed") == 0:
            # a source with no llm.anthropic payloads writes zero blob files, and a glob
            # over nothing is a binder error rather than an empty table
            con.execute(
                "CREATE VIEW blobs AS SELECT NULL::VARCHAR AS ref, NULL::VARCHAR AS kind, "
                "NULL::VARCHAR AS body WHERE false"
            )
        else:
            con.execute(f"""CREATE VIEW blobs AS SELECT * FROM read_parquet(
                {quote_literal(store.read_glob("blobs_typed"))},
                hive_partitioning=true, union_by_name=true)""")
