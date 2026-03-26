from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import requests

from driftpkg.registry_paths import encode_repo


@dataclass(frozen=True)
class PlannedBlob:
    digest: str
    role: str
    size_bytes: int
    num_parts: int


@dataclass(frozen=True)
class TagPlan:
    repo: str
    tag: str
    manifest: dict[str, Any]
    blobs: tuple[PlannedBlob, ...]

    @property
    def total_bytes(self) -> int:
        return sum(b.size_bytes for b in self.blobs)

    @property
    def total_parts(self) -> int:
        return sum(b.num_parts for b in self.blobs)

    @property
    def n_blobs(self) -> int:
        return len(self.blobs)


def format_bytes(n: int) -> str:
    if n < 0:
        n = 0
    if n < 1024:
        return f"{n} B"
    x = float(n)
    for u in ("KiB", "MiB", "GiB", "TiB"):
        x /= 1024.0
        if x < 1024.0:
            s = f"{x:.2f}".rstrip("0").rstrip(".")
            return f"{s} {u}"
    return f"{x / 1024:.2f} PiB"


def _num_parts(size: int, chunk_size: int) -> int:
    if size <= 0 or chunk_size <= 0:
        return 0
    return math.ceil(size / chunk_size)


def head_blob_size(
    session: requests.Session,
    registry: str,
    repo: str,
    digest: str,
    timeout: float,
) -> int:
    name = encode_repo(repo)
    url = f"{registry.rstrip('/')}/v2/{name}/blobs/{digest}"
    r = session.head(url, timeout=timeout, allow_redirects=True)
    if r.status_code != 200:
        r.raise_for_status()
    cl = r.headers.get("Content-Length")
    if cl is None:
        return 0
    return int(cl)


def build_tag_plan(
    session: requests.Session,
    registry: str,
    repo: str,
    tag: str,
    manifest: dict[str, Any],
    chunk_size: int,
    head_timeout: float,
) -> TagPlan:
    rows: list[PlannedBlob] = []
    cfg = manifest.get("config") or {}
    digest = cfg.get("digest")
    if digest:
        sz = head_blob_size(session, registry, repo, digest, head_timeout)
        rows.append(PlannedBlob(digest, "config", sz, 0))
    for layer in manifest.get("layers") or []:
        d = layer.get("digest")
        if not d:
            continue
        sz = head_blob_size(session, registry, repo, d, head_timeout)
        rows.append(PlannedBlob(d, "layer", sz, _num_parts(sz, chunk_size)))

    return TagPlan(repo=repo, tag=tag, manifest=manifest, blobs=tuple(rows))


def digest_tail(d: str, width: int = 26) -> str:
    if len(d) <= width:
        return d
    return d[: width - 1] + "..."


def print_plans(plans: list[TagPlan], chunk_size: int) -> None:
    print(f"\n{'=' * 72}")
    print("DOWNLOAD PLAN (from manifest + HEAD each blob for size)")
    print(f"Parallel part size (--chunk-size): {format_bytes(chunk_size)}")
    print(f"{'=' * 72}")

    for plan in plans:
        print(f"\n  {plan.repo}:{plan.tag}")
        print(
            f"  {'role':<8} {'digest':<28} {'size':>14} {'parts':>8}  (layer parts = parallel chunks)"
        )
        print(f"  {'-' * 70}")
        for b in plan.blobs:
            pcol = "-" if b.role == "config" else str(b.num_parts)
            print(
                f"  {b.role:<8} {digest_tail(b.digest, 28):<28} "
                f"{format_bytes(b.size_bytes):>14} {pcol:>8}"
            )
        print(
            f"  {'':>8} {'- tag subtotal -':<28} {format_bytes(plan.total_bytes):>14} {plan.total_parts:>8}"
        )

    naive = sum(p.total_bytes for p in plans)
    naive_parts = sum(p.total_parts for p in plans)
    n_tags = len(plans)
    blob_rows = sum(p.n_blobs for p in plans)
    unique_sizes: dict[str, int] = {}
    for p in plans:
        for b in p.blobs:
            if b.digest not in unique_sizes:
                unique_sizes[b.digest] = b.size_bytes
    unique_bytes = sum(unique_sizes.values())

    print(f"\n{'=' * 72}")
    print("SUMMARY")
    print(f"  Tags:              {n_tags}")
    print(f"  Blob rows:         {blob_rows} (manifest entries: config + each layer)")
    print(f"  Naive sum (tags):  {format_bytes(naive)}  (counts a blob again per tag if repeated)")
    print(
        f"  Unique digests:    {len(unique_sizes)} blobs, {format_bytes(unique_bytes)} "
        "(minimum bytes if each digest is stored once)"
    )
    print(
        f"  Layer part slots:  {naive_parts}  (sum of ceil(layer_size/chunk-size); config rows use -)"
    )
    if unique_bytes < naive:
        print(
            "  Note:             Shared layers across tags reduce disk/network after the first fetch."
        )
    print(
        "  Config blobs use a single-stream download (not parallel parts); layer rows show "
        "ceil(size/chunk-size) for the ranged downloader.\n"
    )
