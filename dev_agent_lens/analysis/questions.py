"""Question library for dal_catalog.sessions.

Provides pre-built queries grouped by intent:
  • What did my team do today/this week?
  • Where is time actually going?
  • What patterns repeat?
  • Which sessions stalled?
  • Who is active and what changed?

All queries build on dal_catalog.sessions (ENG2-1470) — a unified view of
1,333+ sessions (2026-05-06 → 08-04) with summary, category, timestamps,
person attribution, and call/tool counts.

Usage:
    from dev_agent_lens.analysis.questions import get_team_activity
    df = get_team_activity(days=7)
    print(df)
"""

from __future__ import annotations

import logging
import os
import warnings
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pandas as pd

# Suppress pandas warning about psycopg connection not being SQLAlchemy
warnings.filterwarnings("ignore", message=".*pandas only supports SQLAlchemy.*")

if TYPE_CHECKING:
    from datetime import tzinfo

logger = logging.getLogger(__name__)


def _get_connection():
    """Get psycopg connection to Supabase (dal_catalog schema)."""
    try:
        import psycopg
    except ImportError:
        raise ImportError("psycopg[binary] is required. Install with: uv add 'psycopg[binary]'")

    url = os.getenv("PHOENIX_SQL_DATABASE_URL")
    if not url:
        raise ValueError("PHOENIX_SQL_DATABASE_URL not set. Check .env file.")

    return psycopg.connect(url, autocommit=True)


def get_team_activity(days: int = 7, tz: tzinfo | None = None) -> pd.DataFrame:
    """What did my team do in the past N days?

    Returns per-person activity: sessions, LLM calls, tool calls, top categories.

    Args:
        days: Look back N days from now (default: 7)
        tz: Timezone for relative dates (default: UTC)

    Returns:
        DataFrame with columns: person, sessions, llm_calls, tool_calls, categories
    """
    con = _get_connection()

    sql = f"""
    SELECT
      CASE WHEN a.email IS NOT NULL THEN split_part(a.email, '@', 1)
           ELSE '(unclaimed:' || substr(s.account_uuid, 1, 8) || ')'
      END AS person,
      COUNT(DISTINCT s.session_id) AS sessions,
      SUM(COALESCE(s.n_llm_calls, 0)) AS llm_calls,
      SUM(COALESCE(s.n_tool_calls, 0)) AS tool_calls,
      string_agg(DISTINCT s.category, ', ' ORDER BY s.category) AS categories
    FROM dal_catalog.sessions s
    LEFT JOIN dal_catalog.accounts a ON s.account_uuid = a.account_uuid
    WHERE s.started_at > now() - INTERVAL '{days} days'
    GROUP BY person
    ORDER BY sessions DESC
    """

    df = pd.read_sql(sql, con)
    con.close()
    return df


def get_weekly_summary() -> pd.DataFrame:
    """Where is time going? Breakdown by category and person.

    Returns activity per category and person for the past 7 days.

    Returns:
        DataFrame with columns: person, category, sessions, llm_calls, tool_calls
    """
    con = _get_connection()

    sql = """SELECT
      CASE WHEN a.email IS NOT NULL THEN split_part(a.email, '@', 1)
           ELSE '(unclaimed)'
      END AS person,
      COALESCE(s.category, 'uncategorized') AS category,
      COUNT(DISTINCT s.session_id) AS sessions,
      SUM(COALESCE(s.n_llm_calls, 0)) AS llm_calls,
      SUM(COALESCE(s.n_tool_calls, 0)) AS tool_calls
    FROM dal_catalog.sessions s
    LEFT JOIN dal_catalog.accounts a ON s.account_uuid = a.account_uuid
    WHERE s.started_at > now() - INTERVAL '7 days'
    GROUP BY person, category
    ORDER BY person, llm_calls DESC
    """

    df = pd.read_sql(sql, con)
    con.close()
    return df


def get_project_distribution() -> pd.DataFrame:
    """Where is time going? Breakdown by project.

    Shows session count, LLM calls, and tool calls per project path.

    Returns:
        DataFrame with columns: project, sessions, llm_calls, tool_calls
    """
    con = _get_connection()

    sql = """SELECT
      COALESCE(s.project_path, 'unknown') AS project,
      COUNT(DISTINCT s.session_id) AS sessions,
      SUM(COALESCE(s.n_llm_calls, 0)) AS llm_calls,
      SUM(COALESCE(s.n_tool_calls, 0)) AS tool_calls
    FROM dal_catalog.sessions s
    WHERE s.started_at > now() - INTERVAL '7 days'
    GROUP BY project
    ORDER BY llm_calls DESC
    """

    df = pd.read_sql(sql, con)
    con.close()
    return df


def get_repeated_patterns(min_sessions: int = 3) -> pd.DataFrame:
    """What patterns repeat often enough to be a skill?

    Shows categories with N+ sessions, indicating repeated workflows.
    Coverage note: Phoenix project dark since 2026-06-06 (ENG2-1375),
    thinking-token capture unexplained (ENG2-1487).

    Args:
        min_sessions: Minimum session count to show (default: 3)

    Returns:
        DataFrame with columns: category, sessions, pct_of_total, avg_llm_calls,
                               avg_tool_calls, avg_duration_minutes
    """
    con = _get_connection()

    sql = f"""WITH cats AS (
      SELECT
        s.category,
        COUNT(DISTINCT s.session_id) AS sessions,
        AVG(COALESCE(s.n_llm_calls, 0))::int AS avg_llm_calls,
        AVG(COALESCE(s.n_tool_calls, 0))::int AS avg_tool_calls,
        ROUND(AVG(EXTRACT(EPOCH FROM (s.ended_at - s.started_at)) / 60.0))::int AS avg_duration_minutes
      FROM dal_catalog.sessions s
      WHERE s.started_at > now() - INTERVAL '60 days'
      GROUP BY s.category
    )
    SELECT
      category,
      sessions,
      ROUND(100.0 * sessions / (SELECT SUM(sessions) FROM cats), 1) AS pct_of_total,
      avg_llm_calls,
      avg_tool_calls,
      avg_duration_minutes
    FROM cats
    WHERE sessions >= {min_sessions}
    ORDER BY sessions DESC
    """

    df = pd.read_sql(sql, con)
    con.close()
    return df


def get_stalled_sessions(hours: int = 3) -> pd.DataFrame:
    """Which sessions stalled and on what?

    Returns sessions where the time from start to end suggests they ran long.

    Args:
        hours: Minimum duration to consider "stalled" (default: 3)

    Returns:
        DataFrame with columns: person, session_id, started_at, ended_at,
                               duration_hours, category, summary
    """
    con = _get_connection()

    sql = f"""SELECT
      CASE WHEN a.email IS NOT NULL THEN split_part(a.email, '@', 1)
           ELSE '(unclaimed)'
      END AS person,
      s.session_id,
      s.started_at,
      s.ended_at,
      ROUND(EXTRACT(EPOCH FROM (s.ended_at - s.started_at)) / 3600.0, 1) AS duration_hours,
      s.category,
      LEFT(s.summary, 80) || CASE WHEN length(s.summary) > 80 THEN '...' ELSE '' END AS summary
    FROM dal_catalog.sessions s
    LEFT JOIN dal_catalog.accounts a ON s.account_uuid = a.account_uuid
    WHERE s.ended_at > now() - INTERVAL '14 days'
      AND (s.ended_at - s.started_at) > INTERVAL '{hours} hours'
    ORDER BY s.ended_at DESC
    """

    df = pd.read_sql(sql, con)
    con.close()
    return df


def get_active_roster() -> pd.DataFrame:
    """Who is active and what changed since last week?

    Returns per-person activity summary with week-over-week change.

    Returns:
        DataFrame with columns: person, this_week_sessions, last_week_sessions,
                               change, this_week_calls, this_week_tools
    """
    con = _get_connection()

    sql = """
    WITH this_week AS (
      SELECT
        CASE WHEN a.email IS NOT NULL THEN split_part(a.email, '@', 1)
             ELSE '(unclaimed:' || substr(s.account_uuid, 1, 8) || ')'
        END AS person,
        COUNT(DISTINCT s.session_id) AS sessions,
        SUM(COALESCE(s.n_llm_calls, 0)) AS calls,
        SUM(COALESCE(s.n_tool_calls, 0)) AS tools
      FROM dal_catalog.sessions s
      LEFT JOIN dal_catalog.accounts a ON s.account_uuid = a.account_uuid
      WHERE s.started_at > now() - INTERVAL '7 days'
      GROUP BY person
    ),
    last_week AS (
      SELECT
        CASE WHEN a.email IS NOT NULL THEN split_part(a.email, '@', 1)
             ELSE '(unclaimed:' || substr(s.account_uuid, 1, 8) || ')'
        END AS person,
        COUNT(DISTINCT s.session_id) AS sessions
      FROM dal_catalog.sessions s
      LEFT JOIN dal_catalog.accounts a ON s.account_uuid = a.account_uuid
      WHERE s.started_at > now() - INTERVAL '14 days'
        AND s.started_at <= now() - INTERVAL '7 days'
      GROUP BY person
    )
    SELECT
      t.person,
      t.sessions AS this_week_sessions,
      COALESCE(l.sessions, 0) AS last_week_sessions,
      CASE WHEN l.sessions = 0 AND t.sessions > 0 THEN 'NEW'
           WHEN l.sessions = 0 THEN '—'
           ELSE CASE WHEN t.sessions > l.sessions THEN '↑'
                     WHEN t.sessions < l.sessions THEN '↓'
                     ELSE '→'
                END || ' ' || COALESCE(((t.sessions - l.sessions)::float / NULLIF(l.sessions, 0) * 100)::int::text, '')
      END AS change,
      t.calls AS this_week_calls,
      t.tools AS this_week_tools
    FROM this_week t
    LEFT JOIN last_week l ON t.person = l.person
    ORDER BY t.sessions DESC
    """

    df = pd.read_sql(sql, con)
    con.close()
    return df


def get_session_history(
    session_id: str | None = None,
    account_uuid: str | None = None,
    limit: int = 10,
) -> pd.DataFrame:
    """Get session history filtered by session_id or person.

    Args:
        session_id: Specific session to fetch
        account_uuid: Specific person's sessions
        limit: Max rows (default: 10)

    Returns:
        DataFrame with columns: session_id, person, started_at, ended_at,
                               n_llm_calls, n_tool_calls, category, summary
    """
    con = _get_connection()

    if session_id:
        sql = """
        SELECT
          s.session_id,
          CASE WHEN a.email IS NOT NULL THEN a.email ELSE s.account_uuid END AS person,
          s.started_at,
          s.ended_at,
          s.n_llm_calls,
          s.n_tool_calls,
          s.category,
          s.summary
        FROM dal_catalog.sessions s
        LEFT JOIN dal_catalog.accounts a ON s.account_uuid = a.account_uuid
        WHERE s.session_id = %s
        """
        df = pd.read_sql(sql, con, params=(session_id,))
    elif account_uuid:
        sql = """
        SELECT
          s.session_id,
          a.email AS person,
          s.started_at,
          s.ended_at,
          s.n_llm_calls,
          s.n_tool_calls,
          s.category,
          s.summary
        FROM dal_catalog.sessions s
        LEFT JOIN dal_catalog.accounts a ON s.account_uuid = a.account_uuid
        WHERE s.account_uuid = %s
        ORDER BY s.started_at DESC
        LIMIT %s
        """
        df = pd.read_sql(sql, con, params=(account_uuid, limit))
    else:
        sql = """
        SELECT
          s.session_id,
          CASE WHEN a.email IS NOT NULL THEN split_part(a.email, '@', 1)
               ELSE '(unclaimed)'
          END AS person,
          s.started_at,
          s.ended_at,
          s.n_llm_calls,
          s.n_tool_calls,
          s.category,
          LEFT(s.summary, 100) || CASE WHEN length(s.summary) > 100 THEN '...' ELSE '' END AS summary
        FROM dal_catalog.sessions s
        LEFT JOIN dal_catalog.accounts a ON s.account_uuid = a.account_uuid
        ORDER BY s.started_at DESC
        LIMIT %s
        """
        df = pd.read_sql(sql, con, params=(limit,))

    con.close()
    return df
