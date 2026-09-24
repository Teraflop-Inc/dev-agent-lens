# Git webhook events

DAL stores the actions of coding agents and their reviewers. GitHub and Forgejo
can send signed events to `/v1/events/github` and `/v1/events/forgejo` on the DAL
receiver. A delivery becomes a raw span with its producer as `source`, event name,
actor, and git metadata. Reviews and comments on a pull request share a trace.

Other product events belong in their own systems of record. Join to those systems
by identifier when an analysis needs them, rather than copying their state into
DAL.

## Configure a producer

1. Set `DAL_EVENTS_SECRET` on the receiver and the same secret on the git host.
2. Use webhook content type `application/json`.
3. Subscribe to pushes, pull requests, reviews, and review comments.
4. Keep the receiver on a private network, or put a TLS gateway in front that
   only exposes the intended signed webhook paths. Do not expose `/v1/traces`
   publicly without authentication.

The receiver logs whether event signatures are required. An unsigned configuration
is only appropriate for a private test network. Bodies above 25 MB are rejected,
and a stalled connection is dropped after 30 seconds.

## Replay history

```sh
uv run dal github-backfill --repo owner/repository \
  --receiver http://127.0.0.1:4318 --until 2026-01-01T00:00:00Z
```

The command replays pull requests, reviews, and comments through the same signed
endpoint. Set its required credentials in the environment; inspect `--help` for
options. Backfilled events are marked in their git metadata.

GitHub does not automatically retry failed deliveries. Use its Recent deliveries
view to redeliver after an outage. Stable delivery/span IDs make retries
idempotent. Forgejo includes at most ten commits in a push payload; DAL retains
the delivery identifier and last commit time, while the complete history remains
on the git host.
