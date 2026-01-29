"""
DAL CLI - Dev Agent Lens Command Line Interface

Provides unified CLI for trace data collection, querying, and analysis.

Commands:
    dal sync              Robust sync with checkpointing, retry, and session unification
    dal sync --full       Full sync ignoring state
    dal sync --status     Show in-progress sync status
    dal sync --history    Show completed sync history
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
from dev_agent_lens.analysis.chains import (
    build_conversation_chains,
    ConversationChain,
)


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
# Source selection
@click.option(
    "--source",
    "source_name",
    type=str,
    help="Sync from a named source (configured via 'dal config add-source')",
)
@click.option(
    "--all-sources",
    is_flag=True,
    help="Sync from all configured sources",
)
# Date range (priority: --start-date > --days > last_sync > default 30 days)
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
    "--days",
    type=int,
    default=None,
    help="Number of days to sync. If not specified, uses last_sync or defaults to 30.",
)
@click.option(
    "--full",
    is_flag=True,
    help="Ignore saved state, sync full date range (default: 30 days or --days N)",
)
# Batch control
@click.option(
    "--batch-days",
    type=int,
    default=None,
    help="Days per batch (default: auto based on sync size)",
)
@click.option(
    "--batch-hours",
    type=float,
    default=None,
    help="Hours per batch (overrides --batch-days). Use 0.25 for 15-min batches.",
)
# Performance
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Max spans per request (default: auto based on source type)",
)
@click.option(
    "--timeout",
    type=int,
    default=60,
    help="Request timeout in seconds (default: 60)",
)
@click.option(
    "--delay",
    type=float,
    default=None,
    help="Delay between requests in seconds (default: auto based on sync size)",
)
@click.option(
    "--retries",
    type=int,
    default=3,
    help="Retries per batch on failure (default: 3)",
)
# Processing
@click.option(
    "--with-annotations",
    is_flag=True,
    help="Also fetch annotations (slower, disabled by default)",
)
@click.option(
    "--skip-normalize",
    is_flag=True,
    help="Save raw data only without normalization (faster for large backfills)",
)
@click.option(
    "--skip-unify",
    is_flag=True,
    help="Skip session unification after sync (default: unify sessions)",
)
@click.option(
    "--no-auto-subdivide",
    is_flag=True,
    help="Disable automatic batch subdivision when hitting limits",
)
# State control
@click.option(
    "--no-update-state",
    is_flag=True,
    help="Don't update last_sync after completion. Useful for filling gaps.",
)
@click.option(
    "--resume/--no-resume",
    default=True,
    help="Resume from checkpoint if available (default: resume)",
)
# Status & cleanup
@click.option(
    "--status",
    "show_status",
    is_flag=True,
    help="Show in-progress sync status without syncing",
)
@click.option(
    "--history",
    "show_history",
    is_flag=True,
    help="Include completed syncs in --status output",
)
@click.option(
    "--clean",
    is_flag=True,
    help="Clean up completed sync checkpoint files",
)
# SQLite mode (Phoenix only)
@click.option(
    "--sqlite",
    is_flag=True,
    help="Use direct SQLite access instead of HTTP API (Phoenix only, requires local Docker)",
)
@click.option(
    "--sqlite-container",
    type=str,
    default=None,
    help="Docker container name for SQLite access (e.g., 'dev-agent-lens-phoenix-1')",
)
# Verbosity
@click.option(
    "-v", "--verbose",
    is_flag=True,
    help="Enable verbose logging for debugging sync issues",
)
def sync(
    source_name: str | None,
    all_sources: bool,
    start_date: str | None,
    end_date: str | None,
    days: int | None,
    full: bool,
    batch_days: int | None,
    batch_hours: float | None,
    limit: int | None,
    timeout: int,
    delay: float | None,
    retries: int,
    with_annotations: bool,
    skip_normalize: bool,
    skip_unify: bool,
    no_auto_subdivide: bool,
    no_update_state: bool,
    resume: bool,
    show_status: bool,
    show_history: bool,
    clean: bool,
    sqlite: bool,
    sqlite_container: str | None,
    verbose: bool,
) -> None:
    """Sync trace data from configured sources with robust checkpointing.

    Features automatic retry, checkpointing for large syncs, and session
    unification. Combines the best of incremental and historical sync.

    Date Range Priority:
        1. --start-date/--end-date (explicit range)
        2. --days N (last N days)
        3. last_sync from state (incremental)
        4. Default: last 30 days

    Smart Defaults:
        - Syncs < 7 days: lightweight mode (no checkpointing, 0.5s delay)
        - Syncs >= 7 days: robust mode (checkpointing enabled, 2s delay)

    Examples:

        dal sync                        # Incremental sync from last_sync

        dal sync --source phoenix-alex  # Sync from named source

        dal sync --days 7               # Sync last 7 days

        dal sync --full                 # Full sync ignoring saved state

        dal sync --start-date 2024-12-01 --end-date 2024-12-15

        dal sync --status               # Check sync progress

        dal sync --skip-unify           # Skip session unification (for large backfills)

        dal sync --sqlite               # Use direct SQLite access (Phoenix only)
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
    from dev_agent_lens.query.query import _group_by_session

    # Configure logging based on --verbose flag
    if verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
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
            click.echo(click.style("Sync Status", bold=True))
            click.echo()
            for sync_state in syncs:
                progress = sync_state.progress_percent
                eta = sync_state.get_eta()
                eta_str = f" (ETA: {eta})" if eta else ""

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

                if sync_state.current_run:
                    run_info = f"    Run: {sync_state.current_run.run_id}"
                    if status_code == SyncStatus.IN_PROGRESS:
                        run_info += f" (PID: {sync_state.current_run.pid})"
                    elif status_code == SyncStatus.STALE:
                        run_info += f" (PID: {sync_state.current_run.pid} - dead)"
                    click.echo(run_info)

                remaining_ranges = sync_state.get_remaining_ranges()
                click.echo(f"    Batches: {sync_state.stats.batches_completed} completed, {sync_state.stats.batches_failed} failed")
                if remaining_ranges:
                    click.echo(f"    Remaining gaps: {len(remaining_ranges)}")
                if sync_state.failed_ranges:
                    click.echo(f"    Failed ranges pending retry: {len(sync_state.failed_ranges)}")
                click.echo()

        if show_history:
            sync_state_obj = SyncState()
            source_manager = SourceManager()
            all_sources = source_manager.list_sources()

            in_progress_sources = {s.source for s in syncs}
            completed_sources = []

            for source in all_sources:
                last_sync = sync_state_obj.get_last_sync(source.name)
                if last_sync and source.name not in in_progress_sources:
                    completed_sources.append((source, last_sync))

            if completed_sources:
                if has_in_progress:
                    click.echo()
                click.echo(click.style("Completed Syncs", bold=True))
                click.echo()
                for source, last_sync in sorted(completed_sources, key=lambda x: x[1], reverse=True):
                    status = click.style("completed", fg="green")
                    click.echo(f"  {source.name}: {status}")
                    click.echo(f"    Last sync: {last_sync.strftime('%Y-%m-%d %H:%M:%S')}")
                    click.echo(f"    Type: {source.source_type.value}")
                    click.echo()
            elif not has_in_progress:
                click.echo("No syncs found (in-progress or completed).")
                click.echo("Use 'dal sync --source <name>' to start a sync.")
                return

        if not has_in_progress and not show_history:
            click.echo("No syncs in progress.")
            click.echo("Tip: Use --history to see completed syncs.")
        return

    sync_start = time.time()

    # Determine sources to sync
    source_manager = SourceManager()
    sources_to_sync: list[SourceConfig] = []

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
    elif all_sources:
        # Sync from all configured sources
        sources_to_sync = source_manager.list_sources()
        if not sources_to_sync:
            click.echo(
                click.style(
                    "Error: No sources configured. "
                    "Use 'dal config add-source' to add sources.",
                    fg="red",
                )
            )
            raise SystemExit(1)
    else:
        # Auto-detect: use named sources
        sources_to_sync = source_manager.list_sources()
        if not sources_to_sync:
            click.echo(
                click.style(
                    "Error: No sources configured.\n"
                    "  Use 'dal config add-source' to add named sources",
                    fg="red",
                )
            )
            raise SystemExit(1)

    # Parse end date
    if end_date:
        try:
            end_date_parsed = datetime.strptime(end_date, "%Y-%m-%d")
            if end_date_parsed.date() == datetime.now().date():
                sync_end_time = datetime.now()
            else:
                sync_end_time = end_date_parsed.replace(hour=23, minute=59, second=59)
        except ValueError:
            click.echo(click.style(f"Error: Invalid end-date format '{end_date}'. Use YYYY-MM-DD.", fg="red"))
            raise SystemExit(1)
    else:
        sync_end_time = datetime.now()

    # Parse start date with priority: --start-date > --days > last_sync > default 30
    state = SyncState()
    sync_start_time: datetime | None = None
    date_source = ""

    if start_date:
        try:
            sync_start_time = datetime.strptime(start_date, "%Y-%m-%d")
            date_source = f"explicit start date: {start_date}"
        except ValueError:
            click.echo(click.style(f"Error: Invalid start-date format '{start_date}'. Use YYYY-MM-DD.", fg="red"))
            raise SystemExit(1)
    elif days is not None:
        sync_start_time = sync_end_time - timedelta(days=days)
        date_source = f"--days {days}"
    elif not full:
        # Try to use last_sync for incremental (use first source for now)
        if sources_to_sync:
            last_sync = state.get_last_sync(sources_to_sync[0].name)
            if last_sync:
                sync_start_time = last_sync
                date_source = f"incremental from last_sync: {last_sync.strftime('%Y-%m-%d %H:%M')}"

    if sync_start_time is None:
        # Default to 30 days
        default_days = 30
        sync_start_time = sync_end_time - timedelta(days=default_days)
        date_source = f"default {default_days} days"

    # Calculate sync duration for smart defaults
    sync_duration = sync_end_time - sync_start_time
    sync_days = sync_duration.days
    is_large_sync = sync_days >= 7

    # Smart defaults based on sync size
    effective_delay = delay if delay is not None else (2.0 if is_large_sync else 0.5)
    effective_batch_days = batch_days if batch_days is not None else (1 if is_large_sync else None)
    use_checkpointing = is_large_sync

    # Set effective limit based on source type (will be determined per-source)
    effective_limit = limit if limit is not None else 50000

    # Handle --sqlite flag validation
    use_sqlite = False
    resolved_sqlite_container: str | None = None

    if sqlite or sqlite_container:
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

        resolved_sqlite_container = sqlite_container or source.sqlite_container
        if not resolved_sqlite_container:
            click.echo(click.style(
                f"Error: --sqlite requires a Docker container name.\n"
                f"  Use --sqlite-container <name> or add sqlite_container to source config.",
                fg="red",
            ))
            raise SystemExit(1)

        use_sqlite = True
        click.echo(click.style(
            f"SQLite mode: Using direct database access via container '{resolved_sqlite_container}'",
            fg="cyan",
        ))

    # Display sync plan
    click.echo(click.style("Sync Configuration", bold=True))
    click.echo(f"  Sources: {', '.join(s.name for s in sources_to_sync)}")
    click.echo(f"  Date range: {sync_start_time.strftime('%Y-%m-%d')} to {sync_end_time.strftime('%Y-%m-%d')} ({date_source})")
    click.echo(f"  Duration: {sync_days} days")
    click.echo(f"  Mode: {'robust (checkpointing enabled)' if use_checkpointing else 'lightweight'}")
    click.echo(f"  Delay: {effective_delay}s between requests")
    if effective_batch_days:
        click.echo(f"  Batch size: {effective_batch_days} day(s)")
    if batch_hours:
        click.echo(f"  Batch size: {batch_hours} hour(s)")
    click.echo(f"  Session unification: {'disabled (--skip-unify)' if skip_unify else 'enabled'}")
    if no_update_state:
        click.echo(click.style("  State update: DISABLED", fg="yellow"))
    click.echo()

    # Create sync config
    sync_config = SyncConfig(
        batch_hours=batch_hours,
        batch_days=effective_batch_days or 1,
        limit=effective_limit,
        timeout=timeout,
        delay=effective_delay,
    )

    # Minimum subdivision window
    MIN_SUBDIVISION = timedelta(minutes=1)

    total_spans = 0
    total_batches_completed = 0
    total_batches_failed = 0
    total_subdivisions = 0
    all_errors: list[str] = []
    synced_sources: list[str] = []  # Track successfully synced sources for unification

    def pre_subdivide_ranges(
        ranges: list[tuple[datetime, datetime]],
        initial_window: timedelta,
    ) -> list[tuple[datetime, datetime]]:
        """Pre-subdivide ranges into smaller chunks for aggressive querying."""
        if initial_window <= timedelta(0):
            return ranges
        result = []
        for range_start, range_end in ranges:
            range_duration = range_end - range_start
            if range_duration <= initial_window:
                result.append((range_start, range_end))
            else:
                current = range_start
                while current < range_end:
                    chunk_end = min(current + initial_window, range_end)
                    result.append((current, chunk_end))
                    current = chunk_end
        return result

    def fetch_with_subdivision(
        client,
        batch_start: datetime,
        batch_end: datetime,
        depth: int = 0,
        is_first_request: bool = True,
        store: OxenStore | None = None,
        checkpoint_state: HistoricalSyncState | None = None,
        normalizer_fn=None,
        backend_name: str | None = None,
        batch_key: str | None = None,
    ) -> tuple[list, int, int]:
        """Fetch spans with auto-subdivision and incremental persistence."""
        nonlocal total_subdivisions

        indent = "    " * depth if depth > 0 else ""
        incremental_mode = store is not None and checkpoint_state is not None

        # Add delay between requests (except for the very first one)
        if not is_first_request and effective_delay > 0:
            time.sleep(effective_delay)

        # Attempt fetch with retries
        batch_df = None
        last_error = None
        for attempt in range(1, retries + 1):
            try:
                batch_df = client.get_spans_dataframe(
                    start_time=batch_start,
                    end_time=batch_end,
                    limit=effective_limit,
                )
                break
            except Exception as e:
                last_error = e
                if attempt < retries:
                    backoff = max(effective_delay, 2 ** attempt)
                    time.sleep(backoff)

        if batch_df is None:
            if last_error:
                raise last_error
            return [], 0, 0

        if hasattr(batch_df, "empty") and batch_df.empty:
            return [], 0, 0

        batch_count = len(batch_df)

        # Check if we hit the limit and should subdivide
        if effective_limit is not None and batch_count >= effective_limit and not no_auto_subdivide:
            window_size = batch_end - batch_start
            if window_size > MIN_SUBDIVISION:
                midpoint = batch_start + window_size / 2
                total_subdivisions += 1

                if depth == 0:
                    click.echo(click.style(f" hit limit ({batch_count:,}), subdividing...", fg="yellow"))
                else:
                    click.echo(f"{indent}→ {batch_start.strftime('%H:%M')}-{batch_end.strftime('%H:%M')}: " +
                              click.style(f"hit limit, subdividing...", fg="yellow"))

                left_dfs, left_subs, left_saved = fetch_with_subdivision(
                    client, batch_start, midpoint, depth + 1, is_first_request=False,
                    store=store, checkpoint_state=checkpoint_state, normalizer_fn=normalizer_fn,
                    backend_name=backend_name, batch_key=batch_key,
                )
                right_dfs, right_subs, right_saved = fetch_with_subdivision(
                    client, midpoint, batch_end, depth + 1, is_first_request=False,
                    store=store, checkpoint_state=checkpoint_state, normalizer_fn=normalizer_fn,
                    backend_name=backend_name, batch_key=batch_key,
                )

                return left_dfs + right_dfs, left_subs + right_subs + 1, left_saved + right_saved

        # Normal case: didn't hit limit
        if depth > 0:
            click.echo(f"{indent}→ {batch_start.strftime('%H:%M')}-{batch_end.strftime('%H:%M')}: " +
                      click.style(f"{batch_count:,} spans", fg="green"))

            if incremental_mode and batch_key:
                try:
                    if normalizer_fn and not skip_normalize:
                        try:
                            normalized = normalizer_fn(batch_df)
                            store.append_spans(normalized, backend=backend_name)
                        except Exception:
                            store.append_spans(batch_df, backend=f"{backend_name}-raw")
                    else:
                        store.append_spans(batch_df, backend=backend_name)
                    checkpoint_state.add_partial_range(batch_key, batch_start, batch_end, batch_count)
                    return [], 0, batch_count
                except Exception as save_err:
                    click.echo(click.style(f" WARN: failed to save partial: {save_err}", fg="yellow"))

        return [batch_df], 0, 0

    def process_source_sync(
        source: SourceConfig,
        source_start: datetime,
        source_end: datetime,
    ) -> tuple[int, int, int]:
        """Process sync for a single source. Returns (spans, completed_batches, failed_batches)."""
        nonlocal total_subdivisions

        # Create source-specific store
        source_store = OxenStore.for_source(source.name)

        # Set up environment and determine client/normalizer
        sqlite_client = None

        if source.source_type == SourceType.PHOENIX:
            if source.url:
                os.environ["DAL_PHOENIX_URL"] = source.url
            if source.project:
                os.environ["DAL_PHOENIX_PROJECT"] = source.project

            if use_sqlite and resolved_sqlite_container:
                db_path = f"docker://{resolved_sqlite_container}:/root/.phoenix/phoenix.db"
                sqlite_client = PhoenixSQLiteClient(
                    db_path=db_path,
                    project=source.project or os.getenv("DAL_PHOENIX_PROJECT", "dev-agent-lens"),
                )
                try:
                    if not sqlite_client.test_connection():
                        click.echo(click.style(
                            f"Error: Could not connect to Phoenix SQLite database",
                            fg="red",
                        ))
                        return 0, 0, 1
                except Exception as e:
                    click.echo(click.style(f"Error: SQLite connection test failed: {e}", fg="red"))
                    return 0, 0, 1

            client_class = PhoenixClient
            normalizer_fn = normalize_phoenix
            is_phoenix = True
        else:  # ARIZE
            if source.space_key:
                os.environ["ARIZE_SPACE_KEY"] = source.space_key
            if source.model_id:
                os.environ["ARIZE_MODEL_ID"] = source.model_id
            client_class = ArizeClient
            normalizer_fn = normalize_arize
            is_phoenix = False

        # For large syncs (7+ days), use checkpointing
        if use_checkpointing:
            return _process_with_checkpointing(
                source, source_start, source_end,
                source_store, client_class, normalizer_fn, is_phoenix, sqlite_client
            )
        else:
            return _process_lightweight(
                source, source_start, source_end,
                source_store, client_class, normalizer_fn, is_phoenix, sqlite_client
            )

    def _process_lightweight(
        source: SourceConfig,
        source_start: datetime,
        source_end: datetime,
        source_store: OxenStore,
        client_class: type,
        normalizer_fn,
        is_phoenix: bool,
        sqlite_client=None,
    ) -> tuple[int, int, int]:
        """Lightweight sync without checkpointing (for small syncs < 7 days)."""
        click.echo(f"[{source.name}] Starting sync (lightweight mode)...")

        # Create client
        if sqlite_client:
            client = sqlite_client
        elif is_phoenix:
            client = client_class(timeout=float(timeout))
        else:
            client = client_class()

        # Generate batches
        batches = []
        if batch_hours:
            batch_duration = timedelta(hours=batch_hours)
        elif effective_batch_days:
            batch_duration = timedelta(days=effective_batch_days)
        else:
            batch_duration = source_end - source_start  # Single batch

        batch_end = source_end
        while batch_end > source_start:
            batch_start_calc = max(batch_end - batch_duration, source_start)
            batches.append((batch_start_calc, batch_end))
            batch_end = batch_start_calc

        batches.sort(key=lambda x: x[1], reverse=True)

        if len(batches) > 1:
            click.echo(f"  Processing {len(batches)} batches...")

        source_spans = 0
        completed = 0
        failed = 0

        for i, (b_start, b_end) in enumerate(batches):
            if i > 0 and effective_delay > 0:
                time.sleep(effective_delay)

            click.echo(f"  Batch {i+1}/{len(batches)}: {b_start.strftime('%Y-%m-%d %H:%M')} to {b_end.strftime('%Y-%m-%d %H:%M')}", nl=False)

            try:
                dataframes, subdivisions, _ = fetch_with_subdivision(
                    client, b_start, b_end,
                    store=source_store,
                    normalizer_fn=normalizer_fn,
                    backend_name=source.name,
                )

                if dataframes:
                    combined_df = pd.concat(dataframes, ignore_index=True) if len(dataframes) > 1 else dataframes[0]
                    batch_count = len(combined_df)

                    # Save results
                    if skip_normalize:
                        source_store.append_spans(combined_df, backend=source.name)
                    else:
                        try:
                            normalized = normalizer_fn(combined_df)
                            source_store.append_spans(normalized, backend=source.name)
                        except Exception:
                            source_store.append_spans(combined_df, backend=f"{source.name}-raw")

                    source_spans += batch_count
                    click.echo(click.style(f" {batch_count:,} spans", fg="green"))
                else:
                    click.echo(click.style(" no spans", fg="yellow"))

                completed += 1

            except Exception as e:
                click.echo(click.style(f" FAILED: {e}", fg="red"))
                failed += 1

        click.echo(f"  [{source.name}] Total: {source_spans:,} spans")
        return source_spans, completed, failed

    def _process_with_checkpointing(
        source: SourceConfig,
        source_start: datetime,
        source_end: datetime,
        source_store: OxenStore,
        client_class: type,
        normalizer_fn,
        is_phoenix: bool,
        sqlite_client=None,
    ) -> tuple[int, int, int]:
        """Robust sync with checkpointing (for large syncs >= 7 days)."""

        # Load or create checkpoint state
        checkpoint_state, is_resuming = HistoricalSyncState.load_or_create(
            source=source.name,
            target_start=source_start,
            target_end=source_end,
            config=sync_config,
            force_resume=False,
        )

        if is_resuming and resume:
            progress = checkpoint_state.progress_percent
            click.echo(f"[{source.name}] Resuming from checkpoint ({progress:.1f}% complete)")
            click.echo(f"  Previously synced: {checkpoint_state.stats.total_spans:,} spans")
        else:
            click.echo(f"[{source.name}] Starting sync (robust mode with checkpointing)...")

        # Create client
        if sqlite_client:
            client = sqlite_client
        elif is_phoenix:
            client = client_class(timeout=float(timeout))
        else:
            client = client_class()

        # Get remaining ranges to process
        if is_resuming and resume:
            remaining_ranges = checkpoint_state.get_remaining_ranges()
            if not remaining_ranges:
                click.echo(click.style(f"  Already complete!", fg="green"))
                return checkpoint_state.stats.total_spans, checkpoint_state.stats.batches_completed, 0
        else:
            remaining_ranges = [(source_start, source_end)]

        # Generate batches from remaining ranges
        batch_duration = sync_config.batch_duration
        batches = []
        for range_start, range_end in remaining_ranges:
            batch_end = range_end
            while batch_end > range_start:
                b_start = max(batch_end - batch_duration, range_start)
                batches.append((b_start, batch_end))
                batch_end = b_start

        batches.sort(key=lambda x: x[1], reverse=True)

        persisted_failed_ranges = [(r.start, r.end) for r in checkpoint_state.failed_ranges]

        if not batches and not persisted_failed_ranges:
            click.echo(click.style(f"  No batches to process.", fg="yellow"))
            return checkpoint_state.stats.total_spans, checkpoint_state.stats.batches_completed, 0

        if batches:
            click.echo(f"  Processing {len(batches)} batches...")
        if persisted_failed_ranges:
            click.echo(click.style(f"  {len(persisted_failed_ranges)} previously failed batches to retry", fg="cyan"))

        source_spans = checkpoint_state.stats.total_spans if (is_resuming and resume) else 0
        completed = checkpoint_state.stats.batches_completed if (is_resuming and resume) else 0
        failed = 0
        failed_batches: list[tuple[datetime, datetime]] = []

        try:
            for i, (b_start, b_end) in enumerate(batches):
                if i > 0 and effective_delay > 0:
                    time.sleep(effective_delay)

                overall_progress = checkpoint_state.progress_percent
                batch_key = b_start.strftime("%Y-%m-%d %H:%M:%S")

                click.echo(
                    f"  [{overall_progress:.0f}%] Batch {i+1}/{len(batches)}: "
                    f"{b_start.strftime('%Y-%m-%d')} to {b_end.strftime('%Y-%m-%d')}",
                    nl=False,
                )

                try:
                    checkpoint_state.mark_batch_started(b_start, b_end)

                    dataframes, subdivisions, saved_incrementally = fetch_with_subdivision(
                        client, b_start, b_end,
                        store=source_store,
                        checkpoint_state=checkpoint_state,
                        normalizer_fn=normalizer_fn,
                        backend_name=f"{source.name}-historical",
                        batch_key=batch_key,
                    )

                    # Handle non-incrementally saved data
                    if dataframes:
                        combined_df = pd.concat(dataframes, ignore_index=True) if len(dataframes) > 1 else dataframes[0]
                        batch_count = len(combined_df)

                        if skip_normalize:
                            source_store.append_spans(combined_df, backend=f"{source.name}-historical")
                        else:
                            try:
                                normalized = normalizer_fn(combined_df)
                                source_store.append_spans(normalized, backend=f"{source.name}-historical")
                            except Exception:
                                source_store.append_spans(combined_df, backend=f"{source.name}-historical-raw")

                        saved_incrementally += batch_count

                    checkpoint_state.clear_partial_ranges(batch_key)
                    checkpoint_state.mark_batch_completed(b_start, b_end, saved_incrementally)
                    source_spans += saved_incrementally
                    completed += 1

                    click.echo(click.style(f" {saved_incrementally:,} spans", fg="green"))

                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    click.echo(click.style(f" FAILED: {e}", fg="red"))
                    checkpoint_state.mark_batch_failed()
                    failed += 1
                    failed_batches.append((b_start, b_end))

        except KeyboardInterrupt:
            click.echo()
            click.echo(click.style("  Interrupted! Progress saved.", fg="yellow"))
            click.echo(f"  Resume with: dal sync --source {source.name}")
            raise SystemExit(130)

        # Retry failed batches
        if failed_batches:
            click.echo()
            click.echo(click.style(f"  Retrying {len(failed_batches)} failed batches...", fg="cyan"))
            retry_delay = max(effective_delay * 5, 10.0)

            for i, (b_start, b_end) in enumerate(failed_batches):
                time.sleep(retry_delay)
                failed -= 1  # Pre-decrement since we're retrying

                batch_key = b_start.strftime("%Y-%m-%d %H:%M:%S")
                click.echo(f"  [retry] {b_start.strftime('%Y-%m-%d')} to {b_end.strftime('%Y-%m-%d')}", nl=False)

                try:
                    checkpoint_state.mark_batch_started(b_start, b_end)
                    dataframes, _, saved = fetch_with_subdivision(
                        client, b_start, b_end,
                        store=source_store, checkpoint_state=checkpoint_state,
                        normalizer_fn=normalizer_fn, backend_name=f"{source.name}-historical",
                        batch_key=batch_key,
                    )

                    if dataframes:
                        combined_df = pd.concat(dataframes, ignore_index=True) if len(dataframes) > 1 else dataframes[0]
                        if skip_normalize:
                            source_store.append_spans(combined_df, backend=f"{source.name}-historical")
                        else:
                            try:
                                normalized = normalizer_fn(combined_df)
                                source_store.append_spans(normalized, backend=f"{source.name}-historical")
                            except Exception:
                                source_store.append_spans(combined_df, backend=f"{source.name}-historical-raw")
                        saved += len(combined_df)

                    checkpoint_state.clear_partial_ranges(batch_key)
                    checkpoint_state.mark_batch_completed(b_start, b_end, saved)
                    source_spans += saved
                    completed += 1
                    click.echo(click.style(f" {saved:,} spans", fg="green"))

                except Exception as e:
                    click.echo(click.style(f" FAILED: {e}", fg="red"))
                    checkpoint_state.mark_batch_failed()
                    failed += 1

        click.echo()
        click.echo(f"  [{source.name}] Total: {source_spans:,} spans")

        if checkpoint_state.is_complete:
            click.echo(click.style(f"  Sync complete for {source.name}!", fg="green"))

        return source_spans, completed, failed

    # Process each source
    for source in sources_to_sync:
        try:
            spans, completed, failed = process_source_sync(source, sync_start_time, sync_end_time)
            total_spans += spans
            total_batches_completed += completed
            total_batches_failed += failed

            if failed == 0:
                synced_sources.append(source.name)

        except Exception as e:
            error_msg = f"[{source.name}] Error: {e}"
            all_errors.append(error_msg)
            click.echo(click.style(f"  [FAIL] {e}", fg="red"))

        click.echo()

    # Session unification (if not skipped and we had successful syncs)
    if not skip_unify and synced_sources:
        click.echo(click.style("Session Unification", bold=True))
        for source_name_to_unify in synced_sources:
            try:
                click.echo(f"  Unifying sessions for {source_name_to_unify}...", nl=False)

                # Get storage paths
                from dev_agent_lens.storage import get_storage_path
                from pathlib import Path
                import json

                storage_path = get_storage_path()
                sessions_dir = Path(storage_path) / "sessions" / source_name_to_unify
                raw_dir = Path(storage_path) / "raw" / source_name_to_unify

                # Determine data source
                sessions_exist = sessions_dir.exists() and any(sessions_dir.glob("sessions_*.jsonl"))
                raw_exists = raw_dir.exists() and any(raw_dir.glob("sync_*.jsonl"))

                if not sessions_exist and not raw_exists:
                    click.echo(click.style(" no data found", fg="yellow"))
                    continue

                # Read spans
                if sessions_exist:
                    sessions_file = sessions_dir / "sessions_current.jsonl"
                    if not sessions_file.exists():
                        jsonl_files = list(sessions_dir.glob("sessions_*.jsonl"))
                        if jsonl_files:
                            sessions_file = max(jsonl_files, key=lambda f: f.stat().st_mtime)
                        else:
                            sessions_file = None

                    if sessions_file:
                        from dev_agent_lens.core.unify import read_sessions_file
                        df = read_sessions_file(sessions_file)
                        spans = df.to_dict(orient="records") if not df.empty else []
                    else:
                        spans = []
                else:
                    spans = []
                    for raw_file in sorted(raw_dir.glob("sync_*.jsonl")):
                        with open(raw_file) as f:
                            for line in f:
                                if line.strip():
                                    try:
                                        spans.append(json.loads(line))
                                    except json.JSONDecodeError:
                                        continue

                if not spans:
                    click.echo(click.style(" no spans to unify", fg="yellow"))
                    continue

                # Group by session
                sessions = _group_by_session(spans)

                # Write unified sessions
                unified_dir = Path(storage_path) / "unified"
                unified_dir.mkdir(parents=True, exist_ok=True)
                out_file = unified_dir / f"{source_name_to_unify}_sessions.jsonl"

                with open(out_file, "w") as f:
                    for session in sessions:
                        json.dump(session, f, default=str)
                        f.write("\n")

                total_session_spans = sum(s.get("span_count", 0) for s in sessions)
                click.echo(click.style(f" {len(sessions):,} sessions ({total_session_spans:,} spans)", fg="green"))

            except Exception as e:
                click.echo(click.style(f" FAILED: {e}", fg="red"))
                # Don't fail the whole sync if unification fails
                click.echo(click.style(
                    f"    Raw data was saved. Retry with: dal export-sessions --source {source_name_to_unify}",
                    fg="yellow"
                ))

    # Update sync state
    if not no_update_state and total_batches_completed > 0 and total_batches_failed == 0:
        for source in sources_to_sync:
            state.set_last_sync(source.name, sync_end_time)
            click.echo(f"Updated sync state for '{source.name}' to {sync_end_time.strftime('%Y-%m-%d %H:%M')}")

            # Clean up completed historical sync checkpoint if it exists
            if use_checkpointing:
                clear_historical_sync(source.name)

    # Final summary
    elapsed = time.time() - sync_start
    click.echo()
    click.echo("=" * 50)
    click.echo(click.style("Sync Summary", bold=True))
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
        for error in all_errors[:10]:
            click.echo(f"  - {error}")
        if len(all_errors) > 10:
            click.echo(f"  ... and {len(all_errors) - 10} more")
        raise SystemExit(1)

    click.echo()
    click.echo(click.style("Sync complete!", fg="green"))


# End of sync command - sync-historical has been removed and unified into sync


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
@click.option(
    "--skill",
    type=str,
    help="Filter by skill name (e.g., 'draft-project', 'commit')",
)
def stats(file: str | None, source_name: str | None, by_session: bool, output: str, top_tools: int, skill: str | None) -> None:
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

    # Apply skill filter if specified
    if skill:
        from dev_agent_lens.query.parquet_query import extract_skill_name_from_span

        original_count = len(spans)
        spans = [s for s in spans if extract_skill_name_from_span(s) == skill]
        click.echo(f"Filtered to {len(spans):,} spans with skill '{skill}' (from {original_count:,})")
        if not spans:
            click.echo(click.style(f"No spans found with skill '{skill}'.", fg="yellow"))
            return

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

        # Skill statistics (only show if skills were used)
        if tools.skill_call_count > 0:
            click.echo(click.style("Skill Statistics", bold=True, underline=True))
            click.echo(f"  Total Skill Calls: {tools.skill_call_count:,}")
            if tools.skill_breakdown:
                click.echo("  Skills Used:")
                for skill_name, count in sorted(tools.skill_breakdown.items(), key=lambda x: -x[1]):
                    click.echo(f"    {skill_name:30} {count:6,} calls")
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
    else:
        for backend_id in backends:
            last_sync = state.get_last_sync(backend_id)
            if last_sync:
                click.echo(f"{backend_id}: Last sync {last_sync.isoformat()}")
            else:
                click.echo(f"{backend_id}: Never synced")

    click.echo()

    # Show available sources (includes parquet sources like claude-local)
    sources = store.list_sources()
    if sources:
        click.echo(click.style("Available Sources", bold=True))
        for source in sources:
            # Check if this source has a parquet file
            parquet_file = store.data_path / "parquet" / f"{source}_events.parquet"
            if parquet_file.exists():
                size_kb = parquet_file.stat().st_size / 1024
                click.echo(f"  {source}: {size_kb:.1f} KB (events parquet)")
            else:
                click.echo(f"  {source}")
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

    Automatically searches all Parquet sources for the session (10-100x faster
    than JSONL). Falls back to JSONL if not found in Parquet.

    Examples:

        dal session abc123

        dal session abc123 --output json
    """
    from dev_agent_lens.query import query_sessions

    # Use query_sessions which auto-detects Parquet sources
    sessions = query_sessions(session_id=session_id)

    if not sessions:
        click.echo(click.style(f"Session not found: {session_id}", fg="red"))
        raise SystemExit(1)

    session = sessions[0]
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
@click.option("--source", "-s", help="Source name to query (optional, auto-detects if not specified)")
@click.option("--parquet/--no-parquet", default=True, help="Use Parquet backend when available")
@click.option("--output", type=click.Choice(["text", "json"]), default="text", help="Output format")
def analyze_tokens_cmd(session_id: str, source: str | None, parquet: bool, output: str) -> None:
    """Analyze token breakdown for a session.

    Shows tokens by category:
    - Input: tool calls, user messages, system prompts
    - Output: model-generated tokens

    Automatically searches all Parquet sources for the session (10-100x faster
    than JSONL). Falls back to JSONL if not found in Parquet.

    Examples:

        dal analyze-tokens abc123

        dal analyze-tokens abc123 --source my-project

        dal analyze-tokens abc123 --output json

        dal analyze-tokens abc123 --no-parquet
    """
    from dev_agent_lens.analysis.tokens import analyze_session_tokens, estimate_cost
    from dev_agent_lens.query import query_sessions

    # Use query_sessions which auto-detects Parquet sources
    sessions = query_sessions(source=source, session_id=session_id, prefer_parquet=parquet)

    if not sessions:
        click.echo(click.style(f"Session not found: {session_id}", fg="red"))
        raise SystemExit(1)

    session = sessions[0]
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
@click.option(
    "--source",
    "-s",
    "sources",
    type=str,
    multiple=True,
    help="Only push files for specific source(s). Can be repeated. E.g., -s claude-local -s phoenix-alex",
)
@click.option(
    "--parquet-only",
    is_flag=True,
    default=False,
    help="Only push parquet files, skip unified JSONL files (much faster)",
)
@click.option(
    "--unified-only",
    is_flag=True,
    default=False,
    help="Only push unified JSONL files, skip parquet files",
)
def push(message: str | None, sources: tuple[str, ...], parquet_only: bool, unified_only: bool):
    """Push unified session files to Oxen remote.

    Commits any changes in the unified/ and parquet/ directories and pushes
    to the configured Oxen remote.

    Multi-user workflow: Each user should name their source 'claude-local-<name>'
    so multiple people can push to the same repo without conflicts.

    Examples:

        dal push                              # Push all files

        dal push --parquet-only               # Skip large unified JSONL files

        dal push -s claude-local-alex --parquet-only  # Push only your Claude data

        dal push -s claude-local-alex -s phoenix-local-alex --parquet-only
    """
    from dev_agent_lens.config import get_oxen_remote, is_oxen_configured

    if parquet_only and unified_only:
        click.echo(click.style("Error: Cannot use both --parquet-only and --unified-only", fg="red"))
        raise SystemExit(1)

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

    # Determine what to include
    include_unified = not parquet_only
    include_parquet = not unified_only
    source_list = list(sources) if sources else None

    # Check if unified directory has content
    unified_files = list(store.unified_dir.glob("*.jsonl")) if store.unified_dir.exists() else []
    if source_list:
        unified_files = [f for f in unified_files if any(f.stem.startswith(s) for s in source_list)]

    # Check if parquet directory has content
    parquet_dir = store.data_path / "parquet"
    parquet_files = list(parquet_dir.glob("*.parquet")) if parquet_dir.exists() else []
    if source_list:
        parquet_files = [f for f in parquet_files if any(f.stem.startswith(s) for s in source_list)]

    # Filter based on flags
    if parquet_only:
        unified_files = []
    if unified_only:
        parquet_files = []

    if not unified_files and not parquet_files:
        if source_list:
            click.echo(
                click.style(
                    f"No files found for source(s): {', '.join(source_list)}\n"
                    "Run 'dal status' to see available sources.",
                    fg="yellow",
                )
            )
        else:
            click.echo(
                click.style(
                    "No files to push.\n"
                    "Run 'dal export-sessions' for unified session files or\n"
                    "'dal export-events' for Claude events parquet.",
                    fg="yellow",
                )
            )
        raise SystemExit(1)

    click.echo(f"Oxen remote: {remote_url}")
    if source_list:
        click.echo(f"Filtering to source(s): {', '.join(source_list)}")
    if parquet_only:
        click.echo("Mode: parquet-only (skipping unified JSONL)")
    elif unified_only:
        click.echo("Mode: unified-only (skipping parquet)")

    if unified_files and include_unified:
        total_size_mb = sum(f.stat().st_size for f in unified_files) / (1024 * 1024)
        click.echo(f"Unified files: {len(unified_files)} ({total_size_mb:.1f} MB total)")
        for f in unified_files:
            size_mb = f.stat().st_size / (1024 * 1024)
            click.echo(f"  - {f.name} ({size_mb:.1f} MB)")
    if parquet_files and include_parquet:
        total_size_mb = sum(f.stat().st_size for f in parquet_files) / (1024 * 1024)
        click.echo(f"Parquet files: {len(parquet_files)} ({total_size_mb:.1f} MB total)")
        for f in parquet_files:
            size_mb = f.stat().st_size / (1024 * 1024)
            click.echo(f"  - {f.name} ({size_mb:.1f} MB)")
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
        all_files = unified_files + parquet_files
        if all_files:
            file_names = ", ".join(f.stem for f in all_files[:3])
            if len(all_files) > 3:
                file_names += f" +{len(all_files) - 3} more"
            if parquet_only:
                message = f"Update parquet: {file_names} ({timestamp})"
            elif unified_only:
                message = f"Update unified: {file_names} ({timestamp})"
            else:
                message = f"Update data: {file_names} ({timestamp})"
        else:
            message = f"Update data ({timestamp})"

    # Commit with selective options
    click.echo(f"Committing: {message}")
    if not store.commit(
        message,
        include_unified=include_unified,
        include_parquet=include_parquet,
        sources=source_list,
    ):
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
    parquet_dir = store.data_path / "parquet"
    parquet_files = list(parquet_dir.glob("*.parquet")) if parquet_dir.exists() else []

    click.echo(click.style("Pull complete!", fg="green", bold=True))
    if unified_files:
        click.echo(f"Unified session files: {len(unified_files)}")
        for f in unified_files:
            size_mb = f.stat().st_size / (1024 * 1024)
            click.echo(f"  - {f.name} ({size_mb:.1f} MB)")
    if parquet_files:
        click.echo(f"Parquet files: {len(parquet_files)}")
        for f in parquet_files:
            size_kb = f.stat().st_size / 1024
            click.echo(f"  - {f.name} ({size_kb:.1f} KB)")


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


@main.command("export-events")
@click.option(
    "--source",
    "source_name",
    type=str,
    default="claude-local",
    help="Source name. Use 'claude-local-<yourname>' for multi-user repos (e.g., claude-local-alex)",
)
@click.option(
    "--output",
    "-o",
    "output_path",
    type=str,
    default=None,
    help="Output path for Parquet file (default: ~/.dal/data/parquet/<source>_events.parquet)",
)
@click.option(
    "--session-id",
    "session_id",
    type=str,
    default=None,
    help="Export only a specific session ID",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Maximum number of sessions to export",
)
@click.option(
    "--compression",
    type=click.Choice(["zstd", "snappy", "gzip", "none"]),
    default="zstd",
    help="Parquet compression codec (default: zstd)",
)
@click.option(
    "--claude-dir",
    type=str,
    default=None,
    help="Custom Claude sessions directory (default: ~/.claude/projects)",
)
def export_events(
    source_name: str,
    output_path: str | None,
    session_id: str | None,
    limit: int | None,
    compression: str,
    claude_dir: str | None,
) -> None:
    """Export Claude sessions to events Parquet format.

    Exports Claude Code sessions to an events-based Parquet format
    optimized for DuckDB analytics queries. Each row represents a
    conversation event (user message, assistant response, tool call, etc.).

    Best Practice: Use 'claude-local-<yourname>' as source name when sharing
    via Oxen, so multiple users can push their data to the same repo.

    Examples:

        dal export-events --source claude-local-alex

        dal export-events --source claude-local-alex --limit 100

        dal export-events --session-id abc123

        dal export-events --output ~/analysis/events.parquet
    """
    from pathlib import Path

    from dev_agent_lens.clients.claude import ClaudeClient
    from dev_agent_lens.export.parquet_events import (
        export_claude_to_events_parquet,
    )

    # Set up Claude client
    client = ClaudeClient(claude_dir=claude_dir)

    if not client.test_connection():
        click.echo(
            click.style(
                f"Error: Claude sessions directory not found: {client.claude_dir}",
                fg="red",
            )
        )
        raise SystemExit(1)

    # Find session files
    if session_id:
        session_file = client.get_session_file_path(session_id)
        if session_file is None:
            click.echo(
                click.style(
                    f"Error: Session not found: {session_id}",
                    fg="red",
                )
            )
            raise SystemExit(1)
        session_files = [session_file]
        click.echo(f"Exporting session: {session_id}")
    else:
        sessions = client.list_sessions(limit=limit)
        if not sessions:
            click.echo(
                click.style(
                    "No Claude sessions found.",
                    fg="yellow",
                )
            )
            raise SystemExit(0)
        session_files = [s.file_path for s in sessions]
        click.echo(f"Found {len(sessions)} sessions")

    # Determine output path - default to ~/.dal/data/parquet/<source>_events.parquet
    if output_path:
        out_path = Path(output_path).expanduser()
    else:
        from dev_agent_lens.storage.oxen_store import get_default_data_path

        parquet_dir = get_default_data_path() / "parquet"
        parquet_dir.mkdir(parents=True, exist_ok=True)
        out_path = parquet_dir / f"{source_name}_events.parquet"

    click.echo(f"Source: {source_name}")
    click.echo(f"Output: {out_path}")
    click.echo(f"Compression: {compression}")
    click.echo()

    # Export
    click.echo("Exporting to events Parquet...")
    result = export_claude_to_events_parquet(
        session_files=session_files,
        output_path=out_path,
        compression=compression,
    )

    click.echo()
    click.echo(click.style("Export complete!", fg="green", bold=True))
    click.echo(f"  Sessions: {result.session_count:,}")
    click.echo(f"  Events: {result.event_count:,}")
    click.echo(f"  Output size: {result.bytes_written / 1024:.1f} KB")
    click.echo()
    click.echo("Event types:")
    for event_type, count in sorted(result.event_type_counts.items()):
        click.echo(f"  {event_type}: {count:,}")
    click.echo()
    click.echo(f"Output: {result.output_path}")


@main.command("query-events")
@click.option(
    "--source",
    "source_name",
    type=str,
    default="claude-local",
    help="Source name (default: claude-local)",
)
@click.option(
    "--session-id",
    "session_id",
    type=str,
    default=None,
    help="Filter to a specific session ID",
)
@click.option(
    "--event-type",
    "event_type",
    type=str,
    default=None,
    help="Filter by event type: user, assistant, tool, subagent, compaction",
)
@click.option(
    "--tool-name",
    "tool_name",
    type=str,
    default=None,
    help="Filter by tool name (e.g., Read, Edit, Bash)",
)
@click.option(
    "--text",
    "text_contains",
    type=str,
    default=None,
    help="Search for text in event content (case-insensitive)",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Maximum number of events to return",
)
@click.option(
    "--stats",
    is_flag=True,
    default=False,
    help="Show statistics instead of events",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format (default: table)",
)
def query_events_cmd(
    source_name: str,
    session_id: str | None,
    event_type: str | None,
    tool_name: str | None,
    text_contains: str | None,
    limit: int | None,
    stats: bool,
    output_format: str,
) -> None:
    """Query events from Claude sessions Parquet file.

    Provides conversation-aware queries for Claude Code session events.
    Supports filtering by event type, tool name, and text search.

    Examples:

        dal query-events --source claude-local --limit 10

        dal query-events --source claude-local --stats

        dal query-events --source claude-local --event-type tool --tool-name Read

        dal query-events --source claude-local --text "def "

        dal query-events --source claude-local --session-id abc123
    """
    import json as json_module

    from dev_agent_lens.query.events_query import (
        find_events_files,
        get_events_stats,
        query_events,
    )

    # Find events file for the source
    events_files = find_events_files(source=source_name)

    if source_name not in events_files:
        click.echo(
            click.style(
                f"Error: No events file found for source '{source_name}'",
                fg="red",
            )
        )
        click.echo(
            f"Run 'dal export-events --source {source_name}' first to generate it."
        )
        raise SystemExit(1)

    events_path = events_files[source_name]

    # Stats mode
    if stats:
        stats_result = get_events_stats(
            events_path,
            session_id=session_id,
            event_type=event_type,
            tool_name=tool_name,
            text_contains=text_contains,
        )
        click.echo(click.style("Events Statistics", fg="cyan", bold=True))
        if stats_result.get("filters"):
            click.echo(click.style("  Filters:", fg="yellow"))
            for k, v in stats_result["filters"].items():
                click.echo(f"    {k}: {v}")
        click.echo(f"  File: {stats_result['file_path']}")
        click.echo(f"  Size: {stats_result['file_size_bytes'] / 1024:.1f} KB")
        click.echo(f"  Total events: {stats_result['total_events']:,}")
        click.echo(f"  Sessions: {stats_result['session_count']:,}")
        click.echo()
        click.echo(click.style("Event Type Counts:", fg="cyan"))
        for etype, count in stats_result["event_type_counts"].items():
            click.echo(f"  {etype}: {count:,}")
        click.echo()
        if stats_result["top_tools"]:
            click.echo(click.style("Top Tools:", fg="cyan"))
            for tname, count in list(stats_result["top_tools"].items())[:10]:
                click.echo(f"  {tname}: {count:,}")
        raise SystemExit(0)

    # Query events
    result = query_events(
        events_path=events_path,
        session_id=session_id,
        event_type=event_type,
        tool_name=tool_name,
        text_contains=text_contains,
        limit=limit,
    )

    if result.total_events == 0:
        click.echo(click.style("No events found.", fg="yellow"))
        raise SystemExit(0)

    # Output results
    if output_format == "json":
        output = {
            "total_events": result.total_events,
            "session_ids": result.session_ids,
            "events": result.events,
        }
        click.echo(json_module.dumps(output, indent=2, default=str))
    else:
        # Table format
        click.echo(
            click.style(
                f"Found {result.total_events} events across {len(result.session_ids)} session(s)",
                fg="green",
            )
        )
        click.echo()

        for event in result.events:
            session_id_display = event.get("session_id", "")[:8]
            order_idx = event.get("order_idx", "?")
            event_type_display = event.get("event_type", "?")
            tool_name_display = event.get("tool_name") or ""

            # Get a preview of text content
            text = event.get("text") or event.get("tool_input") or ""
            if isinstance(text, str) and len(text) > 80:
                text = text[:77] + "..."

            click.echo(
                f"{click.style(session_id_display, fg='blue')} "
                f"[{order_idx:3}] "
                f"{click.style(event_type_display, fg='cyan'):12} "
                f"{click.style(tool_name_display, fg='yellow'):15} "
                f"{text}"
            )


@main.command("query-spans")
@click.option(
    "--source",
    "source_name",
    type=str,
    required=True,
    help="Source name (e.g., phoenix-local-alex, arize-sightline)",
)
@click.option(
    "--session-id",
    "session_id",
    type=str,
    default=None,
    help="Filter to a specific session ID",
)
@click.option(
    "--status-code",
    "status_code",
    type=str,
    default=None,
    help="Filter by status code: OK, ERROR, UNSET",
)
@click.option(
    "--model",
    "model_name",
    type=str,
    default=None,
    help="Filter by LLM model name (case-insensitive partial match)",
)
@click.option(
    "--name",
    "name_pattern",
    type=str,
    default=None,
    help="Filter by span name pattern (e.g., 'Tool', 'Bash')",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Maximum number of spans to return",
)
@click.option(
    "--stats",
    is_flag=True,
    default=False,
    help="Show statistics instead of spans",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format (default: table)",
)
def query_spans_cmd(
    source_name: str,
    session_id: str | None,
    status_code: str | None,
    model_name: str | None,
    name_pattern: str | None,
    limit: int | None,
    stats: bool,
    output_format: str,
) -> None:
    """Query spans from Phoenix/Arize Parquet files.

    Provides high-performance queries for LiteLLM/Phoenix pipeline spans.
    Supports filtering by session, status, model, and span name.

    Examples:

        dal query-spans --source phoenix-local-alex --stats

        dal query-spans --source phoenix-local-alex --status-code ERROR

        dal query-spans --source phoenix-local-alex --name Tool --limit 20

        dal query-spans --source phoenix-local-alex --model claude --stats

        dal query-spans --source arize-sightline --session-id abc123
    """
    import json as json_module

    from dev_agent_lens.query.parquet_query import (
        find_parquet_files,
        get_spans_stats,
        query_spans_simple,
    )

    # Find parquet file for the source
    parquet_files = find_parquet_files(source=source_name)

    if source_name not in parquet_files:
        click.echo(
            click.style(
                f"Error: No spans file found for source '{source_name}'",
                fg="red",
            )
        )
        # List available sources
        all_files = find_parquet_files()
        if all_files:
            click.echo("Available sources:")
            for src in sorted(all_files.keys()):
                click.echo(f"  - {src}")
        raise SystemExit(1)

    spans_path = parquet_files[source_name]["spans"]

    # Stats mode
    if stats:
        stats_result = get_spans_stats(
            spans_path,
            session_id=session_id,
            status_code=status_code,
            model_name=model_name,
            name_pattern=name_pattern,
        )
        click.echo(click.style("Spans Statistics", fg="cyan", bold=True))
        if stats_result.get("filters"):
            click.echo(click.style("  Filters:", fg="yellow"))
            for k, v in stats_result["filters"].items():
                click.echo(f"    {k}: {v}")
        click.echo(f"  File: {stats_result['file_path']}")
        click.echo(f"  Size: {stats_result['file_size_bytes'] / 1024 / 1024:.1f} MB")
        click.echo(f"  Total spans: {stats_result['total_spans']:,}")
        click.echo(f"  Sessions: {stats_result['session_count']:,}")
        click.echo()
        click.echo(click.style("Status Code Counts:", fg="cyan"))
        for scode, count in stats_result["status_code_counts"].items():
            click.echo(f"  {scode}: {count:,}")
        click.echo()
        if stats_result["span_name_counts"]:
            click.echo(click.style("Top Span Names:", fg="cyan"))
            for sname, count in list(stats_result["span_name_counts"].items())[:15]:
                click.echo(f"  {sname}: {count:,}")
        click.echo()
        if stats_result["top_models"]:
            click.echo(click.style("Top Models:", fg="cyan"))
            for mname, count in list(stats_result["top_models"].items())[:10]:
                click.echo(f"  {mname}: {count:,}")
        raise SystemExit(0)

    # Query spans using the simple function for listing
    spans = query_spans_simple(
        spans_path=spans_path,
        session_id=session_id,
        status_code=status_code,
        model_name=model_name,
        name_pattern=name_pattern,
        limit=limit or 50,
    )

    if not spans:
        click.echo(click.style("No spans found.", fg="yellow"))
        raise SystemExit(0)

    # Get unique session count for display
    session_ids = set(s.get("session_id") for s in spans if s.get("session_id"))

    # Output results
    if output_format == "json":
        output = {
            "total_spans": len(spans),
            "total_sessions": len(session_ids),
            "spans": spans,
        }
        click.echo(json_module.dumps(output, indent=2, default=str))
    else:
        # Table format
        click.echo(
            click.style(
                f"Found {len(spans):,} spans across {len(session_ids)} session(s)",
                fg="green",
            )
        )
        click.echo()

        for span in spans:
            session_id_display = (span.get("session_id") or "")[:8]
            span_name = span.get("name", "?")
            status = span.get("status_code", "?")
            start = span.get("start_time", "?")
            if hasattr(start, 'isoformat'):
                start = start.strftime("%Y-%m-%d %H:%M:%S")
            elif hasattr(start, 'strftime'):
                start = start.strftime("%Y-%m-%d %H:%M:%S")
            elif isinstance(start, str) and len(start) > 19:
                start = start[:19]

            # Color-code status
            if status == "OK":
                status_styled = click.style(status, fg="green")
            elif status == "ERROR":
                status_styled = click.style(status, fg="red")
            else:
                status_styled = click.style(status, fg="yellow")

            click.echo(
                f"{click.style(session_id_display, fg='blue')} "
                f"{start} "
                f"{status_styled:5} "
                f"{span_name}"
            )


@main.command("reconstruct")
@click.option(
    "--source",
    "source_name",
    type=str,
    required=True,
    help="Source name to reconstruct (e.g., phoenix-local-alex)",
)
@click.option(
    "--session-id",
    "session_id",
    type=str,
    default=None,
    help="Specific session ID to reconstruct (optional)",
)
@click.option(
    "--output",
    "-o",
    "output_path",
    type=click.Path(),
    help="Output file path (default: stdout)",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["json", "summary"]),
    default="json",
    help="Output format (default: json)",
)
@click.option(
    "--parquet/--jsonl",
    "use_parquet",
    default=True,
    help="Use Parquet (default) or JSONL source",
)
def reconstruct(
    source_name: str,
    session_id: str | None,
    output_path: str | None,
    output_format: str,
    use_parquet: bool,
) -> None:
    """Reconstruct conversations with thread classification.

    Reads unified session data and classifies spans into conversation threads:
    - main_thread: Primary user <-> agent conversation (Sonnet/Opus)
    - ancillary: Status line, topic detection (Haiku models)
    - sub_agent: Task tool invocations and execution spans

    This separates the main conversation flow from background operations,
    making it easier to analyze conversation structure.

    Classification is specific to Claude Code traces. Other coding agents
    may require different classification rules.

    Examples:

        # Reconstruct all sessions from a source (summary view)
        dal reconstruct --source phoenix-local-alex --format summary

        # Reconstruct a specific session (JSON output)
        dal reconstruct --source phoenix-local-alex --session-id abc123

        # Export to file
        dal reconstruct --source arize-ax-alex -o ~/exports/classified.json
    """
    import json
    from pathlib import Path

    from dev_agent_lens.analysis.threads import (
        classify_session_threads,
        get_thread_summary,
    )
    from dev_agent_lens.storage import get_storage_path

    storage_path = get_storage_path()

    # Load session data
    if use_parquet:
        parquet_dir = Path(storage_path) / "parquet"
        spans_file = parquet_dir / f"{source_name}_spans.parquet"

        if not spans_file.exists():
            click.echo(
                click.style(
                    f"Error: Parquet file not found: {spans_file}\n"
                    f"Run 'dal export-parquet --source {source_name}' first.",
                    fg="red",
                )
            )
            raise SystemExit(1)

        click.echo(f"Loading from Parquet: {spans_file}")

        import duckdb

        conn = duckdb.connect()

        if session_id:
            # Load specific session
            query = f"""
                SELECT * FROM '{spans_file}'
                WHERE session_id = ?
                ORDER BY start_time
            """
            df = conn.execute(query, [session_id]).fetchdf()
            if df.empty:
                click.echo(click.style(f"Session not found: {session_id}", fg="red"))
                raise SystemExit(1)

            sessions_data = [{
                "session_id": session_id,
                "spans": df.to_dict(orient="records"),
            }]
        else:
            # Load all spans at once (much faster than per-session queries)
            click.echo("Loading all spans...")
            all_spans_df = conn.execute(f"""
                SELECT * FROM '{spans_file}'
                ORDER BY session_id, start_time
            """).fetchdf()
            click.echo(f"Loaded {len(all_spans_df):,} spans")

            # Group by session in Python
            click.echo("Grouping by session...")
            sessions_data = []
            for sid, group in all_spans_df.groupby("session_id", sort=False):
                sessions_data.append({
                    "session_id": sid,
                    "spans": group.to_dict(orient="records"),
                })
            click.echo(f"Found {len(sessions_data):,} sessions")
    else:
        # Load from JSONL
        unified_dir = Path(storage_path) / "unified"
        jsonl_file = unified_dir / f"{source_name}_sessions.jsonl"

        if not jsonl_file.exists():
            click.echo(
                click.style(
                    f"Error: JSONL file not found: {jsonl_file}\n"
                    f"Run 'dal export-sessions --source {source_name}' first.",
                    fg="red",
                )
            )
            raise SystemExit(1)

        click.echo(f"Loading from JSONL: {jsonl_file}")

        sessions_data = []
        with open(jsonl_file) as f:
            for line in f:
                if line.strip():
                    try:
                        session = json.loads(line)
                        if session_id and session.get("session_id") != session_id:
                            continue
                        sessions_data.append(session)
                    except json.JSONDecodeError:
                        continue

        if session_id and not sessions_data:
            click.echo(click.style(f"Session not found: {session_id}", fg="red"))
            raise SystemExit(1)

        click.echo(f"Loaded {len(sessions_data):,} sessions")

    # Classify sessions
    click.echo("Classifying spans by conversation thread...")

    if output_format == "summary":
        # Summary output
        results = []
        total_stats = {"main_thread": 0, "ancillary": 0, "sub_agent": 0, "unknown": 0}

        for session in sessions_data:
            summary = get_thread_summary(session)
            results.append({
                "session_id": session.get("session_id"),
                "span_count": len(session.get("spans", [])),
                "thread_counts": summary,
            })
            for k, v in summary.items():
                total_stats[k] += v

        click.echo()
        click.echo(click.style("Classification Summary", fg="cyan", bold=True))
        click.echo(f"  Sessions:     {len(results):,}")
        click.echo(f"  Main thread:  {total_stats['main_thread']:,} spans")
        click.echo(f"  Ancillary:    {total_stats['ancillary']:,} spans")
        click.echo(f"  Sub-agents:   {total_stats['sub_agent']:,} spans")
        click.echo(f"  Unknown:      {total_stats['unknown']:,} spans")

        if output_path:
            with open(output_path, "w") as f:
                json.dump({"sessions": results, "totals": total_stats}, f, indent=2)
            click.echo(f"\nWritten to: {output_path}")
        else:
            click.echo()
            # Show top 5 sessions by span count
            results.sort(key=lambda x: x["span_count"], reverse=True)
            click.echo("Top 5 sessions by span count:")
            for r in results[:5]:
                tc = r["thread_counts"]
                click.echo(
                    f"  {r['session_id'][:40]}... "
                    f"({r['span_count']:,} spans: "
                    f"main={tc['main_thread']}, "
                    f"ancillary={tc['ancillary']}, "
                    f"sub_agent={tc['sub_agent']})"
                )
    else:
        # Full JSON output with classified spans
        classified_sessions = []
        for session in sessions_data:
            classified = classify_session_threads(session)
            classified_sessions.append(classified.to_dict())

        output_data = {
            "source": source_name,
            "session_count": len(classified_sessions),
            "sessions": classified_sessions,
        }

        if output_path:
            with open(output_path, "w") as f:
                json.dump(output_data, f, indent=2, default=str)
            click.echo(f"Written to: {output_path}")
        else:
            click.echo(json.dumps(output_data, indent=2, default=str))


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


def _load_sessions_from_parquet(source_name: str) -> list[dict] | None:
    """Load sessions from parquet file with raw_attributes_json for chain detection."""
    import pandas as pd
    from dev_agent_lens.storage import get_storage_path

    storage_path = get_storage_path()
    parquet_file = storage_path / "parquet" / f"{source_name}_spans.parquet"

    if not parquet_file.exists():
        return None

    df = pd.read_parquet(parquet_file)

    # Group spans by session_id
    sessions = []
    for session_id, group in df.groupby("session_id"):
        spans = group.to_dict("records")
        sessions.append({"session_id": session_id, "spans": spans})

    return sessions


@main.command("chain-list")
@click.option(
    "--source",
    "source_name",
    type=str,
    required=True,
    help="Source name to list chains from",
)
@click.option(
    "--min-sessions",
    type=int,
    default=1,
    help="Minimum sessions per chain to display (default: 1). Note: when sessions are "
         "already unified by Claude UUID, each conversation appears as a single session.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format (default: table)",
)
def chain_list_cmd(source_name: str, min_sessions: int, output_format: str) -> None:
    """List conversation chains (linked sessions across compactions).

    Shows chains of sessions that are linked by compaction events,
    representing conversations that have been continued across
    context window limits.

    Example:
        dal chain-list --source phoenix-local-alex
        dal chain-list --source phoenix-local-alex --min-sessions 5
    """
    import json as json_module

    # Load sessions from parquet (has raw_attributes_json for compaction detection)
    sessions = _load_sessions_from_parquet(source_name)
    if sessions is None:
        click.echo(
            click.style(
                f"No parquet data found for '{source_name}'. Run 'dal sync --source {source_name}' first.",
                fg="red",
            )
        )
        return

    click.echo(f"Loaded {len(sessions)} sessions from parquet...")

    click.echo(f"Building chains from {len(sessions)} sessions...")

    # Build chains
    chains = build_conversation_chains(sessions)

    # Filter by min sessions
    chains = [c for c in chains if c.session_count >= min_sessions]

    if not chains:
        click.echo(
            click.style(
                f"No chains with {min_sessions}+ sessions found.",
                fg="yellow",
            )
        )
        return

    # Sort by session count descending
    chains.sort(key=lambda c: c.session_count, reverse=True)

    if output_format == "json":
        output = []
        for chain in chains:
            duration_hours = chain.duration_minutes / 60.0 if chain.duration_minutes else 0.0
            output.append({
                "chain_id": chain.chain_id,
                "session_count": chain.session_count,
                "compaction_count": chain.compaction_count,
                "total_spans": chain.total_spans,
                "start_time": chain.start_time.isoformat() if chain.start_time else None,
                "end_time": chain.end_time.isoformat() if chain.end_time else None,
                "duration_hours": round(duration_hours, 2),
                "session_ids": chain.session_ids,
            })
        click.echo(json_module.dumps(output, indent=2))
    else:
        # Table format
        click.echo()
        click.echo(
            click.style(
                f"Found {len(chains)} conversation chains",
                fg="cyan",
                bold=True,
            )
        )
        click.echo()

        # Header
        click.echo(
            f"{'Chain ID':<36}  {'Sessions':>8}  {'Compactions':>11}  {'Spans':>8}  {'Duration':>10}"
        )
        click.echo("-" * 85)

        for chain in chains:
            duration_hours = chain.duration_minutes / 60.0 if chain.duration_minutes else 0.0
            duration_str = f"{duration_hours:.1f}h" if duration_hours else "N/A"
            click.echo(
                f"{chain.chain_id:<36}  {chain.session_count:>8}  "
                f"{chain.compaction_count:>11}  {chain.total_spans:>8}  {duration_str:>10}"
            )

        click.echo()
        click.echo(
            f"Total: {sum(c.session_count for c in chains)} sessions "
            f"across {len(chains)} chains"
        )


@main.command("chain-export")
@click.option(
    "--source",
    "source_name",
    type=str,
    required=True,
    help="Source name to export from",
)
@click.option(
    "--chain-id",
    type=str,
    help="Specific chain ID to export (from 'dal chain-list')",
)
@click.option(
    "--index",
    type=int,
    help="Chain index to export (0 = longest chain)",
)
@click.option(
    "--output",
    "-o",
    "output_file",
    type=click.Path(),
    help="Output file path (default: auto-named file)",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["markdown", "jsonl", "json"]),
    default="markdown",
    help="Output format: markdown (LLM-friendly), jsonl (canonical, streamable), json (single file)",
)
@click.option(
    "--include-tool-calls/--no-tool-calls",
    default=True,
    help="Include tool call details in markdown (default: True)",
)
@click.option(
    "--no-raw-attributes",
    is_flag=True,
    default=False,
    help="Exclude raw_attributes_json from JSON output (smaller files)",
)
@click.option(
    "--include-ancillary",
    is_flag=True,
    default=False,
    help="Include ancillary turns (tool results, system reminders). Default: main thread only.",
)
def chain_export_cmd(
    source_name: str,
    chain_id: str | None,
    index: int | None,
    output_file: str | None,
    output_format: str,
    include_tool_calls: bool,
    no_raw_attributes: bool,
    include_ancillary: bool,
) -> None:
    """Export a conversation chain to markdown, JSONL, or JSON.

    Exports a linked conversation chain (sessions connected by compactions)
    to:
    - markdown: LLM-friendly, readable format for analysis
    - jsonl: Canonical, streamable format (one JSON object per line) - RECOMMENDED
    - json: Single JSON file with all data

    Example:
        dal chain-export --source phoenix-local-alex --index 0
        dal chain-export --source phoenix-local-alex --index 0 --format jsonl
        dal chain-export --source phoenix-local-alex --chain-id abc123 -o conversation.md
    """
    from pathlib import Path

    # Load sessions from parquet (has raw_attributes_json for compaction detection)
    sessions = _load_sessions_from_parquet(source_name)
    if sessions is None:
        click.echo(
            click.style(
                f"No parquet data found for '{source_name}'. Run 'dal sync --source {source_name}' first.",
                fg="red",
            )
        )
        return

    click.echo(f"Loaded {len(sessions)} sessions from parquet...")
    click.echo(f"Building chains from {len(sessions)} sessions...")

    # Build chains
    chains = build_conversation_chains(sessions)

    # Sort by session count descending (single-session chains are now included
    # since sessions may already be unified by Claude UUID)
    chains.sort(key=lambda c: (c.session_count, c.total_spans), reverse=True)

    if not chains:
        click.echo(
            click.style("No chains found.", fg="yellow")
        )
        return

    # Find the target chain
    target_chain = None
    if chain_id:
        for chain in chains:
            if chain.chain_id == chain_id:
                target_chain = chain
                break
        if not target_chain:
            click.echo(
                click.style(f"Chain '{chain_id}' not found.", fg="red")
            )
            return
    elif index is not None:
        if index < 0 or index >= len(chains):
            click.echo(
                click.style(
                    f"Index {index} out of range. Found {len(chains)} chains.",
                    fg="red",
                )
            )
            return
        target_chain = chains[index]
    else:
        # Default to longest chain
        target_chain = chains[0]
        click.echo(f"Using longest chain (index 0, {target_chain.session_count} sessions)")

    click.echo(
        f"Exporting chain {target_chain.chain_id} "
        f"({target_chain.session_count} sessions, {target_chain.compaction_count} compactions)..."
    )

    # Export based on format
    if output_format == "jsonl":
        from dev_agent_lens.analysis.chains import export_chain_to_jsonl
        import json as json_module

        # Use canonical event-based JSONL format
        records = export_chain_to_jsonl(
            target_chain,
            sessions,
        )
        # JSONL: one JSON object per line
        content = "\n".join(json_module.dumps(record, default=str) for record in records)
        ext = ".jsonl"
        event_count = sum(1 for r in records if r.get("record_type") == "event")
        click.echo(f"Exported {event_count} events as JSONL (event-based format, {len(records)} total records)")
    elif output_format == "json":
        from dev_agent_lens.analysis.chains import export_chain_to_json
        import json as json_module

        json_data = export_chain_to_json(
            target_chain,
            sessions,
            include_raw_attributes=not no_raw_attributes,
            include_ancillary=include_ancillary,
        )
        content = json_module.dumps(json_data, indent=2, default=str)
        ext = ".json"
        mode = "with ancillary" if include_ancillary else "main thread only"
        click.echo(f"Exported {json_data.get('turn_count', 0)} turns as JSON ({mode})")
    else:
        # Markdown format - use unified export pipeline per AGREED_FORMAT.md
        from dev_agent_lens.export.markdown_litellm import (
            export_chain_to_unified_markdown,
            export_to_files,
        )

        ext = ".md"
        main_file = output_file if output_file else f"chain_{target_chain.chain_id[:8]}{ext}"
        output_dir = Path(main_file).parent if Path(main_file).parent.exists() else Path(".")

        # Export using the unified JSONL->Markdown pipeline
        export_result = export_chain_to_unified_markdown(
            target_chain,
            sessions,
        )

        # Write files to disk
        written_files = export_to_files(
            export_result,
            output_dir,
            main_filename=Path(main_file).name,
        )

        # Report main file
        main_content_size = written_files[0].stat().st_size
        click.echo(
            click.style(
                f"Exported to {written_files[0]} ({main_content_size:,} bytes)",
                fg="green",
                bold=True,
            )
        )

        # Report stats
        stats = export_result.stats
        click.echo(
            f"  {stats.get('user_turns', 0)} user turns, "
            f"{stats.get('assistant_turns', 0)} assistant turns, "
            f"{stats.get('tool_calls', 0)} tool calls, "
            f"{stats.get('subagents', 0)} subagents"
        )

        # Report subagent files if any
        if len(written_files) > 1:
            subagent_count = len(written_files) - 1
            click.echo(f"  + {subagent_count} additional file(s):")
            for extra_file in written_files[1:]:
                extra_size = extra_file.stat().st_size
                click.echo(f"    - {extra_file.name} ({extra_size:,} bytes)")

        return  # Already wrote files

    # For JSONL and JSON formats
    if output_file:
        Path(output_file).write_text(content)
        click.echo(
            click.style(
                f"Exported to {output_file} ({len(content):,} characters)",
                fg="green",
                bold=True,
            )
        )
    else:
        # Generate auto filename
        auto_file = f"chain_{target_chain.chain_id[:8]}{ext}"
        Path(auto_file).write_text(content)
        click.echo(
            click.style(
                f"Exported to {auto_file} ({len(content):,} characters)",
                fg="green",
                bold=True,
            )
        )


@main.command("claude-session-logs-to-markdown")
@click.argument("session_file", type=click.Path(exists=True))
@click.option(
    "--output-dir",
    "-o",
    type=click.Path(),
    default=".",
    help="Output directory for markdown files (default: current directory)",
)
def session_to_markdown(
    session_file: str,
    output_dir: str,
) -> None:
    """Export a Claude Code session JSONL file to readable markdown.

    Converts raw ~/.claude/projects/**/*.jsonl session files into LLM-friendly
    markdown format with flat structure and reference-based subagent handling.

    Examples:

        dal claude-session-logs-to-markdown ~/.claude/projects/-Users-me-myproject/abc123.jsonl

        dal claude-session-logs-to-markdown session.jsonl -o ./exports/
    """
    from dev_agent_lens.export.markdown import (
        export_session_to_markdown,
        export_to_files,
    )

    click.echo(f"Exporting: {session_file}")

    try:
        export = export_session_to_markdown(session_file)

        files = export_to_files(export, output_dir)

        click.echo(
            click.style(
                f"Exported {len(files)} file(s) to {output_dir}",
                fg="green",
                bold=True,
            )
        )

        for f in files:
            size = f.stat().st_size
            click.echo(f"  - {f.name} ({size:,} bytes)")

        click.echo()
        click.echo(click.style("Stats:", bold=True))
        for key, value in export.stats.items():
            click.echo(f"  {key}: {value}")

    except Exception as e:
        click.echo(click.style(f"Error: {e}", fg="red"))
        raise


# =============================================================================
# Run command group - Testing infrastructure
# =============================================================================


@main.group()
def run() -> None:
    """Run tests and utilities.

    Subcommands for testing and automation:

        dal run testbed    Run end-to-end pipeline test
    """
    pass


@run.command("testbed")
@click.option(
    "--backend",
    type=click.Choice(["arize", "phoenix"]),
    default="phoenix",
    help="Observability backend to test against",
)
@click.option(
    "--stop-after",
    is_flag=True,
    help="Stop test container after run (default: keep running)",
)
@click.option(
    "--run-id",
    default=None,
    help="Specific run ID (default: auto-generated)",
)
@click.option(
    "--prompt",
    default="stress_test.txt",
    help="Prompt file to use from testbed/prompts/ (default: stress_test.txt)",
)
@click.option(
    "--cleanup",
    is_flag=True,
    help="Remove run directory after test completes",
)
def run_testbed(
    backend: str, stop_after: bool, run_id: str | None, prompt: str, cleanup: bool
):
    """Run end-to-end pipeline test.

    Executes Claude Code in an isolated test environment and validates
    that traces flow correctly through the LiteLLM -> observability pipeline.

    This command:
    1. Starts the test LiteLLM container (if not running)
    2. Creates an isolated run directory in tests/e2e/testbed/runs/
    3. Runs Claude Code with --print mode against a stress test prompt
    4. Syncs traces from the observability backend
    5. Validates expected spans exist

    Examples:
        dal run testbed                    # Test against Phoenix
        dal run testbed --backend arize    # Test against Arize AX
        dal run testbed --stop-after       # Stop container when done
    """
    import asyncio

    from dev_agent_lens.testing import TestBackend, TestConfig, TestOrchestrator

    config = TestConfig(
        backend=TestBackend(backend),
        stop_container_after=stop_after,
        prompt_file=prompt,
    )
    if run_id:
        config.test_run_id = run_id

    click.echo(click.style("Pipeline Test", bold=True))
    click.echo(f"  Run ID:  {config.test_run_id}")
    click.echo(f"  Backend: {backend}")
    click.echo(f"  Project: dal-test-{config.test_run_id}")
    click.echo(f"  Prompt:  {prompt}")
    click.echo()

    orchestrator = TestOrchestrator(config)

    click.echo("Starting test container...")
    try:
        orchestrator.container.start()
        click.echo(
            click.style(
                f"  Container ready at {orchestrator.container.get_proxy_url()}", fg="green"
            )
        )
    except Exception as e:
        click.echo(click.style(f"  Failed to start container: {e}", fg="red"))
        raise SystemExit(1)

    click.echo()
    click.echo("Running Claude Code...")
    result = asyncio.run(orchestrator.run())

    click.echo()

    # Output results
    if result.error:
        click.echo(click.style(f"Error: {result.error}", fg="red"))
        raise SystemExit(1)

    click.echo(f"Spans captured: {result.span_count}")
    click.echo()
    click.echo(click.style("Assertions:", bold=True))
    for name, passed in result.assertions.items():
        status = click.style("PASS", fg="green") if passed else click.style("FAIL", fg="red")
        click.echo(f"  [{status}] {name}")

    click.echo()
    if result.run_dir:
        click.echo(f"Run directory: {result.run_dir}")

    if cleanup and result.run_dir:
        orchestrator.cleanup_run_dir()
        click.echo("Run directory cleaned up.")

    click.echo()
    if result.passed:
        click.echo(click.style("Test PASSED", fg="green", bold=True))
    else:
        click.echo(click.style("Test FAILED", fg="red", bold=True))
        raise SystemExit(1)


@run.command("cleanup")
@click.option(
    "--stale",
    "stale_hours",
    type=int,
    default=None,
    help="Delete test data older than N hours (e.g., --stale 24)",
)
@click.option(
    "--all",
    "delete_all",
    is_flag=True,
    help="Delete ALL test data (requires confirmation)",
)
@click.option(
    "--list",
    "list_only",
    is_flag=True,
    help="List test data without deleting",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip confirmation prompts (use with caution)",
)
@click.option(
    "--phoenix-only",
    is_flag=True,
    help="Only clean Phoenix projects (skip Claude sessions)",
)
@click.option(
    "--sessions-only",
    is_flag=True,
    help="Only clean Claude sessions (skip Phoenix projects)",
)
@click.option(
    "--phoenix-url",
    default="http://localhost:6006",
    help="Phoenix server URL (default: http://localhost:6006)",
)
def run_cleanup(
    stale_hours: int | None,
    delete_all: bool,
    list_only: bool,
    yes: bool,
    phoenix_only: bool,
    sessions_only: bool,
    phoenix_url: str,
):
    """Clean up test data from Phoenix and Claude sessions.

    Safely removes test data created by the testbed runner:

    \b
    Phoenix projects:
      - Only 'dal-test-*' projects can be deleted
      - Protected: dev-agent-lens, default (NEVER deleted)

    \b
    Claude sessions:
      - Only sessions from testbed runs can be deleted
      - Safety based on path structure (must contain testbed path)
      - Normal user sessions are NEVER touched

    \b
    Examples:
        dal run cleanup --list          List all test data
        dal run cleanup --stale 24      Delete data older than 24h
        dal run cleanup --all           Delete all test data (confirm)
        dal run cleanup --all -y        Delete all without confirmation
        dal run cleanup --phoenix-only  Only clean Phoenix projects
        dal run cleanup --sessions-only Only clean Claude sessions
    """
    from dev_agent_lens.testing import ClaudeSessionCleaner, PhoenixProjectCleaner

    # Validate options
    if phoenix_only and sessions_only:
        click.echo(click.style("Error: Cannot use both --phoenix-only and --sessions-only", fg="red"))
        raise SystemExit(1)

    clean_phoenix = not sessions_only
    clean_sessions = not phoenix_only

    phoenix_stats = None
    session_stats = None

    # Get Phoenix stats
    if clean_phoenix:
        phoenix_cleaner = PhoenixProjectCleaner(phoenix_url=phoenix_url)
        try:
            phoenix_stats = phoenix_cleaner.get_stats()
        except RuntimeError as e:
            click.echo(click.style(f"Phoenix error: {e}", fg="yellow"))
            click.echo("Phoenix cleanup will be skipped.")
            clean_phoenix = False

    # Get Claude session stats
    if clean_sessions:
        session_cleaner = ClaudeSessionCleaner()
        session_stats = session_cleaner.get_stats()

    # Display stats
    click.echo(click.style("Test Data Cleanup", bold=True))
    click.echo()

    if phoenix_stats:
        click.echo(click.style("Phoenix Projects:", bold=True))
        click.echo(f"  URL: {phoenix_url}")
        click.echo(f"  Test projects (dal-test-*): {phoenix_stats['test_projects']}")
        click.echo(f"  Protected: {', '.join(phoenix_stats['protected_names'])}")
        click.echo()

    if session_stats:
        click.echo(click.style("Claude Sessions:", bold=True))
        click.echo(f"  Testbed sessions: {session_stats['testbed_sessions']}")
        click.echo(f"  Size: {session_stats['testbed_size_mb']} MB")
        click.echo(f"  Pattern: *tests-e2e-testbed-runs-run-*")
        click.echo()

    # List mode
    if list_only:
        if clean_phoenix and phoenix_stats and phoenix_stats["test_project_names"]:
            click.echo(click.style("Phoenix test projects:", bold=True))
            test_projects = phoenix_cleaner.list_test_projects()
            for p in sorted(test_projects, key=lambda x: x.created_at or datetime.min):
                age = ""
                if p.created_at:
                    age_td = datetime.now(p.created_at.tzinfo or None) - p.created_at
                    hours = int(age_td.total_seconds() / 3600)
                    age = f" ({hours}h ago)"
                spans = f", {p.span_count} spans" if p.span_count else ""
                click.echo(f"  - {p.name}{age}{spans}")
            click.echo()

        if clean_sessions and session_stats and session_stats["testbed_sessions"] > 0:
            click.echo(click.style("Claude testbed sessions:", bold=True))
            testbed_sessions = session_cleaner.list_testbed_sessions()
            for s in sorted(testbed_sessions, key=lambda x: x.modified_at or datetime.min):
                age = ""
                if s.modified_at:
                    age_td = datetime.now(s.modified_at.tzinfo) - s.modified_at
                    hours = int(age_td.total_seconds() / 3600)
                    age = f" ({hours}h ago)"
                # Extract just the run ID from the long path
                run_id = s.name.split("run-")[-1] if "run-" in s.name else s.name[-20:]
                click.echo(f"  - run-{run_id}{age}")
            click.echo()

        if (not phoenix_stats or not phoenix_stats["test_project_names"]) and \
           (not session_stats or session_stats["testbed_sessions"] == 0):
            click.echo("No test data found.")
        return

    # Must specify --stale or --all
    if stale_hours is None and not delete_all:
        click.echo("Specify --stale, --all, or --list. Use --help for options.")
        raise SystemExit(1)

    # Stale cleanup
    if stale_hours is not None:
        if stale_hours <= 0:
            click.echo(click.style("Error: --stale value must be positive", fg="red"))
            raise SystemExit(1)

        click.echo(f"Deleting test data older than {stale_hours} hours...")
        click.echo()

        total_deleted = 0

        if clean_phoenix and phoenix_stats:
            deleted = phoenix_cleaner.cleanup_stale(hours=stale_hours)
            if deleted:
                click.echo(click.style(f"Deleted {len(deleted)} Phoenix project(s):", fg="green"))
                for name in deleted:
                    click.echo(f"  - {name}")
                total_deleted += len(deleted)
            else:
                click.echo("No stale Phoenix projects found.")
            click.echo()

        if clean_sessions and session_stats:
            deleted = session_cleaner.cleanup_stale(hours=stale_hours)
            if deleted:
                click.echo(click.style(f"Deleted {len(deleted)} Claude session(s):", fg="green"))
                for name in deleted:
                    run_id = name.split("run-")[-1] if "run-" in name else name[-20:]
                    click.echo(f"  - run-{run_id}")
                total_deleted += len(deleted)
            else:
                click.echo("No stale Claude sessions found.")

        if total_deleted == 0:
            click.echo()
            click.echo("No stale test data found.")
        return

    # Delete all
    if delete_all:
        items_to_delete = []

        if clean_phoenix and phoenix_stats:
            test_projects = phoenix_cleaner.list_test_projects()
            for p in test_projects:
                items_to_delete.append(("phoenix", p.name, p))

        if clean_sessions and session_stats:
            testbed_sessions = session_cleaner.list_testbed_sessions()
            for s in testbed_sessions:
                items_to_delete.append(("session", s.name, s))

        if not items_to_delete:
            click.echo("No test data to delete.")
            return

        click.echo(click.style(f"About to delete {len(items_to_delete)} item(s):", fg="yellow"))
        click.echo()

        phoenix_items = [i for i in items_to_delete if i[0] == "phoenix"]
        session_items = [i for i in items_to_delete if i[0] == "session"]

        if phoenix_items:
            click.echo(f"  Phoenix projects ({len(phoenix_items)}):")
            for _, name, _ in phoenix_items:
                click.echo(f"    - {name}")

        if session_items:
            click.echo(f"  Claude sessions ({len(session_items)}):")
            for _, name, _ in session_items:
                run_id = name.split("run-")[-1] if "run-" in name else name[-20:]
                click.echo(f"    - run-{run_id}")

        click.echo()

        if not yes:
            confirmed = click.confirm(
                click.style("This cannot be undone. Continue?", fg="yellow"),
                default=False,
            )
            if not confirmed:
                click.echo("Aborted.")
                return

        total_deleted = 0

        if clean_phoenix and phoenix_stats:
            deleted = phoenix_cleaner.cleanup_all(confirm=False)
            total_deleted += len(deleted)

        if clean_sessions and session_stats:
            deleted = session_cleaner.cleanup_all()
            total_deleted += len(deleted)

        click.echo()
        click.echo(click.style(f"Deleted {total_deleted} item(s)", fg="green"))


if __name__ == "__main__":
    main()
