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
    "--backend",
    type=click.Choice(list(BACKENDS.keys())),
    help="Sync specific backend only",
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
def sync(full: bool, backend: str | None, push: bool, skip_annotations: bool, limit: int, days: int, batch_days: int | None) -> None:
    """Sync trace data from configured backends.

    By default, performs incremental sync using saved state.
    Use --full to ignore state and fetch all available data.

    Examples:

        dal sync                    # Incremental sync from default backend

        dal sync --full             # Full sync, ignore state

        dal sync --backend arize    # Sync only from Arize

        dal sync --push             # Sync and push to Oxen remote
    """
    sync_start = time.time()

    # Determine which backends to sync
    if backend:
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
    else:
        backends_to_sync = get_configured_backends()
        if not backends_to_sync:
            click.echo(
                click.style(
                    "Error: No backends configured. Set DAL_PHOENIX_URL or ARIZE_API_KEY.",
                    fg="red",
                )
            )
            raise SystemExit(1)

    click.echo(f"Syncing from: {', '.join(backends_to_sync)}")
    click.echo(f"Mode: {'full' if full else 'incremental'}")
    click.echo(f"Time range: {days} days")
    if batch_days:
        click.echo(f"Batch size: {batch_days} days per batch")
    if skip_annotations:
        click.echo("Annotations: skipped")
    click.echo()

    # Initialize components
    state = SyncState()
    store = OxenStore()

    total_spans = 0
    total_new_sessions = 0
    total_continued_sessions = 0
    sync_errors = []

    # Import timedelta for time-based batching
    from datetime import timedelta

    # Sync each backend
    for backend_id in backends_to_sync:
        config = BACKENDS[backend_id]
        click.echo(f"[{config['name']}] Starting sync...")

        try:
            # Calculate time range
            end_time = datetime.now()
            if full:
                start_time = end_time - timedelta(days=days)
                click.echo(f"  Full sync: last {days} days")
            else:
                last_sync = state.get_last_sync(backend_id)
                if last_sync:
                    start_time = last_sync
                    click.echo(f"  Incremental from: {last_sync.isoformat()}")
                else:
                    start_time = end_time - timedelta(days=days)
                    click.echo(f"  First sync: last {days} days")

            # Create client
            client_class = config["client_class"]
            client = client_class()

            # Determine batches
            if batch_days:
                # Split into time batches
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
            import pandas as pd
            if all_spans:
                spans_df = pd.concat(all_spans, ignore_index=True)
                # Remove duplicates by span_id if present
                if "context.span_id" in spans_df.columns:
                    spans_df = spans_df.drop_duplicates(subset=["context.span_id"])
            else:
                spans_df = pd.DataFrame()

            if spans_df.empty:
                click.echo(click.style("  No new spans found", fg="yellow"))
                continue

            click.echo(f"  Fetched {len(spans_df)} spans")

            # Normalize spans
            click.echo("  Normalizing...")
            normalized = config["normalizer"](spans_df)

            # Store raw data
            click.echo("  Storing raw data...")
            raw_file = store.append_spans(normalized, backend=backend_id)
            click.echo(f"  Saved to: {raw_file.name}")

            # Fetch annotations for Phoenix (Arize annotations not yet supported)
            annotations_count = 0
            if not skip_annotations and backend_id == "phoenix-local":
                click.echo("  Fetching annotations...")
                try:
                    annotations_df = client.get_span_annotations_dataframe(
                        spans_dataframe=spans_df,
                    )
                    if not annotations_df.empty:
                        # Normalize and store annotations
                        normalized_annotations = normalize_phoenix_annotations(annotations_df)
                        store.append_spans(
                            normalized_annotations,
                            backend=f"{backend_id}-annotations",
                        )
                        annotations_count = len(annotations_df)
                        click.echo(f"  Fetched {annotations_count} annotations")
                    else:
                        click.echo("  No annotations found")
                except Exception as ann_err:
                    click.echo(
                        click.style(f"  Warning: Could not fetch annotations: {ann_err}", fg="yellow")
                    )

            # Get existing sessions file
            current_sessions = store.sessions_dir / "sessions_current.jsonl"

            # Unify with existing sessions
            click.echo("  Unifying sessions...")
            unified_df, report = unify_sessions(
                normalized,
                existing_file=current_sessions if current_sessions.exists() else None,
                output_file=store.sessions_dir / f"sessions_{datetime.now().strftime('%Y%m%d')}.jsonl",
            )

            # Update stats
            total_spans += len(spans_df)
            total_new_sessions += len(report.new_sessions)
            total_continued_sessions += len(report.continued_sessions)

            # Report results for this backend
            click.echo(
                f"  New sessions: {len(report.new_sessions)}, "
                f"Continued: {len(report.continued_sessions)}, "
                f"Duplicates removed: {report.duplicates_removed}"
            )

            # Update state on success
            state.set_last_sync(backend_id, datetime.now())
            click.echo(click.style(f"  [OK] {config['name']} sync complete", fg="green"))

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
    "--days",
    type=int,
    default=90,
    help="Number of days of historical data to sync (default: 90, max: 365)",
)
@click.option(
    "--batch-size",
    type=int,
    default=7,
    help="Days per batch (default: 7). Smaller batches are more reliable.",
)
@click.option(
    "--backend",
    type=click.Choice(list(BACKENDS.keys())),
    help="Sync specific backend only",
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
def sync_historical(
    days: int, batch_size: int, backend: str | None, retries: int, skip_normalize: bool
) -> None:
    """One-time historical backfill of trace data.

    This command exports historical data in small sequential batches to avoid
    API throttling and memory issues. Use for initial setup or backfill scenarios.

    Unlike 'dal sync', this command:
    - Always fetches historical data (ignores sync state)
    - Uses smaller batch sizes for reliability
    - Saves intermediate results after each batch
    - Has built-in retry logic

    Examples:

        dal sync-historical                      # Last 90 days, 7-day batches

        dal sync-historical --days 120           # Last 120 days

        dal sync-historical --batch-size 3       # Smaller 3-day batches

        dal sync-historical --backend arize-cloud  # Only from Arize
    """
    from datetime import timedelta
    import pandas as pd

    sync_start = time.time()

    # Validate days
    if days > 365:
        click.echo(
            click.style("Warning: Limiting to 365 days (maximum)", fg="yellow")
        )
        days = 365

    # Determine which backends to sync
    if backend:
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
    else:
        backends_to_sync = get_configured_backends()
        if not backends_to_sync:
            click.echo(
                click.style(
                    "Error: No backends configured. Set DAL_PHOENIX_URL or ARIZE_API_KEY.",
                    fg="red",
                )
            )
            raise SystemExit(1)

    # Calculate batches
    num_batches = (days + batch_size - 1) // batch_size  # Ceiling division
    click.echo(click.style("Historical Sync", bold=True))
    click.echo(f"Backends: {', '.join(backends_to_sync)}")
    click.echo(f"Time range: {days} days")
    click.echo(f"Batch size: {batch_size} days ({num_batches} batches)")
    click.echo(f"Retries per batch: {retries}")
    click.echo()

    # Initialize storage
    store = OxenStore()

    total_spans = 0
    total_batches_completed = 0
    total_batches_failed = 0
    all_errors = []

    # Process each backend sequentially
    for backend_id in backends_to_sync:
        config = BACKENDS[backend_id]
        click.echo(f"[{config['name']}] Starting historical sync...")

        # Create client
        client_class = config["client_class"]
        try:
            client = client_class()
        except Exception as e:
            click.echo(click.style(f"  [FAIL] Could not create client: {e}", fg="red"))
            all_errors.append(f"{backend_id}: Client creation failed - {e}")
            continue

        # Generate batches (most recent first)
        end_time = datetime.now()
        batches = []
        batch_end = end_time
        for _ in range(num_batches):
            batch_start = batch_end - timedelta(days=batch_size)
            # Don't go beyond the requested days
            if batch_start < end_time - timedelta(days=days):
                batch_start = end_time - timedelta(days=days)
            batches.append((batch_start, batch_end))
            batch_end = batch_start
            if batch_start <= end_time - timedelta(days=days):
                break

        click.echo(f"  Processing {len(batches)} batches sequentially...")
        click.echo()

        backend_spans = 0
        for i, (batch_start, batch_end) in enumerate(batches):
            batch_num = i + 1
            click.echo(
                f"  Batch {batch_num}/{len(batches)}: "
                f"{batch_start.strftime('%Y-%m-%d')} to {batch_end.strftime('%Y-%m-%d')}",
                nl=False,
            )

            # Attempt fetch with retries
            batch_df = None
            last_error = None
            for attempt in range(1, retries + 1):
                try:
                    batch_df = client.get_spans_dataframe(
                        start_time=batch_start,
                        end_time=batch_end,
                    )
                    break  # Success
                except Exception as e:
                    last_error = e
                    if attempt < retries:
                        click.echo(f" (retry {attempt})", nl=False)
                        time.sleep(2 ** attempt)  # Exponential backoff

            if batch_df is None or (hasattr(batch_df, "empty") and batch_df.empty):
                if last_error:
                    click.echo(click.style(f" FAILED: {last_error}", fg="red"))
                    all_errors.append(
                        f"{backend_id} batch {batch_num}: {last_error}"
                    )
                    total_batches_failed += 1
                else:
                    click.echo(click.style(" (no data)", fg="yellow"))
                    total_batches_completed += 1
                continue

            # Save intermediate results
            batch_count = len(batch_df)
            backend_spans += batch_count

            if skip_normalize:
                # Save raw data directly
                raw_file = store.append_spans(
                    batch_df,
                    backend=f"{backend_id}-historical",
                )
            else:
                # Normalize and save
                try:
                    normalized = config["normalizer"](batch_df)
                    raw_file = store.append_spans(
                        normalized,
                        backend=f"{backend_id}-historical",
                    )
                except Exception as e:
                    click.echo(
                        click.style(f" WARN: normalize failed, saving raw - {e}", fg="yellow")
                    )
                    raw_file = store.append_spans(
                        batch_df,
                        backend=f"{backend_id}-historical-raw",
                    )

            click.echo(click.style(f" {batch_count:,} spans", fg="green"))
            total_batches_completed += 1

        click.echo()
        click.echo(f"  [{config['name']}] Total: {backend_spans:,} spans")
        total_spans += backend_spans
        click.echo()

    # Final summary
    elapsed = time.time() - sync_start
    click.echo("=" * 50)
    click.echo(click.style("Historical Sync Summary", bold=True))
    click.echo("=" * 50)
    click.echo(f"Total spans fetched: {total_spans:,}")
    click.echo(f"Batches completed: {total_batches_completed}")
    click.echo(f"Batches failed: {total_batches_failed}")
    click.echo(f"Time elapsed: {elapsed:.1f}s")

    if all_errors:
        click.echo()
        click.echo(click.style("Errors:", fg="red"))
        for error in all_errors[:10]:  # Show first 10 errors
            click.echo(f"  - {error}")
        if len(all_errors) > 10:
            click.echo(f"  ... and {len(all_errors) - 10} more")
        raise SystemExit(1)

    click.echo()
    click.echo(click.style("Historical sync complete!", fg="green"))


@main.command()
def config() -> None:
    """Show current DAL configuration."""
    click.echo(click.style("DAL Configuration", bold=True))
    click.echo()

    # Show backends
    click.echo("Backends:")
    for backend_id, backend_config in BACKENDS.items():
        env_var = backend_config["env_check"]
        is_configured = bool(os.getenv(env_var))
        status = click.style("configured", fg="green") if is_configured else click.style("not set", fg="red")
        click.echo(f"  {backend_id}: {status} ({env_var})")

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


if __name__ == "__main__":
    main()
