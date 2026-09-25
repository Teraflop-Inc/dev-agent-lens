"""Immutable typed generations and a conditional, atomic current-manifest write."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from urllib.parse import urlsplit, urlunsplit

from dev_agent_lens.storage.spanstore import open_store
from dev_agent_lens.storage.spanstore.localfs import LocalSpanStore
from dev_agent_lens.storage.spanstore.s3 import S3SpanStore

CURRENT = "_typed/current.json"


class SnapshotIO:
    def __init__(self, store):
        self.store = store
        self.local = isinstance(store, LocalSpanStore)
        if not self.local and not isinstance(store, S3SpanStore):
            raise ValueError("typed snapshots require a local or S3-compatible store")

    def key(self, relative):
        return "/".join(p for p in (self.store.s.prefix, relative) if p)

    def read_current(self):
        if self.local:
            try:
                body = (self.store.root / CURRENT).read_bytes()
            except FileNotFoundError:
                return None, None
            token = hashlib.sha256(body).hexdigest()
        else:
            from botocore.exceptions import ClientError

            try:
                obj = self.store._client().get_object(
                    Bucket=self.store.s.bucket, Key=self.key(CURRENT)
                )
            except ClientError as exc:
                if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
                    return None, None
                raise
            try:
                body = obj["Body"].read()
            finally:
                obj["Body"].close()
            token = obj["ETag"]
        manifest = json.loads(body)
        if manifest.get("format") != 1:
            raise ValueError("unsupported typed snapshot manifest format")
        return manifest, token

    def publish(self, manifest, expected):
        body = json.dumps(manifest, sort_keys=True).encode()
        archive = f"_typed/generations/{manifest['snapshot']}/manifest.json"
        if not self.local:
            self.store._client().put_object(
                Bucket=self.store.s.bucket,
                Key=self.key(archive),
                Body=body,
                ContentType="application/json",
                IfNoneMatch="*",
            )
            condition = {"IfMatch": expected} if expected else {"IfNoneMatch": "*"}
            self.store._client().put_object(
                Bucket=self.store.s.bucket,
                Key=self.key(CURRENT),
                Body=body,
                ContentType="application/json",
                **condition,
            )
            return
        import fcntl

        parent = self.store.root / "_typed"
        parent.mkdir(parents=True, exist_ok=True)
        with (parent / "publish.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.read_current()[1] != expected:
                raise RuntimeError("typed snapshot changed during build; retry")
            with (self.store.root / archive).open("xb") as history:
                history.write(body)
                history.flush()
                os.fsync(history.fileno())
            fd, name = tempfile.mkstemp(dir=parent)
            try:
                with os.fdopen(fd, "wb") as out:
                    out.write(body)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(name, self.store.root / CURRENT)
            finally:
                if os.path.exists(name):
                    os.unlink(name)

    def files(self, dataset):
        """Physical names + revision tokens; never read payloads to find changed days."""
        if self.local:
            return {
                str(p): {
                    "revision": f"{p.stat().st_mtime_ns}:{p.stat().st_size}",
                    "size": p.stat().st_size,
                }
                for p in sorted((self.store.root / dataset).rglob("*.parquet"))
            }
        result = {}
        for page in (
            self.store._client()
            .get_paginator("list_objects_v2")
            .paginate(Bucket=self.store.s.bucket, Prefix=self.key(dataset).rstrip("/") + "/")
        ):
            for obj in page.get("Contents", []):
                if obj["Key"].endswith(".parquet"):
                    result[f"s3://{self.store.s.bucket}/{obj['Key']}"] = {
                        "revision": obj["ETag"],
                        "size": obj["Size"],
                    }
        return result

    def generation(self, name):
        relative = "_typed/generations/" + name
        if self.local:
            child = open_store(str(self.store.root / relative))
            child.ensure()
            return child
        uri = urlsplit(self.store.uri)
        return open_store(urlunsplit(uri._replace(path=uri.path.rstrip("/") + "/" + relative)))
