from __future__ import annotations

import json
import math
import os
import random
import shutil
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import requests
from tqdm import tqdm

from driftpkg.config import DriftConfig
from driftpkg.controller import AdaptiveController
from driftpkg.registry_paths import encode_repo
from driftpkg.utils import mkdir, safe_digest, verify_blob


class BlobDownloader:
    """Parallel ranged blob download with optional fragmented sub-reads."""

    def __init__(self, config: DriftConfig, session: requests.Session):
        self._cfg = config
        self._session = session
        self._controller = AdaptiveController(
            base_workers=config.base_workers,
            max_workers=config.max_workers,
            base_rate=config.base_size_per_sec,
            max_rate=config.max_bytes_per_sec,
        )
        self._progress_lock = threading.Lock()
        self._global_progress: tqdm | None = None

    def _digest_short(self, digest: str) -> str:
        return digest.split(":")[-1][:12] if ":" in digest else digest[:12]

    def _refresh_global_postfix(
        self, digest: str, num_parts: int, n_completed: int
    ) -> None:
        with self._progress_lock:
            bar = self._global_progress
            if bar is None:
                return
            bar.set_description(f"blob:{self._digest_short(digest)}", refresh=False)
            bar.set_postfix_str(f"parts {n_completed}/{num_parts}", refresh=True)

    def _jitter(self, min_ms: float, max_ms: float) -> None:
        if not self._cfg.jitter_enabled:
            return
        time.sleep(random.uniform(min_ms / 1000.0, max_ms / 1000.0))

    def _maybe_inline_jitter(self) -> None:
        if not self._cfg.jitter_enabled:
            return
        if random.random() < self._cfg.jitter_inline_probability:
            self._jitter(self._cfg.jitter_inline_min_ms, self._cfg.jitter_inline_max_ms)

    def _throttled_write(self, f, chunk: bytes, start_time: float, written: int) -> int:
        f.write(chunk)
        written += len(chunk)
        rate = self._controller.get_rate()
        elapsed = time.time() - start_time
        if elapsed > 0:
            current = written / elapsed
            if current > rate:
                sleep_time = (written / rate) - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)
        return written

    def _read_limit_for_subrequest(self, request_size: int) -> int:
        if self._cfg.fragmented:
            return max(int(request_size * self._cfg.partial_ratio), self._cfg.min_partial)
        return request_size

    def download_range(
        self,
        url: str,
        start: int,
        end: int,
        out_path: str,
        idx: int,
        num_parts: int,
        on_subrange_error: Callable[[], None] | None = None,
    ) -> bool:
        total_chunk_size = end - start + 1
        chunk_sz = self._cfg.chunk_size
        part_label = f"part {idx + 1}/{max(num_parts, 1)}"

        for attempt in range(self._cfg.chunk_retries):
            current = start
            restart_whole_part = False
            try:
                start_time = time.time()
                written = 0
                sub_requests = 0

                with open(out_path, "wb") as f, tqdm(
                    total=total_chunk_size,
                    unit="B",
                    unit_scale=True,
                    desc=part_label,
                    position=idx % 10 + 1,
                    leave=False,
                ) as pbar:
                    while current <= end:
                        remaining = end - current + 1
                        request_size = min(remaining, chunk_sz)
                        req_end = current + request_size - 1
                        headers = {"Range": f"bytes={current}-{req_end}"}
                        read_limit = min(
                            self._read_limit_for_subrequest(request_size), request_size
                        )
                        bytes_read = 0
                        sub_requests += 1
                        pbar.set_postfix(sub=sub_requests, refresh=False)

                        try:
                            with self._session.get(
                                url,
                                headers=headers,
                                stream=True,
                                timeout=self._cfg.subrequest_timeout,
                            ) as r:
                                if r.status_code not in (200, 206):
                                    raise RuntimeError(f"Bad status {r.status_code}")

                                for chunk in r.iter_content(self._cfg.stream_chunk):
                                    if not chunk:
                                        continue
                                    if bytes_read + len(chunk) > read_limit:
                                        chunk = chunk[: read_limit - bytes_read]

                                    written = self._throttled_write(f, chunk, start_time, written)
                                    bytes_read += len(chunk)
                                    pbar.update(len(chunk))

                                    with self._progress_lock:
                                        if self._global_progress is not None:
                                            self._global_progress.update(len(chunk))

                                    if bytes_read >= read_limit:
                                        break

                                    self._maybe_inline_jitter()

                        except Exception as e:
                            if on_subrange_error:
                                on_subrange_error()
                            rec = self._cfg.subrange_recover
                            print(
                                f"[!] Subrange error part {idx} (blob offset {current}, "
                                f"{written}/{total_chunk_size} B in this part): {e} "
                                f"[subrange recover: {rec}]"
                            )
                            self._controller.record_error()
                            time.sleep(
                                random.uniform(
                                    self._cfg.micro_backoff_min_s,
                                    self._cfg.micro_backoff_max_s,
                                )
                            )
                            if rec == "restart-part":
                                restart_whole_part = True
                                break
                            part_off = current - start
                            f.flush()
                            f.seek(part_off)
                            f.truncate()
                            written = part_off
                            try:
                                pbar.reset(total=total_chunk_size)
                                pbar.update(part_off)
                            except Exception:
                                pbar.n = part_off
                                pbar.last_print_n = part_off
                                pbar.refresh()
                            continue

                        current += bytes_read
                        self._jitter(
                            self._cfg.jitter_between_sub_min_ms,
                            self._cfg.jitter_between_sub_max_ms,
                        )

                        progress_ratio = written / total_chunk_size if total_chunk_size else 0
                        if progress_ratio > 0.8:
                            self._jitter(
                                self._cfg.jitter_inner_near_min_ms,
                                self._cfg.jitter_inner_near_max_ms,
                            )

                    if restart_whole_part:
                        continue

                self._controller.record_success()
                return True

            except Exception as e:
                print(f"[!] Part {idx} outer error: {e}")
                self._controller.record_error()
                backoff = min(2**attempt, 30)
                time.sleep(backoff + random.uniform(0.5, 2.0))

        return False

    def parallel_download_blob(self, repo: str, digest: str, archive_dir: str) -> str:
        filename = safe_digest(digest)
        final_path = os.path.join(archive_dir, filename)
        parts_dir = final_path + ".parts"
        meta_path = final_path + ".meta.json"
        path_repo = encode_repo(repo)
        url = f"{self._cfg.registry.rstrip('/')}/v2/{path_repo}/blobs/{digest}"
        chunk_sz = self._cfg.chunk_size

        mkdir(parts_dir)

        if os.path.exists(final_path):
            print(f"[~] Found existing blob: {digest}")
            if verify_blob(final_path, digest):
                print(f"[✓] Already valid: {digest}")
                return final_path
            print("[!] Existing blob corrupted → removing")
            os.remove(final_path)

        head = self._session.head(url)
        head.raise_for_status()
        total_size = int(head.headers.get("Content-Length", 0))

        num_parts = math.ceil(total_size / chunk_sz) if total_size else 0
        print(f"[+] Adaptive download {digest} ({num_parts} parallel parts)")

        def expected_part_bytes(i: int) -> int:
            off = i * chunk_sz
            return min(chunk_sz, max(0, total_size - off))

        completed: set[int] = set()
        problematic_parts: set[int] = set()
        if os.path.exists(meta_path):
            try:
                with open(meta_path) as f:
                    meta = json.load(f)
                saved = set(meta.get("completed", []))
                problematic_parts = set(meta.get("problematic", []))
                for i in saved:
                    part_path = os.path.join(parts_dir, f"part_{i}")
                    if os.path.exists(part_path):
                        completed.add(i)
            except Exception:
                print("[!] Corrupted metadata → ignoring")

        lock = threading.Lock()
        digest_retry_round = 0

        def _flush_meta_unlocked() -> None:
            with open(meta_path, "w") as mf:
                json.dump(
                    {
                        "completed": sorted(completed),
                        "problematic": sorted(problematic_parts),
                    },
                    mf,
                )

        def persist_meta() -> None:
            with lock:
                _flush_meta_unlocked()

        with self._progress_lock:
            if self._global_progress is not None:
                self._global_progress.close()
            self._global_progress = tqdm(
                total=total_size,
                unit="B",
                unit_scale=True,
                desc=f"blob:{self._digest_short(digest)}",
                position=0,
                leave=True,
            )
            self._global_progress.set_postfix_str(
                f"parts {len(completed)}/{num_parts}", refresh=False
            )

        downloaded_bytes = 0
        for i in completed:
            downloaded_bytes += expected_part_bytes(i)

        if self._global_progress:
            self._global_progress.update(downloaded_bytes)
        self._refresh_global_postfix(digest, num_parts, len(completed))

        while True:
            indices = list(range(num_parts))
            random.shuffle(indices)

            def worker(i: int) -> None:
                if i in completed:
                    return
                start = i * chunk_sz
                end = min(start + chunk_sz - 1, total_size - 1)
                part_path = os.path.join(parts_dir, f"part_{i}")

                def mark_problematic() -> None:
                    with lock:
                        problematic_parts.add(i)
                        _flush_meta_unlocked()

                success = self.download_range(
                    url,
                    start,
                    end,
                    part_path,
                    i,
                    num_parts,
                    on_subrange_error=mark_problematic,
                )
                if success:
                    if not os.path.exists(part_path):
                        print(f"[!] Missing part file {i}")
                        return
                    exp = expected_part_bytes(i)
                    got = os.path.getsize(part_path)
                    if got != exp:
                        print(
                            f"[!] Part {i} size {got} B != expected {exp} B — will re-download"
                        )
                        try:
                            os.remove(part_path)
                        except OSError:
                            pass
                        with lock:
                            problematic_parts.add(i)
                            completed.discard(i)
                            _flush_meta_unlocked()
                        return
                    with lock:
                        completed.add(i)
                        _flush_meta_unlocked()
                    self._refresh_global_postfix(digest, num_parts, len(completed))

                progress = len(completed) / num_parts if num_parts else 1.0
                if progress > 0.8:
                    self._jitter(
                        self._cfg.jitter_worker_near_min_ms,
                        self._cfg.jitter_worker_near_max_ms,
                    )

            while len(completed) < num_parts:
                workers = self._controller.get_workers()
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    executor.map(worker, indices)
                print(
                    f"[i] Parts done: {len(completed)}/{num_parts} | workers={workers} | "
                    f"throttle={self._controller.get_rate() / 1024 / 1024:.2f}MiB/s"
                )
                self._jitter(
                    self._cfg.jitter_pool_loop_min_ms,
                    self._cfg.jitter_pool_loop_max_ms,
                )

            print("[+] Verifying parts before merge...")

            missing: list[int] = []
            for i in range(num_parts):
                part_path = os.path.join(parts_dir, f"part_{i}")
                if not os.path.exists(part_path) or os.path.getsize(part_path) == 0:
                    missing.append(i)

            if missing:
                print(
                    f"[!] Missing parts {missing}: deleting {parts_dir!r} and meta.json; "
                    f"restarting entire blob download from scratch for {digest}"
                )
                shutil.rmtree(parts_dir, ignore_errors=True)
                if os.path.exists(meta_path):
                    os.remove(meta_path)
                return self.parallel_download_blob(repo, digest, archive_dir)

            wrong_sz: list[tuple[int, int, int]] = []
            for i in range(num_parts):
                pp = os.path.join(parts_dir, f"part_{i}")
                exp = expected_part_bytes(i)
                got = os.path.getsize(pp)
                if got != exp:
                    wrong_sz.append((i, got, exp))

            if wrong_sz:
                print(
                    f"[!] Part size mismatch before merge: {wrong_sz[:8]}"
                    f"{' ...' if len(wrong_sz) > 8 else ''} — scheduling re-download"
                )
                digest_retry_round += 1
                for i, _, _ in wrong_sz:
                    problematic_parts.add(i)
                    completed.discard(i)
                    pth = os.path.join(parts_dir, f"part_{i}")
                    if os.path.exists(pth):
                        os.remove(pth)
                if os.path.exists(final_path):
                    os.remove(final_path)
                persist_meta()
                if digest_retry_round > self._cfg.digest_mismatch_max_rounds:
                    print(
                        "[!] Too many size-fix rounds → full reset (remove all parts + final)"
                    )
                    shutil.rmtree(parts_dir, ignore_errors=True)
                    if os.path.exists(meta_path):
                        os.remove(meta_path)
                    if os.path.exists(final_path):
                        os.remove(final_path)
                    mkdir(parts_dir)
                    return self.parallel_download_blob(repo, digest, archive_dir)
                continue

            print("[+] Merging parts...")
            with open(final_path, "wb") as out:
                for i in range(num_parts):
                    with open(os.path.join(parts_dir, f"part_{i}"), "rb") as f:
                        shutil.copyfileobj(f, out)

            if verify_blob(final_path, digest):
                with lock:
                    problematic_parts.clear()
                    _flush_meta_unlocked()
                print(f"[✓] Completed: {digest}")
                return final_path

            digest_retry_round += 1
            want_partial = (
                self._cfg.digest_mismatch_recover == "retry-problematic"
                and digest_retry_round <= self._cfg.digest_mismatch_max_rounds
            )
            if not want_partial:
                print(
                    f"[!] Merged blob failed digest verify (round {digest_retry_round}); "
                    f"strategy={self._cfg.digest_mismatch_recover}. "
                    f"Full reset: rm {parts_dir!r}, final artifact, meta.json"
                )
                if os.path.exists(parts_dir):
                    shutil.rmtree(parts_dir)
                if os.path.exists(final_path):
                    os.remove(final_path)
                if os.path.exists(meta_path):
                    os.remove(meta_path)
                mkdir(parts_dir)
                return self.parallel_download_blob(repo, digest, archive_dir)

            targets = (
                sorted(problematic_parts) if problematic_parts else list(range(num_parts))
            )
            print(
                f"[!] Merged blob failed digest verify (retry round {digest_retry_round}/"
                f"{self._cfg.digest_mismatch_max_rounds}). Re-downloading part index(es): "
                f"{targets[:30]}{' ...' if len(targets) > 30 else ''}"
            )
            if problematic_parts:
                print(
                    f"    Flagged parts (subrange/transport or size issues): "
                    f"{sorted(problematic_parts)}"
                )
            else:
                print(
                    "    No flagged parts — re-downloading all parts (needed for a consistent merge)."
                )

            for i in targets:
                completed.discard(i)
                pp = os.path.join(parts_dir, f"part_{i}")
                if os.path.exists(pp):
                    os.remove(pp)
            problematic_parts.clear()
            if os.path.exists(final_path):
                os.remove(final_path)
            persist_meta()

    def download_blob(self, repo: str, digest: str, archive_dir: str) -> str:
        filename = safe_digest(digest)
        path = os.path.join(archive_dir, filename)
        path_repo = encode_repo(repo)
        url = f"{self._cfg.registry}/v2/{path_repo}/blobs/{digest}"

        for attempt in range(self._cfg.blob_max_retries):
            try:
                tmp_path = path + ".part"

                if os.path.exists(path):
                    if verify_blob(path, digest):
                        print(f"[=] Already valid: {digest}")
                        return path
                    print("[!] Corrupted existing file → removing")
                    os.remove(path)

                existing_size = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0
                headers: dict[str, str] = {}
                mode = "ab" if existing_size > 0 else "wb"

                if existing_size > 0:
                    headers["Range"] = f"bytes={existing_size}-"
                    print(f"[~] Resume {digest} @ {existing_size}")
                else:
                    print(f"[+] Download {digest}")

                r = self._session.get(
                    url, headers=headers, stream=True, timeout=self._cfg.blob_timeout
                )

                if r.status_code == 416:
                    print("[!] 416 → resetting partial")
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                    if os.path.exists(path):
                        os.remove(path)
                    continue

                if r.status_code not in (200, 206):
                    r.raise_for_status()

                total_hdr = r.headers.get("Content-Length")
                total = None
                if total_hdr:
                    total = int(total_hdr) + (
                        existing_size if r.status_code == 206 else 0
                    )

                with open(tmp_path, mode) as f, tqdm(
                    total=total,
                    initial=existing_size,
                    unit="B",
                    unit_scale=True,
                    desc=digest[:12],
                ) as pbar:
                    for chunk in r.iter_content(self._cfg.stream_chunk):
                        if chunk:
                            f.write(chunk)
                            pbar.update(len(chunk))

                if os.path.exists(path):
                    os.remove(path)
                os.replace(tmp_path, path)

                if not verify_blob(path, digest):
                    print("[!] Hash mismatch → retry")
                    os.remove(path)
                    continue

                return path

            except Exception as e:
                print(f"[!] Attempt {attempt + 1} failed: {e}")
                time.sleep(2)

        raise RuntimeError(f"Download failed: {digest}")
