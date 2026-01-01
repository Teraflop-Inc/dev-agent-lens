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

import os
import time
from datetime import datetime
from typing import Any

import click

from dev_agent_lens.clients.arize import ArizeClient
from dev_agent_lens.clients.phoenix import PhoenixClient
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
    "--skip-annotations",
    is_flag=True,
    help="Skip fetching annotations (faster sync)",
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
def sync(
    full: bool,
    source_name: str | None,
    backend: str | None,
    all_sources: bool,
    push: bool,
    skip_annotations: bool,
    limit: int,
    days: int,
    batch_days: int | None,
    timeout: int,
) -> None:
    """Sync trace data from configured sources or backends.

    By default, performs incremental sync using saved state.
    Use --full to ignore state and fetch all available data.

    Examples:

        dal sync                        # Sync from configured sources/backends

        dal sync --source phoenix-alex  # Sync from named source

        dal sync --all-sources          # Sync from all configured sources

        dal sync --full                 # Full sync, ignore state

        dal sync --backend arize        # [Deprecated] Use --source instead

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
    if skip_annotations:
        click.echo("Annotations: skipped")
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
        end_time = datetime.now()
        if full:
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

        # Fetch spans in batches
        all_spans = []
        for i, (batch_start, batch_end) in enumerate(batches):
            if batch_days:
                click.echo(f"  Batch {i+1}/{len(batches)}: {batch_start.date()} to {batch_end.date()}")

            click.echo(f"  Fetching spans (limit {limit})...")
            batch_df = client.get_spans_dataframe(
                start_time=batch_start,
                end_time=batch_end,
                limit=limit,
            )
            if not batch_df.empty:
                all_spans.append(batch_df)
                click.echo(f"    Got {len(batch_df)} spans")
            else:
                click.echo(f"    No spans in this batch")

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

        click.echo(f"  Fetched {len(spans_df)} spans")

        # Normalize spans
        click.echo("  Normalizing...")
        normalized = normalizer(spans_df)

        # Store raw data
        click.echo("  Storing raw data...")
        raw_file = source_store.append_spans(normalized, backend=source_or_backend_id)
        click.echo(f"  Saved to: {raw_file.name}")

        # Fetch annotations for Phoenix
        if not skip_annotations and is_phoenix:
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

        # Update state
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
    "--status",
    "show_status",
    is_flag=True,
    help="Show status of in-progress historical syncs without syncing",
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
    show_status: bool,
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

    Examples:

        dal sync-historical --source arize-ax-alex      # Sync everything (simplest)

        dal sync-historical --source arize-ax-alex --status  # Check progress

        dal sync-historical --source arize-ax-alex --days 30  # Last 30 days only

        dal sync-historical --source arize-ax-alex --reset  # Start over

        dal sync-historical --start-date 2025-11-01     # From specific date
    """
    from datetime import timedelta
    import pandas as pd
    from dev_agent_lens.core.sources import SourceConfig, SourceManager, SourceType
    from dev_agent_lens.core.historical_sync import (
        HistoricalSyncState,
        SyncConfig,
        list_historical_syncs,
        clear_historical_sync,
    )

    # Handle --status flag: show status and exit
    if show_status:
        syncs = list_historical_syncs()
        if not syncs:
            click.echo("No historical syncs in progress.")
            return

        click.echo(click.style("Historical Sync Status", bold=True))
        click.echo()
        for sync_state in syncs:
            progress = sync_state.progress_percent
            eta = sync_state.get_eta()
            eta_str = f" (ETA: {eta})" if eta else ""

            if sync_state.is_complete:
                status = click.style("complete", fg="green")
            elif sync_state.current_batch:
                status = click.style("in progress", fg="yellow")
            else:
                status = click.style("paused", fg="cyan")

            click.echo(f"  {sync_state.source}: {progress:.1f}% {status}{eta_str}")
            click.echo(f"    Range: {sync_state.target_start.strftime('%Y-%m-%d')} to {sync_state.target_end.strftime('%Y-%m-%d')}")
            click.echo(f"    Spans: {sync_state.stats.total_spans:,}")
            click.echo(f"    Batches: {sync_state.stats.batches_completed} completed, {sync_state.stats.batches_failed} failed")
            if sync_state.stats.subdivisions > 0:
                click.echo(f"    Subdivisions: {sync_state.stats.subdivisions}")
            click.echo()
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
            sync_end_time = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
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

    # Helper function to fetch with auto-subdivision
    def fetch_with_subdivision(
        client,
        batch_start: datetime,
        batch_end: datetime,
        depth: int = 0,
        is_first_request: bool = True,
    ) -> tuple[list, int]:
        """Fetch spans, auto-subdividing if limit is hit.

        Returns (list of dataframes, subdivision_count)
        """
        nonlocal total_subdivisions

        indent = "    " * depth if depth > 0 else ""

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
            return [], 0

        if hasattr(batch_df, "empty") and batch_df.empty:
            return [], 0

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
                left_dfs, left_subs = fetch_with_subdivision(client, batch_start, midpoint, depth + 1, is_first_request=False)
                right_dfs, right_subs = fetch_with_subdivision(client, midpoint, batch_end, depth + 1, is_first_request=False)

                return left_dfs + right_dfs, left_subs + right_subs + 1
            else:
                # Can't subdivide further, warn user
                if depth == 0:
                    click.echo(click.style(f" {batch_count:,} spans (at limit, window too small to subdivide)", fg="yellow"))
                else:
                    click.echo(f"{indent}→ {batch_start.strftime('%H:%M')}-{batch_end.strftime('%H:%M')}: " +
                              click.style(f"{batch_count:,} spans (at limit)", fg="yellow"))
                return [batch_df], 0

        # Normal case: didn't hit limit
        if depth > 0:
            click.echo(f"{indent}→ {batch_start.strftime('%H:%M')}-{batch_end.strftime('%H:%M')}: " +
                      click.style(f"{batch_count:,} spans", fg="green"))
        return [batch_df], 0

    # Helper function to process a single source/backend
    def process_historical_sync(
        name: str,
        display_name: str,
        client_class: type,
        normalizer: callable,
        store: OxenStore,
        is_phoenix: bool = False,
    ) -> tuple[int, int, int]:
        """Process historical sync and return (spans, completed, failed)."""

        # Load or create checkpoint state
        checkpoint_state, is_resuming = HistoricalSyncState.load_or_create(
            source=name,
            target_start=sync_start_time,
            target_end=sync_end_time,
            config=sync_config,
        )

        if is_resuming and resume:
            progress = checkpoint_state.progress_percent
            click.echo(f"[{display_name}] Resuming from checkpoint ({progress:.1f}% complete)")
            click.echo(f"  Previously synced: {checkpoint_state.stats.total_spans:,} spans")
        else:
            click.echo(f"[{display_name}] Starting historical sync...")

        # Create client with timeout
        try:
            if is_phoenix:
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

        if not batches:
            click.echo(click.style(f"  No batches to process.", fg="yellow"))
            return checkpoint_state.stats.total_spans, checkpoint_state.stats.batches_completed, 0

        click.echo(f"  Processing {len(batches)} batches sequentially...")

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

            click.echo(
                f"  [{overall_progress:.0f}%] Batch {batch_num}/{total}: "
                f"{batch_start.strftime(time_format)} to {batch_end.strftime(time_format)}{retry_tag}",
                nl=False,
            )

            try:
                # Mark batch as started in checkpoint
                checkpoint_state.mark_batch_started(batch_start, batch_end)

                # Fetch with auto-subdivision
                dataframes, subdivisions = fetch_with_subdivision(client, batch_start, batch_end)

                if not dataframes:
                    click.echo(click.style(" (no data)", fg="yellow"))
                    # Mark as completed even if no data (we've processed this range)
                    checkpoint_state.mark_batch_completed(batch_start, batch_end, 0)
                    completed += 1
                    return True

                # Combine all dataframes from this batch (including subdivisions)
                if len(dataframes) == 1:
                    combined_df = dataframes[0]
                else:
                    combined_df = pd.concat(dataframes, ignore_index=True)

                batch_count = len(combined_df)
                source_spans += batch_count

                # Save results
                if skip_normalize:
                    store.append_spans(combined_df, backend=f"{name}-historical")
                else:
                    try:
                        normalized = normalizer(combined_df)
                        store.append_spans(normalized, backend=f"{name}-historical")
                    except Exception as e:
                        click.echo(
                            click.style(f" WARN: normalize failed, saving raw - {e}", fg="yellow")
                        )
                        store.append_spans(combined_df, backend=f"{name}-historical-raw")

                # Show result (if we didn't subdivide, show count here)
                if subdivisions == 0:
                    click.echo(click.style(f" {batch_count:,} spans", fg="green"))
                else:
                    click.echo(f"    Total: " + click.style(f"{batch_count:,} spans", fg="green") +
                              f" (from {subdivisions + 1} sub-batches)")

                # Mark batch as completed in checkpoint
                checkpoint_state.mark_batch_completed(batch_start, batch_end, batch_count)
                if subdivisions > 0:
                    for _ in range(subdivisions):
                        checkpoint_state.add_subdivision()

                completed += 1
                return True

            except KeyboardInterrupt:
                raise  # Re-raise to handle at outer level

            except Exception as e:
                error_str = str(e).lower()
                if "rate" in error_str or "limit" in error_str or "exhausted" in error_str:
                    click.echo(click.style(f" RATE LIMITED - will retry later", fg="yellow"))
                else:
                    click.echo(click.style(f" FAILED: {e}", fg="red"))
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
        if source.source_type == SourceType.PHOENIX:
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

        spans, completed, failed = process_historical_sync(
            source.name,
            source.get_display_info(),
            client_class,
            normalizer,
            source_store,
            is_phoenix=is_phoenix,
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
def config_add_source(
    name: str,
    source_type: str,
    url: str | None,
    project: str | None,
    space_key: str | None,
    model_id: str | None,
    local_only: bool,
) -> None:
    """Add a new named source.

    Examples:

        dal config add-source phoenix-local --type phoenix --url localhost:6006

        dal config add-source arize-team --type arize --space-key ABC --model-id my-model --shared
    """
    from dev_agent_lens.core.sources import SourceConfig, SourceManager, SourceType

    # Create source config
    source = SourceConfig(
        name=name,
        source_type=SourceType(source_type),
        local_only=local_only,
        url=url,
        project=project,
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
def summarize(
    session_id: str,
    model: str | None,
    max_spans: int | None,
    output: str,
    prompt_file: str | None,
    preview: bool,
) -> None:
    """Generate an LLM-powered summary of a session.

    Requires OPENAI_API_KEY to be set in ~/.dal/.env or environment.

    Examples:

        dal summarize abc123              # Summarize session abc123

        dal summarize abc123 --max-spans 50  # Limit to 50 spans

        dal summarize abc123 --preview    # Preview without LLM call

        dal summarize abc123 --output json
    """
    import json as json_lib
    from pathlib import Path

    from dev_agent_lens.query import query
    from dev_agent_lens.llm import (
        NoLLMConfigError,
        check_llm_availability,
        get_summary_preview,
        summarize_session_sync,
    )

    store = OxenStore()

    # Find session
    current_sessions_file = store.sessions_dir / "sessions_current.jsonl"
    if not current_sessions_file.exists():
        session_files = list(store.sessions_dir.glob("sessions_*.jsonl"))
        if session_files:
            current_sessions_file = max(session_files, key=lambda p: p.stat().st_mtime)
        else:
            click.echo(
                click.style("No session data found. Run 'dal sync' first.", fg="yellow")
            )
            return

    # Query for the session
    result = query(session_id=session_id, file_path=current_sessions_file)

    if not result.sessions:
        click.echo(click.style(f"Session '{session_id}' not found.", fg="red"))
        return

    session = result.sessions[0]

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
) -> None:
    """Cluster sessions by behavioral similarity.

    Requires OPENAI_API_KEY for embeddings.

    Examples:

        dal cluster                     # Cluster current sessions

        dal cluster --n-clusters 5      # Force 5 clusters

        dal cluster --limit 50          # Process at most 50 sessions

        dal cluster --sample 20         # Randomly sample 20 sessions

        dal cluster --preview           # Preview without LLM call
    """
    import json as json_lib
    from pathlib import Path

    from dev_agent_lens.query import query
    from dev_agent_lens.llm import (
        NoLLMConfigError,
        check_llm_availability,
        cluster_sessions_sync,
        get_cluster_preview,
    )

    store = OxenStore()

    # Load sessions
    if sessions:
        file_path = Path(sessions)
    else:
        current_sessions_file = store.sessions_dir / "sessions_current.jsonl"
        if not current_sessions_file.exists():
            session_files = list(store.sessions_dir.glob("sessions_*.jsonl"))
            if session_files:
                current_sessions_file = max(session_files, key=lambda p: p.stat().st_mtime)
            else:
                click.echo(
                    click.style("No session data found. Run 'dal sync' first.", fg="yellow")
                )
                return
        file_path = current_sessions_file

    # Query all sessions
    result = query(file_path=file_path)

    if not result.sessions:
        click.echo(click.style("No sessions found.", fg="yellow"))
        return

    sessions_to_process = result.sessions
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
def suggest(
    session_id: str,
    model: str | None,
    max_spans: int | None,
    category: tuple[str, ...],
    output: str,
    prompt_file: str | None,
    preview: bool,
) -> None:
    """Generate improvement suggestions for a session.

    Requires OPENAI_API_KEY for full suggestions.
    Use --preview for heuristic-only suggestions without API calls.

    Examples:

        dal suggest abc123                      # Get suggestions

        dal suggest abc123 --max-spans 50       # Limit to 50 spans

        dal suggest abc123 --preview            # Heuristic only

        dal suggest abc123 --category error     # Focus on errors
    """
    import json as json_lib
    from pathlib import Path

    from dev_agent_lens.query import query
    from dev_agent_lens.llm import (
        NoLLMConfigError,
        check_llm_availability,
        get_suggestion_preview,
        suggest_improvements_sync,
    )

    store = OxenStore()

    # Find session
    current_sessions_file = store.sessions_dir / "sessions_current.jsonl"
    if not current_sessions_file.exists():
        session_files = list(store.sessions_dir.glob("sessions_*.jsonl"))
        if session_files:
            current_sessions_file = max(session_files, key=lambda p: p.stat().st_mtime)
        else:
            click.echo(
                click.style("No session data found. Run 'dal sync' first.", fg="yellow")
            )
            return

    # Query for the session
    result = query(session_id=session_id, file_path=current_sessions_file)

    if not result.sessions:
        click.echo(click.style(f"Session '{session_id}' not found.", fg="red"))
        return

    session = result.sessions[0]

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


@main.command("analyze-tokens")
@click.argument("session_id")
@click.option("--output", type=click.Choice(["text", "json"]), default="text", help="Output format")
def analyze_tokens_cmd(session_id: str, output: str) -> None:
    """Analyze token breakdown for a session.

    Shows tokens by category:
    - Input: tool calls, user messages, system prompts
    - Output: model-generated tokens

    Examples:

        dal analyze-tokens abc123

        dal analyze-tokens abc123 --output json
    """
    from pathlib import Path

    from dev_agent_lens.analysis.tokens import analyze_session_tokens, estimate_cost
    from dev_agent_lens.query import query
    from dev_agent_lens.storage import get_storage_path

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
        click.echo()
        click.echo(click.style(f"Token Analysis: {session_id}", fg="cyan", bold=True))
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


@main.command("analyze-duplicates")
@click.option("--output", type=click.Choice(["text", "json"]), default="text", help="Output format")
@click.option("--min-containment", type=float, default=50.0, help="Minimum containment % to report")
def analyze_duplicates_cmd(output: str, min_containment: float) -> None:
    """Analyze duplicate/subset relationships between sessions.

    Identifies sessions that are fully or partially contained in other sessions.
    These subset sessions could potentially be deleted to save storage.

    Examples:

        dal analyze-duplicates

        dal analyze-duplicates --output json

        dal analyze-duplicates --min-containment 80
    """
    from dev_agent_lens.analysis.subsets import analyze_coverage
    from dev_agent_lens.query import query_sessions

    click.echo("Analyzing sessions for duplicates...")

    # Load sessions using query module which handles session grouping
    sessions = query_sessions()

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
@click.option("--output", type=click.Choice(["text", "json"]), default="text", help="Output format")
def coverage_cmd(output: str) -> None:
    """Show coverage metrics for sessions.

    Reports what percentage of sessions are complete vs partial copies.

    Examples:

        dal coverage

        dal coverage --output json
    """
    from pathlib import Path

    from dev_agent_lens.analysis.subsets import analyze_coverage
    from dev_agent_lens.query import query_sessions

    # Load sessions using query module which handles session grouping
    sessions = query_sessions()

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


if __name__ == "__main__":
    main()
