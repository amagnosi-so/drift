from __future__ import annotations

import hashlib
import os


def mkdir(p: str) -> None:
    os.makedirs(p, exist_ok=True)


def safe_digest(d: str) -> str:
    return d.replace(":", "_")


def verify_blob(path: str, digest: str) -> bool:
    expected = digest.split(":")[1]
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest() == expected


def verify_diff_id(blob_path: str, expected: str) -> bool:
    import gzip

    h = hashlib.sha256()
    with gzip.open(blob_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest() == expected


def hash_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
