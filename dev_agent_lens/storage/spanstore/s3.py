"""S3-protocol span store: AWS, and every self-hosted implementation of the same API.

One driver covers MinIO, SeaweedFS, Ceph RGW, an on-prem gateway, and GCS via its
S3-compatible endpoint. That is a claim worth distrusting until measured, so
`scripts/verify_spanstore.py` runs at least two independent server implementations rather
than testing MinIO twice and calling the protocol portable.

A store URI is validated at construction and rejected if it could be anything other than
data: no credentials in the userinfo part, no quote or statement characters anywhere,
endpoint/region/bucket restricted to their legal alphabets. Every value that reaches
DuckDB SQL goes through `quote_literal`, which refuses rather than escapes. The first
version of this module documented URIs as "safe to paste into a ticket" while
interpolating them raw into SQL; a review injected through a `'` in a prefix and read the
S3 secret back out of `current_setting()`. The safety claim is only true with these checks.

Credentials come from boto3's default chain (environment including AWS_SESSION_TOKEN,
shared profile, SSO, instance/IRSA role) and the SAME resolved credentials are handed to
DuckDB, so `probe()` and `COPY` cannot disagree about who they are. Air-gap note: set
DAL_DUCKDB_HTTPFS_PATH to a vendored httpfs build that matches this DuckDB patch version
exactly and is named `httpfs.duckdb_extension`; it loads with signature enforcement on.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from dev_agent_lens.storage.spanstore.base import (
    SpanStore,
    StoreCapabilities,
    StoreHealth,
    dataset_of,
    quote_literal,
)

log = logging.getLogger(__name__)

_ENDPOINT = re.compile(r"^[A-Za-z0-9.-]+(:\d{1,5})?$")
_REGION = re.compile(r"^[a-z0-9-]+$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_PREFIX_BAD = re.compile(r"[\s'\";\\]")
_SCHEME_DEFAULT_ENDPOINT = {"gs": "storage.googleapis.com"}
_SCHEMES_NEED_ENDPOINT = {"minio"}

_VENDORED_OK: dict[str, bool] = {}


def vendored_httpfs_ok(path: str | None = None) -> bool:
    """Does the vendored httpfs actually load into THIS DuckDB and register as httpfs?

    Two independent ways it fails, both measured 2026-09-04: the file is built for one
    DuckDB patch version and refuses any other; and DuckDB derives the entrypoint symbol
    from the basename, so `httpfs-v1.4.3.duckdb_extension` is silently unloadable. Cached
    per path.
    """
    path = path or os.getenv("DAL_DUCKDB_HTTPFS_PATH")
    if not path or not os.path.exists(path):
        return False
    if path in _VENDORED_OK:
        return _VENDORED_OK[path]
    try:
        import duckdb

        con = duckdb.connect()
        con.execute(f"LOAD {quote_literal(path)}")
        loaded = (
            con.execute(
                "SELECT count(*) FROM duckdb_extensions() WHERE extension_name='httpfs' AND loaded"
            ).fetchone()[0]
            == 1
        )
        con.close()
        _VENDORED_OK[path] = loaded
    except Exception as e:  # noqa: BLE001
        log.warning("[spanstore:s3] vendored httpfs unusable path=%s: %s", path, e)
        _VENDORED_OK[path] = False
    return _VENDORED_OK[path]


@dataclass
class S3Settings:
    bucket: str
    prefix: str
    endpoint: str | None  # host[:port]; None = real AWS
    region: str
    use_ssl: bool
    url_style: str  # "path" for self-hosted, "vhost" for AWS

    @property
    def flavor(self) -> str:
        return "aws" if not self.endpoint else "s3-compatible"


def _settings_from_uri(uri: str) -> S3Settings:
    """Parse s3://bucket/prefix?endpoint=host:port&tls=1&region=...&url_style=path.

    Rejects anything that is not inert data. Credentials in the URI are refused outright:
    they would be echoed by every error message and persisted to config.json.
    """
    p = urlparse(uri)
    if p.username or p.password:
        raise ValueError(
            "credentials in a store URI are not accepted; use the environment "
            "(AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN)"
        )
    if "'" in uri or ";" in uri:
        raise ValueError("store URI may not contain quote or statement characters")
    q = {k: v[0] for k, v in parse_qs(p.query).items()}
    scheme = p.scheme
    endpoint = (
        q.get("endpoint")
        or os.getenv("DAL_S3_ENDPOINT")
        or os.getenv("AWS_ENDPOINT_URL_S3")
        or os.getenv("AWS_ENDPOINT_URL")
        or _SCHEME_DEFAULT_ENDPOINT.get(scheme)
        or None
    )
    if endpoint:
        endpoint = re.sub(r"^https?://", "", endpoint).rstrip("/")
        if not _ENDPOINT.match(endpoint):
            raise ValueError(f"endpoint {endpoint!r} is not a host[:port]")
    elif scheme in _SCHEMES_NEED_ENDPOINT:
        raise ValueError(
            f"{scheme}:// requires ?endpoint=host:port; without it the URI "
            "would silently target AWS with your credentials"
        )
    region = (
        q.get("region") or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"
    )
    if not _REGION.match(region):
        raise ValueError(f"region {region!r} is not a valid region name")
    if not _BUCKET.match(p.netloc):
        raise ValueError(f"bucket {p.netloc!r} is not a valid bucket name")
    prefix = p.path.strip("/")
    if _PREFIX_BAD.search(prefix):
        raise ValueError("prefix may not contain whitespace, quotes, ';' or '\\\\'")
    url_style = q.get("url_style") or ("vhost" if endpoint is None else "path")
    if url_style not in ("path", "vhost"):
        raise ValueError("url_style must be 'path' or 'vhost'")
    # TLS defaults ON even with an endpoint. Defaulting it off for self-hosted stores meant
    # an operator who forgot tls=1 sent the whole corpus in cleartext; the local presets
    # spell tls=0 out explicitly, so nothing is lost by making the safe thing the default.
    use_ssl = q.get("tls", "1").lower() not in ("0", "false", "no", "off")
    if not use_ssl:
        log.warning(
            "[spanstore:s3] TLS is OFF for %s — acceptable for a local container only",
            endpoint or "aws",
        )
    return S3Settings(
        bucket=p.netloc,
        prefix=prefix,
        endpoint=endpoint,
        region=region,
        use_ssl=use_ssl,
        url_style=url_style,
    )


def _resolve_credentials():
    """One resolution of the boto3 default chain, shared by boto3 and DuckDB."""
    import boto3

    creds = boto3.Session().get_credentials()
    return creds.get_frozen_credentials() if creds else None


class S3SpanStore(SpanStore):
    scheme = "s3"

    def __init__(self, uri: str) -> None:
        super().__init__(uri)
        self.s = _settings_from_uri(uri)
        log.debug(
            "[spanstore:s3] bucket=%s prefix=%s endpoint=%s ssl=%s style=%s",
            self.s.bucket,
            self.s.prefix,
            self.s.endpoint,
            self.s.use_ssl,
            self.s.url_style,
        )

    # -- engine wiring --------------------------------------------------------
    def _base(self, dataset: str) -> str:
        parts = [p for p in (self.s.prefix, dataset) if p]
        return f"s3://{self.s.bucket}/" + "/".join(parts)

    def read_glob(self, dataset: str = "spans") -> str:
        return f"{self._base(dataset)}/**/*.parquet"

    def write_target(self, dataset: str = "spans") -> str:
        return self._base(dataset)

    def attach_duckdb(self, con: Any) -> None:
        t0 = time.perf_counter()
        vendored = os.getenv("DAL_DUCKDB_HTTPFS_PATH")
        if vendored and vendored_httpfs_ok(vendored):
            con.execute(f"LOAD {quote_literal(vendored)}")
            log.info("[spanstore:s3] httpfs loaded from vendored file (airgap-safe)")
        else:
            if vendored:
                ver = con.execute("SELECT version()").fetchone()[0]
                log.error(
                    "[spanstore:s3] vendored httpfs at DAL_DUCKDB_HTTPFS_PATH does not "
                    "load into DuckDB %s; falling back to network install",
                    ver,
                )
            con.execute("INSTALL httpfs; LOAD httpfs;")
            log.info(
                "[spanstore:s3] httpfs installed from network "
                "(set DAL_DUCKDB_HTTPFS_PATH to a matching build for air-gapped installs)"
            )
        if self.s.endpoint:
            con.execute(f"SET s3_endpoint={quote_literal(self.s.endpoint)}")
        con.execute(f"SET s3_region={quote_literal(self.s.region)}")
        con.execute(f"SET s3_url_style={quote_literal(self.s.url_style)}")
        con.execute(f"SET s3_use_ssl={'true' if self.s.use_ssl else 'false'}")
        creds = _resolve_credentials()
        if creds:
            con.execute(f"SET s3_access_key_id={quote_literal(creds.access_key)}")
            con.execute(f"SET s3_secret_access_key={quote_literal(creds.secret_key)}")
            if creds.token:
                con.execute(f"SET s3_session_token={quote_literal(creds.token)}")
        log.debug("[spanstore:s3] attach_duckdb done in %.1fms", (time.perf_counter() - t0) * 1000)

    # -- lifecycle ------------------------------------------------------------
    def _client(self):
        try:
            import boto3
            import botocore.exceptions  # noqa: F401 - imported here so the hint fires for both
        except ModuleNotFoundError as e:
            raise ModuleNotFoundError(
                "S3-protocol span stores need boto3, an optional dependency so a "
                "local-directory install stays dependency-free. Install it with:\n"
                "    uv sync --extra s3        (or: pip install 'dev_agent_lens[s3]')"
            ) from e
        kw: dict[str, Any] = {"region_name": self.s.region}
        if self.s.endpoint:
            kw["endpoint_url"] = f"{'https' if self.s.use_ssl else 'http'}://{self.s.endpoint}"
        return boto3.client("s3", **kw)  # credentials: default chain, same as DuckDB

    def ensure(self) -> None:
        import botocore.exceptions

        c = self._client()
        kw: dict[str, Any] = {"Bucket": self.s.bucket}
        if self.s.endpoint is None and self.s.region != "us-east-1":
            kw["CreateBucketConfiguration"] = {"LocationConstraint": self.s.region}
        try:
            c.create_bucket(**kw)
            log.info("[spanstore:s3] bucket created %s", self.s.bucket)
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] not in (
                "BucketAlreadyOwnedByYou",
                "BucketAlreadyExists",
            ):
                raise
            log.debug("[spanstore:s3] bucket exists %s", self.s.bucket)

    def clear(self, dataset: str = "spans") -> int:
        """Delete every object under the dataset prefix. Returns the count removed."""
        c = self._client()
        prefix = "/".join(p for p in (self.s.prefix, dataset) if p) + "/"
        removed, token = 0, None
        while True:
            kw = {"Bucket": self.s.bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            r = c.list_objects_v2(**kw)
            keys = [{"Key": o["Key"]} for o in r.get("Contents", [])]
            if keys:
                c.delete_objects(Bucket=self.s.bucket, Delete={"Objects": keys, "Quiet": True})
                removed += len(keys)
            if not r.get("IsTruncated"):
                break
            token = r["NextContinuationToken"]
        log.info("[spanstore:s3] cleared %d object(s) under %s", removed, prefix)
        return removed

    def probe(self) -> StoreHealth:
        t0 = time.perf_counter()
        key = f"{self.s.prefix}/.dal-probe".lstrip("/")
        try:
            c = self._client()
            c.put_object(Bucket=self.s.bucket, Key=key, Body=b"ok")
            body = c.get_object(Bucket=self.s.bucket, Key=key)["Body"].read()
            c.delete_object(Bucket=self.s.bucket, Key=key)
            el = (time.perf_counter() - t0) * 1000
            log.info(
                "[spanstore:s3] probe ok bucket=%s endpoint=%s rt=%.1fms",
                self.s.bucket,
                self.s.endpoint or "aws",
                el,
            )
            return StoreHealth(
                ok=True,
                uri=self.uri,
                readable=body == b"ok",
                writable=True,
                elapsed_ms=el,
                detail=f"{self.s.flavor} @ {self.s.endpoint or 'aws'}",
            )
        except ModuleNotFoundError:
            raise
        except Exception as e:  # noqa: BLE001 - probe must never raise on reachability
            el = (time.perf_counter() - t0) * 1000
            missing = "NoSuchBucket" in str(e)
            (log.info if missing else log.warning)(
                "[spanstore:s3] probe %s bucket=%s endpoint=%s: %s",
                "found no bucket yet" if missing else "FAILED",
                self.s.bucket,
                self.s.endpoint,
                type(e).__name__,
            )
            return StoreHealth(
                ok=False, uri=self.uri, detail=f"{type(e).__name__}: {e}", elapsed_ms=el
            )

    def capabilities(self) -> StoreCapabilities:
        vendored = vendored_httpfs_ok()
        return StoreCapabilities(
            name=f"S3 ({self.s.flavor})",
            needs_network=True,
            needs_duckdb_extension="httpfs",
            airgap_ready=vendored,
            notes=(
                "httpfs vendored and verified loadable; works with no outbound network"
                if vendored
                else "httpfs will be fetched from extensions.duckdb.org on first use. For "
                "air-gapped installs set DAL_DUCKDB_HTTPFS_PATH to a vendored build that "
                "matches this DuckDB patch version exactly and is named "
                "httpfs.duckdb_extension (DuckDB derives the entrypoint from the filename)"
            ),
            extras={
                "endpoint": self.s.endpoint or "aws",
                "url_style": self.s.url_style,
                "tls": self.s.use_ssl,
                "region": self.s.region,
            },
        )

    def list_datasets(self) -> list[str]:
        c = self._client()
        prefix = f"{self.s.prefix}/" if self.s.prefix else ""
        found: set[str] = set()
        token, scanned = None, 0
        while True:
            kw: dict[str, Any] = {"Bucket": self.s.bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            r = c.list_objects_v2(**kw)
            for o in r.get("Contents", []):
                scanned += 1
                key = o["Key"][len(prefix) :]
                if key.endswith(".parquet"):
                    found.add(dataset_of(key.split("/")))
            if not r.get("IsTruncated"):
                break
            token = r["NextContinuationToken"]
        found.discard("")
        log.debug("[spanstore:s3] datasets=%s (scanned %d objects)", sorted(found), scanned)
        return sorted(found)

    def size_bytes(self, dataset: str = "spans") -> int:
        c = self._client()
        prefix = (
            "/".join(p for p in (self.s.prefix, dataset) if p) + "/"
        )  # exact, not string-prefix
        total, token = 0, None
        while True:
            kw = {"Bucket": self.s.bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            r = c.list_objects_v2(**kw)
            total += sum(o["Size"] for o in r.get("Contents", []))
            if not r.get("IsTruncated"):
                return total
            token = r["NextContinuationToken"]
