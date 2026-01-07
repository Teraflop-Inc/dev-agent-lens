"""
DAL CLI - Dev Agent Lens Command Line Interface

Provides unified CLI for trace data collection, querying, and analysis.

Commands:
    dal sync              Incremental sync trace data from backends
    dal sync --full       Full sync ignoring state
    dal sync --push       Sync and push to Oxen remote
    dal sync-historical   One-time historical backfill (for large exports)
    dal config            Show configuration
    dal status            Show sync status
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any

import click

# Set up logging for the CLI module
logger = logging.getLogger(__name__)

from dev_agent_lens.clients.arize import ArizeClient
from dev_agent_lens.clients.phoenix import PhoenixClient
from dev_agent_lens.clients.phoenix_sqlite import PhoenixSQLiteClient
from dev_agent_lens.core.schema import (
    normalize_arize,
    normalize_phoenix,
    normalize_phoenix_annotations,
)
from dev_agent_lens.core.state import SyncState
from dev_agent_lens.core.unify import unify_sessions
from dev_agent_lens.storage.oxen_store import OxenStore


# Available backends
BACKENDS = {
    "phoenix-local": {
        "name": "Phoenix Local",
        "client_class": PhoenixClient,
        "normalizer": normalize_phoenix,
        "env_check": "DAL_PHOENIX_URL",
    },
    "arize-cloud": {
        "name": "Arize Cloud",
        "client_class": ArizeClient,
        "normalizer": normalize_arize,
        "env_check": "ARIZE_API_KEY",
    },
}


def get_configured_backends() -> list[str]:
    """Get list of backends that have required environment variables set."""
    configured = []
    for backend_id, config in BACKENDS.items():
        if os.getenv(config["env_check"]):
            configured.append(backend_id)
    return configured


def get_default_backend() -> str | None:
    """Get the default backend from env or first configured one."""
    default = os.getenv("DAL_DEFAULT_BACKEND")
    if default and default in BACKENDS:
        return default

    configured = get_configured_backends()
    return configured[0] if configured else None


@click.group()
@click.version_option(version="0.1.0", prog_name="dal")
def main():
    """DAL - Dev Agent Lens CLI for trace analysis.

    Sync, query, and analyze Claude Code traces from Phoenix and Arize backends.
    """
    pass


@main.command()
@click.option(
    "--full",
    is_flag=True,
    help="Full sync, ignore state and fetch all data",
)
@click.option(
    "--source",
    "source_name",
    type=str,
    help="Sync from a named source (configured via 'dal config add-source')",
)
@click.option(
    "--backend",
    type=click.Choice(list(BACKENDS.keys())),
    help="[Deprecated] Use --source instead. Sync specific legacy backend.",
)
@click.option(
    "--all-sources",
    is_flag=True,
    help="Sync from all configured sources",
)
@click.option(
    "--push",
    is_flag=True,
    help="Push to Oxen remote after sync (requires OXEN_REMOTE_URL)",
)
@click.option(
    "--with-annotations",
    is_flag=True,
    help="Also fetch annotations (slower, disabled by default)",
)
@click.option(
    "--limit",
    type=int,
    default=10000,
    help="Maximum number of spans to fetch per batch (default: 10000)",
)
@click.option(
    "--days",
    type=int,
    default=30,
    help="Number of days to sync (default: 30)",
)
@click.option(
    "--batch-days",
    type=int,
    default=None,
    help="Split sync into batches of N days each (useful for large datasets)",
)
@click.option(
    "--timeout",
    type=int,
    default=30,
    help="Timeout in seconds for each API request (default: 30, increase for large batches)",
)
@click.option(
    "--start-date",
    type=str,
    default=None,
    help="Start date for sync (YYYY-MM-DD). Overrides --days and last sync state.",
)
@click.option(
    "--end-date",
    type=str,
    default=None,
    help="End date for sync (YYYY-MM-DD). Default: now.",
)
@click.option(
    "--no-update-state",
    is_flag=True,
    help="Don't update last_sync state after sync. Useful for filling gaps without affecting incremental sync.",
)
def sync(
    full: bool,
    source_name: str | None,
    backend: str | None,
    all_sources: bool,
    push: bool,
    with_annotations: bool,
    limit: int,
    days: int,
    batch_days: int | None,
    timeout: int,
    start_date: str | None,
    end_date: str | None,
    no_update_state: bool,
) -> None:
    """Sync trace data from configured sources or backends.

    By default, performs incremental sync using saved state.
    Use --full to ignore state and fetch all available data.

    Examples:

        dal sync                        # Sync from configured sources/backends

        dal sync --source phoenix-alex  # Sync from named source

        dal sync --all-sources          # Sync from all configured sources

        dal sync --full                 # Full sync, ignore state

        dal sync --start-date 2024-12-01 --end-date 2024-12-05  # Sync specific range

        dal sync --start-date 2024-12-01 --no-update-state      # Fill gap without updating state

        dal sync --push                 # Sync and push to Oxen remote
    """
    from dev_agent_lens.core.sources import SourceConfig, SourceManager, SourceType

    sync_start = time.time()

    # Determine sync mode: named sources or legacy backends
    source_manager = SourceManager()
    sources_to_sync: list[SourceConfig] = []
    backends_to_sync: list[str] = []
    use_legacy_mode = False

    if source_name:
        # Sync from specific named source
        source = source_manager.get_source(source_name)
        if not source:
            click.echo(
                click.style(
                    f"Error: Source '{source_name}' not found. "
                    f"Use 'dal config list-sources' to see available sources.",
                    fg="red",
                )
            )
            raise SystemExit(1)
        sources_to_sync = [source]
        click.echo(f"Syncing from source: {source_name}")
    elif all_sources:
        # Sync from all configured sources
        sources_to_sync = source_manager.list_sources()
        if not sources_to_sync:
            click.echo(
                click.style(
                    "Error: No sources configured. "
                    "Use 'dal config add-source' to add sources, "
                    "or use --backend for legacy mode.",
                    fg="yellow",
                )
            )
            raise SystemExit(1)
        click.echo(f"Syncing from all sources: {', '.join(s.name for s in sources_to_sync)}")
    elif backend:
        # Legacy mode with specific backend
        use_legacy_mode = True
        backends_to_sync = [backend]
        if not os.getenv(BACKENDS[backend]["env_check"]):
            click.echo(
                click.style(
                    f"Error: Backend '{backend}' is not configured. "
                    f"Set {BACKENDS[backend]['env_check']} environment variable.",
                    fg="red",
                )
            )
            raise SystemExit(1)
        click.echo(click.style("Note: --backend is deprecated. Use 'dal config add-source' instead.", fg="yellow"))
    else:
        # Auto-detect: prefer named sources, fall back to legacy backends
        sources_to_sync = source_manager.list_sources()
        if sources_to_sync:
            click.echo(f"Syncing from configured sources: {', '.join(s.name for s in sources_to_sync)}")
        else:
            # Fall back to legacy backend mode
            use_legacy_mode = True
            backends_to_sync = get_configured_backends()
            if not backends_to_sync:
                click.echo(
                    click.style(
                        "Error: No sources or backends configured.\n"
                        "  - Use 'dal config add-source' to add named sources (recommended)\n"
                        "  - Or set DAL_PHOENIX_URL or ARIZE_API_KEY for legacy mode",
                        fg="red",
                    )
                )
                raise SystemExit(1)

    if use_legacy_mode:
        click.echo(f"Syncing from (legacy): {', '.join(backends_to_sync)}")

    click.echo(f"Mode: {'full' if full else 'incremental'}")
    click.echo(f"Time range: {days} days")
    if batch_days:
        click.echo(f"Batch size: {batch_days} days per batch")
    if with_annotations:
        click.echo("Annotations: enabled")
    click.echo()

    # Initialize components
    state = SyncState()

    # For named sources, we'll process them differently
    # For legacy mode, use flat storage
    store = OxenStore()

    total_spans = 0
    total_new_sessions = 0
    total_continued_sessions = 0
    sync_errors = []

    # Import timedelta for time-based batching
    from datetime import timedelta
    import pandas as pd

    # Parse date range parameters
    parsed_start_date: datetime | None = None
    parsed_end_date: datetime | None = None

    if start_date:
        try:
            parsed_start_date = datetime.strptime(start_date, "%Y-%m-%d")
            click.echo(f"Using start date: {start_date}")
        except ValueError:
            click.echo(click.style(f"Error: Invalid start-date format '{start_date}'. Use YYYY-MM-DD.", fg="red"))
            raise SystemExit(1)

    if end_date:
        try:
            end_date_parsed = datetime.strptime(end_date, "%Y-%m-%d")
            # If end date is today, use now() to get current time
            if end_date_parsed.date() == datetime.now().date():
                parsed_end_date = datetime.now()
                click.echo(f"Using end date: today (now)")
            else:
                # For past dates, use end of day
                parsed_end_date = end_date_parsed.replace(hour=23, minute=59, second=59)
                click.echo(f"Using end date: {end_date} (end of day)")
        except ValueError:
            click.echo(click.style(f"Error: Invalid end-date format '{end_date}'. Use YYYY-MM-DD.", fg="red"))
            raise SystemExit(1)

    if no_update_state:
        click.echo(click.style("State will NOT be updated after sync", fg="yellow"))

    # Helper function to sync a single source/backend
    def sync_single(
        source_or_backend_id: str,
        client_class: type,
        normalizer: callable,
        display_name: str,
        source_store: OxenStore,
        is_phoenix: bool = False,
        client_timeout: int = 30,
    ) -> tuple[int, int, int]:
        """Sync a single source and return (spans, new_sessions, continued_sessions)."""
        click.echo(f"[{display_name}] Starting sync...")

        # Calculate time range
        # Priority: --start-date/--end-date > --full > last_sync > --days
        end_time = parsed_end_date if parsed_end_date else datetime.now()

        if parsed_start_date:
            # Explicit date range overrides everything
            start_time = parsed_start_date
            click.echo(f"  Date range: {start_time.date()} to {end_time.date()}")
        elif full:
            start_time = end_time - timedelta(days=days)
            click.echo(f"  Full sync: last {days} days")
        else:
            last_sync = state.get_last_sync(source_or_backend_id)
            if last_sync:
                start_time = last_sync
                click.echo(f"  Incremental from: {last_sync.isoformat()}")
            else:
                start_time = end_time - timedelta(days=days)
                click.echo(f"  First sync: last {days} days")

        # Create client with timeout (Phoenix supports timeout, Arize may not)
        if is_phoenix:
            client = client_class(timeout=float(client_timeout))
        else:
            client = client_class()

        # Determine batches
        if batch_days:
            batches = []
            batch_start = start_time
            while batch_start < end_time:
                batch_end = min(batch_start + timedelta(days=batch_days), end_time)
                batches.append((batch_start, batch_end))
                batch_start = batch_end
            click.echo(f"  Processing {len(batches)} batches...")
        else:
            batches = [(start_time, end_time)]

        # Fetch spans in batches, looping until we get all data
        all_spans = []
        max_fetched_time = None  # Track max timestamp for state update
        total_iterations = 0
        max_iterations = 100  # Safety limit to prevent infinite loops

        for i, (batch_start, batch_end) in enumerate(batches):
            if batch_days:
                click.echo(f"  Batch {i+1}/{len(batches)}: {batch_start.date()} to {batch_end.date()}")

            # Loop within each batch until we get all spans
            current_start = batch_start
            batch_iteration = 0
            keep_fetching = True

            while keep_fetching and total_iterations < max_iterations:
                total_iterations += 1
                batch_iteration += 1

                if batch_iteration > 1:
                    click.echo(f"    Continuation {batch_iteration}: from {current_start.isoformat()}")

                click.echo(f"  Fetching spans (limit {limit})...")
                batch_df = client.get_spans_dataframe(
                    start_time=current_start,
                    end_time=batch_end,
                    limit=limit,
                )

                if batch_df.empty:
                    click.echo(f"    No spans in this range")
                    keep_fetching = False
                    break

                all_spans.append(batch_df)
                click.echo(f"    Got {len(batch_df)} spans")

                # Track max timestamp from fetched spans
                if "start_time" in batch_df.columns:
                    batch_max = batch_df["start_time"].max()
                    if batch_max is not None:
                        if max_fetched_time is None or batch_max > max_fetched_time:
                            max_fetched_time = batch_max

                # Check if we hit the limit - need to continue fetching
                if len(batch_df) >= limit:
                    # Convert pandas Timestamp to datetime if needed
                    if hasattr(batch_max, 'to_pydatetime'):
                        current_start = batch_max.to_pydatetime()
                    else:
                        current_start = batch_max
                    click.echo(click.style(
                        f"    Hit limit ({limit}), continuing from {current_start.isoformat()}...",
                        fg="yellow"
                    ))
                    # keep_fetching stays True
                else:
                    # Got all spans in this batch
                    keep_fetching = False

        if total_iterations >= max_iterations:
            click.echo(click.style(
                f"  ⚠️  Reached max iterations ({max_iterations}). Some data may be missing.",
                fg="red"
            ))

        # Combine all batches
        if all_spans:
            spans_df = pd.concat(all_spans, ignore_index=True)
            if "context.span_id" in spans_df.columns:
                spans_df = spans_df.drop_duplicates(subset=["context.span_id"])
        else:
            spans_df = pd.DataFrame()

        if spans_df.empty:
            click.echo(click.style("  No new spans found", fg="yellow"))
            return 0, 0, 0

        if total_iterations > 1:
            click.echo(f"  Fetched {len(spans_df)} spans (after {total_iterations} iterations)")
        else:
            click.echo(f"  Fetched {len(spans_df)} spans")

        # Normalize spans
        click.echo("  Normalizing...")
        normalized = normalizer(spans_df)

        # Store raw data
        click.echo("  Storing raw data...")
        raw_file = source_store.append_spans(normalized, backend=source_or_backend_id)
        click.echo(f"  Saved to: {raw_file.name}")

        # Fetch annotations for Phoenix (opt-in)
        if with_annotations and is_phoenix:
            click.echo("  Fetching annotations...")
            try:
                annotations_df = client.get_span_annotations_dataframe(
                    spans_dataframe=spans_df,
                )
                if not annotations_df.empty:
                    normalized_annotations = normalize_phoenix_annotations(annotations_df)
                    source_store.append_spans(
                        normalized_annotations,
                        backend=f"{source_or_backend_id}-annotations",
                    )
                    click.echo(f"  Fetched {len(annotations_df)} annotations")
                else:
                    click.echo("  No annotations found")
            except Exception as ann_err:
                click.echo(
                    click.style(f"  Warning: Could not fetch annotations: {ann_err}", fg="yellow")
                )

        # Get existing sessions file
        current_sessions = source_store.sessions_dir / "sessions_current.jsonl"

        # Unify with existing sessions
        click.echo("  Unifying sessions...")
        output_file = source_store.sessions_dir / f"sessions_{datetime.now().strftime('%Y%m%d')}.jsonl"
        unified_df, report = unify_sessions(
            normalized,
            existing_file=current_sessions if current_sessions.exists() else None,
            output_file=output_file,
        )

        # Update the sessions_current symlink
        source_store._update_current_symlink(output_file)

        # Report results
        click.echo(
            f"  New sessions: {len(report.new_sessions)}, "
            f"Continued: {len(report.continued_sessions)}, "
            f"Duplicates removed: {report.duplicates_removed}"
        )

        # Update state - use max fetched timestamp if we hit max iterations, otherwise now()
        if no_update_state:
            click.echo(click.style(
                f"  State NOT updated (--no-update-state flag)",
                fg="yellow"
            ))
        elif total_iterations >= max_iterations and max_fetched_time is not None:
            # We hit the safety limit, save where we got to
            if hasattr(max_fetched_time, 'to_pydatetime'):
                sync_time = max_fetched_time.to_pydatetime()
            else:
                sync_time = max_fetched_time
            state.set_last_sync(source_or_backend_id, sync_time)
            click.echo(click.style(
                f"  ⚠️  State set to max fetched time ({sync_time.isoformat()}) due to iteration limit. "
                f"Run sync again to get remaining data.",
                fg="yellow"
            ))
        else:
            # Normal completion - we got all data up to now
            state.set_last_sync(source_or_backend_id, datetime.now())
        click.echo(click.style(f"  [OK] {display_name} sync complete", fg="green"))

        return len(spans_df), len(report.new_sessions), len(report.continued_sessions)

    # Process named sources (new mode)
    for source in sources_to_sync:
        try:
            # Create source-specific store
            source_store = OxenStore.for_source(source.name)

            # Determine client class and normalizer based on source type
            if source.source_type == SourceType.PHOENIX:
                # For Phoenix, we need to set env vars temporarily if source has custom config
                if source.url:
                    os.environ["DAL_PHOENIX_URL"] = source.url
                if source.project:
                    os.environ["DAL_PHOENIX_PROJECT"] = source.project

                client_class = PhoenixClient
                normalizer = normalize_phoenix
                is_phoenix = True
            else:  # ARIZE
                if source.space_key:
                    os.environ["ARIZE_SPACE_KEY"] = source.space_key
                if source.model_id:
                    os.environ["ARIZE_MODEL_ID"] = source.model_id

                client_class = ArizeClient
                normalizer = normalize_arize
                is_phoenix = False

            spans, new_sess, cont_sess = sync_single(
                source.name,
                client_class,
                normalizer,
                source.get_display_info(),
                source_store,
                is_phoenix=is_phoenix,
                client_timeout=timeout,
            )
            total_spans += spans
            total_new_sessions += new_sess
            total_continued_sessions += cont_sess

        except Exception as e:
            error_msg = f"[{source.name}] Error: {e}"
            sync_errors.append(error_msg)
            click.echo(click.style(f"  [FAIL] {e}", fg="red"))
            continue

        click.echo()

    # Process legacy backends (backward compatibility)
    for backend_id in backends_to_sync:
        config = BACKENDS[backend_id]

        try:
            spans, new_sess, cont_sess = sync_single(
                backend_id,
                config["client_class"],
                config["normalizer"],
                config["name"],
                store,  # Use flat store for legacy mode
                is_phoenix=(backend_id == "phoenix-local"),
                client_timeout=timeout,
            )
            total_spans += spans
            total_new_sessions += new_sess
            total_continued_sessions += cont_sess

        except Exception as e:
            error_msg = f"[{config['name']}] Error: {e}"
            sync_errors.append(error_msg)
            click.echo(click.style(f"  [FAIL] {e}", fg="red"))
            continue

        click.echo()

    # Handle Oxen push
    if push:
        click.echo("Pushing to Oxen remote...")
        if not store.oxen_enabled:
            click.echo(
                click.style(
                    "Warning: OXEN_REMOTE_URL not set, skipping push",
                    fg="yellow",
                )
            )
        else:
            store.init_oxen()
            if store.commit(f"Sync {datetime.now().isoformat()}"):
                if store.push():
                    click.echo(click.style("Pushed to Oxen remote", fg="green"))
                else:
                    click.echo(click.style("Failed to push to Oxen", fg="red"))
            else:
                click.echo(click.style("Failed to commit to Oxen", fg="red"))

    # Final summary
    elapsed = time.time() - sync_start
    click.echo()
    click.echo("=" * 50)
    click.echo(click.style("Sync Summary", bold=True))
    click.echo("=" * 50)
    click.echo(f"Total spans fetched: {total_spans}")
    click.echo(f"New sessions: {total_new_sessions}")
    click.echo(f"Continued sessions: {total_continued_sessions}")
    click.echo(f"Time elapsed: {elapsed:.2f}s")

    if sync_errors:
        click.echo()
        click.echo(click.style("Errors:", fg="red"))
        for error in sync_errors:
            click.echo(f"  - {error}")
        raise SystemExit(1)

    click.echo()
    click.echo(click.style("Sync complete!", fg="green"))


@main.command("sync-historical")
@click.option(
    "--source",
    "source_name",
    type=str,
    help="Sync from a named source (configured via 'dal config add-source')",
)
@click.option(
    "--days",
    type=int,
    default=None,
    help="Number of days of historical data to sync. If not specified, syncs all available.",
)
@click.option(
    "--start-date",
    type=str,
    help="Start date for sync (YYYY-MM-DD). Overrides --days.",
)
@click.option(
    "--end-date",
    type=str,
    help="End date for sync (YYYY-MM-DD). Defaults to today.",
)
@click.option(
    "--batch-size",
    type=int,
    default=1,
    help="Days per batch (default: 1). Smaller batches are more reliable.",
)
@click.option(
    "--batch-hours",
    type=int,
    default=None,
    help="Hours per batch (overrides --batch-size). Use for high-volume days.",
)
@click.option(
    "--limit",
    type=int,
    default=50000,
    help="Maximum spans per batch (default: 50000)",
)
@click.option(
    "--timeout",
    type=int,
    default=120,
    help="Timeout in seconds for each API request (default: 120)",
)
@click.option(
    "--backend",
    type=click.Choice(list(BACKENDS.keys())),
    help="[Deprecated] Use --source instead. Sync specific legacy backend.",
)
@click.option(
    "--retries",
    type=int,
    default=3,
    help="Number of retries per batch on failure (default: 3)",
)
@click.option(
    "--skip-normalize",
    is_flag=True,
    help="Skip normalization, save raw data only (faster for large backfills)",
)
@click.option(
    "--no-auto-subdivide",
    is_flag=True,
    help="Disable automatic subdivision when hitting limits",
)
@click.option(
    "--delay",
    type=float,
    default=2.0,
    help="Delay in seconds between API requests (default: 2.0). Increase to avoid rate limiting.",
)
@click.option(
    "--resume/--no-resume",
    default=True,
    help="Resume from previous checkpoint if available (default: resume)",
)
@click.option(
    "--reset",
    is_flag=True,
    help="Clear any existing checkpoint and start fresh",
)
@click.option(
    "--force-resume",
    is_flag=True,
    help="Resume existing checkpoint regardless of date range specified",
)
@click.option(
    "--clean",
    is_flag=True,
    help="Clean up completed sync state files (use with --status to see what would be cleaned)",
)
@click.option(
    "--status",
    "show_status",
    is_flag=True,
    help="Show status of in-progress historical syncs without syncing",
)
@click.option(
    "--history",
    "show_history",
    is_flag=True,
    help="Include completed syncs in --status output (shows last sync times from SyncState)",
)
@click.option(
    "--with-annotations",
    is_flag=True,
    help="Also fetch annotations (slower, disabled by default)",
)
@click.option(
    "--verbose", "-v",
    is_flag=True,
    help="Enable verbose logging for debugging sync issues",
)
@click.option(
    "--sqlite",
    is_flag=True,
    help="Use direct SQLite access instead of HTTP API (Phoenix only, requires local Docker)",
)
@click.option(
    "--sqlite-container",
    type=str,
    default=None,
    help="Docker container name for SQLite access (e.g., 'dev-agent-lens-phoenix-1'). "
         "Overrides source config sqlite_container setting.",
)
def sync_historical(
    source_name: str | None,
    days: int | None,
    start_date: str | None,
    end_date: str | None,
    batch_size: int,
    batch_hours: int | None,
    limit: int,
    timeout: int,
    backend: str | None,
    retries: int,
    skip_normalize: bool,
    no_auto_subdivide: bool,
    delay: float,
    resume: bool,
    reset: bool,
    force_resume: bool,
    clean: bool,
    show_status: bool,
    show_history: bool,
    with_annotations: bool,
    verbose: bool,
    sqlite: bool,
    sqlite_container: str | None,
) -> None:
    """Sync all historical trace data from a source.

    This command handles everything automatically:
    - Detects the full date range of available data
    - Downloads in daily batches (subdivides high-volume days automatically)
    - Saves progress checkpoints so you can resume if interrupted
    - Updates sync state so future 'dal sync' commands continue from where this left off

    The simplest usage is just: dal sync-historical --source <name>

    Features:
    - Resume capability: Interrupted syncs continue from where they stopped
    - Auto-subdivision: High-volume days automatically split into smaller chunks
    - Progress tracking: See ETA and percentage complete
    - State integration: After completion, regular 'dal sync' picks up from here

    Phoenix Tips (high-volume sources):
    - Use --limit 10000 for more frequent checkpoints and reliable syncing
    - Use --delay 5 to avoid overwhelming the Phoenix server
    - Auto-subdivide handles busy days automatically (splits time windows recursively)
    - If you see connection errors, reduce --limit and increase --delay

    Examples:

        dal sync-historical --source arize-ax-alex      # Sync everything (simplest)

        dal sync-historical --source arize-ax-alex --status  # Check progress

        dal sync-historical --source arize-ax-alex --days 30  # Last 30 days only

        dal sync-historical --source arize-ax-alex --reset  # Start over

        dal sync-historical --start-date 2025-11-01     # From specific date

        # Phoenix with high volume - use lower limit and delay
        dal sync-historical --source my-phoenix --limit 10000 --delay 5
    """
    from datetime import timedelta
    import pandas as pd
    from dev_agent_lens.core.sources import SourceConfig, SourceManager, SourceType
    from dev_agent_lens.core.historical_sync import (
        HistoricalSyncState,
        SyncConfig,
        SyncStatus,
        list_historical_syncs,
        clear_historical_sync,
    )

    # Configure logging based on --verbose flag
    if verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
        # Also enable debug for our modules
        logging.getLogger("dev_agent_lens").setLevel(logging.DEBUG)
        click.echo(click.style("Verbose logging enabled", fg="cyan"))
        click.echo()

    # Handle --clean flag: delete completed sync state files
    if clean:
        syncs = list_historical_syncs()
        completed_syncs = [s for s in syncs if s.get_status() == SyncStatus.COMPLETE]

        if not completed_syncs:
            click.echo("No completed syncs to clean up.")
            return

        for sync_state in completed_syncs:
            if sync_state.delete():
                click.echo(f"Cleaned up: {sync_state.source}")
            else:
                click.echo(f"Failed to clean: {sync_state.source}")

        click.echo(f"\nCleaned {len(completed_syncs)} completed sync state file(s).")
        return

    # Handle --status flag: show status and exit
    if show_status or show_history:
        syncs = list_historical_syncs()
        has_in_progress = bool(syncs)

        if has_in_progress:
            click.echo(click.style("Historical Sync Status", bold=True))
            click.echo()
            for sync_state in syncs:
                progress = sync_state.progress_percent
                eta = sync_state.get_eta()
                eta_str = f" (ETA: {eta})" if eta else ""

                # Use the new get_status() method for better status detection
                status_code = sync_state.get_status()
                if status_code == SyncStatus.COMPLETE:
                    status = click.style("complete", fg="green")
                elif status_code == SyncStatus.INCOMPLETE:
                    status = click.style("incomplete", fg="red")
                elif status_code == SyncStatus.IN_PROGRESS:
                    status = click.style("in progress", fg="yellow")
                elif status_code == SyncStatus.STALE:
                    status = click.style("stale (process died)", fg="red")
                else:
                    status = click.style("paused", fg="cyan")

                click.echo(f"  {sync_state.source}: {progress:.1f}% {status}{eta_str}")
                click.echo(f"    Range: {sync_state.target_start.strftime('%Y-%m-%d')} to {sync_state.target_end.strftime('%Y-%m-%d')}")
                click.echo(f"    Spans: {sync_state.stats.total_spans:,}")

                # Show run info if available (for in_progress or stale syncs)
                if sync_state.current_run:
                    run_info = f"    Run: {sync_state.current_run.run_id}"
                    if status_code == SyncStatus.IN_PROGRESS:
                        run_info += f" (PID: {sync_state.current_run.pid})"
                    elif status_code == SyncStatus.STALE:
                        run_info += f" (PID: {sync_state.current_run.pid} - dead)"
                    click.echo(run_info)

                # Show remaining ranges count for clearer progress
                remaining_ranges = sync_state.get_remaining_ranges()
                click.echo(f"    Batches: {sync_state.stats.batches_completed} completed, {sync_state.stats.batches_failed} failed")
                if remaining_ranges:
                    click.echo(f"    Remaining gaps: {len(remaining_ranges)}")
                if sync_state.failed_ranges:
                    click.echo(f"    Failed ranges pending retry: {len(sync_state.failed_ranges)}")
                if sync_state.stats.subdivisions > 0:
                    click.echo(f"    Subdivisions: {sync_state.stats.subdivisions}")
                click.echo()

        # Show completed syncs from SyncState if --history is specified
        if show_history:
            from dev_agent_lens.core.state import SyncState
            from dev_agent_lens.core.sources import SourceManager

            sync_state_obj = SyncState()
            source_manager = SourceManager()
            all_sources = source_manager.list_sources()

            # Get sources with completed syncs (have last_sync but no in-progress checkpoint)
            in_progress_sources = {s.source for s in syncs}
            completed_sources = []

            for source in all_sources:
                last_sync = sync_state_obj.get_last_sync(source.name)
                if last_sync and source.name not in in_progress_sources:
                    completed_sources.append((source, last_sync))

            if completed_sources:
                if has_in_progress:
                    click.echo()
                click.echo(click.style("Completed Historical Syncs", bold=True))
                click.echo()
                for source, last_sync in sorted(completed_sources, key=lambda x: x[1], reverse=True):
                    status = click.style("completed", fg="green")
                    click.echo(f"  {source.name}: {status}")
                    click.echo(f"    Last sync: {last_sync.strftime('%Y-%m-%d %H:%M:%S')}")
                    click.echo(f"    Type: {source.source_type.value}")
                    click.echo(f"    Backend: {source.get_display_info()}")
                    click.echo()
            elif not has_in_progress:
                click.echo("No historical syncs found (in-progress or completed).")
                click.echo("Use 'dal sync-historical --source <name>' to start a sync.")
                return

        if not has_in_progress and not show_history:
            click.echo("No historical syncs in progress.")
            click.echo("Tip: Use --history to see completed syncs.")
        return

    sync_start = time.time()

    # Determine what to sync: named source or legacy backends
    source_manager = SourceManager()
    sources_to_sync: list[SourceConfig] = []
    backends_to_sync: list[str] = []
    use_legacy_mode = False

    if source_name:
        # Named source mode
        source = source_manager.get_source(source_name)
        if not source:
            click.echo(
                click.style(
                    f"Error: Source '{source_name}' not found. "
                    f"Use 'dal config list-sources' to see available sources.",
                    fg="red",
                )
            )
            raise SystemExit(1)
        sources_to_sync = [source]
    elif backend:
        # Legacy backend mode
        use_legacy_mode = True
        backends_to_sync = [backend]
        if not os.getenv(BACKENDS[backend]["env_check"]):
            click.echo(
                click.style(
                    f"Error: Backend '{backend}' is not configured. "
                    f"Set {BACKENDS[backend]['env_check']} environment variable.",
                    fg="red",
                )
            )
            raise SystemExit(1)
        click.echo(click.style("Note: --backend is deprecated. Use --source instead.", fg="yellow"))
    else:
        # Auto-detect: prefer named sources, fall back to legacy
        sources_to_sync = source_manager.list_sources()
        if not sources_to_sync:
            use_legacy_mode = True
            backends_to_sync = get_configured_backends()
            if not backends_to_sync:
                click.echo(
                    click.style(
                        "Error: No sources or backends configured.\n"
                        "  - Use 'dal config add-source' to add named sources\n"
                        "  - Or set DAL_PHOENIX_URL or ARIZE_API_KEY for legacy mode",
                        fg="red",
                    )
                )
                raise SystemExit(1)

    # Parse date range
    if end_date:
        try:
            end_date_parsed = datetime.strptime(end_date, "%Y-%m-%d")
            # If end date is today, use now() to get current time
            if end_date_parsed.date() == datetime.now().date():
                sync_end_time = datetime.now()
            else:
                # For past dates, use end of day
                sync_end_time = end_date_parsed.replace(hour=23, minute=59, second=59)
        except ValueError:
            click.echo(click.style(f"Error: Invalid end-date format '{end_date}'. Use YYYY-MM-DD.", fg="red"))
            raise SystemExit(1)
    else:
        sync_end_time = datetime.now()

    if start_date:
        try:
            sync_start_time = datetime.strptime(start_date, "%Y-%m-%d")
        except ValueError:
            click.echo(click.style(f"Error: Invalid start-date format '{start_date}'. Use YYYY-MM-DD.", fg="red"))
            raise SystemExit(1)
    elif days is not None:
        # Use --days
        if days > 365:
            click.echo(
                click.style("Warning: Limiting to 365 days (maximum)", fg="yellow")
            )
            days = 365
        sync_start_time = sync_end_time - timedelta(days=days)
    else:
        # No date range specified - default to 90 days
        # NOTE: Auto-detection is not practical for Arize because:
        # - The Arize SDK has no API to query date ranges without downloading data
        # - Probing would require downloading all data in a large window
        # - This defeats the purpose of incremental batching
        # Users should specify --start-date or --days based on their knowledge of the data.
        click.echo("No date range specified, defaulting to last 90 days.")
        click.echo("  Tip: Use --start-date YYYY-MM-DD or --days N for a specific range.")
        sync_start_time = sync_end_time - timedelta(days=90)

    # Handle --reset flag
    for source in sources_to_sync:
        if reset:
            if clear_historical_sync(source.name):
                click.echo(f"Cleared checkpoint for '{source.name}'")
    for backend_id in backends_to_sync:
        if reset:
            if clear_historical_sync(backend_id):
                click.echo(f"Cleared checkpoint for '{backend_id}'")

    # Determine batch duration
    if batch_hours:
        batch_duration = timedelta(hours=batch_hours)
        batch_description = f"{batch_hours} hours"
    else:
        batch_duration = timedelta(days=batch_size)
        batch_description = f"{batch_size} day{'s' if batch_size > 1 else ''}"

    # Create sync config
    sync_config = SyncConfig(
        batch_hours=batch_hours,
        batch_days=batch_size,
        limit=limit,
        timeout=timeout,
        delay=delay,
    )

    # Handle --sqlite flag: validate and resolve container name
    use_sqlite = False
    resolved_sqlite_container: str | None = None

    if sqlite or sqlite_container:
        # SQLite mode requested - validate
        if use_legacy_mode:
            click.echo(click.style(
                "Error: --sqlite is not supported with legacy backends. Use --source instead.",
                fg="red",
            ))
            raise SystemExit(1)

        if len(sources_to_sync) > 1:
            click.echo(click.style(
                "Error: --sqlite only supports a single source at a time.",
                fg="red",
            ))
            raise SystemExit(1)

        source = sources_to_sync[0]

        if source.source_type != SourceType.PHOENIX:
            click.echo(click.style(
                f"Error: --sqlite only works with Phoenix sources ('{source.name}' is {source.source_type.value}).",
                fg="red",
            ))
            raise SystemExit(1)

        # Resolve container name: CLI flag > source config > error
        resolved_sqlite_container = sqlite_container or source.sqlite_container

        if not resolved_sqlite_container:
            click.echo(click.style(
                f"Error: --sqlite requires a Docker container name.\n"
                f"  Either:\n"
                f"    1. Use --sqlite-container <name> (e.g., --sqlite-container dev-agent-lens-phoenix-1)\n"
                f"    2. Add sqlite_container to source config:\n"
                f"       dal config add-source {source.name} phoenix --url {source.url or 'URL'} "
                f"--sqlite-container CONTAINER_NAME",
                fg="red",
            ))
            raise SystemExit(1)

        use_sqlite = True
        click.echo(click.style(
            f"SQLite mode: Using direct database access via container '{resolved_sqlite_container}'",
            fg="cyan",
        ))

    # Calculate estimated batches (may increase with auto-subdivision)
    total_duration = sync_end_time - sync_start_time
    estimated_batches = max(1, int(total_duration / batch_duration) + 1)

    click.echo(click.style("Historical Sync", bold=True))
    if sources_to_sync:
        click.echo(f"Sources: {', '.join(s.name for s in sources_to_sync)}")
    else:
        click.echo(f"Backends (legacy): {', '.join(backends_to_sync)}")
    click.echo(f"Date range: {sync_start_time.strftime('%Y-%m-%d')} to {sync_end_time.strftime('%Y-%m-%d')}")
    click.echo(f"Batch size: {batch_description} (~{estimated_batches} batches)")
    click.echo(f"Limit per batch: {limit:,}")
    click.echo(f"Auto-subdivide: {'disabled' if no_auto_subdivide else 'enabled'}")
    click.echo(f"Timeout: {timeout}s")
    click.echo(f"Retries per batch: {retries}")
    click.echo(f"Request delay: {delay}s")
    click.echo()

    total_spans = 0
    total_batches_completed = 0
    total_batches_failed = 0
    total_subdivisions = 0
    all_errors = []

    # Minimum subdivision window (1 hour) to prevent infinite recursion
    MIN_SUBDIVISION = timedelta(hours=1)

    def pre_subdivide_ranges(
        ranges: list[tuple[datetime, datetime]],
        initial_window: timedelta,
    ) -> list[tuple[datetime, datetime]]:
        """Pre-subdivide ranges into smaller chunks for aggressive querying.

        When a batch has failed multiple times, we pre-subdivide into smaller
        time windows before making any Phoenix queries. This helps Phoenix
        handle high-volume days by never asking for the full 24h at once.

        Args:
            ranges: List of (start, end) tuples to subdivide
            initial_window: The maximum window size for each chunk

        Returns:
            List of (start, end) tuples, each no larger than initial_window
        """
        if initial_window <= timedelta(0):
            return ranges

        result = []
        for range_start, range_end in ranges:
            range_duration = range_end - range_start
            if range_duration <= initial_window:
                # Already small enough
                result.append((range_start, range_end))
            else:
                # Subdivide into chunks of initial_window size
                current = range_start
                while current < range_end:
                    chunk_end = min(current + initial_window, range_end)
                    result.append((current, chunk_end))
                    current = chunk_end

        return result

    # Helper function to fetch with auto-subdivision and incremental persistence
    def fetch_with_subdivision(
        client,
        batch_start: datetime,
        batch_end: datetime,
        depth: int = 0,
        is_first_request: bool = True,
        # Incremental persistence context (optional - if provided, saves immediately)
        store: OxenStore | None = None,
        checkpoint_state: HistoricalSyncState | None = None,
        normalizer: callable | None = None,
        backend_name: str | None = None,
        batch_key: str | None = None,
    ) -> tuple[list, int, int]:
        """Fetch spans, auto-subdividing if limit is hit.

        When incremental persistence context is provided (store, checkpoint_state, etc.),
        each successful sub-batch is saved immediately to disk and recorded in state.
        This prevents data loss when later sub-batches fail.

        Returns (list of dataframes, subdivision_count, spans_saved_incrementally)
        """
        nonlocal total_subdivisions

        indent = "    " * depth if depth > 0 else ""
        incremental_mode = store is not None and checkpoint_state is not None

        # Add delay between requests (except for the very first one)
        if not is_first_request and delay > 0:
            time.sleep(delay)

        # Attempt fetch with retries
        batch_df = None
        last_error = None
        for attempt in range(1, retries + 1):
            try:
                batch_df = client.get_spans_dataframe(
                    start_time=batch_start,
                    end_time=batch_end,
                    limit=limit,
                )
                break  # Success
            except Exception as e:
                last_error = e
                if attempt < retries:
                    # Exponential backoff with base delay
                    backoff = max(delay, 2 ** attempt)
                    time.sleep(backoff)

        if batch_df is None:
            if last_error:
                raise last_error
            return [], 0, 0

        if hasattr(batch_df, "empty") and batch_df.empty:
            return [], 0, 0

        batch_count = len(batch_df)

        # Check if we hit the limit and should subdivide
        if batch_count >= limit and not no_auto_subdivide:
            window_size = batch_end - batch_start
            if window_size > MIN_SUBDIVISION:
                # Subdivide into two halves
                midpoint = batch_start + window_size / 2
                total_subdivisions += 1

                if depth == 0:
                    click.echo(click.style(f" hit limit ({batch_count:,}), subdividing...", fg="yellow"))
                else:
                    click.echo(f"{indent}→ {batch_start.strftime('%H:%M')}-{batch_end.strftime('%H:%M')}: " +
                              click.style(f"hit limit, subdividing...", fg="yellow"))

                # Fetch both halves recursively (neither is first request anymore)
                # Pass incremental persistence context down
                left_dfs, left_subs, left_saved = fetch_with_subdivision(
                    client, batch_start, midpoint, depth + 1, is_first_request=False,
                    store=store, checkpoint_state=checkpoint_state, normalizer=normalizer,
                    backend_name=backend_name, batch_key=batch_key,
                )
                right_dfs, right_subs, right_saved = fetch_with_subdivision(
                    client, midpoint, batch_end, depth + 1, is_first_request=False,
                    store=store, checkpoint_state=checkpoint_state, normalizer=normalizer,
                    backend_name=backend_name, batch_key=batch_key,
                )

                return left_dfs + right_dfs, left_subs + right_subs + 1, left_saved + right_saved
            else:
                # Can't subdivide further, warn user
                if depth == 0:
                    click.echo(click.style(f" {batch_count:,} spans (at limit, window too small to subdivide)", fg="yellow"))
                else:
                    click.echo(f"{indent}→ {batch_start.strftime('%H:%M')}-{batch_end.strftime('%H:%M')}: " +
                              click.style(f"{batch_count:,} spans (at limit)", fg="yellow"))

                # In incremental mode, save this sub-batch immediately
                if incremental_mode and batch_key:
                    try:
                        if normalizer:
                            try:
                                normalized = normalizer(batch_df)
                                store.append_spans(normalized, backend=backend_name)
                            except Exception:
                                store.append_spans(batch_df, backend=f"{backend_name}-raw")
                        else:
                            store.append_spans(batch_df, backend=backend_name)
                        checkpoint_state.add_partial_range(batch_key, batch_start, batch_end, batch_count)
                        return [], 0, batch_count  # Return empty list since data is saved
                    except Exception as save_err:
                        click.echo(click.style(f" WARN: failed to save partial: {save_err}", fg="yellow"))

                return [batch_df], 0, 0

        # Normal case: didn't hit limit (this is a leaf sub-batch)
        if depth > 0:
            click.echo(f"{indent}→ {batch_start.strftime('%H:%M')}-{batch_end.strftime('%H:%M')}: " +
                      click.style(f"{batch_count:,} spans", fg="green"))

            # In incremental mode, save this sub-batch immediately
            if incremental_mode and batch_key:
                try:
                    if normalizer:
                        try:
                            normalized = normalizer(batch_df)
                            store.append_spans(normalized, backend=backend_name)
                        except Exception:
                            store.append_spans(batch_df, backend=f"{backend_name}-raw")
                    else:
                        store.append_spans(batch_df, backend=backend_name)
                    checkpoint_state.add_partial_range(batch_key, batch_start, batch_end, batch_count)
                    return [], 0, batch_count  # Return empty list since data is saved
                except Exception as save_err:
                    click.echo(click.style(f" WARN: failed to save partial: {save_err}", fg="yellow"))

        return [batch_df], 0, 0

    # Helper function to process a single source/backend
    def process_historical_sync(
        name: str,
        display_name: str,
        client_class: type,
        normalizer: callable,
        store: OxenStore,
        is_phoenix: bool = False,
        sqlite_client=None,  # Pre-created SQLite client (overrides client_class if provided)
    ) -> tuple[int, int, int]:
        """Process historical sync and return (spans, completed, failed)."""

        # Load or create checkpoint state
        checkpoint_state, is_resuming = HistoricalSyncState.load_or_create(
            source=name,
            target_start=sync_start_time,
            target_end=sync_end_time,
            config=sync_config,
            force_resume=force_resume,
        )

        if is_resuming and resume:
            progress = checkpoint_state.progress_percent
            click.echo(f"[{display_name}] Resuming from checkpoint ({progress:.1f}% complete)")
            click.echo(f"  Previously synced: {checkpoint_state.stats.total_spans:,} spans")
        else:
            click.echo(f"[{display_name}] Starting historical sync...")

        # Create client with timeout (or use pre-created SQLite client)
        try:
            if sqlite_client is not None:
                client = sqlite_client
                click.echo(f"  Using SQLite direct access")
            elif is_phoenix:
                client = client_class(timeout=float(timeout))
            else:
                client = client_class()
        except Exception as e:
            click.echo(click.style(f"  [FAIL] Could not create client: {e}", fg="red"))
            return 0, 0, 1

        # Get remaining ranges to process
        if is_resuming and resume:
            remaining_ranges = checkpoint_state.get_remaining_ranges()
            if not remaining_ranges:
                click.echo(click.style(f"  Already complete!", fg="green"))
                return checkpoint_state.stats.total_spans, checkpoint_state.stats.batches_completed, 0
        else:
            remaining_ranges = [(sync_start_time, sync_end_time)]

        # Generate batches from remaining ranges (most recent first)
        batches = []
        for range_start, range_end in remaining_ranges:
            batch_end = range_end
            while batch_end > range_start:
                batch_start = max(batch_end - batch_duration, range_start)
                batches.append((batch_start, batch_end))
                batch_end = batch_start

        # Sort by end time descending (most recent first)
        batches.sort(key=lambda x: x[1], reverse=True)

        # Also get previously failed batches from checkpoint for retry
        persisted_failed_ranges = [
            (r.start, r.end) for r in checkpoint_state.failed_ranges
        ]

        if not batches and not persisted_failed_ranges:
            click.echo(click.style(f"  No batches to process.", fg="yellow"))
            return checkpoint_state.stats.total_spans, checkpoint_state.stats.batches_completed, 0

        if batches:
            click.echo(f"  Processing {len(batches)} batches sequentially...")
        if persisted_failed_ranges:
            click.echo(click.style(f"  {len(persisted_failed_ranges)} previously failed batches to retry", fg="cyan"))

        # Show ETA if resuming
        if is_resuming and resume:
            eta = checkpoint_state.get_eta()
            if eta:
                click.echo(f"  Estimated time remaining: {eta}")
        click.echo()

        source_spans = checkpoint_state.stats.total_spans if (is_resuming and resume) else 0
        completed = checkpoint_state.stats.batches_completed if (is_resuming and resume) else 0
        failed = 0
        failed_batches: list[tuple[datetime, datetime]] = []  # Queue for retry

        def process_batch(batch_start: datetime, batch_end: datetime, batch_num: int, total: int, is_retry: bool = False) -> bool:
            """Process a single batch. Returns True if successful."""
            nonlocal source_spans, completed, failed

            # Format time range based on batch granularity
            if batch_hours:
                time_format = "%Y-%m-%d %H:%M"
            else:
                time_format = "%Y-%m-%d"

            # Calculate overall progress
            overall_progress = checkpoint_state.progress_percent
            retry_tag = click.style(" [retry]", fg="cyan") if is_retry else ""

            # Check for existing partial ranges (from previous interrupted subdivisions)
            batch_key = batch_start.strftime("%Y-%m-%d %H:%M:%S")
            partial_spans = checkpoint_state.get_partial_spans_count(batch_key)
            unfetched_ranges = checkpoint_state.get_unfetched_ranges(batch_start, batch_end)

            # If we have partial progress, show it
            partial_tag = ""
            if partial_spans > 0:
                partial_tag = click.style(f" [resuming, {partial_spans:,} spans already saved]", fg="cyan")

            click.echo(
                f"  [{overall_progress:.0f}%] Batch {batch_num}/{total}: "
                f"{batch_start.strftime(time_format)} to {batch_end.strftime(time_format)}{retry_tag}{partial_tag}",
                nl=False,
            )

            # If all ranges are already fetched, complete immediately
            if not unfetched_ranges:
                logger.info(f"Batch {batch_key}: All ranges already fetched, completing from partials")
                total_spans = checkpoint_state.complete_partial_day(batch_key, batch_start, batch_end)
                source_spans += total_spans
                click.echo(click.style(f" {total_spans:,} spans (from partial ranges)", fg="green"))
                completed += 1
                return True

            try:
                # Mark batch as started in checkpoint
                checkpoint_state.mark_batch_started(batch_start, batch_end)
                logger.debug(f"Batch {batch_key}: Starting with {len(unfetched_ranges)} unfetched range(s)")

                # Backend name for storage
                backend_name = f"{name}-historical"

                # Check failure history for aggressive pre-subdivision
                failure_count = checkpoint_state.get_failure_count(batch_key)
                batch_duration = batch_end - batch_start
                recommended_window = checkpoint_state.get_recommended_initial_window(batch_key, batch_duration)

                # If we need aggressive subdivision, pre-split ranges before querying
                ranges_to_fetch = unfetched_ranges
                if failure_count > 0 and recommended_window < batch_duration:
                    ranges_to_fetch = pre_subdivide_ranges(unfetched_ranges, recommended_window)
                    if len(ranges_to_fetch) > len(unfetched_ranges):
                        click.echo()
                        click.echo(
                            f"    {click.style('Aggressive mode:', fg='cyan')} "
                            f"{failure_count} failures → pre-splitting into {len(ranges_to_fetch)} chunks "
                            f"(max {recommended_window.total_seconds() / 3600:.1f}h each)"
                        )

                # Process each unfetched range
                all_dataframes = []
                total_subdivisions_batch = 0
                total_saved_incrementally = partial_spans  # Start with already-saved spans

                # In aggressive mode, save each chunk immediately to prevent data loss
                aggressive_mode = failure_count > 0 and len(ranges_to_fetch) > len(unfetched_ranges)

                for chunk_idx, (range_start, range_end) in enumerate(ranges_to_fetch):
                    logger.debug(f"Fetching range: {range_start.strftime('%Y-%m-%d %H:%M')} to {range_end.strftime('%Y-%m-%d %H:%M')}")
                    # Fetch with auto-subdivision and incremental persistence
                    # Pass context so sub-batches are saved immediately
                    dataframes, subdivisions, saved_incrementally = fetch_with_subdivision(
                        client, range_start, range_end,
                        store=store,
                        checkpoint_state=checkpoint_state,
                        normalizer=None if skip_normalize else normalizer,
                        backend_name=backend_name,
                        batch_key=batch_key,
                    )
                    total_subdivisions_batch += subdivisions
                    total_saved_incrementally += saved_incrementally

                    # In aggressive mode, save each chunk's DataFrames immediately
                    # This ensures we don't lose progress if a later chunk fails
                    if aggressive_mode and dataframes:
                        for df in dataframes:
                            try:
                                df_count = len(df)
                                if skip_normalize:
                                    store.append_spans(df, backend=backend_name)
                                else:
                                    try:
                                        normalized = normalizer(df)
                                        store.append_spans(normalized, backend=backend_name)
                                    except Exception:
                                        store.append_spans(df, backend=f"{backend_name}-raw")
                                checkpoint_state.add_partial_range(batch_key, range_start, range_end, df_count)
                                total_saved_incrementally += df_count
                                click.echo(
                                    f"    [chunk {chunk_idx + 1}/{len(ranges_to_fetch)}] "
                                    f"{range_start.strftime('%H:%M')}-{range_end.strftime('%H:%M')}: "
                                    + click.style(f"{df_count:,} spans saved", fg="green")
                                )
                            except Exception as save_err:
                                click.echo(click.style(f"    WARN: failed to save chunk: {save_err}", fg="yellow"))
                                all_dataframes.extend(dataframes)  # Keep for later attempt
                    else:
                        all_dataframes.extend(dataframes)

                # Handle any remaining dataframes not saved incrementally (non-subdivided data)
                if all_dataframes:
                    if len(all_dataframes) == 1:
                        combined_df = all_dataframes[0]
                    else:
                        combined_df = pd.concat(all_dataframes, ignore_index=True)

                    batch_count = len(combined_df)

                    # Save results (only the non-incrementally-saved portion)
                    if skip_normalize:
                        store.append_spans(combined_df, backend=backend_name)
                    else:
                        try:
                            normalized = normalizer(combined_df)
                            store.append_spans(normalized, backend=backend_name)
                        except Exception as e:
                            click.echo(
                                click.style(f" WARN: normalize failed, saving raw - {e}", fg="yellow")
                            )
                            store.append_spans(combined_df, backend=f"{backend_name}-raw")

                    total_saved_incrementally += batch_count

                # Total spans for this batch (incremental + final)
                batch_total = total_saved_incrementally
                source_spans += batch_total - partial_spans  # Don't double-count partial_spans

                # Fetch annotations for Phoenix (opt-in) - only for non-incrementally saved data
                annotations_count = 0
                if with_annotations and is_phoenix and all_dataframes:
                    try:
                        combined_for_annotations = pd.concat(all_dataframes, ignore_index=True) if len(all_dataframes) > 1 else all_dataframes[0]
                        annotations_df = client.get_span_annotations_dataframe(
                            spans_dataframe=combined_for_annotations,
                        )
                        if not annotations_df.empty:
                            normalized_annotations = normalize_phoenix_annotations(annotations_df)
                            store.append_spans(
                                normalized_annotations,
                                backend=f"{name}-historical-annotations",
                            )
                            annotations_count = len(annotations_df)
                    except Exception as ann_err:
                        click.echo(
                            click.style(f" WARN: annotations fetch failed - {ann_err}", fg="yellow")
                        )

                # Clear partial ranges and mark batch as completed
                checkpoint_state.clear_partial_ranges(batch_key)

                # Show result
                annotations_suffix = f", {annotations_count:,} annotations" if annotations_count > 0 else ""
                if total_subdivisions_batch == 0 and partial_spans == 0:
                    click.echo(click.style(f" {batch_total:,} spans{annotations_suffix}", fg="green"))
                else:
                    sub_count = total_subdivisions_batch + (1 if partial_spans > 0 else 0)
                    click.echo(f"    Total: " + click.style(f"{batch_total:,} spans{annotations_suffix}", fg="green") +
                              f" (from {sub_count + 1} sub-batches)")

                # Mark batch as completed in checkpoint
                checkpoint_state.mark_batch_completed(batch_start, batch_end, batch_total)
                if total_subdivisions_batch > 0:
                    for _ in range(total_subdivisions_batch):
                        checkpoint_state.add_subdivision()

                completed += 1
                return True

            except KeyboardInterrupt:
                raise  # Re-raise to handle at outer level

            except Exception as e:
                error_str = str(e).lower()
                partial_saved = checkpoint_state.get_partial_spans_count(batch_key)
                if "rate" in error_str or "limit" in error_str or "exhausted" in error_str:
                    click.echo(click.style(f" RATE LIMITED - will retry later", fg="yellow"))
                    logger.warning(f"Batch {batch_key}: Rate limited, {partial_saved:,} spans saved before failure")
                else:
                    click.echo(click.style(f" FAILED: {e}", fg="red"))
                    logger.error(f"Batch {batch_key}: Failed with error: {e}, {partial_saved:,} spans saved before failure", exc_info=True)
                checkpoint_state.mark_batch_failed()
                failed += 1
                return False

        # First pass: process all batches
        try:
            for i, (batch_start, batch_end) in enumerate(batches):
                batch_num = i + 1

                # Add delay between main batches (not on first batch)
                if i > 0 and delay > 0:
                    time.sleep(delay)

                success = process_batch(batch_start, batch_end, batch_num, len(batches))
                if not success:
                    failed_batches.append((batch_start, batch_end))

        except KeyboardInterrupt:
            click.echo()
            click.echo(click.style("  Interrupted! Progress saved.", fg="yellow"))
            click.echo(f"  Resume with: dal sync-historical --source {name}")
            raise SystemExit(130)

        # Retry pass: retry failed batches with longer delay
        if failed_batches:
            click.echo()
            click.echo(click.style(f"  Retrying {len(failed_batches)} failed batches with 10s delay...", fg="cyan"))
            retry_delay = max(delay * 5, 10.0)  # At least 10 seconds between retries

            still_failed = []
            try:
                for i, (batch_start, batch_end) in enumerate(failed_batches):
                    # Longer delay for retries
                    if i > 0:
                        time.sleep(retry_delay)
                    else:
                        time.sleep(retry_delay)  # Also wait before first retry

                    # Decrement failed count since we're retrying
                    failed -= 1

                    success = process_batch(batch_start, batch_end, i + 1, len(failed_batches), is_retry=True)
                    if not success:
                        still_failed.append((batch_start, batch_end))

            except KeyboardInterrupt:
                click.echo()
                click.echo(click.style("  Interrupted during retry! Progress saved.", fg="yellow"))
                click.echo(f"  Resume with: dal sync-historical --source {name}")
                raise SystemExit(130)

            if still_failed:
                click.echo()
                click.echo(click.style(f"  {len(still_failed)} batches still failed after retry:", fg="red"))
                for batch_start, batch_end in still_failed[:5]:
                    click.echo(f"    - {batch_start.strftime('%Y-%m-%d')} to {batch_end.strftime('%Y-%m-%d')}")
                if len(still_failed) > 5:
                    click.echo(f"    ... and {len(still_failed) - 5} more")

        # Retry pass for previously failed batches (from earlier runs)
        if persisted_failed_ranges:
            click.echo()
            click.echo(click.style(f"  Retrying {len(persisted_failed_ranges)} previously failed batches...", fg="cyan"))
            persisted_retry_delay = max(delay * 5, 10.0)  # At least 10 seconds between retries

            persisted_still_failed = []
            try:
                for i, (batch_start, batch_end) in enumerate(persisted_failed_ranges):
                    # Wait before each retry
                    if i > 0:
                        time.sleep(persisted_retry_delay)
                    else:
                        time.sleep(persisted_retry_delay)  # Also wait before first retry

                    # Clear from failed_ranges to mark retry attempt
                    checkpoint_state.clear_failed_range(batch_start, batch_end)

                    success = process_batch(batch_start, batch_end, i + 1, len(persisted_failed_ranges), is_retry=True)
                    if not success:
                        persisted_still_failed.append((batch_start, batch_end))

            except KeyboardInterrupt:
                click.echo()
                click.echo(click.style("  Interrupted during persisted retry! Progress saved.", fg="yellow"))
                click.echo(f"  Resume with: dal sync-historical --source {name}")
                raise SystemExit(130)

            if persisted_still_failed:
                click.echo()
                click.echo(click.style(f"  {len(persisted_still_failed)} previously failed batches still failed:", fg="red"))
                for batch_start, batch_end in persisted_still_failed[:5]:
                    click.echo(f"    - {batch_start.strftime('%Y-%m-%d')} to {batch_end.strftime('%Y-%m-%d')}")
                if len(persisted_still_failed) > 5:
                    click.echo(f"    ... and {len(persisted_still_failed) - 5} more")
            else:
                click.echo(click.style(f"  All {len(persisted_failed_ranges)} previously failed batches recovered!", fg="green"))

        click.echo()
        click.echo(f"  [{display_name}] Total: {source_spans:,} spans")

        # Check if sync is complete
        if checkpoint_state.is_complete:
            click.echo(click.style(f"  Historical sync complete for {name}!", fg="green"))

        return source_spans, completed, failed

    # Process named sources
    for source in sources_to_sync:
        # Create source-specific store
        source_store = OxenStore.for_source(source.name)

        # Set up environment and determine client/normalizer
        sqlite_client = None  # Pre-created SQLite client if using SQLite mode

        if source.source_type == SourceType.PHOENIX:
            if source.url:
                os.environ["DAL_PHOENIX_URL"] = source.url
            if source.project:
                os.environ["DAL_PHOENIX_PROJECT"] = source.project

            # Use SQLite client if enabled
            if use_sqlite and resolved_sqlite_container:
                db_path = f"docker://{resolved_sqlite_container}:/root/.phoenix/phoenix.db"
                sqlite_client = PhoenixSQLiteClient(
                    db_path=db_path,
                    project=source.project or os.getenv("DAL_PHOENIX_PROJECT", "dev-agent-lens"),
                )
                # Test connection before proceeding
                try:
                    if not sqlite_client.test_connection():
                        click.echo(click.style(
                            f"Error: Could not connect to Phoenix SQLite database in container '{resolved_sqlite_container}'",
                            fg="red",
                        ))
                        raise SystemExit(1)
                except Exception as e:
                    click.echo(click.style(
                        f"Error: SQLite connection test failed: {e}",
                        fg="red",
                    ))
                    raise SystemExit(1)

            client_class = PhoenixClient
            normalizer = normalize_phoenix
            is_phoenix = True
        else:  # ARIZE
            if source.space_key:
                os.environ["ARIZE_SPACE_KEY"] = source.space_key
            if source.model_id:
                os.environ["ARIZE_MODEL_ID"] = source.model_id
            client_class = ArizeClient
            normalizer = normalize_arize
            is_phoenix = False

        spans, completed, failed = process_historical_sync(
            source.name,
            source.get_display_info(),
            client_class,
            normalizer,
            source_store,
            is_phoenix=is_phoenix,
            sqlite_client=sqlite_client,
        )
        total_spans += spans
        total_batches_completed += completed
        total_batches_failed += failed
        click.echo()

    # Process legacy backends (backward compatibility)
    if use_legacy_mode:
        store = OxenStore()
        for backend_id in backends_to_sync:
            config = BACKENDS[backend_id]
            spans, completed, failed = process_historical_sync(
                backend_id,
                config["name"],
                config["client_class"],
                config["normalizer"],
                store,
                is_phoenix=(backend_id == "phoenix-local"),
            )
            total_spans += spans
            total_batches_completed += completed
            total_batches_failed += failed
            if failed > 0:
                all_errors.append(f"{backend_id}: {failed} batches failed")
            click.echo()

    # Final summary
    elapsed = time.time() - sync_start
    click.echo("=" * 50)
    click.echo(click.style("Historical Sync Summary", bold=True))
    click.echo("=" * 50)
    click.echo(f"Total spans fetched: {total_spans:,}")
    click.echo(f"Batches completed: {total_batches_completed}")
    click.echo(f"Batches failed: {total_batches_failed}")
    if total_subdivisions > 0:
        click.echo(f"Auto-subdivisions: {total_subdivisions}")
    click.echo(f"Time elapsed: {elapsed:.1f}s")

    if all_errors:
        click.echo()
        click.echo(click.style("Errors:", fg="red"))
        for error in all_errors[:10]:  # Show first 10 errors
            click.echo(f"  - {error}")
        if len(all_errors) > 10:
            click.echo(f"  ... and {len(all_errors) - 10} more")
        raise SystemExit(1)

    # Update sync state so dal sync picks up from here
    # Only update if we had successful batches and no failures
    if total_batches_completed > 0 and total_batches_failed == 0:
        from dev_agent_lens.core.state import SyncState
        state = SyncState()
        sync_time = sync_end_time  # Use the end time of the sync, not current time

        for source in sources_to_sync:
            state.set_last_sync(source.name, sync_time)
            click.echo(f"Updated sync state for '{source.name}' to {sync_time.strftime('%Y-%m-%d')}")
            # Clean up completed historical sync checkpoint
            clear_historical_sync(source.name)

        for backend_id in backends_to_sync:
            state.set_last_sync(backend_id, sync_time)
            click.echo(f"Updated sync state for '{backend_id}' to {sync_time.strftime('%Y-%m-%d')}")
            clear_historical_sync(backend_id)

        click.echo()
        click.echo("Future 'dal sync' commands will continue from this point.")

    click.echo()
    click.echo(click.style("Historical sync complete!", fg="green"))


@main.group()
def config() -> None:
    """Manage DAL configuration and sources.

    Use subcommands to manage named source profiles:

        dal config show              Show current configuration

        dal config add-source        Add a new source

        dal config list-sources      List configured sources

        dal config remove-source     Remove a source
    """
    pass


@config.command("show")
def config_show() -> None:
    """Show current DAL configuration."""
    from dev_agent_lens.core.sources import SourceManager

    click.echo(click.style("DAL Configuration", bold=True))
    click.echo()

    # Show legacy backends (env-based)
    click.echo(click.style("Legacy Backends (env-based):", underline=True))
    for backend_id, backend_config in BACKENDS.items():
        env_var = backend_config["env_check"]
        is_configured = bool(os.getenv(env_var))
        status = click.style("configured", fg="green") if is_configured else click.style("not set", fg="red")
        click.echo(f"  {backend_id}: {status} ({env_var})")

    click.echo()

    # Show configured sources
    manager = SourceManager()
    sources = manager.list_sources()

    click.echo(click.style("Named Sources:", underline=True))
    if sources:
        for source in sources:
            local_tag = click.style("[local]", fg="yellow") if source.local_only else click.style("[shared]", fg="green")
            click.echo(f"  {source.name}: {source.get_display_info()} {local_tag}")
    else:
        click.echo("  No named sources configured. Use 'dal config add-source' to add one.")

    click.echo()

    # Show default backend
    default = get_default_backend()
    if default:
        click.echo(f"Default backend: {default}")
    else:
        click.echo(click.style("No backends configured", fg="yellow"))

    click.echo()

    # Show Oxen status
    oxen_url = os.getenv("OXEN_REMOTE_URL")
    if oxen_url:
        click.echo(f"Oxen remote: {click.style('enabled', fg='green')}")
    else:
        click.echo(f"Oxen remote: {click.style('disabled', fg='yellow')}")

    # Show data path
    data_path = os.getenv("DAL_DATA_PATH", "~/.dal/data")
    click.echo(f"Data path: {data_path}")


@config.command("add-source")
@click.argument("name")
@click.option(
    "--type",
    "source_type",
    type=click.Choice(["phoenix", "arize"]),
    required=True,
    help="Type of backend (phoenix or arize)",
)
@click.option(
    "--url",
    help="Phoenix server URL (e.g., localhost:6006)",
)
@click.option(
    "--project",
    help="Phoenix project name",
)
@click.option(
    "--space-key",
    help="Arize space key",
)
@click.option(
    "--model-id",
    help="Arize model ID",
)
@click.option(
    "--local-only/--shared",
    default=True,
    help="Mark as local-only (won't sync to Oxen) or shared",
)
@click.option(
    "--sqlite-container",
    help="Docker container name for direct SQLite access (Phoenix only)",
)
def config_add_source(
    name: str,
    source_type: str,
    url: str | None,
    project: str | None,
    space_key: str | None,
    model_id: str | None,
    local_only: bool,
    sqlite_container: str | None,
) -> None:
    """Add a new named source.

    Examples:

        dal config add-source phoenix-local --type phoenix --url localhost:6006

        dal config add-source arize-team --type arize --space-key ABC --model-id my-model --shared

        dal config add-source phoenix-docker --type phoenix --url localhost:6006 --sqlite-container dev-agent-lens-phoenix-1
    """
    from dev_agent_lens.core.sources import SourceConfig, SourceManager, SourceType

    # Create source config
    source = SourceConfig(
        name=name,
        source_type=SourceType(source_type),
        local_only=local_only,
        url=url,
        project=project,
        sqlite_container=sqlite_container,
        space_key=space_key,
        model_id=model_id,
    )

    # Validate
    errors = source.validate()
    if errors:
        for error in errors:
            click.echo(click.style(f"Error: {error}", fg="red"))
        raise SystemExit(1)

    # Save
    manager = SourceManager()
    existing = manager.get_source(name)

    try:
        manager.add_source(source)
        if existing:
            click.echo(click.style(f"Updated source: {name}", fg="yellow"))
        else:
            click.echo(click.style(f"Added source: {name}", fg="green"))

        click.echo(f"  Type: {source_type}")
        click.echo(f"  {source.get_display_info()}")
        click.echo(f"  Local only: {local_only}")

    except ValueError as e:
        click.echo(click.style(f"Error: {e}", fg="red"))
        raise SystemExit(1)


@config.command("list-sources")
@click.option(
    "--output",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format",
)
def config_list_sources(output: str) -> None:
    """List all configured sources.

    Examples:

        dal config list-sources

        dal config list-sources --output json
    """
    import json as json_lib

    from dev_agent_lens.core.sources import SourceManager

    manager = SourceManager()
    sources = manager.list_sources()

    if not sources:
        click.echo("No sources configured. Use 'dal config add-source' to add one.")
        return

    if output == "json":
        data = {
            "sources": [
                {"name": s.name, **s.to_dict()}
                for s in sources
            ]
        }
        click.echo(json_lib.dumps(data, indent=2))
    else:
        click.echo(click.style("Configured Sources", bold=True))
        click.echo()
        click.echo(f"{'Name':<20} {'Type':<10} {'Details':<30} {'Sync':<10}")
        click.echo("-" * 70)

        for source in sources:
            sync_status = "shared" if not source.local_only else "local"
            sync_color = "green" if not source.local_only else "yellow"
            click.echo(
                f"{source.name:<20} "
                f"{source.source_type.value:<10} "
                f"{source.get_display_info():<30} "
                f"{click.style(sync_status, fg=sync_color):<10}"
            )


@config.command("remove-source")
@click.argument("name")
@click.option(
    "--force",
    is_flag=True,
    help="Skip confirmation prompt",
)
def config_remove_source(name: str, force: bool) -> None:
    """Remove a configured source.

    Examples:

        dal config remove-source phoenix-old

        dal config remove-source arize-test --force
    """
    from dev_agent_lens.core.sources import SourceManager

    manager = SourceManager()
    source = manager.get_source(name)

    if not source:
        click.echo(click.style(f"Source not found: {name}", fg="red"))
        raise SystemExit(1)

    if not force:
        click.echo(f"Source: {name}")
        click.echo(f"  {source.get_display_info()}")
        if not click.confirm("Are you sure you want to remove this source?"):
            click.echo("Cancelled.")
            return

    manager.remove_source(name)
    click.echo(click.style(f"Removed source: {name}", fg="green"))


@config.command("import-env")
def config_import_env() -> None:
    """Import sources from environment variables.

    Creates named sources based on DAL_PHOENIX_URL and ARIZE_API_KEY.
    This provides a migration path from env-based to named source configuration.

    Examples:

        dal config import-env
    """
    from dev_agent_lens.core.sources import SourceManager, create_source_from_env

    sources = create_source_from_env()

    if not sources:
        click.echo("No sources found in environment variables.")
        click.echo("Set DAL_PHOENIX_URL or ARIZE_API_KEY to import.")
        return

    manager = SourceManager()

    for source in sources:
        existing = manager.get_source(source.name)
        if existing:
            click.echo(f"Skipping {source.name} (already exists)")
            continue

        manager.add_source(source)
        click.echo(click.style(f"Imported: {source.name}", fg="green"))
        click.echo(f"  {source.get_display_info()}")


@config.command("migrate")
@click.argument("source_name")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be migrated without making changes",
)
def config_migrate(source_name: str, dry_run: bool) -> None:
    """Migrate legacy data to a named source.

    Moves existing session and raw files from the flat directory structure
    to source-specific subdirectories.

    Examples:

        dal config migrate phoenix-alex           # Migrate to source

        dal config migrate phoenix-alex --dry-run # Preview migration
    """
    store = OxenStore()

    if not store.has_legacy_data():
        click.echo("No legacy data found to migrate.")
        click.echo("Legacy data is stored directly in ~/.dal/data/sessions/ (not in subdirectories).")
        return

    if dry_run:
        click.echo(click.style("DRY RUN - No changes will be made", fg="yellow"))
        click.echo()

        # Count files that would be migrated
        session_count = 0
        raw_count = 0

        sessions_base = store.data_path / "sessions"
        raw_base = store.data_path / "raw"

        if sessions_base.exists():
            for path in sessions_base.iterdir():
                if path.is_file() and path.suffix == ".jsonl":
                    click.echo(f"  Would migrate: sessions/{path.name}")
                    session_count += 1
                elif path.is_symlink() and path.name == "sessions_current.jsonl":
                    click.echo(f"  Would migrate: sessions/{path.name} (symlink)")
                    session_count += 1

        if raw_base.exists():
            for path in raw_base.iterdir():
                if path.is_file() and path.suffix == ".jsonl":
                    click.echo(f"  Would migrate: raw/{path.name}")
                    raw_count += 1

        click.echo()
        click.echo(f"Total: {session_count} session files, {raw_count} raw files")
        click.echo(f"Target: {source_name}/")
        return

    # Perform migration
    click.echo(f"Migrating legacy data to source: {source_name}")
    stats = store.migrate_legacy_to_source(source_name)

    click.echo()
    click.echo(click.style("Migration complete!", fg="green"))
    click.echo(f"  Sessions migrated: {stats['sessions_migrated']}")
    click.echo(f"  Raw files migrated: {stats['raw_migrated']}")

    if stats['errors']:
        click.echo()
        click.echo(click.style("Errors:", fg="red"))
        for error in stats['errors']:
            click.echo(f"  {error}")


def _get_sessions_file_for_source(source_name: str | None) -> Path | None:
    """Get the sessions file path for a source or legacy mode.

    Args:
        source_name: Named source, or None for legacy mode.

    Returns:
        Path to the sessions file, or None if not found.
    """
    if source_name:
        store = OxenStore.for_source(source_name)
    else:
        store = OxenStore()

    current_sessions_file = store.sessions_dir / "sessions_current.jsonl"
    if current_sessions_file.exists():
        return current_sessions_file

    # Try dated file
    session_files = list(store.sessions_dir.glob("sessions_*.jsonl"))
    if session_files:
        return max(session_files, key=lambda p: p.stat().st_mtime)

    return None


@main.command()
@click.argument("file", required=False, type=click.Path(exists=True))
@click.option(
    "--source",
    "source_name",
    type=str,
    help="Load data from a named source",
)
@click.option(
    "--by-session",
    is_flag=True,
    help="Show stats grouped by session",
)
@click.option(
    "--output",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format (default: table)",
)
@click.option(
    "--top-tools",
    type=int,
    default=10,
    help="Number of top tools to show (default: 10)",
)
def stats(file: str | None, source_name: str | None, by_session: bool, output: str, top_tools: int) -> None:
    """Show statistics for trace data.

    By default, shows stats for the current sessions file.
    Optionally specify a FILE to analyze a specific JSONL file.

    Examples:

        dal stats                        # Stats for current sessions

        dal stats --source phoenix-alex  # Stats for named source

        dal stats myfile.jsonl           # Stats for specific file

        dal stats --by-session           # Breakdown by session

        dal stats --output json          # JSON output
    """
    import json as json_lib
    from pathlib import Path

    from dev_agent_lens.analysis import (
        aggregate_session_metrics,
        aggregate_tools,
        compute_session_metrics_batch,
        detect_churn,
        detect_failures,
        get_churn_summary,
        get_classification_summary,
        get_failure_summary,
        get_top_tools,
        session_metrics,
    )
    from dev_agent_lens.query import query

    # Load spans
    if file:
        file_path = Path(file)
        click.echo(f"Loading spans from: {file_path}")
        spans = []
        with open(file_path) as f:
            for line in f:
                if line.strip():
                    spans.append(json_lib.loads(line))
    else:
        # Use current sessions file (from source or legacy)
        current_sessions_file = _get_sessions_file_for_source(source_name)
        if not current_sessions_file:
            if source_name:
                click.echo(
                    click.style(f"No session data found for source '{source_name}'. Run 'dal sync --source {source_name}' first.", fg="yellow")
                )
            else:
                click.echo(
                    click.style("No session data found. Run 'dal sync' first.", fg="yellow")
                )
            return

        source_info = f" (source: {source_name})" if source_name else ""
        click.echo(f"Loading spans from: {current_sessions_file}{source_info}")
        spans = []
        with open(current_sessions_file) as f:
            for line in f:
                if line.strip():
                    spans.append(json_lib.loads(line))

    if not spans:
        click.echo(click.style("No spans found in file.", fg="yellow"))
        return

    click.echo(f"Loaded {len(spans):,} spans")
    click.echo()

    # Compute stats
    classification = get_classification_summary(spans)
    tools = aggregate_tools(spans)
    failures = detect_failures(spans)
    failure_summary = get_failure_summary(failures)
    churn = detect_churn(spans)
    churn_summary = get_churn_summary(churn)

    # Get session-level metrics
    result = query(spans=spans)  # Groups by session
    session_metrics_list = compute_session_metrics_batch(result.sessions)
    aggregate_metrics = aggregate_session_metrics(session_metrics_list)

    if output == "json":
        # JSON output
        output_data = {
            "summary": {
                "total_spans": len(spans),
                "total_sessions": result.total_sessions,
            },
            "classification": classification,
            "tools": tools.to_dict(),
            "failures": failure_summary,
            "churn": churn_summary,
            "session_metrics": aggregate_metrics,
        }
        click.echo(json_lib.dumps(output_data, indent=2, default=str))
    else:
        # Table output
        click.echo(click.style("=" * 60, bold=True))
        click.echo(click.style("                    DAL Statistics", bold=True))
        click.echo(click.style("=" * 60, bold=True))
        click.echo()

        # Summary
        click.echo(click.style("Summary", bold=True, underline=True))
        click.echo(f"  Total Spans:    {len(spans):,}")
        click.echo(f"  Total Sessions: {result.total_sessions:,}")
        click.echo()

        # Classification breakdown
        click.echo(click.style("Span Classification", bold=True, underline=True))
        for category, count in sorted(classification.items(), key=lambda x: -x[1]):
            if count > 0:
                pct = (count / len(spans)) * 100
                click.echo(f"  {category:20} {count:8,} ({pct:5.1f}%)")
        click.echo()

        # Tool statistics
        click.echo(click.style("Tool Statistics", bold=True, underline=True))
        click.echo(f"  Total Tool Calls: {tools.total_tool_calls:,}")
        click.echo(f"  Success Rate:     {tools.overall_success_rate:.1f}%")
        click.echo()

        if tools.tools:
            click.echo("  Top Tools:")
            top = get_top_tools(tools, n=top_tools)
            for t in top:
                click.echo(
                    f"    {t['name']:20} {t['total_calls']:6,} calls "
                    f"({t['success_rate']:.0f}% success)"
                )
        click.echo()

        # Failure statistics
        click.echo(click.style("Failure Analysis", bold=True, underline=True))
        click.echo(f"  Total Failures: {failure_summary['total_failures']}")
        by_type = failure_summary.get("by_type", {})
        if any(v > 0 for v in by_type.values()):
            click.echo("  By Type:")
            for ftype, count in by_type.items():
                if count > 0:
                    click.echo(f"    {ftype:15} {count:,}")
        click.echo()

        # Churn statistics
        click.echo(click.style("Code Churn Analysis", bold=True, underline=True))
        click.echo(f"  Churn Detected:     {'Yes' if churn_summary['has_churn'] else 'No'}")
        click.echo(f"  Churn Ratio:        {churn_summary['churn_ratio']:.2f}")
        click.echo(f"  Multi-Edit Files:   {churn_summary['multi_edit_file_count']}")
        click.echo(f"  Write-Then-Edit:    {churn_summary['write_edit_file_count']}")
        click.echo()

        # Session metrics aggregate
        click.echo(click.style("Session Metrics (Aggregate)", bold=True, underline=True))
        click.echo(f"  Avg Turns/Session:  {aggregate_metrics.get('avg_turns_per_session', 0):.1f}")
        click.echo(f"  Avg Duration:       {aggregate_metrics.get('avg_duration_minutes', 0):.1f} min")
        click.echo(f"  Avg Tokens/Session: {aggregate_metrics.get('avg_tokens_per_session', 0):,.0f}")
        click.echo(f"  Total Tool Calls:   {aggregate_metrics.get('total_tool_calls', 0):,}")
        click.echo(f"  Total Failures:     {aggregate_metrics.get('total_failures', 0):,}")
        click.echo()

        # Per-session breakdown if requested
        if by_session and session_metrics_list:
            click.echo(click.style("Per-Session Breakdown", bold=True, underline=True))
            for sm in session_metrics_list[:20]:  # Limit to 20 sessions
                session_id = sm.session_id or "unknown"
                if len(session_id) > 30:
                    session_id = session_id[:27] + "..."
                click.echo(
                    f"  {session_id:30} "
                    f"turns={sm.turn_count:3} "
                    f"tools={sm.tool_call_count:4} "
                    f"tokens={sm.token_count_total:7,}"
                )
            if len(session_metrics_list) > 20:
                click.echo(f"  ... and {len(session_metrics_list) - 20} more sessions")
            click.echo()

        click.echo(click.style("=" * 60, bold=True))


@main.command()
def status() -> None:
    """Show sync status for each backend."""
    state = SyncState()
    store = OxenStore()

    click.echo(click.style("Sync Status", bold=True))
    click.echo()

    backends = state.get_all_backends()
    if not backends:
        click.echo("No sync history yet. Run 'dal sync' to start.")
        return

    for backend_id in backends:
        last_sync = state.get_last_sync(backend_id)
        if last_sync:
            click.echo(f"{backend_id}: Last sync {last_sync.isoformat()}")
        else:
            click.echo(f"{backend_id}: Never synced")

    click.echo()

    # Show storage stats
    raw_files = store.get_raw_files()
    click.echo(f"Raw files: {len(raw_files)}")

    current_sessions = store.get_current_sessions()
    click.echo(f"Total sessions: {len(current_sessions) if not current_sessions.empty else 0}")


@main.command()
@click.argument("session_id")
@click.option(
    "--model",
    type=str,
    help="LLM model to use (default: gpt-5-nano)",
)
@click.option(
    "--max-spans",
    type=int,
    help="Maximum spans to include (default: 100)",
)
@click.option(
    "--output",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format (default: text)",
)
@click.option(
    "--prompt-file",
    type=click.Path(exists=True),
    help="Custom prompt file to use",
)
@click.option(
    "--preview",
    is_flag=True,
    help="Show preview without calling LLM",
)
@click.option(
    "--source",
    type=str,
    help="Data source to query (e.g., 'phoenix-local-alex', 'arize-ax-alex')",
)
@click.option(
    "--parquet/--no-parquet",
    default=True,
    help="Use Parquet backend when available (default: True)",
)
def summarize(
    session_id: str,
    model: str | None,
    max_spans: int | None,
    output: str,
    prompt_file: str | None,
    preview: bool,
    source: str | None,
    parquet: bool,
) -> None:
    """Generate an LLM-powered summary of a session.

    Requires OPENAI_API_KEY to be set in ~/.dal/.env or environment.

    Examples:

        dal summarize abc123              # Summarize session abc123

        dal summarize abc123 --max-spans 50  # Limit to 50 spans

        dal summarize abc123 --preview    # Preview without LLM call

        dal summarize abc123 --output json

        dal summarize abc123 --source phoenix-local-alex  # Use specific source
    """
    import json as json_lib
    from pathlib import Path

    from dev_agent_lens.query import query_sessions
    from dev_agent_lens.llm import (
        NoLLMConfigError,
        check_llm_availability,
        get_summary_preview,
        summarize_session_sync,
    )

    # Query for the session using query_sessions (supports Parquet)
    sessions = query_sessions(
        session_id=session_id,
        source=source,
        prefer_parquet=parquet,
    )

    if not sessions:
        click.echo(click.style(f"Session '{session_id}' not found.", fg="red"))
        return

    session = sessions[0]

    # Apply --max-spans
    original_span_count = len(session.get("spans", []))
    if max_spans is not None:
        spans = session.get("spans", [])
        if len(spans) > max_spans:
            session["spans"] = spans[:max_spans]
            click.echo(f"Truncated spans: {original_span_count} → {max_spans}")

    click.echo(f"Session: {session_id}")
    click.echo(f"Spans: {len(session.get('spans', []))}")
    click.echo()

    if preview:
        # Preview mode (summarize)
        preview_data = get_summary_preview(session)
        if output == "json":
            click.echo(json_lib.dumps(preview_data, indent=2, default=str))
        else:
            click.echo(click.style("Preview (no LLM call):", bold=True))
            click.echo(f"  Prompt length: {preview_data['prompt_length']} chars")
            click.echo(f"  Estimated tokens: {preview_data['estimated_tokens']}")
            click.echo(f"  Categories: {preview_data['batch_summary']['categories']}")
            click.echo(f"  Has errors: {preview_data['batch_summary']['has_errors']}")
        return

    # Check LLM availability
    availability = check_llm_availability()
    if not availability["summarize_available"]:
        click.echo(
            click.style(
                "No LLM configured for summarization.\n"
                "Set OPENAI_API_KEY in ~/.dal/.env or environment.",
                fg="red",
            )
        )
        return

    click.echo("Generating summary...")
    try:
        summary = summarize_session_sync(
            session,
            model=model,
            prompt_file=prompt_file,
        )

        if output == "json":
            click.echo(json_lib.dumps(summary.to_dict(), indent=2, default=str))
        else:
            click.echo()
            click.echo(click.style("=" * 60, bold=True))
            click.echo(click.style("Session Summary", bold=True))
            click.echo(click.style("=" * 60, bold=True))
            click.echo()
            click.echo(summary.summary)
            click.echo()
            click.echo(click.style("-" * 60, dim=True))
            click.echo(f"Model: {summary.model_used}")
            click.echo(f"Tokens used: {summary.tokens_used.get('total_tokens', 'N/A')}")

    except NoLLMConfigError as e:
        click.echo(click.style(f"LLM Error: {e}", fg="red"))
    except Exception as e:
        click.echo(click.style(f"Error: {e}", fg="red"))


@main.command()
@click.option(
    "--sessions",
    type=click.Path(exists=True),
    help="Sessions file to cluster (default: current sessions)",
)
@click.option(
    "--limit",
    type=int,
    help="Maximum number of sessions to process",
)
@click.option(
    "--sample",
    type=int,
    help="Randomly sample N sessions (for cost control)",
)
@click.option(
    "--max-spans",
    type=int,
    help="Maximum spans per session to include (default: 100)",
)
@click.option(
    "--n-clusters",
    type=int,
    help="Number of clusters (auto-detected if not specified)",
)
@click.option(
    "--min-cluster-size",
    type=int,
    default=2,
    help="Minimum sessions per cluster (default: 2)",
)
@click.option(
    "--model",
    type=str,
    help="Embedding model to use (default: text-embedding-3-small)",
)
@click.option(
    "--output",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format (default: table)",
)
@click.option(
    "--preview",
    is_flag=True,
    help="Show preview without calling LLM",
)
@click.option(
    "--no-labels",
    is_flag=True,
    help="Skip LLM label generation",
)
@click.option(
    "--source",
    type=str,
    help="Data source to query (e.g., 'phoenix-local-alex', 'arize-ax-alex')",
)
@click.option(
    "--parquet/--no-parquet",
    default=True,
    help="Use Parquet backend when available (default: True)",
)
def cluster(
    sessions: str | None,
    limit: int | None,
    sample: int | None,
    max_spans: int | None,
    n_clusters: int | None,
    min_cluster_size: int,
    model: str | None,
    output: str,
    preview: bool,
    no_labels: bool,
    source: str | None,
    parquet: bool,
) -> None:
    """Cluster sessions by behavioral similarity.

    Requires OPENAI_API_KEY for embeddings.

    Examples:

        dal cluster                     # Cluster current sessions

        dal cluster --n-clusters 5      # Force 5 clusters

        dal cluster --limit 50          # Process at most 50 sessions

        dal cluster --sample 20         # Randomly sample 20 sessions

        dal cluster --preview           # Preview without LLM call

        dal cluster --source phoenix-local-alex  # Use specific source
    """
    import json as json_lib
    from pathlib import Path

    from dev_agent_lens.query import query, query_sessions
    from dev_agent_lens.llm import (
        NoLLMConfigError,
        check_llm_availability,
        cluster_sessions_sync,
        get_cluster_preview,
    )

    # Load sessions - prefer --source with Parquet, fall back to --sessions file
    if sessions:
        # Explicit file path provided - use legacy query
        file_path = Path(sessions)
        result = query(file_path=file_path)
        if not result.sessions:
            click.echo(click.style("No sessions found.", fg="yellow"))
            return
        sessions_to_process = result.sessions
    else:
        # Use query_sessions with Parquet support
        sessions_to_process = query_sessions(
            source=source,
            prefer_parquet=parquet,
        )
        if not sessions_to_process:
            click.echo(click.style("No sessions found. Run 'dal sync' first.", fg="yellow"))
            return
    original_count = len(sessions_to_process)

    # Apply --sample first (random sampling)
    if sample is not None and sample < len(sessions_to_process):
        import random
        sessions_to_process = random.sample(sessions_to_process, sample)
        click.echo(f"Sampled {sample} of {original_count} sessions")

    # Apply --limit (take first N)
    if limit is not None and limit < len(sessions_to_process):
        sessions_to_process = sessions_to_process[:limit]
        click.echo(f"Limited to {limit} sessions")

    # Apply --max-spans (truncate spans per session)
    if max_spans is not None:
        for session in sessions_to_process:
            spans = session.get("spans", [])
            if len(spans) > max_spans:
                session["spans"] = spans[:max_spans]
        click.echo(f"Max spans per session: {max_spans}")

    click.echo(f"Sessions to cluster: {len(sessions_to_process)}")

    if len(sessions_to_process) < 2:
        click.echo(click.style("Need at least 2 sessions for clustering.", fg="red"))
        return

    if preview:
        # Preview mode
        preview_data = get_cluster_preview(sessions_to_process)
        if output == "json":
            click.echo(json_lib.dumps(preview_data, indent=2, default=str))
        else:
            click.echo()
            click.echo(click.style("Preview (no LLM call):", bold=True))
            click.echo(f"  Sessions: {preview_data['n_sessions']}")
            click.echo(f"  Estimated embedding tokens: {preview_data['estimated_embedding_tokens']}")
            click.echo(f"  Recommended clusters: {preview_data['recommended_clusters']}")
            click.echo()
            click.echo("  Sample session texts:")
            for i, text in enumerate(preview_data["sample_texts"][:3]):
                click.echo(f"    {i+1}. {text[:80]}...")
        return

    # Check LLM availability
    availability = check_llm_availability()
    if not availability["cluster_available"]:
        click.echo(
            click.style(
                "OpenAI required for clustering (embeddings).\n"
                "Set OPENAI_API_KEY in ~/.dal/.env or environment.",
                fg="red",
            )
        )
        return

    click.echo("Clustering sessions...")
    try:
        result_clusters = cluster_sessions_sync(
            sessions_to_process,
            n_clusters=n_clusters,
            min_cluster_size=min_cluster_size,
            model=model,
            generate_labels=not no_labels,
        )

        if output == "json":
            click.echo(json_lib.dumps(result_clusters.to_dict(), indent=2, default=str))
        else:
            click.echo()
            click.echo(click.style("=" * 60, bold=True))
            click.echo(click.style("Clustering Results", bold=True))
            click.echo(click.style("=" * 60, bold=True))
            click.echo()
            click.echo(f"Clusters found: {result_clusters.n_clusters}")
            click.echo(f"Sessions clustered: {result_clusters.n_sessions}")
            if result_clusters.silhouette_score is not None:
                click.echo(f"Silhouette score: {result_clusters.silhouette_score:.3f}")
            click.echo()

            for cluster in result_clusters.clusters:
                click.echo(click.style(f"Cluster {cluster.cluster_id + 1}: {cluster.label}", bold=True))
                click.echo(f"  Size: {cluster.size} sessions")
                click.echo(f"  Sessions: {', '.join(cluster.session_ids[:5])}")
                if len(cluster.session_ids) > 5:
                    click.echo(f"    ... and {len(cluster.session_ids) - 5} more")
                click.echo()

            if result_clusters.outliers:
                # Filter out None values from outliers
                outlier_ids = [o for o in result_clusters.outliers if o is not None]
                if outlier_ids:
                    click.echo(click.style("Outliers:", dim=True))
                    click.echo(f"  {', '.join(outlier_ids)}")

    except NoLLMConfigError as e:
        click.echo(click.style(f"LLM Error: {e}", fg="red"))
    except ImportError as e:
        click.echo(click.style(f"Missing dependency: {e}", fg="red"))
    except Exception as e:
        click.echo(click.style(f"Error: {e}", fg="red"))


@main.command()
@click.argument("session_id")
@click.option(
    "--model",
    type=str,
    help="LLM model to use (default: gpt-5-nano)",
)
@click.option(
    "--max-spans",
    type=int,
    help="Maximum spans to include (default: 100)",
)
@click.option(
    "--category",
    type=click.Choice(["error", "efficiency", "churn", "best_practice", "performance"]),
    multiple=True,
    help="Focus on specific categories",
)
@click.option(
    "--output",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format (default: text)",
)
@click.option(
    "--prompt-file",
    type=click.Path(exists=True),
    help="Custom prompt file to use",
)
@click.option(
    "--preview",
    is_flag=True,
    help="Show heuristic suggestions only (no LLM)",
)
@click.option(
    "--source",
    type=str,
    help="Data source to query (e.g., 'phoenix-local-alex', 'arize-ax-alex')",
)
@click.option(
    "--parquet/--no-parquet",
    default=True,
    help="Use Parquet backend when available (default: True)",
)
def suggest(
    session_id: str,
    model: str | None,
    max_spans: int | None,
    category: tuple[str, ...],
    output: str,
    prompt_file: str | None,
    preview: bool,
    source: str | None,
    parquet: bool,
) -> None:
    """Generate improvement suggestions for a session.

    Requires OPENAI_API_KEY for full suggestions.
    Use --preview for heuristic-only suggestions without API calls.

    Examples:

        dal suggest abc123                      # Get suggestions

        dal suggest abc123 --max-spans 50       # Limit to 50 spans

        dal suggest abc123 --preview            # Heuristic only

        dal suggest abc123 --category error     # Focus on errors

        dal suggest abc123 --source phoenix-local-alex  # Use specific source
    """
    import json as json_lib
    from pathlib import Path

    from dev_agent_lens.query import query_sessions
    from dev_agent_lens.llm import (
        NoLLMConfigError,
        check_llm_availability,
        get_suggestion_preview,
        suggest_improvements_sync,
    )

    # Query for the session using query_sessions (supports Parquet)
    sessions = query_sessions(
        session_id=session_id,
        source=source,
        prefer_parquet=parquet,
    )

    if not sessions:
        click.echo(click.style(f"Session '{session_id}' not found.", fg="red"))
        return

    session = sessions[0]

    # Apply --max-spans
    original_span_count = len(session.get("spans", []))
    if max_spans is not None:
        spans = session.get("spans", [])
        if len(spans) > max_spans:
            session["spans"] = spans[:max_spans]
            click.echo(f"Truncated spans: {original_span_count} → {max_spans}")

    click.echo(f"Session: {session_id}")
    click.echo(f"Spans: {len(session.get('spans', []))}")
    click.echo()

    if preview:
        # Preview mode (heuristic only, suggest)
        preview_data = get_suggestion_preview(session)
        if output == "json":
            click.echo(json_lib.dumps(preview_data, indent=2, default=str))
        else:
            click.echo(click.style("Heuristic Suggestions (no LLM):", bold=True))
            click.echo()
            if not preview_data["heuristic_suggestions"]:
                click.echo("  No issues detected by heuristics.")
            else:
                for s in preview_data["heuristic_suggestions"]:
                    severity_color = {
                        "high": "red",
                        "medium": "yellow",
                        "low": "blue",
                    }.get(s["severity"], "white")
                    click.echo(
                        f"  [{click.style(s['severity'].upper(), fg=severity_color)}] "
                        f"{s['title']}"
                    )
                    click.echo(f"    {s['description'][:80]}")
                    click.echo()
        return

    # Check LLM availability
    availability = check_llm_availability()
    if not availability["suggest_available"]:
        click.echo(
            click.style(
                "No LLM configured for suggestions.\n"
                "Set OPENAI_API_KEY in ~/.dal/.env or environment.\n"
                "Use --preview for heuristic-only suggestions.",
                fg="yellow",
            )
        )
        # Fall back to preview mode
        preview_data = get_suggestion_preview(session)
        click.echo()
        click.echo(click.style("Heuristic Suggestions:", bold=True))
        for s in preview_data["heuristic_suggestions"]:
            click.echo(f"  [{s['severity'].upper()}] {s['title']}")
        return

    click.echo("Generating suggestions...")
    try:
        categories = list(category) if category else None
        suggestions = suggest_improvements_sync(
            session,
            model=model,
            prompt_file=prompt_file,
            categories=categories,
        )

        if output == "json":
            click.echo(json_lib.dumps(suggestions.to_dict(), indent=2, default=str))
        else:
            click.echo()
            click.echo(click.style("=" * 60, bold=True))
            click.echo(click.style("Improvement Suggestions", bold=True))
            click.echo(click.style("=" * 60, bold=True))
            click.echo()

            if not suggestions.suggestions:
                click.echo("No suggestions - session looks good!")
            else:
                for s in suggestions.suggestions:
                    severity_color = {
                        "high": "red",
                        "medium": "yellow",
                        "low": "blue",
                    }.get(s.severity.value, "white")

                    click.echo(
                        f"[{click.style(s.severity.value.upper(), fg=severity_color)}] "
                        f"[{s.category.value}] {s.title}"
                    )
                    click.echo(f"  {s.description}")
                    if s.recommendation:
                        click.echo(f"  → {s.recommendation}")
                    click.echo()

            click.echo(click.style("-" * 60, dim=True))
            click.echo(f"Model: {suggestions.model_used}")
            click.echo(f"Tokens used: {suggestions.tokens_used.get('total_tokens', 'N/A')}")

    except NoLLMConfigError as e:
        click.echo(click.style(f"LLM Error: {e}", fg="red"))
    except Exception as e:
        click.echo(click.style(f"Error: {e}", fg="red"))


# =============================================================================
# Fabric Integration Commands (Theme 5-7)
# =============================================================================


@main.command("meeting-sessions")
@click.argument("meeting_id")
@click.option(
    "--output",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format (default: text)",
)
def meeting_sessions(meeting_id: str, output: str) -> None:
    """Find Claude Code sessions related to a meeting.

    Searches trace data for sessions that reference the meeting ID.

    Examples:

        dal meeting-sessions 712a463f-4417-4765-8ce6-7f01ecd33ba0

        dal meeting-sessions abc123 --output json
    """
    import json as json_lib

    from dev_agent_lens.fabric.queries import get_meeting_sessions

    click.echo(f"Searching for sessions referencing meeting: {meeting_id}")

    try:
        sessions = get_meeting_sessions(meeting_id)

        if not sessions:
            click.echo(click.style("No sessions found.", fg="yellow"))
            return

        if output == "json":
            output_data = {
                "meeting_id": meeting_id,
                "session_count": len(sessions),
                "sessions": [
                    {
                        "session_id": s.get("session_id"),
                        "span_count": len(s.get("spans", [])),
                    }
                    for s in sessions
                ],
            }
            click.echo(json_lib.dumps(output_data, indent=2, default=str))
        else:
            click.echo()
            click.echo(click.style(f"Found {len(sessions)} session(s)", fg="green"))
            click.echo()
            for session in sessions[:20]:  # Limit display
                session_id = session.get("session_id", "unknown")
                spans = session.get("spans", [])
                click.echo(f"  {session_id}: {len(spans)} spans")

            if len(sessions) > 20:
                click.echo(f"  ... and {len(sessions) - 20} more")

    except Exception as e:
        click.echo(click.style(f"Error: {e}", fg="red"))


@main.command("ticket-sessions")
@click.argument("ticket_id")
@click.option(
    "--output",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format (default: text)",
)
def ticket_sessions(ticket_id: str, output: str) -> None:
    """Find Claude Code sessions related to a Linear ticket.

    Searches trace data for sessions that reference the ticket ID.

    Examples:

        dal ticket-sessions ENG2-123

        dal ticket-sessions CWORK-456 --output json
    """
    import json as json_lib

    from dev_agent_lens.fabric.queries import get_ticket_sessions

    click.echo(f"Searching for sessions referencing ticket: {ticket_id}")

    try:
        sessions = get_ticket_sessions(ticket_id)

        if not sessions:
            click.echo(click.style("No sessions found.", fg="yellow"))
            return

        if output == "json":
            output_data = {
                "ticket_id": ticket_id,
                "session_count": len(sessions),
                "sessions": [
                    {
                        "session_id": s.get("session_id"),
                        "span_count": len(s.get("spans", [])),
                    }
                    for s in sessions
                ],
            }
            click.echo(json_lib.dumps(output_data, indent=2, default=str))
        else:
            click.echo()
            click.echo(click.style(f"Found {len(sessions)} session(s)", fg="green"))
            click.echo()
            for session in sessions[:20]:
                session_id = session.get("session_id", "unknown")
                spans = session.get("spans", [])
                click.echo(f"  {session_id}: {len(spans)} spans")

            if len(sessions) > 20:
                click.echo(f"  ... and {len(sessions) - 20} more")

    except Exception as e:
        click.echo(click.style(f"Error: {e}", fg="red"))


@main.command("session-context")
@click.argument("session_id")
@click.option(
    "--output",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format (default: text)",
)
def session_context(session_id: str, output: str) -> None:
    """Get business context for a Claude Code session.

    Shows meetings, tickets, and other business entities referenced in the session.

    Examples:

        dal session-context abc123

        dal session-context abc123 --output json
    """
    import json as json_lib

    from dev_agent_lens.fabric.queries import get_session_context

    click.echo(f"Getting context for session: {session_id}")

    try:
        context = get_session_context(session_id)

        if not context.spans:
            click.echo(click.style("Session not found.", fg="yellow"))
            return

        if output == "json":
            click.echo(json_lib.dumps(context.to_dict(), indent=2, default=str))
        else:
            click.echo()
            click.echo(click.style("Session Context", bold=True))
            click.echo(f"  Session ID:  {context.session_id}")
            click.echo(f"  Span Count:  {len(context.spans)}")
            click.echo(f"  Tokens:      {context.token_count:,}")
            click.echo(f"  Duration:    {context.duration_minutes:.1f} min")
            click.echo()

            if context.meeting_ids:
                click.echo(click.style("Referenced Meetings:", bold=True))
                for mid in context.meeting_ids[:10]:
                    click.echo(f"  {mid}")
                if len(context.meeting_ids) > 10:
                    click.echo(f"  ... and {len(context.meeting_ids) - 10} more")
                click.echo()

            if context.ticket_ids:
                click.echo(click.style("Referenced Tickets:", bold=True))
                for tid in context.ticket_ids[:10]:
                    click.echo(f"  {tid}")
                if len(context.ticket_ids) > 10:
                    click.echo(f"  ... and {len(context.ticket_ids) - 10} more")

    except Exception as e:
        click.echo(click.style(f"Error: {e}", fg="red"))


@main.command("cost")
@click.argument("entity_type", type=click.Choice(["meeting", "ticket", "project"]))
@click.argument("entity_id")
@click.option(
    "--output",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format (default: text)",
)
def cost_analysis(entity_type: str, entity_id: str, output: str) -> None:
    """Analyze cost for a meeting, ticket, or project.

    Calculates token usage and estimated cost for all Claude Code sessions
    related to the specified entity.

    Examples:

        dal cost meeting 712a463f-4417-4765-8ce6-7f01ecd33ba0

        dal cost ticket ENG2-123

        dal cost project abc123 --output json
    """
    import json as json_lib

    from dev_agent_lens.fabric.analysis import (
        analyze_meeting_cost,
        analyze_ticket_cost,
        analyze_project_cost,
    )

    click.echo(f"Analyzing cost for {entity_type}: {entity_id}")

    try:
        if entity_type == "meeting":
            result = analyze_meeting_cost(entity_id)
        elif entity_type == "ticket":
            result = analyze_ticket_cost(entity_id)
        elif entity_type == "project":
            result = analyze_project_cost(entity_id)
        else:
            click.echo(click.style(f"Unknown entity type: {entity_type}", fg="red"))
            return

        if output == "json":
            click.echo(json_lib.dumps(result.to_dict(), indent=2, default=str))
        else:
            click.echo()
            click.echo(click.style("=" * 50, bold=True))
            click.echo(click.style("Cost Analysis", bold=True))
            click.echo(click.style("=" * 50, bold=True))
            click.echo()
            click.echo(f"Entity:          {entity_type} / {entity_id}")
            click.echo(f"Sessions:        {result.session_count}")
            click.echo(f"Total Tokens:    {result.total_tokens:,}")
            click.echo(f"  Input:         {result.input_tokens:,}")
            click.echo(f"  Output:        {result.output_tokens:,}")
            click.echo(f"Duration:        {result.duration_minutes:.1f} min")
            click.echo(
                f"Estimated Cost:  ${result.estimated_cost_usd:.4f}"
            )
            click.echo()

            if result.sessions:
                click.echo(click.style("Sessions:", bold=True))
                for s in result.sessions[:10]:
                    click.echo(
                        f"  {s['session_id'][:30]:30} "
                        f"{s['tokens']:8,} tokens "
                        f"${s['cost_usd']:.4f}"
                    )
                if len(result.sessions) > 10:
                    click.echo(f"  ... and {len(result.sessions) - 10} more")

    except Exception as e:
        click.echo(click.style(f"Error: {e}", fg="red"))


@main.command("quality")
@click.argument("entity_type", type=click.Choice(["meeting", "ticket", "project"]))
@click.argument("entity_id")
@click.option(
    "--output",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format (default: text)",
)
def quality_analysis(entity_type: str, entity_id: str, output: str) -> None:
    """Analyze quality for a meeting, ticket, or project.

    Calculates quality metrics including failures, errors, and churn
    for all Claude Code sessions related to the specified entity.

    Examples:

        dal quality meeting 712a463f-4417-4765-8ce6-7f01ecd33ba0

        dal quality ticket ENG2-123

        dal quality project abc123 --output json
    """
    import json as json_lib

    from dev_agent_lens.fabric.analysis import (
        analyze_meeting_quality,
        analyze_ticket_quality,
        analyze_project_quality,
    )

    click.echo(f"Analyzing quality for {entity_type}: {entity_id}")

    try:
        if entity_type == "meeting":
            result = analyze_meeting_quality(entity_id)
        elif entity_type == "ticket":
            result = analyze_ticket_quality(entity_id)
        elif entity_type == "project":
            result = analyze_project_quality(entity_id)
        else:
            click.echo(click.style(f"Unknown entity type: {entity_type}", fg="red"))
            return

        if output == "json":
            click.echo(json_lib.dumps(result.to_dict(), indent=2, default=str))
        else:
            click.echo()
            click.echo(click.style("=" * 50, bold=True))
            click.echo(click.style("Quality Analysis", bold=True))
            click.echo(click.style("=" * 50, bold=True))
            click.echo()
            click.echo(f"Entity:          {entity_type} / {entity_id}")
            click.echo(f"Sessions:        {result.session_count}")
            click.echo(f"Total Failures:  {result.total_failures}")
            click.echo(f"Failure Rate:    {result.failure_rate:.2f} per session")
            click.echo(f"Errors:          {result.error_count}")
            click.echo(f"Rate Limits:     {result.rate_limit_count}")
            click.echo()

            # Quality score with color
            score = result.quality_score
            if score >= 80:
                score_color = "green"
            elif score >= 60:
                score_color = "yellow"
            else:
                score_color = "red"
            click.echo(
                f"Quality Score:   {click.style(f'{score:.1f}/100', fg=score_color)}"
            )
            click.echo()

            if result.churn_files:
                click.echo(click.style("Files with Churn:", bold=True))
                for f in result.churn_files[:10]:
                    click.echo(f"  {f}")
                if len(result.churn_files) > 10:
                    click.echo(f"  ... and {len(result.churn_files) - 10} more")

    except Exception as e:
        click.echo(click.style(f"Error: {e}", fg="red"))


@main.command("daily-usage")
@click.option(
    "--days",
    type=int,
    default=7,
    help="Number of days to include (default: 7)",
)
@click.option(
    "--output",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format (default: text)",
)
def daily_usage(days: int, output: str) -> None:
    """Show daily usage statistics.

    Displays session counts, token usage, and estimated costs by day.

    Examples:

        dal daily-usage

        dal daily-usage --days 30

        dal daily-usage --output json
    """
    import json as json_lib

    from dev_agent_lens.fabric.analysis import get_daily_usage

    click.echo(f"Getting daily usage for past {days} days...")

    try:
        usage = get_daily_usage(days=days)

        if not usage:
            click.echo(click.style("No usage data found.", fg="yellow"))
            return

        if output == "json":
            click.echo(json_lib.dumps(usage, indent=2, default=str))
        else:
            click.echo()
            click.echo(click.style("Daily Usage Statistics", bold=True))
            click.echo()
            click.echo(
                f"{'Date':<12} {'Sessions':>10} {'Spans':>10} "
                f"{'Tokens':>12} {'Cost':>10}"
            )
            click.echo("-" * 56)

            total_sessions = 0
            total_spans = 0
            total_tokens = 0
            total_cost = 0.0

            for day in usage:
                total_sessions += day["session_count"]
                total_spans += day["span_count"]
                total_tokens += day["token_count"]
                total_cost += day["estimated_cost_usd"]

                click.echo(
                    f"{day['date']:<12} {day['session_count']:>10} "
                    f"{day['span_count']:>10} {day['token_count']:>12,} "
                    f"${day['estimated_cost_usd']:>9.4f}"
                )

            click.echo("-" * 56)
            click.echo(
                f"{'Total':<12} {total_sessions:>10} {total_spans:>10} "
                f"{total_tokens:>12,} ${total_cost:>9.4f}"
            )

    except Exception as e:
        click.echo(click.style(f"Error: {e}", fg="red"))


@main.command("fabric-status")
def fabric_status() -> None:
    """Show Solutions Fabric integration status."""
    from dev_agent_lens.fabric.client import is_fabric_configured

    click.echo(click.style("Solutions Fabric Integration Status", bold=True))
    click.echo()

    if is_fabric_configured():
        click.echo(f"Fabric API: {click.style('configured', fg='green')}")
    else:
        click.echo(f"Fabric API: {click.style('not configured', fg='yellow')}")
        click.echo()
        click.echo("To configure:")
        click.echo("  1. Add to ~/.dal/.env:")
        click.echo("     SOLUTIONS_FABRIC_API_KEY=sf_live_...")
        click.echo("     SOLUTIONS_FABRIC_API_URL=https://solutionsfabric.teraflop.io")

    click.echo()
    click.echo("Available commands:")
    click.echo("  dal meeting-sessions <meeting-id>    Find sessions for a meeting")
    click.echo("  dal ticket-sessions <ticket-id>      Find sessions for a ticket")
    click.echo("  dal session-context <session-id>     Get business context for session")
    click.echo("  dal cost <type> <id>                 Analyze cost for entity")
    click.echo("  dal quality <type> <id>              Analyze quality for entity")
    click.echo("  dal daily-usage                      Show daily usage stats")


@main.command("llm-status")
def llm_status() -> None:
    """Show LLM configuration status."""
    from dev_agent_lens.llm import check_llm_availability

    click.echo(click.style("LLM Configuration Status", bold=True))
    click.echo()

    availability = check_llm_availability()

    # OpenAI status
    if availability["openai_available"]:
        click.echo(f"OpenAI: {click.style('configured', fg='green')}")
    else:
        click.echo(f"OpenAI: {click.style('not configured', fg='red')}")

    # Anthropic status
    if availability["anthropic_available"]:
        click.echo(f"Anthropic: {click.style('configured', fg='green')}")
    else:
        click.echo(f"Anthropic: {click.style('not configured', fg='yellow')} (optional)")

    click.echo()
    click.echo("Command availability:")

    # Summarize
    if availability["summarize_available"]:
        click.echo(f"  dal summarize: {click.style('available', fg='green')}")
    else:
        click.echo(f"  dal summarize: {click.style('needs OPENAI_API_KEY', fg='red')}")

    # Cluster
    if availability["cluster_available"]:
        click.echo(f"  dal cluster: {click.style('available', fg='green')}")
    else:
        click.echo(f"  dal cluster: {click.style('needs OPENAI_API_KEY', fg='red')}")

    # Suggest
    if availability["suggest_available"]:
        click.echo(f"  dal suggest: {click.style('available', fg='green')}")
    else:
        click.echo(f"  dal suggest: {click.style('needs OPENAI_API_KEY (preview available)', fg='yellow')}")

    click.echo()
    click.echo("To configure:")
    click.echo("  1. Create ~/.dal/.env")
    click.echo("  2. Add: OPENAI_API_KEY=sk-...")


# =============================================================================
# Session Analysis Commands (Story 2.6, 3.7, 3.8, 3.9)
# =============================================================================


@main.command("session")
@click.argument("session_id")
@click.option("--output", type=click.Choice(["text", "json"]), default="text", help="Output format")
def session_view(session_id: str, output: str) -> None:
    """View unified session end-to-end.

    Shows all spans in a session with chronological ordering.

    Examples:

        dal session abc123

        dal session abc123 --output json
    """
    from pathlib import Path

    from dev_agent_lens.query import query
    from dev_agent_lens.storage import get_storage_path

    storage_path = get_storage_path()
    sessions_file = Path(storage_path) / "sessions" / "sessions_current.jsonl"

    if not sessions_file.exists():
        click.echo(click.style("No session data found. Run 'dal sync' first.", fg="red"))
        raise SystemExit(1)

    # Query for the session
    result = query(file_path=sessions_file, session_id=session_id)

    if not result.sessions:
        click.echo(click.style(f"Session not found: {session_id}", fg="red"))
        raise SystemExit(1)

    session = result.sessions[0]
    spans = session.get("spans", [])

    if output == "json":
        import json

        click.echo(json.dumps(session, indent=2, default=str))
    else:
        click.echo()
        click.echo(click.style(f"Session: {session_id}", fg="cyan", bold=True))
        click.echo("=" * 60)
        click.echo(f"Spans: {len(spans)}")
        click.echo(f"Start: {session.get('start_time', 'N/A')}")
        click.echo(f"End: {session.get('end_time', 'N/A')}")
        click.echo()

        # Show spans chronologically
        for i, span in enumerate(spans, 1):
            name = span.get("name", "unknown")[:40]
            status = span.get("status_code", "")
            tokens = span.get("llm_token_count_total", 0) or 0

            # Handle NaN values
            import math
            try:
                tokens_val = float(tokens) if tokens else 0
                if math.isnan(tokens_val):
                    tokens_val = 0
            except (ValueError, TypeError):
                tokens_val = 0

            status_color = "green" if status == "OK" else "red" if status == "ERROR" else "yellow"
            status_icon = "✓" if status == "OK" else "✗" if status == "ERROR" else "○"

            click.echo(f"  {i:3}. {click.style(status_icon, fg=status_color)} {name}")
            if tokens_val:
                click.echo(f"       └─ {int(tokens_val):,} tokens")


def _display_token_analysis(
    session_id: str,
    breakdown,
    cost: dict,
    source: str | None = None,
) -> None:
    """Display token analysis in text format."""
    click.echo()
    title = f"Token Analysis: {session_id}"
    if source:
        title += f" (source: {source})"
    click.echo(click.style(title, fg="cyan", bold=True))
    click.echo("=" * 60)
    click.echo()
    click.echo(click.style("Input Tokens:", bold=True))
    click.echo(f"  Tool Calls:     {breakdown.tool_tokens:>12,}")
    click.echo(f"  User Messages:  {breakdown.user_tokens:>12,}")
    click.echo(f"  System Prompts: {breakdown.system_tokens:>12,}")
    click.echo(f"  {'-' * 30}")
    click.echo(f"  Total Input:    {breakdown.total_input_tokens:>12,}")
    click.echo()
    click.echo(click.style("Output Tokens:", bold=True))
    click.echo(f"  Model Output:   {breakdown.model_tokens:>12,}")
    click.echo()
    click.echo(click.style("Cost Estimate:", bold=True))
    click.echo(f"  Input Cost:     ${cost['input_cost_usd']:>11.4f}")
    click.echo(f"  Output Cost:    ${cost['output_cost_usd']:>11.4f}")
    click.echo(f"  {'-' * 30}")
    click.echo(f"  Total Cost:     ${cost['total_cost_usd']:>11.4f}")


@main.command("analyze-tokens")
@click.argument("session_id")
@click.option("--source", "-s", help="Source name to query (uses Parquet if available)")
@click.option("--parquet/--no-parquet", default=True, help="Use Parquet backend when available")
@click.option("--output", type=click.Choice(["text", "json"]), default="text", help="Output format")
def analyze_tokens_cmd(session_id: str, source: str | None, parquet: bool, output: str) -> None:
    """Analyze token breakdown for a session.

    Shows tokens by category:
    - Input: tool calls, user messages, system prompts
    - Output: model-generated tokens

    Uses Parquet backend (10-100x faster) when --source is specified
    and Parquet files exist. Falls back to JSONL otherwise.

    Examples:

        dal analyze-tokens abc123

        dal analyze-tokens abc123 --source my-project

        dal analyze-tokens abc123 --output json

        dal analyze-tokens abc123 --source my-project --no-parquet
    """
    from pathlib import Path

    from dev_agent_lens.analysis.tokens import analyze_session_tokens, estimate_cost
    from dev_agent_lens.query import query, query_sessions
    from dev_agent_lens.storage import get_storage_path

    # Try Parquet backend if source specified
    if source:
        sessions = query_sessions(source=source, session_id=session_id, prefer_parquet=parquet)
        if sessions:
            session = sessions[0]
            spans = session.get("spans", [])

            # Analyze tokens
            breakdown = analyze_session_tokens(spans)
            cost = estimate_cost(breakdown)

            if output == "json":
                import json

                data = {
                    "session_id": session_id,
                    "source": source,
                    "token_breakdown": breakdown.to_dict(),
                    "cost_estimate": cost,
                }
                click.echo(json.dumps(data, indent=2))
            else:
                _display_token_analysis(session_id, breakdown, cost, source=source)
            return

        click.echo(click.style(f"Session not found in source '{source}': {session_id}", fg="red"))
        raise SystemExit(1)

    # Fall back to JSONL
    storage_path = get_storage_path()
    sessions_file = Path(storage_path) / "sessions" / "sessions_current.jsonl"

    if not sessions_file.exists():
        click.echo(click.style("No session data found. Run 'dal sync' first.", fg="red"))
        raise SystemExit(1)

    result = query(file_path=sessions_file, session_id=session_id)

    if not result.sessions:
        click.echo(click.style(f"Session not found: {session_id}", fg="red"))
        raise SystemExit(1)

    session = result.sessions[0]
    spans = session.get("spans", [])

    # Analyze tokens
    breakdown = analyze_session_tokens(spans)
    cost = estimate_cost(breakdown)

    if output == "json":
        import json

        data = {
            "session_id": session_id,
            "token_breakdown": breakdown.to_dict(),
            "cost_estimate": cost,
        }
        click.echo(json.dumps(data, indent=2))
    else:
        _display_token_analysis(session_id, breakdown, cost)


@main.command("analyze-duplicates")
@click.option("--source", "-s", help="Source name to query (uses Parquet if available)")
@click.option("--parquet/--no-parquet", default=True, help="Use Parquet backend when available")
@click.option("--output", type=click.Choice(["text", "json"]), default="text", help="Output format")
@click.option("--min-containment", type=float, default=50.0, help="Minimum containment % to report")
def analyze_duplicates_cmd(
    source: str | None, parquet: bool, output: str, min_containment: float
) -> None:
    """Analyze duplicate/subset relationships between sessions.

    Identifies sessions that are fully or partially contained in other sessions.
    These subset sessions could potentially be deleted to save storage.

    Uses Parquet backend (10-100x faster) when --source is specified
    and Parquet files exist. Falls back to JSONL otherwise.

    Examples:

        dal analyze-duplicates

        dal analyze-duplicates --source my-project

        dal analyze-duplicates --output json

        dal analyze-duplicates --min-containment 80
    """
    from dev_agent_lens.analysis.subsets import analyze_coverage
    from dev_agent_lens.query import query_sessions

    backend_info = ""
    if source:
        backend_info = f" from {source}"
        if parquet:
            backend_info += " (Parquet)"
    click.echo(f"Analyzing sessions for duplicates{backend_info}...")

    # Load sessions using query module which handles session grouping and backend detection
    sessions = query_sessions(source=source, prefer_parquet=parquet)

    if not sessions:
        click.echo(click.style("No sessions found. Run 'dal sync' first.", fg="yellow"))
        return

    if len(sessions) < 2:
        click.echo("Need at least 2 sessions for comparison.")
        return

    click.echo(f"Comparing {len(sessions)} sessions...")

    # Analyze coverage
    report = analyze_coverage(sessions)

    # Filter relationships by min containment
    filtered_rels = [r for r in report.relationships if r.containment_percentage >= min_containment]

    if output == "json":
        import json

        data = report.to_dict()
        data["relationships"] = [r.to_dict() for r in filtered_rels]
        click.echo(json.dumps(data, indent=2))
    else:
        click.echo()
        click.echo(click.style("Coverage Analysis", fg="cyan", bold=True))
        click.echo("=" * 60)
        click.echo()
        click.echo(click.style("Session Summary:", bold=True))
        click.echo(f"  Total Sessions:    {report.total_sessions}")
        click.echo(f"  Complete Sessions: {report.complete_sessions}")
        click.echo(f"  Subset Sessions:   {report.subset_sessions}")
        click.echo(f"  Partial Sessions:  {report.partial_sessions}")
        click.echo(f"  Unique Sessions:   {report.unique_sessions}")
        click.echo()
        click.echo(click.style("Coverage Metrics:", bold=True))
        click.echo(f"  Coverage:    {report.coverage_percentage:>6.1f}%")
        click.echo(f"  Redundancy:  {report.redundancy_percentage:>6.1f}%")
        click.echo()
        click.echo(click.style("Storage Impact:", bold=True))
        click.echo(f"  Deletable Sessions: {report.deletable_sessions}")
        click.echo(f"  Deletable Spans:    {report.deletable_span_count}")
        click.echo(f"  Total Spans:        {report.total_span_count}")

        if filtered_rels:
            click.echo()
            click.echo(click.style("Subset Relationships:", bold=True))
            for rel in filtered_rels[:10]:  # Show first 10
                status = click.style("SUBSET", fg="red") if rel.is_complete_subset else click.style("PARTIAL", fg="yellow")
                click.echo(f"  {rel.child_session_id[:16]} → {rel.parent_session_id[:16]}")
                click.echo(f"    {status} ({rel.containment_percentage:.1f}% contained)")
                click.echo(f"    Recommendation: {rel.recommendation}")

            if len(filtered_rels) > 10:
                click.echo(f"  ... and {len(filtered_rels) - 10} more")


@main.command("coverage")
@click.option("--source", "-s", help="Source name to query (uses Parquet if available)")
@click.option("--parquet/--no-parquet", default=True, help="Use Parquet backend when available")
@click.option("--output", type=click.Choice(["text", "json"]), default="text", help="Output format")
def coverage_cmd(source: str | None, parquet: bool, output: str) -> None:
    """Show coverage metrics for sessions.

    Reports what percentage of sessions are complete vs partial copies.

    Uses Parquet backend (10-100x faster) when --source is specified
    and Parquet files exist. Falls back to JSONL otherwise.

    Examples:

        dal coverage

        dal coverage --source my-project

        dal coverage --output json
    """
    from dev_agent_lens.analysis.subsets import analyze_coverage
    from dev_agent_lens.query import query_sessions

    # Load sessions using query module which handles session grouping and backend detection
    sessions = query_sessions(source=source, prefer_parquet=parquet)

    if not sessions:
        click.echo(click.style("No sessions found. Run 'dal sync' first.", fg="yellow"))
        return

    # Analyze
    report = analyze_coverage(sessions)

    if output == "json":
        import json

        click.echo(json.dumps(report.to_dict(), indent=2))
    else:
        click.echo()
        click.echo(click.style("Session Coverage Report", fg="cyan", bold=True))
        click.echo("=" * 50)
        click.echo()

        # Create visual bars
        if report.total_sessions > 0:
            complete_pct = (report.complete_sessions / report.total_sessions) * 100
            subset_pct = (report.subset_sessions / report.total_sessions) * 100
            partial_pct = (report.partial_sessions / report.total_sessions) * 100
            unique_pct = (report.unique_sessions / report.total_sessions) * 100

            click.echo(f"Complete:  {_make_bar(complete_pct)} {report.complete_sessions}")
            click.echo(f"Subset:    {_make_bar(subset_pct, 'red')} {report.subset_sessions}")
            click.echo(f"Partial:   {_make_bar(partial_pct, 'yellow')} {report.partial_sessions}")
            click.echo(f"Unique:    {_make_bar(unique_pct, 'cyan')} {report.unique_sessions}")
            click.echo()
            click.echo(f"Coverage Score: {click.style(f'{report.coverage_percentage:.1f}%', fg='green', bold=True)}")
            click.echo(f"Redundancy:     {click.style(f'{report.redundancy_percentage:.1f}%', fg='red')}")
        else:
            click.echo("No sessions to analyze.")


def _make_bar(pct: float, color: str = "green", width: int = 20) -> str:
    """Create a simple text progress bar."""
    filled = int(pct / 100 * width)
    empty = width - filled
    bar = "█" * filled + "░" * empty
    return click.style(bar, fg=color)


# =============================================================================
# Export Commands
# =============================================================================


def _merge_sessions(
    existing_sessions: list[dict],
    new_sessions: list[dict],
) -> tuple[list[dict], int, int, int]:
    """
    Merge new sessions into existing sessions using bucket unification.

    For sessions with the same session_id:
    - Combine spans from both
    - Deduplicate by span_id
    - Re-sort by start_time
    - Update span_count and time range

    Args:
        existing_sessions: List of existing session dicts
        new_sessions: List of new session dicts (already grouped)

    Returns:
        Tuple of (merged_sessions, sessions_updated, sessions_added, spans_added)
    """
    # Build lookup of existing sessions by session_id
    existing_map: dict[str, dict] = {}
    for session in existing_sessions:
        sid = session.get("session_id")
        if sid:
            existing_map[sid] = session

    sessions_updated = 0
    sessions_added = 0
    spans_added = 0

    for new_session in new_sessions:
        sid = new_session.get("session_id")
        new_spans = new_session.get("spans", [])
        spans_added += len(new_spans)

        if sid and sid in existing_map:
            # Merge into existing session
            existing = existing_map[sid]
            existing_spans = existing.get("spans", [])

            # Combine spans
            all_spans = existing_spans + new_spans

            # Deduplicate by span_id (keep last occurrence = newest)
            seen_span_ids: dict[str, dict] = {}
            for span in all_spans:
                span_id = span.get("span_id")
                if span_id:
                    seen_span_ids[span_id] = span
                else:
                    # No span_id, keep it
                    seen_span_ids[id(span)] = span

            deduped_spans = list(seen_span_ids.values())

            # Sort by start_time
            deduped_spans.sort(key=lambda s: s.get("start_time") or "")

            # Update session
            existing["spans"] = deduped_spans
            existing["span_count"] = len(deduped_spans)

            # Update time range
            start_times = [s.get("start_time") for s in deduped_spans if s.get("start_time")]
            end_times = [s.get("end_time") for s in deduped_spans if s.get("end_time")]
            if start_times:
                existing["start_time"] = min(start_times)
            if end_times:
                existing["end_time"] = max(end_times)

            sessions_updated += 1
        else:
            # New session - add to map
            if sid:
                existing_map[sid] = new_session
            sessions_added += 1

    # Convert back to list, sorted by most recent first
    merged = list(existing_map.values())
    merged.sort(key=lambda s: s.get("end_time") or s.get("start_time") or "", reverse=True)

    return merged, sessions_updated, sessions_added, spans_added


def _load_unified_sessions(file_path) -> list[dict]:
    """Load existing unified sessions from JSONL file."""
    import json

    sessions = []
    if file_path.exists():
        with open(file_path, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    sessions.append(json.loads(line))
    return sessions


def _get_new_spans_since(sessions_dir, raw_dir, last_export_time) -> list[dict]:
    """Get spans from session or raw files modified after last_export_time."""
    from dev_agent_lens.core.unify import read_sessions_file

    new_spans = []

    # Check session files first
    if sessions_dir.exists():
        for jsonl_file in sessions_dir.glob("sessions_*.jsonl"):
            # Skip symlinks
            if jsonl_file.is_symlink():
                continue

            # Check modification time
            if jsonl_file.stat().st_mtime > last_export_time:
                df = read_sessions_file(jsonl_file)
                if not df.empty:
                    new_spans.extend(df.to_dict(orient="records"))

    # Also check raw files
    if raw_dir.exists():
        for jsonl_file in raw_dir.glob("sync_*.jsonl"):
            # Check modification time
            if jsonl_file.stat().st_mtime > last_export_time:
                with open(jsonl_file) as f:
                    for line in f:
                        if line.strip():
                            try:
                                new_spans.append(json.loads(line))
                            except json.JSONDecodeError:
                                continue

    return new_spans


@main.command("export-sessions")
@click.option(
    "--source",
    "source_name",
    required=True,
    help="Export unified sessions from a named source",
)
@click.option(
    "--output",
    "-o",
    "output_path",
    type=click.Path(),
    help="Output file path (default: ~/.dal/data/unified/{source}_sessions.jsonl)",
)
@click.option(
    "--update",
    is_flag=True,
    help="Incrementally update existing unified sessions with new spans",
)
def export_sessions(source_name: str, output_path: str | None, update: bool) -> None:
    """Export unified sessions from a source.

    Creates a JSONL file where each line is a complete session with all its spans
    grouped together, sorted chronologically.

    With --update, incrementally merges new spans into existing unified sessions
    using bucket unification (group new spans first, then merge session-to-session).

    Output format (one JSON per line):
        {
            "session_id": "abc123",
            "span_count": 42,
            "start_time": "2025-01-01T00:00:00",
            "end_time": "2025-01-01T01:00:00",
            "spans": [...]
        }

    Examples:

        dal export-sessions --source phoenix-local-alex

        dal export-sessions --source phoenix-local-alex --update

        dal export-sessions --source arize-ax-alex -o ~/exports/arize.jsonl
    """
    import json
    from pathlib import Path

    from dev_agent_lens.query.query import _group_by_session
    from dev_agent_lens.core.unify import read_sessions_file
    from dev_agent_lens.storage import get_storage_path

    storage_path = get_storage_path()
    sessions_dir = Path(storage_path) / "sessions" / source_name
    raw_dir = Path(storage_path) / "raw" / source_name

    # Check if sessions dir exists and has files
    sessions_exist = sessions_dir.exists() and any(sessions_dir.glob("sessions_*.jsonl"))
    raw_exists = raw_dir.exists() and any(raw_dir.glob("sync_*.jsonl"))

    if not sessions_exist and not raw_exists:
        click.echo(click.style(f"Source not found: {source_name}", fg="red"))
        click.echo(f"Expected directory: {sessions_dir} or {raw_dir}")
        raise SystemExit(1)

    # Determine data source
    use_raw = not sessions_exist and raw_exists

    # Determine output path
    if output_path:
        out_file = Path(output_path).expanduser()
    else:
        unified_dir = Path(storage_path) / "unified"
        unified_dir.mkdir(parents=True, exist_ok=True)
        out_file = unified_dir / f"{source_name}_sessions.jsonl"

    if update and out_file.exists():
        # Incremental update mode
        click.echo(click.style("Incremental update mode", fg="cyan"))

        # Get last export time
        last_export_time = out_file.stat().st_mtime
        click.echo(f"Last export: {Path(out_file).stat().st_mtime}")

        # Load existing unified sessions
        click.echo(f"Loading existing sessions from: {out_file}")
        existing_sessions = _load_unified_sessions(out_file)
        click.echo(f"Loaded {len(existing_sessions):,} existing sessions")

        # Find new spans since last export
        click.echo("Finding new spans...")
        new_spans = _get_new_spans_since(sessions_dir, raw_dir, last_export_time)

        if not new_spans:
            click.echo(click.style("No new spans found since last export.", fg="yellow"))
            return

        click.echo(f"Found {len(new_spans):,} new spans")

        # Step 1: Group new spans into sessions (bucket sort)
        click.echo("Grouping new spans by session...")
        new_sessions = _group_by_session(new_spans)
        click.echo(f"New spans form {len(new_sessions):,} sessions")

        # Step 2: Merge new sessions into existing (bucket unification)
        click.echo("Merging sessions...")
        merged_sessions, updated, added, spans_added = _merge_sessions(
            existing_sessions, new_sessions
        )

        # Write merged sessions
        out_file.parent.mkdir(parents=True, exist_ok=True)
        with open(out_file, "w") as f:
            for session in merged_sessions:
                json.dump(session, f, default=str)
                f.write("\n")

        # Calculate stats
        total_spans = sum(s.get("span_count", 0) for s in merged_sessions)
        file_size_mb = out_file.stat().st_size / (1024 * 1024)

        click.echo()
        click.echo(click.style("Update complete!", fg="green", bold=True))
        click.echo(f"  Sessions updated: {updated:,}")
        click.echo(f"  Sessions added:   {added:,}")
        click.echo(f"  Spans added:      {spans_added:,}")
        click.echo(f"  Total sessions:   {len(merged_sessions):,}")
        click.echo(f"  Total spans:      {total_spans:,}")
        click.echo(f"  Size:             {file_size_mb:.1f} MB")
        click.echo(f"  Output:           {out_file}")

    else:
        # Full export mode
        if update:
            click.echo(click.style("No existing export found, doing full export.", fg="yellow"))

        if use_raw:
            # Read from raw files
            click.echo(click.style("Using raw sync files (no session files found)", fg="cyan"))
            raw_files = sorted(raw_dir.glob("sync_*.jsonl"))
            click.echo(f"Found {len(raw_files)} raw files in {raw_dir}")

            spans = []
            for raw_file in raw_files:
                click.echo(f"  Reading {raw_file.name}...")
                with open(raw_file) as f:
                    for line in f:
                        if line.strip():
                            try:
                                spans.append(json.loads(line))
                            except json.JSONDecodeError:
                                continue
            click.echo(f"Loaded {len(spans):,} spans from raw files")
        else:
            # Find sessions file
            sessions_file = sessions_dir / "sessions_current.jsonl"
            if not sessions_file.exists():
                # Try to find any sessions file
                jsonl_files = list(sessions_dir.glob("sessions_*.jsonl"))
                if not jsonl_files:
                    click.echo(click.style(f"No session files found in {sessions_dir}", fg="red"))
                    raise SystemExit(1)
                sessions_file = max(jsonl_files, key=lambda f: f.stat().st_mtime)

            click.echo(f"Loading spans from: {sessions_file}")

            # Read all spans
            df = read_sessions_file(sessions_file)
            if df.empty:
                click.echo(click.style("No spans found in session file", fg="yellow"))
                return

            spans = df.to_dict(orient="records")
            click.echo(f"Loaded {len(spans):,} spans")

        # Group by session
        click.echo("Grouping spans by session...")
        sessions = _group_by_session(spans)
        click.echo(f"Found {len(sessions):,} sessions")

        # Write unified sessions
        out_file.parent.mkdir(parents=True, exist_ok=True)
        with open(out_file, "w") as f:
            for session in sessions:
                json.dump(session, f, default=str)
                f.write("\n")

        # Calculate stats
        total_spans = sum(s.get("span_count", 0) for s in sessions)
        file_size_mb = out_file.stat().st_size / (1024 * 1024)

        click.echo()
        click.echo(click.style("Export complete!", fg="green", bold=True))
        click.echo(f"  Sessions: {len(sessions):,}")
        click.echo(f"  Spans:    {total_spans:,}")
        click.echo(f"  Size:     {file_size_mb:.1f} MB")
        click.echo(f"  Output:   {out_file}")


# =============================================================================
# Oxen Push/Pull Commands
# =============================================================================


@main.command()
@click.option(
    "--message",
    "-m",
    type=str,
    default=None,
    help="Commit message (auto-generated if not provided)",
)
def push(message: str | None):
    """Push unified session files to Oxen remote.

    Commits any changes in the unified/ directory and pushes to the
    configured Oxen remote. Run 'dal export-sessions' first to create
    unified session files.

    Example:
        dal export-sessions --source phoenix-alex
        dal push -m "Add phoenix-alex sessions"
    """
    from dev_agent_lens.config import get_oxen_remote, is_oxen_configured

    if not is_oxen_configured():
        click.echo(
            click.style(
                "Error: Oxen is not configured.\n"
                "Run 'dal config oxen --remote <url>' to set up Oxen.",
                fg="red",
            )
        )
        raise SystemExit(1)

    remote_url = get_oxen_remote()
    store = OxenStore()

    # Check if unified directory has content
    unified_files = list(store.unified_dir.glob("*.jsonl")) if store.unified_dir.exists() else []
    if not unified_files:
        click.echo(
            click.style(
                "No unified session files found.\n"
                "Run 'dal export-sessions' first to create unified session files.",
                fg="yellow",
            )
        )
        raise SystemExit(1)

    click.echo(f"Oxen remote: {remote_url}")
    click.echo(f"Unified files: {len(unified_files)}")
    click.echo()

    # Initialize repo if needed
    click.echo("Initializing Oxen repository...")
    if not store.init_oxen():
        click.echo(click.style("Failed to initialize Oxen repository.", fg="red"))
        click.echo("Make sure the 'oxen' package is installed: pip install oxen")
        raise SystemExit(1)

    # Set remote if not already set
    store.set_remote(remote_url)

    # Generate commit message
    if message is None:
        from datetime import datetime

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        file_names = ", ".join(f.stem for f in unified_files[:3])
        if len(unified_files) > 3:
            file_names += f" +{len(unified_files) - 3} more"
        message = f"Update unified sessions: {file_names} ({timestamp})"

    # Commit
    click.echo(f"Committing: {message}")
    if not store.commit(message):
        click.echo(click.style("Failed to commit changes.", fg="red"))
        raise SystemExit(1)

    # Push
    click.echo("Pushing to remote...")
    if not store.push():
        click.echo(click.style("Failed to push to remote.", fg="red"))
        raise SystemExit(1)

    click.echo(click.style("Push complete!", fg="green", bold=True))


@main.command()
def pull():
    """Pull latest unified session files from Oxen remote.

    Fetches the latest unified session files from the configured Oxen remote.
    Use this to sync session data from other team members.

    Example:
        dal pull
    """
    from dev_agent_lens.config import get_oxen_remote, is_oxen_configured

    if not is_oxen_configured():
        click.echo(
            click.style(
                "Error: Oxen is not configured.\n"
                "Run 'dal config oxen --remote <url>' to set up Oxen.",
                fg="red",
            )
        )
        raise SystemExit(1)

    remote_url = get_oxen_remote()
    store = OxenStore()

    click.echo(f"Oxen remote: {remote_url}")
    click.echo()

    # Initialize repo if needed
    click.echo("Initializing Oxen repository...")
    if not store.init_oxen():
        click.echo(click.style("Failed to initialize Oxen repository.", fg="red"))
        click.echo("Make sure the 'oxen' package is installed: pip install oxen")
        raise SystemExit(1)

    # Set remote if not already set
    store.set_remote(remote_url)

    # Pull
    click.echo("Pulling from remote...")
    if not store.pull():
        click.echo(click.style("Failed to pull from remote.", fg="red"))
        raise SystemExit(1)

    # Show what we have now
    unified_files = list(store.unified_dir.glob("*.jsonl")) if store.unified_dir.exists() else []
    click.echo(click.style("Pull complete!", fg="green", bold=True))
    click.echo(f"Unified session files: {len(unified_files)}")
    for f in unified_files:
        size_mb = f.stat().st_size / (1024 * 1024)
        click.echo(f"  - {f.name} ({size_mb:.1f} MB)")


# Add oxen subcommand to config group
@config.command("oxen")
@click.option(
    "--remote",
    type=str,
    required=True,
    help="Oxen remote URL (e.g., hub.oxen.ai/team/dal-sessions)",
)
def config_oxen(remote: str):
    """Configure Oxen remote for push/pull.

    Sets the Oxen remote URL that will be used by 'dal push' and 'dal pull'.
    This is stored in ~/.dal/config.json.

    Example:
        dal config oxen --remote https://hub.oxen.ai/myteam/sessions
    """
    from dev_agent_lens.config import set_oxen_remote, get_config_path

    # Ensure URL is fully qualified
    if not remote.startswith("http://") and not remote.startswith("https://"):
        remote = f"https://{remote}"

    set_oxen_remote(remote)
    click.echo(click.style("Oxen remote configured!", fg="green"))
    click.echo(f"  Remote: {remote}")
    click.echo(f"  Config: {get_config_path()}")
    click.echo()
    click.echo("You can now use:")
    click.echo("  dal push    - Push unified sessions to remote")
    click.echo("  dal pull    - Pull unified sessions from remote")


@main.command("export-parquet")
@click.option(
    "--source",
    "source_name",
    type=str,
    required=True,
    help="Source name to export (e.g., phoenix-local-alex)",
)
@click.option(
    "--output",
    "-o",
    "output_dir",
    type=str,
    default=None,
    help="Output directory for Parquet files (default: ~/.dal/data/parquet/)",
)
@click.option(
    "--compression",
    type=click.Choice(["zstd", "snappy", "gzip", "none"]),
    default="zstd",
    help="Parquet compression codec (default: zstd, ~45%% better than snappy)",
)
@click.option(
    "--no-dedupe",
    is_flag=True,
    help="Skip deduplication of raw_attributes",
)
@click.option(
    "--no-strip-nulls",
    is_flag=True,
    help="Keep null/empty values in raw_attributes",
)
def export_parquet(
    source_name: str,
    output_dir: str | None,
    compression: str,
    no_dedupe: bool,
    no_strip_nulls: bool,
) -> None:
    """Export unified sessions to Parquet format.

    Exports session data to efficient columnar Parquet format with built-in
    compression. Creates two files:
    - {source}_sessions.parquet: Session-level aggregates
    - {source}_spans.parquet: Individual spans with session FK

    By default, deduplicates and strips null values from raw_attributes
    to minimize file size (typically 80-90% smaller than JSONL).

    Examples:

        dal export-parquet --source phoenix-local-alex

        dal export-parquet --source arize-ax-alex --compression snappy

        dal export-parquet --source phoenix-local-alex --no-dedupe
    """
    from pathlib import Path

    from dev_agent_lens.export.parquet import ParquetExporter
    from dev_agent_lens.storage import get_storage_path

    storage_path = get_storage_path()
    unified_dir = Path(storage_path) / "unified"

    # Find input file
    input_file = unified_dir / f"{source_name}_sessions.jsonl"
    if not input_file.exists():
        click.echo(
            click.style(
                f"Error: Unified sessions file not found: {input_file}\n"
                f"Run 'dal export-sessions --source {source_name}' first.",
                fg="red",
            )
        )
        raise SystemExit(1)

    # Determine output directory
    if output_dir:
        out_dir = Path(output_dir).expanduser()
    else:
        out_dir = Path(storage_path) / "parquet"
    out_dir.mkdir(parents=True, exist_ok=True)

    input_size = input_file.stat().st_size
    click.echo(f"Source: {source_name}")
    click.echo(f"Input: {input_file} ({input_size / 1024 / 1024:.1f} MB)")
    click.echo(f"Output: {out_dir}")
    click.echo(f"Compression: {compression}")
    click.echo(f"Dedupe: {'no' if no_dedupe else 'yes'}")
    click.echo(f"Strip nulls: {'no' if no_strip_nulls else 'yes'}")
    click.echo()

    # Export
    exporter = ParquetExporter(
        compression=compression,
        dedupe=not no_dedupe,
        strip_nulls=not no_strip_nulls,
    )

    def progress(n: int) -> None:
        if n % 100 == 0:
            click.echo(f"  Processed {n:,} sessions...")

    click.echo("Exporting to Parquet...")
    stats = exporter.export_source(
        source=source_name,
        input_path=input_file,
        output_dir=out_dir,
        progress_callback=progress,
    )

    click.echo()
    click.echo(click.style("Export complete!", fg="green", bold=True))
    click.echo(f"  Sessions: {stats['sessions']:,}")
    click.echo(f"  Spans: {stats['spans']:,}")
    click.echo(f"  Input size: {stats['input_bytes'] / 1024 / 1024:.1f} MB")
    click.echo(f"  Output size: {stats['output_bytes'] / 1024 / 1024:.1f} MB")
    click.echo(
        f"  Savings: {stats['savings_bytes'] / 1024 / 1024:.1f} MB "
        f"({stats['savings_percent']:.1f}%)"
    )
    click.echo()
    click.echo("Output files:")
    click.echo(f"  {stats['sessions_path']}")
    click.echo(f"  {stats['spans_path']}")


@main.command("clean-sessions")
@click.option(
    "--source",
    "source_name",
    type=str,
    required=True,
    help="Source name to clean (e.g., phoenix-local-alex)",
)
@click.option(
    "--output",
    "-o",
    "output_path",
    type=str,
    default=None,
    help="Output file path (default: overwrites input)",
)
@click.option(
    "--no-dedupe",
    is_flag=True,
    help="Skip deduplication of raw_attributes",
)
@click.option(
    "--no-strip-nulls",
    is_flag=True,
    help="Keep null/empty values in raw_attributes",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be saved without writing",
)
def clean_sessions(
    source_name: str,
    output_path: str | None,
    no_dedupe: bool,
    no_strip_nulls: bool,
    dry_run: bool,
) -> None:
    """Clean unified sessions by removing duplicates and null values.

    Reduces file size by:
    1. Removing duplicated fields from raw_attributes (already in normalized form)
    2. Stripping null/empty/NaN values from raw_attributes

    By default, overwrites the input file. Use --output to write to a new file.

    Examples:

        dal clean-sessions --source phoenix-local-alex

        dal clean-sessions --source arize-ax-alex --output cleaned.jsonl

        dal clean-sessions --source phoenix-local-alex --dry-run
    """
    from pathlib import Path
    import tempfile
    import shutil

    from dev_agent_lens.export.dedupe import clean_sessions_file
    from dev_agent_lens.storage import get_storage_path

    storage_path = get_storage_path()
    unified_dir = Path(storage_path) / "unified"

    # Find input file
    input_file = unified_dir / f"{source_name}_sessions.jsonl"
    if not input_file.exists():
        click.echo(
            click.style(
                f"Error: Unified sessions file not found: {input_file}\n"
                f"Run 'dal export-sessions --source {source_name}' first.",
                fg="red",
            )
        )
        raise SystemExit(1)

    input_size = input_file.stat().st_size
    click.echo(f"Source: {source_name}")
    click.echo(f"Input: {input_file} ({input_size / 1024 / 1024:.1f} MB)")
    click.echo(f"Dedupe: {'no' if no_dedupe else 'yes'}")
    click.echo(f"Strip nulls: {'no' if no_strip_nulls else 'yes'}")
    click.echo()

    if dry_run:
        # Sample first 100 sessions for estimate
        import json

        from dev_agent_lens.export.dedupe import clean_session

        sample_original = 0
        sample_cleaned = 0
        sample_count = 0

        with open(input_file, "r") as f:
            for i, line in enumerate(f):
                if i >= 100:
                    break
                line = line.strip()
                if not line:
                    continue

                session = json.loads(line)
                cleaned = clean_session(
                    session,
                    dedupe=not no_dedupe,
                    strip_nulls=not no_strip_nulls,
                )

                sample_original += len(line)
                sample_cleaned += len(json.dumps(cleaned))
                sample_count += 1

        if sample_count > 0:
            savings_pct = (sample_original - sample_cleaned) / sample_original * 100
            estimated_output = input_size * (1 - savings_pct / 100)
            estimated_savings = input_size - estimated_output

            click.echo(click.style("Dry run - estimated savings:", fg="cyan"))
            click.echo(f"  Sample size: {sample_count} sessions")
            click.echo(f"  Sample savings: {savings_pct:.1f}%")
            click.echo(f"  Estimated output: {estimated_output / 1024 / 1024:.1f} MB")
            click.echo(f"  Estimated savings: {estimated_savings / 1024 / 1024:.1f} MB")
        return

    # Determine output path
    overwrite = output_path is None
    if overwrite:
        # Write to temp file, then replace
        temp_fd, temp_path = tempfile.mkstemp(suffix=".jsonl")
        import os
        os.close(temp_fd)
        out_file = Path(temp_path)
    else:
        out_file = Path(output_path).expanduser()

    def progress(n: int, saved: int) -> None:
        if n % 500 == 0:
            click.echo(f"  Processed {n:,} sessions, saved {saved / 1024 / 1024:.1f} MB...")

    click.echo("Cleaning sessions...")
    stats = clean_sessions_file(
        input_path=str(input_file),
        output_path=str(out_file),
        dedupe=not no_dedupe,
        strip_nulls=not no_strip_nulls,
        progress_callback=progress,
    )

    if overwrite:
        # Replace original with cleaned
        shutil.move(str(out_file), str(input_file))
        final_path = input_file
    else:
        final_path = out_file

    click.echo()
    click.echo(click.style("Cleaning complete!", fg="green", bold=True))
    click.echo(f"  Sessions: {stats['sessions_processed']:,}")
    click.echo(f"  Spans: {stats['spans_processed']:,}")
    click.echo(f"  Original size: {stats['original_bytes'] / 1024 / 1024:.1f} MB")
    click.echo(f"  Cleaned size: {stats['cleaned_bytes'] / 1024 / 1024:.1f} MB")
    click.echo(
        f"  Savings: {stats['savings_bytes'] / 1024 / 1024:.1f} MB "
        f"({stats['savings_percent']:.1f}%)"
    )
    click.echo(f"  Output: {final_path}")


@main.command("purge")
@click.option(
    "--source",
    "source_name",
    type=str,
    default=None,
    help="Source name to purge (e.g., phoenix-local-alex)",
)
@click.option(
    "--all",
    "all_sources",
    is_flag=True,
    help="Purge all sources",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be deleted without actually deleting",
)
@click.option(
    "--force",
    "-f",
    is_flag=True,
    help="Skip confirmation prompt",
)
def purge(
    source_name: str | None,
    all_sources: bool,
    dry_run: bool,
    force: bool,
) -> None:
    """Delete all data files for a source.

    Shows files that would be deleted and their sizes. By default, prompts
    for confirmation before deleting. Use --dry-run to preview without
    deleting, or --force to skip the confirmation prompt.

    Data locations purged:
    - ~/.dal/state/historical-sync-{source}.json (sync state)
    - ~/.dal/data/raw/{source}/ (raw JSONL data)
    - ~/.dal/data/parquet/{source}_*.parquet (Parquet exports)
    - ~/.dal/data/unified/{source}_*.jsonl (unified format)

    Examples:

        dal purge --source phoenix-local-alex --dry-run

        dal purge --source phoenix-local-alex

        dal purge --all --dry-run
    """
    from dataclasses import dataclass
    from pathlib import Path
    import shutil

    from dev_agent_lens.storage import get_storage_path

    @dataclass
    class FileInfo:
        path: Path
        size: int
        category: str

        @property
        def size_str(self) -> str:
            if self.size >= 1024 * 1024 * 1024:
                return f"{self.size / 1024 / 1024 / 1024:.1f}GB"
            elif self.size >= 1024 * 1024:
                return f"{self.size / 1024 / 1024:.1f}MB"
            elif self.size >= 1024:
                return f"{self.size / 1024:.1f}KB"
            return f"{self.size}B"

    def discover_source_files(source: str) -> list[FileInfo]:
        """Discover all files associated with a source."""
        files: list[FileInfo] = []
        storage_path = get_storage_path()
        dal_home = Path.home() / ".dal"

        # State files: ~/.dal/state/historical-sync-{source}.json
        state_dir = dal_home / "state"
        state_file = state_dir / f"historical-sync-{source}.json"
        if state_file.exists():
            files.append(FileInfo(
                path=state_file,
                size=state_file.stat().st_size,
                category="State files",
            ))

        # Raw data: ~/.dal/data/raw/{source}/
        raw_dir = storage_path / "raw" / source
        if raw_dir.exists() and raw_dir.is_dir():
            for raw_file in raw_dir.iterdir():
                if raw_file.is_file():
                    files.append(FileInfo(
                        path=raw_file,
                        size=raw_file.stat().st_size,
                        category="Raw data",
                    ))

        # Parquet files: ~/.dal/data/parquet/{source}_*.parquet
        parquet_dir = storage_path / "parquet"
        if parquet_dir.exists():
            for parquet_file in parquet_dir.glob(f"{source}_*.parquet"):
                files.append(FileInfo(
                    path=parquet_file,
                    size=parquet_file.stat().st_size,
                    category="Parquet files",
                ))

        # Unified files: ~/.dal/data/unified/{source}_*.jsonl
        unified_dir = storage_path / "unified"
        if unified_dir.exists():
            for unified_file in unified_dir.glob(f"{source}_*.jsonl"):
                files.append(FileInfo(
                    path=unified_file,
                    size=unified_file.stat().st_size,
                    category="Unified files",
                ))

        return files

    def get_all_sources() -> set[str]:
        """Discover all sources with data."""
        sources: set[str] = set()
        storage_path = get_storage_path()
        dal_home = Path.home() / ".dal"

        # From state files
        state_dir = dal_home / "state"
        if state_dir.exists():
            for state_file in state_dir.glob("historical-sync-*.json"):
                # Extract source name from filename
                name = state_file.stem.replace("historical-sync-", "")
                sources.add(name)

        # From raw directories
        raw_dir = storage_path / "raw"
        if raw_dir.exists():
            for subdir in raw_dir.iterdir():
                if subdir.is_dir() and not subdir.name.startswith("."):
                    sources.add(subdir.name)

        # From parquet files
        parquet_dir = storage_path / "parquet"
        if parquet_dir.exists():
            for parquet_file in parquet_dir.glob("*_sessions.parquet"):
                name = parquet_file.stem.replace("_sessions", "")
                sources.add(name)
            for parquet_file in parquet_dir.glob("*_spans.parquet"):
                name = parquet_file.stem.replace("_spans", "")
                sources.add(name)

        # From unified files
        unified_dir = storage_path / "unified"
        if unified_dir.exists():
            for unified_file in unified_dir.glob("*_sessions.jsonl"):
                name = unified_file.stem.replace("_sessions", "")
                sources.add(name)

        return sources

    def display_files(source: str, files: list[FileInfo]) -> int:
        """Display files grouped by category, return total size."""
        if not files:
            click.echo(f"  No files found for source '{source}'")
            return 0

        # Group by category
        by_category: dict[str, list[FileInfo]] = {}
        for f in files:
            if f.category not in by_category:
                by_category[f.category] = []
            by_category[f.category].append(f)

        total_size = 0
        for category in ["State files", "Raw data", "Parquet files", "Unified files"]:
            if category not in by_category:
                continue
            click.echo(f"\n  {category}:")
            for f in sorted(by_category[category], key=lambda x: x.path.name):
                # Show path relative to home
                rel_path = str(f.path).replace(str(Path.home()), "~")
                click.echo(f"    {rel_path} ({f.size_str})")
                total_size += f.size

        return total_size

    # Validation
    if not source_name and not all_sources:
        click.echo(
            click.style(
                "Error: Must specify --source or --all",
                fg="red",
            )
        )
        raise SystemExit(1)

    if source_name and all_sources:
        click.echo(
            click.style(
                "Error: Cannot use both --source and --all",
                fg="red",
            )
        )
        raise SystemExit(1)

    # Determine sources to process
    if all_sources:
        sources = sorted(get_all_sources())
        if not sources:
            click.echo("No sources found with data.")
            return
    else:
        sources = [source_name]

    # Discover files for all sources
    all_files: dict[str, list[FileInfo]] = {}
    grand_total = 0
    file_count = 0

    for source in sources:
        files = discover_source_files(source)
        if files:
            all_files[source] = files
            for f in files:
                grand_total += f.size
                file_count += 1

    if not all_files:
        if all_sources:
            click.echo("No data files found for any source.")
        else:
            click.echo(f"No data files found for source '{source_name}'.")
        return

    # Display what would be deleted
    if dry_run:
        click.echo(
            click.style(
                "Files that would be deleted:",
                fg="cyan",
                bold=True,
            )
        )
    else:
        click.echo(
            click.style(
                "Files to be deleted:",
                fg="yellow",
                bold=True,
            )
        )

    for source in sorted(all_files.keys()):
        click.echo(f"\nSource: {click.style(source, fg='white', bold=True)}")
        display_files(source, all_files[source])

    # Summary
    click.echo()
    if grand_total >= 1024 * 1024 * 1024:
        total_str = f"{grand_total / 1024 / 1024 / 1024:.1f}GB"
    elif grand_total >= 1024 * 1024:
        total_str = f"{grand_total / 1024 / 1024:.1f}MB"
    elif grand_total >= 1024:
        total_str = f"{grand_total / 1024:.1f}KB"
    else:
        total_str = f"{grand_total}B"

    click.echo(f"Total: {file_count} files, {total_str}")

    if dry_run:
        click.echo()
        if all_sources:
            click.echo("To delete these files, run: dal purge --all")
        else:
            click.echo(f"To delete these files, run: dal purge --source {source_name}")
        return

    # Confirmation
    if not force:
        click.echo()
        if not click.confirm(
            click.style("Are you sure you want to delete these files?", fg="yellow"),
            default=False,
        ):
            click.echo("Aborted.")
            return

    # Delete files
    click.echo()
    deleted_count = 0
    deleted_size = 0
    errors: list[str] = []

    for source in sorted(all_files.keys()):
        for f in all_files[source]:
            try:
                if f.path.is_dir():
                    shutil.rmtree(f.path)
                else:
                    f.path.unlink()
                deleted_count += 1
                deleted_size += f.size
            except OSError as e:
                errors.append(f"  {f.path}: {e}")

        # Also remove empty raw directory
        storage_path = get_storage_path()
        raw_dir = storage_path / "raw" / source
        if raw_dir.exists() and raw_dir.is_dir():
            try:
                # Only remove if empty
                if not any(raw_dir.iterdir()):
                    raw_dir.rmdir()
            except OSError:
                pass  # Ignore if not empty or other errors

    # Report results
    if deleted_size >= 1024 * 1024 * 1024:
        deleted_str = f"{deleted_size / 1024 / 1024 / 1024:.1f}GB"
    elif deleted_size >= 1024 * 1024:
        deleted_str = f"{deleted_size / 1024 / 1024:.1f}MB"
    elif deleted_size >= 1024:
        deleted_str = f"{deleted_size / 1024:.1f}KB"
    else:
        deleted_str = f"{deleted_size}B"

    click.echo(
        click.style(
            f"Deleted {deleted_count} files ({deleted_str})",
            fg="green",
            bold=True,
        )
    )

    if errors:
        click.echo()
        click.echo(click.style("Errors:", fg="red"))
        for error in errors:
            click.echo(error)


if __name__ == "__main__":
    main()
