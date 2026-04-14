from __future__ import annotations

import argparse
import logging
import os
import tarfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="[%(levelname)s] %(message)s",
    )


def archive_type(path: Path) -> str | None:
    n = path.name.lower()
    if n.endswith(".tar.gz"):
        return ".tar.gz"
    if n.endswith(".tgz"):
        return ".tgz"
    if n.endswith(".tar"):
        return ".tar"
    if n.endswith(".zip"):
        return ".zip"
    return None


def is_archive(path: Path) -> bool:
    try:
        return path.is_file() and archive_type(path) is not None
    except Exception:
        return False


def archive_stem(path: Path) -> str:
    n = path.name
    nl = n.lower()
    if nl.endswith(".tar.gz"):
        return n[:-7]
    if nl.endswith(".tgz"):
        return n[:-4]
    if nl.endswith(".tar"):
        return n[:-4]
    if nl.endswith(".zip"):
        return n[:-4]
    return path.stem


def make_output_dir(archive: Path) -> Path:
    base = archive.parent / f"{archive_stem(archive)}_unpacked"
    if not base.exists():
        return base

    i = 1
    while True:
        candidate = archive.parent / f"{archive_stem(archive)}_unpacked_{i}"
        if not candidate.exists():
            return candidate
        i += 1


def is_within(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except Exception:
        return False


def safe_extract_zip(zf: zipfile.ZipFile, out_dir: Path) -> None:
    for info in zf.infolist():
        try:
            target = out_dir / info.filename
            if not is_within(out_dir, target):
                logging.warning("Skipping suspicious ZIP member: %s", info.filename)
                continue
            zf.extract(info, out_dir)
        except Exception as exc:
            logging.error("Failed ZIP member %s: %s", info.filename, exc)


def safe_extract_tar(tf: tarfile.TarFile, out_dir: Path) -> None:
    for member in tf.getmembers():
        try:
            target = out_dir / member.name
            if not is_within(out_dir, target):
                logging.warning("Skipping suspicious TAR member: %s", member.name)
                continue
            if member.isdev():
                logging.warning("Skipping TAR device member: %s", member.name)
                continue
            tf.extract(member, out_dir)
        except Exception as exc:
            logging.error("Failed TAR member %s: %s", member.name, exc)


def extract_archive(archive: Path) -> Path | None:
    kind = archive_type(archive)
    if not kind:
        return None

    out_dir = make_output_dir(archive)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)

        if kind == ".zip":
            with zipfile.ZipFile(archive, "r") as zf:
                safe_extract_zip(zf, out_dir)
        else:
            with tarfile.open(archive, "r:*") as tf:
                safe_extract_tar(tf, out_dir)

        return out_dir
    except Exception as exc:
        logging.error("Failed extracting %s: %s", archive, exc)
        return None


def discover_archives(root: Path) -> list[Path]:
    found: list[Path] = []
    try:
        for p in root.rglob("*"):
            if is_archive(p):
                found.append(p)
    except Exception as exc:
        logging.error("Failed walking %s: %s", root, exc)
    return found


def process_one_level(
    roots: list[Path],
    already_processed: set[Path],
    workers: int,
    level: int,
) -> list[Path]:
    """
    Fully exhaust one nesting level.

    Repeatedly:
      - scan the current roots
      - extract any not-yet-processed archives found there
      - collect extraction output dirs
      - repeat scanning the SAME level until no new archives appear

    Returns the list of output dirs produced at this level, which become
    the roots for the next level.
    """
    next_level_roots: list[Path] = []
    pass_num = 0

    while True:
        pass_num += 1
        batch: list[Path] = []

        for root in roots:
            for archive in discover_archives(root):
                try:
                    resolved = archive.resolve()
                except Exception:
                    resolved = archive

                if resolved not in already_processed:
                    batch.append(archive)

        if not batch:
            logging.info("Level %d exhausted after %d pass(es).", level, pass_num - 1)
            break

        logging.info(
            "Level %d, pass %d: found %d new archive(s).",
            level,
            pass_num,
            len(batch),
        )

        new_dirs: list[Path] = []
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(extract_archive, archive): archive for archive in batch}

            for future in as_completed(futures):
                archive = futures[future]
                try:
                    try:
                        resolved = archive.resolve()
                    except Exception:
                        resolved = archive
                    already_processed.add(resolved)

                    out_dir = future.result()
                    if out_dir is not None:
                        new_dirs.append(out_dir)
                except Exception as exc:
                    logging.error("Unexpected failure on %s: %s", archive, exc)

        # Important:
        # new archives discovered inside the newly extracted dirs belong
        # to the SAME level for the next pass.
        roots.extend(new_dirs)
        next_level_roots.extend(new_dirs)

    return next_level_roots


def recursive_unpack_bfs(root: Path, max_levels: int, workers: int) -> None:
    processed: set[Path] = set()

    if root.is_file():
        current_roots = [root.parent]
    else:
        current_roots = [root]

    for level in range(1, max_levels + 1):
        logging.info("=== Processing level %d/%d ===", level, max_levels)
        next_roots = process_one_level(current_roots, processed, workers, level)

        if not next_roots:
            logging.info("No deeper extracted content after level %d.", level)
            break

        # Move one nesting level deeper:
        # only directories created by the previous level become roots now.
        current_roots = next_roots


def default_worker_count() -> int:
    return min(8, max(2, os.cpu_count() or 4))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Recursively unpack ZIP/TAR/TAR.GZ/TGZ archives with multi-pass per level."
        )
    )
    parser.add_argument("path", nargs="?", default=".", help="Root path to scan")
    parser.add_argument(
        "-d", "--depth", type=int, default=4, help="Max archive nesting depth"
    )
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=default_worker_count(),
        help="Number of parallel workers",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    return parser


def run(
    path: str,
    *,
    depth: int = 4,
    workers: int | None = None,
    verbose: bool = False,
) -> int:
    setup_logging(verbose)

    root = Path(path).expanduser()
    if not root.exists():
        logging.error("Path does not exist: %s", root)
        return 1

    if root.is_file() and not is_archive(root):
        logging.error("Not a supported archive: %s", root)
        return 1

    w = default_worker_count() if workers is None else workers
    recursive_unpack_bfs(root, depth, w)
    logging.info("Done.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(args.path, depth=args.depth, workers=args.workers, verbose=args.verbose)
