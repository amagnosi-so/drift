from __future__ import annotations

import argparse
import re
from dataclasses import dataclass


_BYTE_SUFFIX_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?)B?\s*$", re.IGNORECASE
)


def parse_byte_size(value: str) -> int:
    """Parse a size like '5242880', '5M', '512K', '1.5G' into bytes."""
    value = value.strip()
    if value.isdigit():
        return int(value)
    m = _BYTE_SUFFIX_RE.match(value)
    if not m:
        raise argparse.ArgumentTypeError(
            f"invalid size {value!r}; use an integer or suffix K/M/G/T (e.g. 5M)"
        )
    num = float(m.group(1))
    suf = m.group(2).upper()
    mult = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}[suf]
    out = int(num * mult)
    if out < 0:
        raise argparse.ArgumentTypeError("size must be non-negative")
    return out


def parse_positive_float(value: str) -> float:
    x = float(value)
    if x <= 0 or x > 1:
        raise argparse.ArgumentTypeError("expected a float in (0, 1]")
    return x


@dataclass(frozen=True)
class DriftConfig:
    registry: str
    output: str
    proxy: str | None
    insecure: bool
    rebuild: bool
    image_prefix: str
    verify_diffid: bool
    fragmented: bool
    chunk_size: int
    base_workers: int
    max_workers: int
    max_bytes_per_sec: int
    base_size_per_sec: int
    partial_ratio: float
    min_partial: int
    subrequest_timeout: float
    stream_chunk: int
    chunk_retries: int
    user_agent: str
    blob_timeout: float
    blob_max_retries: int
    jitter_enabled: bool
    jitter_inline_probability: float
    jitter_inline_min_ms: float
    jitter_inline_max_ms: float
    jitter_between_sub_min_ms: float
    jitter_between_sub_max_ms: float
    jitter_inner_near_min_ms: float
    jitter_inner_near_max_ms: float
    jitter_worker_near_min_ms: float
    jitter_worker_near_max_ms: float
    jitter_pool_loop_min_ms: float
    jitter_pool_loop_max_ms: float
    micro_backoff_min_s: float
    micro_backoff_max_s: float


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Docker Registry Dumper + Rebuilder (robust, configurable)"
    )

    p.add_argument(
        "-r",
        "--registry",
        required=True,
        help="Registry base URL (no trailing slash required)",
    )
    p.add_argument(
        "-o",
        "--output",
        default="dump",
        help="Output directory root",
    )
    p.add_argument("-p", "--proxy", help="HTTP(S) proxy URL")
    p.add_argument(
        "-k",
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification",
    )
    p.add_argument(
        "-b",
        "--rebuild",
        action="store_true",
        help="Rebuild images with docker import after extract",
    )
    p.add_argument(
        "-n",
        "--image-prefix",
        default="recovered",
        help="Docker image name prefix when using --rebuild",
    )
    p.add_argument(
        "-D",
        "--verify-diffid",
        action="store_true",
        help="Verify uncompressed layer hashes (reserved / future use)",
    )

    frag = p.add_mutually_exclusive_group()
    frag.add_argument(
        "-f",
        "--fragmented",
        dest="fragmented",
        action="store_true",
        help="Request a byte range but only consume part of the response (then continue)",
    )
    frag.add_argument(
        "-N",
        "--no-fragmented",
        dest="fragmented",
        action="store_false",
        help="Consume each requested range fully (no partial read trick)",
    )
    p.set_defaults(fragmented=True)

    p.add_argument(
        "-c",
        "--chunk-size",
        type=parse_byte_size,
        default=parse_byte_size("5M"),
        help="Parallel part / sub-request size (bytes or suffix K/M/G)",
    )
    p.add_argument(
        "-w",
        "--base-workers",
        type=int,
        default=1,
        help="Initial worker count for parallel blob download",
    )
    p.add_argument(
        "-W",
        "--max-workers",
        type=int,
        default=1,
        help="Maximum parallel workers cap for adaptive ranged download",
    )
    p.add_argument(
        "-M",
        "--max-bytes-per-sec",
        type=parse_byte_size,
        default=parse_byte_size("2M"),
        help="Adaptive throttle ceiling (bytes/s)",
    )
    p.add_argument(
        "-R",
        "--base-rate",
        type=parse_byte_size,
        default=parse_byte_size("1M"),
        help="Starting throttle rate for adaptive controller (bytes/s)",
    )
    p.add_argument(
        "-P",
        "--partial-ratio",
        type=parse_positive_float,
        default=0.5,
        help="With --fragmented: fraction of each sub-request range to read (0-1]",
    )
    p.add_argument(
        "-m",
        "--min-partial",
        type=parse_byte_size,
        default=parse_byte_size("512K"),
        help="With --fragmented: minimum bytes to read per sub-request",
    )
    p.add_argument(
        "-t",
        "--subrequest-timeout",
        type=float,
        default=15.0,
        help="Timeout (seconds) for each ranged GET in parallel download",
    )
    p.add_argument(
        "-s",
        "--stream-chunk",
        type=int,
        default=8192,
        help="iter_content chunk size for streaming reads",
    )
    p.add_argument(
        "-e",
        "--chunk-retries",
        type=int,
        default=5,
        help="Retries per parallel chunk on failure",
    )
    p.add_argument(
        "-u",
        "--user-agent",
        default="registry-dumper/3.0",
        help="User-Agent header for registry HTTP requests",
    )
    p.add_argument(
        "-T",
        "--blob-timeout",
        type=float,
        default=30.0,
        help="Timeout (seconds) for single-blob (non-parallel) download",
    )
    p.add_argument(
        "-a",
        "--blob-max-retries",
        type=int,
        default=60,
        help="Max attempts for single-blob download",
    )

    p.add_argument(
        "-j",
        "--jitter",
        dest="jitter_enabled",
        action="store_true",
        help="Enable randomized sleeps (default: on)",
    )
    p.add_argument(
        "-J",
        "--no-jitter",
        dest="jitter_enabled",
        action="store_false",
        help="Disable optional jitter sleeps",
    )
    p.set_defaults(jitter_enabled=True)

    p.add_argument(
        "-q",
        "--jitter-inline-prob",
        type=float,
        default=0.05,
        help="Probability of micro-jitter inside streamed read loop",
    )
    p.add_argument("-i", "--jitter-inline-min-ms", type=float, default=5.0)
    p.add_argument("-I", "--jitter-inline-max-ms", type=float, default=30.0)
    p.add_argument("-d", "--jitter-between-sub-min-ms", type=float, default=50.0)
    p.add_argument("-E", "--jitter-between-sub-max-ms", type=float, default=200.0)
    p.add_argument("-l", "--jitter-inner-near-min-ms", type=float, default=200.0)
    p.add_argument("-L", "--jitter-inner-near-max-ms", type=float, default=800.0)
    p.add_argument("-v", "--jitter-worker-near-min-ms", type=float, default=300.0)
    p.add_argument("-X", "--jitter-worker-near-max-ms", type=float, default=1200.0)
    p.add_argument("-y", "--jitter-pool-loop-min-ms", type=float, default=200.0)
    p.add_argument("-Y", "--jitter-pool-loop-max-ms", type=float, default=800.0)
    p.add_argument("-A", "--micro-backoff-min-s", type=float, default=1.0)
    p.add_argument("-C", "--micro-backoff-max-s", type=float, default=3.0)

    return p


def config_from_args(ns: argparse.Namespace) -> DriftConfig:
    if ns.base_workers < 1:
        raise SystemExit("--base-workers must be >= 1")
    if ns.max_workers < 1:
        raise SystemExit("--max-workers must be >= 1")
    if ns.stream_chunk < 1:
        raise SystemExit("--stream-chunk must be >= 1")
    if ns.chunk_retries < 1:
        raise SystemExit("--chunk-retries must be >= 1")
    if ns.max_workers < ns.base_workers:
        raise SystemExit("--max-workers must be >= --base-workers")

    return DriftConfig(
        registry=ns.registry.rstrip("/"),
        output=ns.output,
        proxy=ns.proxy,
        insecure=ns.insecure,
        rebuild=ns.rebuild,
        image_prefix=ns.image_prefix,
        verify_diffid=ns.verify_diffid,
        fragmented=ns.fragmented,
        chunk_size=ns.chunk_size,
        base_workers=ns.base_workers,
        max_workers=ns.max_workers,
        max_bytes_per_sec=ns.max_bytes_per_sec,
        base_size_per_sec=ns.base_rate,
        partial_ratio=ns.partial_ratio,
        min_partial=ns.min_partial,
        subrequest_timeout=ns.subrequest_timeout,
        stream_chunk=ns.stream_chunk,
        chunk_retries=ns.chunk_retries,
        user_agent=ns.user_agent,
        blob_timeout=ns.blob_timeout,
        blob_max_retries=ns.blob_max_retries,
        jitter_enabled=ns.jitter_enabled,
        jitter_inline_probability=ns.jitter_inline_prob,
        jitter_inline_min_ms=ns.jitter_inline_min_ms,
        jitter_inline_max_ms=ns.jitter_inline_max_ms,
        jitter_between_sub_min_ms=ns.jitter_between_sub_min_ms,
        jitter_between_sub_max_ms=ns.jitter_between_sub_max_ms,
        jitter_inner_near_min_ms=ns.jitter_inner_near_min_ms,
        jitter_inner_near_max_ms=ns.jitter_inner_near_max_ms,
        jitter_worker_near_min_ms=ns.jitter_worker_near_min_ms,
        jitter_worker_near_max_ms=ns.jitter_worker_near_max_ms,
        jitter_pool_loop_min_ms=ns.jitter_pool_loop_min_ms,
        jitter_pool_loop_max_ms=ns.jitter_pool_loop_max_ms,
        micro_backoff_min_s=ns.micro_backoff_min_s,
        micro_backoff_max_s=ns.micro_backoff_max_s,
    )


def parse_config(argv: list[str] | None = None) -> DriftConfig:
    p = build_parser()
    ns = p.parse_args(argv)
    return config_from_args(ns)
