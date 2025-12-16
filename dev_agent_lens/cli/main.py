"""
DAL CLI - Dev Agent Lens Command Line Interface

Provides unified CLI for trace data collection, querying, and analysis.

Commands:
    dal sync          Sync trace data from backends
    dal sync --full   Full sync ignoring state
    dal sync --push   Sync and push to Oxen remote
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
def sync(full: bool, backend: str | None, push: bool, skip_annotations: bool) -> None:
    """Sync trace data from configured backends.

    By default, performs incremental sync using saved state.
    Use --full to ignore state and fetch all available data.

    Examples:

        dal sync                    # Incremental sync from default backend

        dal sync --full             # Full sync, ignore state

        dal sync --backend arize    # Sync only from Arize

        dal sync --push             # Sync and push to Oxen remote
    """
    start_time = time.time()

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

    # Sync each backend
    for backend_id in backends_to_sync:
        config = BACKENDS[backend_id]
        click.echo(f"[{config['name']}] Starting sync...")

        try:
            # Get last sync time unless doing full sync
            last_sync = None if full else state.get_last_sync(backend_id)

            if last_sync:
                click.echo(f"  Last sync: {last_sync.isoformat()}")
            else:
                click.echo("  First sync (fetching all data)")

            # Create client and fetch spans
            client_class = config["client_class"]
            client = client_class()

            click.echo("  Fetching spans...")
            spans_df = client.get_spans_dataframe(start_time=last_sync)

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
    elapsed = time.time() - start_time
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
