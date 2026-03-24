from __future__ import annotations

import json
import math
import os
import random
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from tqdm import tqdm

from driftpkg.config import DriftConfig
from driftpkg.controller import AdaptiveController
from driftpkg.utils import hash_file, mkdir, safe_digest, verify_blob


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
    ) -> bool:
        total_chunk_size = end - start + 1
        current = start
        chunk_sz = self._cfg.chunk_size

        for attempt in range(self._cfg.chunk_retries):
            try:
                start_time = time.time()
                written = 0

                with open(out_path, "wb") as f, tqdm(
                    total=total_chunk_size,
                    unit="B",
                    unit_scale=True,
                    desc=f"chunk-{idx}",
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
                            print(f"[!] Subrange error chunk-{idx}: {e}")
                            self._controller.record_error()
                            time.sleep(
                                random.uniform(
                                    self._cfg.micro_backoff_min_s,
                                    self._cfg.micro_backoff_max_s,
                                )
                            )
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

                self._controller.record_success()
                return True

            except Exception as e:
                print(f"[!] Chunk {idx} error: {e}")
                self._controller.record_error()
                backoff = min(2**attempt, 30)
                time.sleep(backoff + random.uniform(0.5, 2.0))

        return False

    def parallel_download_blob(self, repo: str, digest: str, archive_dir: str) -> str:
        filename = safe_digest(digest)
        final_path = os.path.join(archive_dir, filename)
        parts_dir = final_path + ".parts"
        meta_path = final_path + ".meta.json"
        url = f"{self._cfg.registry}/v2/{repo}/blobs/{digest}"
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

        with self._progress_lock:
            if self._global_progress is None:
                self._global_progress = tqdm(
                    total=total_size,
                    unit="B",
                    unit_scale=True,
                    desc="TOTAL",
                    position=0,
                    leave=True,
                )

        num_parts = math.ceil(total_size / chunk_sz) if total_size else 0
        print(f"[+] Adaptive download {digest} ({num_parts} parts)")

        completed: set[int] = set()
        if os.path.exists(meta_path):
            try:
                with open(meta_path) as f:
                    saved = set(json.load(f).get("completed", []))
                for i in saved:
                    part_path = os.path.join(parts_dir, f"part_{i}")
                    if os.path.exists(part_path):
                        completed.add(i)
            except Exception:
                print("[!] Corrupted metadata → ignoring")

        downloaded_bytes = 0
        for i in completed:
            start = i * chunk_sz
            end = min(start + chunk_sz, total_size)
            downloaded_bytes += end - start

        if self._global_progress:
            self._global_progress.update(downloaded_bytes)

        lock = threading.Lock()
        indices = list(range(num_parts))
        random.shuffle(indices)

        def worker(i: int) -> None:
            if i in completed:
                return
            start = i * chunk_sz
            end = min(start + chunk_sz - 1, total_size - 1)
            part_path = os.path.join(parts_dir, f"part_{i}")
            success = self.download_range(url, start, end, part_path, i)
            if success:
                if not os.path.exists(part_path) or os.path.getsize(part_path) == 0:
                    print(f"[!] Invalid part_{i}")
                    return
                if not hash_file(part_path):
                    print(f"[!] Hash failed chunk {i}")
                    return
                with lock:
                    completed.add(i)
                    with open(meta_path, "w") as f:
                        json.dump({"completed": list(completed)}, f)

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
                f"[i] Progress: {len(completed)}/{num_parts} | workers={workers} "
                f"rate={self._controller.get_rate() / 1024 / 1024:.2f}MB/s"
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
            print(f"[!] Missing parts detected ({len(missing)}) → full reset")
            shutil.rmtree(parts_dir, ignore_errors=True)
            if os.path.exists(meta_path):
                os.remove(meta_path)
            return self.parallel_download_blob(repo, digest, archive_dir)

        print("[+] Merging parts...")
        with open(final_path, "wb") as out:
            for i in range(num_parts):
                with open(os.path.join(parts_dir, f"part_{i}"), "rb") as f:
                    shutil.copyfileobj(f, out)

        if not verify_blob(final_path, digest):
            print("[!] Final mismatch → reset")
            if os.path.exists(parts_dir):
                shutil.rmtree(parts_dir)
            if os.path.exists(final_path):
                os.remove(final_path)
            if os.path.exists(meta_path):
                os.remove(meta_path)
            mkdir(parts_dir)
            return self.parallel_download_blob(repo, digest, archive_dir)

        print(f"[✓] Completed: {digest}")
        return final_path

    def download_blob(self, repo: str, digest: str, archive_dir: str) -> str:
        filename = safe_digest(digest)
        path = os.path.join(archive_dir, filename)
        url = f"{self._cfg.registry}/v2/{repo}/blobs/{digest}"

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
