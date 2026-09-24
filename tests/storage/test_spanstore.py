"""Span-store backend tests.

These run without Docker. The cross-backend parity check that actually proves the
"flexible object storage backend" claim needs live servers and lives in
`scripts/verify_spanstore.py`; this file covers the parts that can regress silently.
"""

from __future__ import annotations

import pytest

from dev_agent_lens.storage.spanstore import LocalSpanStore, S3SpanStore, open_store


class TestUriResolution:
    def test_file_uri(self):
        assert isinstance(open_store("file:///tmp/x"), LocalSpanStore)

    def test_bare_path_is_local(self):
        assert isinstance(open_store("/tmp/x"), LocalSpanStore)

    def test_s3_and_aliases(self):
        for uri in ("s3://bkt/p", "s3a://bkt/p", "minio://bkt/p?endpoint=127.0.0.1:9000&tls=0"):
            assert isinstance(open_store(uri), S3SpanStore), uri

    def test_unknown_scheme_names_itself(self):
        with pytest.raises(ValueError, match="no span-store backend"):
            open_store("hdfs://nope/x")

    def test_globs_are_hive_partitioned_parquet(self):
        assert open_store("/tmp/x").read_glob().endswith("/spans/**/*.parquet")
        assert open_store("s3://bkt/p").read_glob() == "s3://bkt/p/spans/**/*.parquet"

    def test_dataset_name_is_honoured(self):
        assert "/events/" in open_store("/tmp/x").read_glob("events")


class TestS3Settings:
    def test_endpoint_selects_path_style_and_plain_http(self):
        s = open_store("s3://bkt/p?endpoint=127.0.0.1:9000&tls=0").s
        assert (s.endpoint, s.url_style, s.use_ssl) == ("127.0.0.1:9000", "path", False)

    def test_aws_defaults_to_vhost_and_tls(self):
        s = open_store("s3://bkt/p").s
        assert s.endpoint is None and s.url_style == "vhost" and s.use_ssl is True

    def test_settings_carry_no_credential_fields(self):
        """Credentials are resolved at attach time from the boto3 chain, never parsed from
        the URI and never stored on the settings object that gets logged and repr'd."""
        s = open_store("s3://bkt/p?endpoint=h:1&tls=0").s
        assert not any(k for k in vars(s) if "key" in k or "secret" in k or "token" in k)


class TestLocalBackend:
    def test_probe_and_ensure(self, tmp_path):
        store = open_store(f"file://{tmp_path}/store")
        store.ensure()
        h = store.probe()
        assert h.ok and h.writable and h.readable

    def test_size_of_empty_store_is_zero(self, tmp_path):
        assert open_store(f"file://{tmp_path}/s").size_bytes() == 0

    def test_capabilities_are_airgap_ready(self, tmp_path):
        c = open_store(f"file://{tmp_path}/s").capabilities()
        assert c.airgap_ready and not c.needs_network
        assert c.needs_duckdb_extension is None


class TestAirgapHonesty:
    """`airgap_ready` must reflect a real load, not the presence of an env var.

    The first version of this reported "air-gap ok" for a vendored extension DuckDB was
    rejecting on every query, because it only checked that the path existed.
    """

    def _reset_cache(self):
        import dev_agent_lens.storage.spanstore.s3 as m

        m._VENDORED_OK.clear()

    def test_missing_path_is_not_airgap_ready(self, monkeypatch):
        monkeypatch.delenv("DAL_DUCKDB_HTTPFS_PATH", raising=False)
        self._reset_cache()
        assert open_store("s3://bkt/p").capabilities().airgap_ready is False

    def test_nonloadable_file_is_not_airgap_ready(self, monkeypatch, tmp_path):
        bogus = tmp_path / "httpfs.duckdb_extension"
        bogus.write_bytes(b"not an extension")
        monkeypatch.setenv("DAL_DUCKDB_HTTPFS_PATH", str(bogus))
        self._reset_cache()
        caps = open_store("s3://bkt/p").capabilities()
        assert caps.airgap_ready is False
        # the notes must name both ways a vendored file fails to load
        assert "patch version" in caps.notes and "httpfs.duckdb_extension" in caps.notes
        self._reset_cache()


class TestWriteSettingsArePinned:
    """Compression level dominates format choice; leaving it to the writer makes size
    numbers meaningless. Measured spread was 17x on identical data."""

    def test_default_level_is_pinned_not_writer_default(self):
        from dev_agent_lens.storage.spanstore.base import DEFAULT_ZSTD_LEVEL

        assert DEFAULT_ZSTD_LEVEL == 9, "changing this changes every published size figure"

    def test_copy_from_glob_is_deterministic_by_default(self):
        """Opt-in reproducibility is the same trap as an unpinned level, one layer down.

        A caller that forgets produces size figures nobody can reproduce, and ADR 0003
        section 10 needs digests to survive a rewrite for the manifest to be checkable.
        """
        import inspect

        from dev_agent_lens.storage.spanstore.base import SpanStore

        sig = inspect.signature(SpanStore.copy_from_glob)
        assert sig.parameters["deterministic"].default is True, (
            "deterministic must default on; opt out explicitly when bulk-loading"
        )

    def test_exporter_writes_the_configured_level_not_pyarrows(self):
        """The export path had the defect the store path was fixed for.

        `ParquetExporter` took no level, so every file under ~/.dal/data/parquet/ was
        pyarrow's default of 1 while the store copy beside it was 9. Those exports are
        what `open_export()` reads and what anyone would measure.
        """
        from dev_agent_lens.config import get_zstd_level
        from dev_agent_lens.export.parquet import ParquetExporter

        assert ParquetExporter().compression_level == get_zstd_level()
        assert ParquetExporter(compression_level=3).compression_level == 3
        # snappy takes no level; passing one to pyarrow raises
        assert ParquetExporter(compression="snappy").compression_level is None
        assert ParquetExporter(compression="none").compression_level is None


class TestAppendFrame:
    """`dal sync` lands each batch through this. It must append, never clear, and must
    accept both shapes a source can hand it."""

    def _batch(self, day, n, prefix, *, direct=True):
        import pandas as pd

        ids = [f"{prefix}{i}" for i in range(n)]
        base = {
            "parent_id": [None] * n,
            "name": ["litellm_request"] * n,
            "span_kind": ["LLM"] * n,
            "start_time": pd.to_datetime([f"{day}T10:00:{i:02d}Z" for i in range(n)]),
            "end_time": pd.to_datetime([f"{day}T10:00:{i:02d}Z" for i in range(n)]),
            "status_code": ["OK"] * n,
            "status_message": [""] * n,
            "events": ["[]"] * n,
            "cumulative_error_count": [0] * n,
            "cumulative_llm_token_count_prompt": [1] * n,
            "cumulative_llm_token_count_completion": [1] * n,
            "llm_token_count_prompt": [1] * n,
            "llm_token_count_completion": [1] * n,
        }
        if direct:  # PhoenixPostgresClient / PhoenixSQLiteClient shape
            base.update(
                {
                    "context.span_id": ids,
                    "context.trace_id": [f"t-{prefix}"] * n,
                    "attributes": ['{"llm":{"model_name":"m"},"metadata":{"k":1}}'] * n,
                }
            )
        else:  # REST client shape: attributes flattened into columns
            base.update(
                {
                    "context.span_id": ids,
                    "context.trace_id": [f"t-{prefix}"] * n,
                    "attributes.llm.model_name": ["m"] * n,
                    "attributes.metadata.k": [1] * n,
                }
            )
        return pd.DataFrame(base)

    def test_two_batches_append_and_do_not_clear(self, tmp_path):
        import duckdb

        store = open_store(f"file://{tmp_path}/s")
        store.ensure()
        con = duckdb.connect()
        assert store.append_frame(con, self._batch("2026-06-01", 3, "a"), "spans_raw") == 3
        assert (
            store.append_frame(con, self._batch("2026-06-01", 2, "b"), "spans_raw") == 2
        )  # same day
        assert store.append_frame(con, self._batch("2026-06-02", 4, "c"), "spans_raw") == 4
        files = sorted(p.name for p in (tmp_path / "s" / "spans_raw").rglob("*.parquet"))
        assert len(files) == 3 and all(f.startswith("sync_") for f in files)
        n, days = con.execute(
            f"SELECT count(*), count(DISTINCT day) FROM read_parquet('{store.read_glob('spans_raw')}', "  # noqa: E501
            "hive_partitioning=true, union_by_name=true)"
        ).fetchone()
        assert (n, days) == (9, 2)

    def test_resync_of_the_same_window_lands_each_span_once(self, tmp_path):
        # The compose sync loop re-pulls an overlapping window every interval. Measured
        # before the fix: 154 spans in, 308 rows out after two iterations.
        import duckdb

        store = open_store(f"file://{tmp_path}/s")
        store.ensure()
        con = duckdb.connect()
        assert (
            store.append_frame(con, self._batch("2026-06-01", 3, "a"), "spans_raw", source="p") == 3
        )
        assert (
            store.append_frame(con, self._batch("2026-06-01", 3, "a"), "spans_raw", source="p") == 0
        )
        overlap = self._batch("2026-06-01", 5, "a")  # a0..a2 present, a3..a4 new
        assert store.append_frame(con, overlap, "spans_raw", source="p") == 2
        # Same ids from another source are that source's spans, not duplicates.
        assert (
            store.append_frame(con, self._batch("2026-06-01", 3, "a"), "spans_raw", source="q") == 3
        )
        # A caller that wants the raw append can still have it.
        assert (
            store.append_frame(
                con, self._batch("2026-06-01", 1, "a"), "spans_raw", source="p", dedupe=False
            )
            == 1
        )
        n, distinct = con.execute(
            f"SELECT count(*), count(DISTINCT (span_id, source)) FROM read_parquet('{store.read_glob('spans_raw')}', "  # noqa: E501
            "hive_partitioning=true, union_by_name=true)"
        ).fetchone()
        assert (n, distinct) == (9, 8)

    def test_all_null_columns_keep_the_raw_type_across_batches(self, tmp_path):
        # A batch of root spans has parent_id null in every row; DuckDB would write it as
        # INT32. A later batch with real parents writes VARCHAR, and the union then fails
        # on the first coalesce. Every all-null column must land as its raw type.
        import duckdb

        store = open_store(f"file://{tmp_path}/s")
        store.ensure()
        con = duckdb.connect()
        roots = self._batch("2026-06-01", 2, "a")
        roots["llm_token_count_prompt"] = [None, None]
        roots["status_message"] = [None, None]
        store.append_frame(con, roots, "spans_raw")
        children = self._batch("2026-06-01", 2, "b")
        children["parent_id"] = ["a0", "a1"]
        store.append_frame(con, children, "spans_raw")
        glob = store.read_glob("spans_raw")
        types = {
            r[0]: r[1]
            for r in con.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{glob}', hive_partitioning=true, union_by_name=true)"  # noqa: E501
            ).fetchall()
        }
        assert types["parent_id"] == "VARCHAR"
        assert types["status_message"] == "VARCHAR"
        assert types["llm_token_count_prompt"] == "DOUBLE"
        # The exact expression the oracle's trace_shape recipe runs on the store.
        rows = con.execute(
            f"SELECT coalesce(parent_id, '') FROM read_parquet('{glob}', hive_partitioning=true, union_by_name=true) ORDER BY 1"  # noqa: E501
        ).fetchall()
        assert [r[0] for r in rows] == ["", "", "a0", "a1"]

    def test_direct_client_ids_are_renamed(self, tmp_path):
        import duckdb

        store = open_store(f"file://{tmp_path}/s")
        store.ensure()
        con = duckdb.connect()
        store.append_frame(con, self._batch("2026-06-01", 2, "a"), "spans_raw")
        cols = {
            r[0]
            for r in con.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{store.read_glob('spans_raw')}', hive_partitioning=true)"  # noqa: E501
            ).fetchall()
        }
        assert {"span_id", "trace_id", "day"} <= cols and "context.span_id" not in cols

    def test_rest_shape_is_renested_into_json(self, tmp_path):
        import duckdb

        store = open_store(f"file://{tmp_path}/s")
        store.ensure()
        con = duckdb.connect()
        store.append_frame(con, self._batch("2026-06-01", 2, "r", direct=False), "spans_raw")
        v = con.execute(
            f"SELECT json_extract_string(attributes,'$.llm.model_name'), json_extract(attributes,'$.metadata.k') "  # noqa: E501
            f"FROM read_parquet('{store.read_glob('spans_raw')}', hive_partitioning=true) LIMIT 1"
        ).fetchone()
        assert v == ("m", "1")

    def test_empty_batch_writes_nothing(self, tmp_path):
        import duckdb
        import pandas as pd

        store = open_store(f"file://{tmp_path}/s")
        store.ensure()
        assert store.append_frame(duckdb.connect(), pd.DataFrame(), "spans_raw") == 0
        assert store.size_bytes("spans_raw") == 0

    def test_typed_layout_builds_from_the_direct_client_shape(self, tmp_path):
        """The typed layout keyed identity on trace_rowid, which direct clients never have."""
        import duckdb

        from dev_agent_lens.storage.layouts import get_layout

        store = open_store(f"file://{tmp_path}/s")
        store.ensure()
        con = duckdb.connect()
        store.append_frame(con, self._batch("2026-06-01", 3, "a"), "spans_raw")
        lay = get_layout("typed")
        res = lay.build(con, store.read_glob("spans_raw"), store, zstd_level=3)
        assert res.rows == 3
        lay.attach(con, store)
        assert con.execute("SELECT count(DISTINCT trace_id) FROM spans").fetchone()[0] == 1

    def test_typed_layout_survives_a_trimmed_invocation_parameters(self, tmp_path):
        """Real corpora carry `invocation_parameters` values that are not JSON: the capture
        path trims oversized ones to a "[dal_trim: ..." marker. The ENG2-1609 rehearsal on
        the dev-agent-lens project had 2 such spans in 36,052 and the typed build threw on
        them, because DuckDB parses json_extract's input even inside an untaken CASE branch.
        The build must land every row and leave the thinking columns NULL for those."""
        import duckdb

        from dev_agent_lens.storage.layouts import get_layout

        batch = self._batch("2026-06-01", 3, "a")
        good = '{"llm":{"model_name":"m","invocation_parameters":"{\\"thinking\\":{\\"type\\":\\"enabled\\",\\"budget_tokens\\":1024},\\"max_tokens\\":8}"}}'  # noqa: E501
        trimmed = '{"llm":{"model_name":"m","invocation_parameters":"[dal_trim: invocation parameters 40961 chars]"}}'  # noqa: E501
        batch["attributes"] = [good, trimmed, good]
        store = open_store(f"file://{tmp_path}/s")
        store.ensure()
        con = duckdb.connect()
        store.append_frame(con, batch, "spans_raw", source="phoenix-dal-dead")
        lay = get_layout("typed")
        res = lay.build(con, store.read_glob("spans_raw"), store, zstd_level=3)
        assert res.rows == 3
        lay.attach(con, store)
        rows = con.execute(
            "SELECT thinking_type, thinking_budget_tokens, max_tokens FROM spans ORDER BY span_id"
        ).fetchall()
        assert rows == [("enabled", 1024, 8), (None, None, None), ("enabled", 1024, 8)]
        # and `source` survives typing, so a multi-project store can still scope per project
        assert con.execute("SELECT DISTINCT source FROM spans").fetchall() == [
            ("phoenix-dal-dead",)
        ]

    def test_typed_layout_stamps_identity_from_a_hooks_shaped_producer(self, tmp_path):
        """The Claude Code hooks producer carries its session at claude.session_id and no
        account. The ENG2-1610 proof landed 3,941 such spans with the typed session_id NULL
        on every one. Identity must come from whichever known path the producer used."""
        import duckdb

        from dev_agent_lens.storage.layouts import get_layout

        batch = self._batch("2026-06-01", 3, "h")
        batch["attributes"] = [
            '{"claude":{"event":"Stop","surface":"cli","session_id":"sess-hooks-1"},"openinference":{"span":{"kind":"UNKNOWN"}}}',
            '{"claude":{"event":"UserPromptSubmit","surface":"cli","session_id":"sess-hooks-1"}}',
            '{"session":{"id":"sess-oi-2"},"user":{"id":"user-oi-2"}}',
        ]
        batch["context.trace_id"] = ["t-h", "t-h", "t-oi"]
        store = open_store(f"file://{tmp_path}/s")
        store.ensure()
        con = duckdb.connect()
        store.append_frame(con, batch, "spans_raw", source="hooks")
        lay = get_layout("typed")
        assert lay.build(con, store.read_glob("spans_raw"), store, zstd_level=3).rows == 3
        lay.attach(con, store)
        rows = con.execute(
            "SELECT trace_id, session_id, account_uuid FROM spans ORDER BY span_id"
        ).fetchall()
        assert rows == [
            ("t-h", "sess-hooks-1", None),
            ("t-h", "sess-hooks-1", None),
            ("t-oi", "sess-oi-2", "user-oi-2"),
        ]

    ACCT = "c4690c79-c548-49cf-8f31-8547d7cecfe9"
    DEV = "b178f75793ebc5b74a004bfd3c20ff1eed953ed7f61c680c8643049ff8e5ea1b"
    SESS = "df34f6f3-3bef-41e6-99ea-ffab07027526"

    def test_typed_layout_reads_identity_as_the_old_producers_wrote_it(self, tmp_path):
        """Two shapes the Oxen imports carry that no JSON path reaches (ENG2-1572): the
        request body under input.value with metadata as a Python repr, and the first
        proxy's user_<hash>_account_<uuid>_session_<uuid> string. 96% of 16.5M imported
        spans had account_uuid NULL on the typed layout because of these."""
        import duckdb

        from dev_agent_lens.storage.layouts import get_layout

        batch = self._batch("2026-06-01", 4, "o")
        import json

        inner = json.dumps(
            {"device_id": self.DEV, "account_uuid": self.ACCT, "session_id": self.SESS}
        )
        body = json.dumps({"model": "claude-opus-4-6", "metadata": repr({"user_id": inner})})
        repr_body = json.dumps({"input": {"value": body}})
        legacy = (
            '{"metadata":{"user_api_key_end_user_id":'
            f'"user_{self.DEV}_account_{self.ACCT}_session_{self.SESS}"}}}}'
        )
        # a child span quoting a session id in its content must not count
        child_quote = f'{{"output":{{"value":"see session_id {self.SESS}"}}}}'
        batch["attributes"] = [repr_body, legacy, child_quote, '{"llm":{"model_name":"m"}}']
        batch["context.trace_id"] = ["t-repr", "t-legacy", "t-child", "t-child"]
        batch["parent_id"] = [None, None, "o3", None]
        batch["name"] = ["litellm_request", "litellm_request", "Claude_Code_Tool_Bash", "x"]
        store = open_store(f"file://{tmp_path}/s")
        store.ensure()
        con = duckdb.connect()
        store.append_frame(con, batch, "spans_raw", source="oxen")
        lay = get_layout("typed")
        assert lay.build(con, store.read_glob("spans_raw"), store, zstd_level=3).rows == 4
        lay.attach(con, store)
        rows = con.execute(
            "SELECT trace_id, account_uuid, device_id, session_id FROM spans ORDER BY span_id"
        ).fetchall()
        assert rows[0] == ("t-repr", self.ACCT, self.DEV, self.SESS)
        assert rows[1] == ("t-legacy", self.ACCT, self.DEV, self.SESS)
        assert rows[2] == ("t-child", None, None, None)

    def test_typed_layout_names_the_person_and_fills_a_session_from_its_other_traces(
        self, tmp_path, monkeypatch
    ):
        """A hooks-producer trace has a session and no account; the proxy trace of the same
        session has both. The session join gives the hooks trace the account, and the
        identity file turns the account into a name on every span (ENG2-1572)."""
        import duckdb

        from dev_agent_lens.storage.layouts import get_layout

        people = tmp_path / "identity.yaml"
        people.write_text(
            "people:\n"
            "  - name: Adam\n    email: a@x\n"
            f"    accounts: [{{uuid: {self.ACCT.upper()}, verified: true}}]\n"
            "  - name: Yashwant\n    email: y@x\n    users: [yashwanth]\n"
        )
        monkeypatch.setenv("DAL_IDENTITY", str(people))
        import json

        inner = json.dumps(
            {"device_id": self.DEV, "account_uuid": self.ACCT, "session_id": self.SESS}
        )
        batch = self._batch("2026-06-01", 4, "p")
        batch["attributes"] = [
            json.dumps({"metadata": {"user_api_key_end_user_id": inner}}),
            f'{{"claude":{{"event":"Stop","session_id":"{self.SESS}"}}}}',
            '{"session":{"id":"s-y"},"user":{"id":"yashwanth"}}',
            '{"session":{"id":"s-nobody"},"user":{"id":"stranger"}}',
        ]
        batch["context.trace_id"] = ["t-proxy", "t-hooks", "t-y", "t-n"]
        store = open_store(f"file://{tmp_path}/s")
        store.ensure()
        con = duckdb.connect()
        store.append_frame(con, batch, "spans_raw", source="mixed")
        lay = get_layout("typed")
        res = lay.build(con, store.read_glob("spans_raw"), store, zstd_level=3)
        assert res.rows == 4
        lay.attach(con, store)
        rows = con.execute(
            "SELECT trace_id, account_uuid, device_id, person FROM spans ORDER BY span_id"
        ).fetchall()
        assert rows == [
            ("t-proxy", self.ACCT, self.DEV, "Adam"),
            ("t-hooks", self.ACCT, self.DEV, "Adam"),
            ("t-y", "yashwanth", None, "Yashwant"),
            ("t-n", "stranger", None, None),
        ]

    def test_typed_layout_stamps_the_ticket_a_session_worked_on(self, tmp_path):
        """ENG2-1540: cost per outcome needs the ticket on the span. It comes from the branch
        attribute the folder ingest stamps, the branch line Claude Code puts in its context,
        or the first ticket id in the opening of the conversation; a trace with none takes
        its session's. Tool results quoting other tickets later in the input do not count."""
        import json

        import duckdb

        from dev_agent_lens.storage.layouts import get_layout

        sess = json.dumps(
            {"device_id": self.DEV, "account_uuid": self.ACCT, "session_id": self.SESS}
        )
        euid = {"metadata": {"user_api_key_end_user_id": sess}}
        batch = self._batch("2026-06-01", 5, "k")
        batch["attributes"] = [
            json.dumps({**euid, "git": {"branch": "adam/eng2-1572-person"}}),
            json.dumps({**euid, "input": {"value": "x" * 5000 + " see ENG2-9999"}}),
            json.dumps(
                {**euid, "llm": {"anthropic": {"messages": "Current branch: adam/eng2-1572-x"}}}
            ),
            json.dumps({"input": {"value": "/ticket ENG2-1416 please"}}),
            json.dumps({"session": {"id": "s-none"}, "input": {"value": "hello"}}),
        ]
        batch["context.trace_id"] = ["t-branch", "t-late", "t-ctx", "t-prompt", "t-none"]
        store = open_store(f"file://{tmp_path}/s")
        store.ensure()
        con = duckdb.connect()
        store.append_frame(con, batch, "spans_raw", source="mixed")
        lay = get_layout("typed")
        res = lay.build(con, store.read_glob("spans_raw"), store, zstd_level=3)
        assert res.rows == 5
        lay.attach(con, store)
        rows = con.execute("SELECT trace_id, ticket FROM spans ORDER BY span_id").fetchall()
        assert rows == [
            ("t-branch", "ENG2-1572"),
            ("t-late", "ENG2-1572"),  # nothing in its first 4,000 chars; the session says
            ("t-ctx", "ENG2-1572"),
            ("t-prompt", "ENG2-1416"),
            ("t-none", None),
        ]

    def test_typed_layout_names_the_harness_on_every_span_of_a_trace(self, tmp_path):
        """ENG2-402: Codex and Claude Code sessions land in one store, so a query must be able
        to split by harness. The ATIF root span says which (agent.name); its children don't,
        so the column is per trace. Proxy traffic has no agent root and stays NULL."""
        import json

        import duckdb

        from dev_agent_lens.storage.layouts import get_layout

        batch = self._batch("2026-06-01", 4, "h")
        batch["context.trace_id"] = ["t-codex", "t-codex", "t-claude", "t-proxy"]
        batch["parent_id"] = [None, "h0", None, None]
        batch["attributes"] = [
            json.dumps({"agent": {"name": "codex"}, "session": {"id": "s1"}}),
            json.dumps({"tool": {"name": "exec_command"}}),
            json.dumps({"agent": {"name": "claude-code"}, "session": {"id": "s2"}}),
            json.dumps({"llm": {"model_name": "m"}}),
        ]
        store = open_store(f"file://{tmp_path}/s")
        store.ensure()
        con = duckdb.connect()
        store.append_frame(con, batch, "spans_raw", source="mixed")
        lay = get_layout("typed")
        assert lay.build(con, store.read_glob("spans_raw"), store, zstd_level=3).rows == 4
        lay.attach(con, store)
        rows = con.execute("SELECT span_id, agent FROM spans ORDER BY span_id").fetchall()
        assert rows == [
            ("h0", "codex"),
            ("h1", "codex"),  # a child span takes its trace's harness
            ("h2", "claude-code"),
            ("h3", None),
        ]

    def test_typed_layout_names_a_codex_person_by_the_git_email_ingest_stamps(
        self, tmp_path, monkeypatch
    ):
        """ENG2-402: Codex session files carry no account, so `ingest-sessions --agent codex`
        stamps the laptop's git email as user.id. A person's email in identity.yaml resolves
        it, with no `users:` entry needed; case does not matter."""
        import json

        import duckdb

        from dev_agent_lens.storage.layouts import get_layout

        people = tmp_path / "identity.yaml"
        people.write_text("people:\n  - name: Developer\n    email: Developer@Example.com\n")
        monkeypatch.setenv("DAL_IDENTITY", str(people))
        batch = self._batch("2026-06-01", 2, "e")
        batch["context.trace_id"] = ["t-codex", "t-other"]
        batch["attributes"] = [
            json.dumps({"agent": {"name": "codex"}, "user": {"id": "developer@example.com"}}),
            json.dumps({"agent": {"name": "codex"}, "user": {"id": "someone@else.io"}}),
        ]
        store = open_store(f"file://{tmp_path}/s")
        store.ensure()
        con = duckdb.connect()
        store.append_frame(con, batch, "spans_raw", source="codex")
        lay = get_layout("typed")
        assert lay.build(con, store.read_glob("spans_raw"), store, zstd_level=3).rows == 2
        lay.attach(con, store)
        rows = con.execute("SELECT trace_id, person FROM spans ORDER BY span_id").fetchall()
        assert rows == [("t-codex", "Developer"), ("t-other", None)]

    def test_typed_layout_maps_tools_to_one_vocabulary_across_harnesses(self, tmp_path):
        """ENG2-402: `Bash` and `exec_command` are both a shell command. tool_kind says so,
        so a tool question means the same thing for Claude Code and Codex."""
        import json

        import duckdb

        from dev_agent_lens.storage.layouts import get_layout

        names = [
            "Bash",
            "exec_command",
            "write_stdin",
            "Edit",
            "apply_patch",
            "Read",
            "Grep",
            "WebSearch",
            "web_search",
            "Task",
            "mcp__linear__get_issue",
            "Brand_New",
            "TaskCreate",
            "TaskOutput",
            "AskUserQuestion",
            "linear-server - update_issue (MCP)",
            "CronCreate",
            "",
        ]
        batch = self._batch("2026-06-01", len(names), "t")
        batch["context.trace_id"] = [f"t{i}" for i in range(len(names))]
        batch["span_kind"] = ["TOOL"] * len(names)
        batch["attributes"] = [json.dumps({"tool": {"name": n}}) for n in names]
        store = open_store(f"file://{tmp_path}/s")
        store.ensure()
        con = duckdb.connect()
        store.append_frame(con, batch, "spans_raw", source="mixed")
        lay = get_layout("typed")
        lay.build(con, store.read_glob("spans_raw"), store, zstd_level=3)
        lay.attach(con, store)
        kinds = dict(con.execute("SELECT tool_name, tool_kind FROM spans").fetchall())
        assert kinds == {
            "Bash": "shell",
            "exec_command": "shell",
            "write_stdin": "shell",
            "Edit": "edit",
            "apply_patch": "edit",
            "Read": "read",
            "Grep": "search",
            "WebSearch": "web_search",
            "web_search": "web_search",
            "Task": "subagent",
            "mcp__linear__get_issue": "mcp",
            "Brand_New": "brand_new",
            "TaskCreate": "plan",
            "TaskOutput": "shell",
            "AskUserQuestion": "ask_user",
            "linear-server - update_issue (MCP)": "mcp",
            "CronCreate": "schedule",
            "": None,
        }

    def test_typed_layout_reads_cached_tokens_from_either_producer(self, tmp_path):
        """ENG2-402: the proxy writes LiteLLM's usage_object; the session ingest (Codex, and
        Claude from files) writes OpenInference's prompt_details. Both fill the column."""
        import json

        import duckdb

        from dev_agent_lens.storage.layouts import get_layout

        batch = self._batch("2026-06-01", 3, "c")
        batch["context.trace_id"] = ["t-proxy", "t-atif", "t-none"]
        batch["attributes"] = [
            json.dumps({"metadata": {"usage_object": {"cache_read_input_tokens": 7}}}),
            json.dumps({"llm": {"token_count": {"prompt_details": {"cache_read": 40}}}}),
            json.dumps({"llm": {"model_name": "m"}}),
        ]
        store = open_store(f"file://{tmp_path}/s")
        store.ensure()
        con = duckdb.connect()
        store.append_frame(con, batch, "spans_raw", source="mixed")
        lay = get_layout("typed")
        lay.build(con, store.read_glob("spans_raw"), store, zstd_level=3)
        lay.attach(con, store)
        rows = con.execute(
            "SELECT trace_id, tokens_cache_read FROM spans ORDER BY span_id"
        ).fetchall()
        assert rows == [("t-proxy", 7), ("t-atif", 40), ("t-none", None)]

    def test_typed_layout_reads_tool_name_from_either_producer(self, tmp_path):
        """LiteLLM spans carry claude_code_tool_name; the session-JSONL OTLP path carries
        OpenInference tool.name. Both are the same column."""
        import duckdb

        from dev_agent_lens.storage.layouts import get_layout

        batch = self._batch("2026-06-01", 2, "t")
        batch["attributes"] = [
            '{"claude_code_tool_name":"Bash"}',
            '{"tool":{"name":"Read"},"openinference":{"span":{"kind":"TOOL"}}}',
        ]
        store = open_store(f"file://{tmp_path}/s")
        store.ensure()
        con = duckdb.connect()
        store.append_frame(con, batch, "spans_raw")
        lay = get_layout("typed")
        lay.build(con, store.read_glob("spans_raw"), store, zstd_level=3)
        lay.attach(con, store)
        assert con.execute("SELECT tool_name FROM spans ORDER BY span_id").fetchall() == [
            ("Bash",),
            ("Read",),
        ]

    def test_typed_layout_tolerates_a_source_without_the_source_stamp(self, tmp_path):
        """Fixtures and pre-stamp rows have no `source` column at all; the build must not
        fail on the missing column, it lands NULL."""
        import duckdb

        from dev_agent_lens.storage.layouts import get_layout

        store = open_store(f"file://{tmp_path}/s")
        store.ensure()
        con = duckdb.connect()
        store.append_frame(con, self._batch("2026-06-01", 2, "b"), "spans_raw")
        lay = get_layout("typed")
        assert lay.build(con, store.read_glob("spans_raw"), store, zstd_level=3).rows == 2
        lay.attach(con, store)
        assert con.execute("SELECT DISTINCT source FROM spans").fetchall() == [(None,)]


class TestUriIsInertData:
    """A store URI is documented as safe to log, commit and paste. That is only true if it
    cannot execute anything. Review 2026-09-04 injected DuckDB SQL through a `'` in a
    file:// root and exfiltrated the S3 secret via a crafted prefix. These fail until fixed.
    """

    def test_credentials_in_uri_are_rejected(self):
        with pytest.raises(ValueError, match="credentials"):
            open_store("s3://AKIAKEY:SECRET@bucket/prefix")

    def test_quote_in_endpoint_is_rejected(self):
        with pytest.raises(ValueError):
            open_store("s3://bkt/p?endpoint=127.0.0.1:9000'%3B%20SELECT%201%3B--&tls=0")

    def test_quote_in_file_root_is_rejected(self, tmp_path):
        bad = tmp_path / "x'; COPY (SELECT 1) TO '/tmp/pwn"
        with pytest.raises(ValueError):
            open_store(f"file://{bad}")

    def test_quote_in_prefix_is_rejected(self):
        with pytest.raises(ValueError):
            open_store("s3://bkt/pre'fix?endpoint=127.0.0.1:9000&tls=0")

    def test_minio_scheme_without_endpoint_does_not_target_aws(self):
        with pytest.raises(ValueError, match="endpoint"):
            open_store("minio://dal/spans")

    def test_tls_defaults_on_even_with_an_endpoint(self):
        s = open_store("s3://bkt/p?endpoint=ceph.internal:443").s
        assert s.use_ssl is True


class TestListDatasets:
    """`dal store status` discovers datasets instead of hard-coding three names."""

    def test_dataset_name_stops_at_the_first_hive_segment(self):
        from dev_agent_lens.storage.spanstore import dataset_of

        assert dataset_of("spans_raw/day=2026-09-03/sync_ab12.parquet".split("/")) == "spans_raw"
        nested = "export/sf/spans/week=2026-W36/data_0.parquet".split("/")
        assert dataset_of(nested) == "export/sf/spans"
        assert dataset_of("blobs_typed/data_0.parquet".split("/")) == "blobs_typed"
        assert dataset_of(["stray.parquet"]) == ""

    def test_local_backend_walks_the_root(self, tmp_path):
        root = tmp_path / "store"
        for rel in (
            "spans_raw/day=2026-09-03/a.parquet",
            "spans_raw/day=2026-09-04/b.parquet",
            "export/sf/spans/week=2026-W36/data_0.parquet",
            "blobs_typed/data_0.parquet",
            "notes/readme.txt",
        ):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(b"x")
        s = open_store(f"file://{root}")
        assert s.list_datasets() == ["blobs_typed", "export/sf/spans", "spans_raw"]

    def test_local_backend_with_no_root_is_empty(self, tmp_path):
        assert open_store(f"file://{tmp_path}/never").list_datasets() == []

    def test_s3_backend_pages_through_the_prefix(self, monkeypatch):
        from unittest.mock import MagicMock

        s = open_store("s3://bkt/traces?endpoint=127.0.0.1:9100&tls=0")
        client = MagicMock()
        client.list_objects_v2.side_effect = [
            {
                "Contents": [
                    {"Key": "traces/spans_raw/day=2026-09-03/a.parquet"},
                    {"Key": "traces/.dal-probe"},
                ],
                "IsTruncated": True,
                "NextContinuationToken": "t1",
            },
            {
                "Contents": [
                    {"Key": "traces/export/sf/sessions/source=sf/data_0.parquet"},
                    {"Key": "traces/spans_typed/day=2026-09-03/data_0.parquet"},
                ],
                "IsTruncated": False,
            },
        ]
        monkeypatch.setattr(type(s), "_client", lambda self: client)
        assert s.list_datasets() == ["export/sf/sessions", "spans_raw", "spans_typed"]
        first, second = client.list_objects_v2.call_args_list
        assert first.kwargs["Prefix"] == "traces/"
        assert second.kwargs["ContinuationToken"] == "t1"
