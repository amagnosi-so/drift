from __future__ import annotations

import argparse
import sys
from dataclasses import replace

from driftpkg.app import DriftApp
from driftpkg.config import add_download_arguments, config_from_args
from driftpkg.deep_unpack import default_worker_count, run as deep_unpack_run
from driftpkg.registry import RegistryClient
from driftpkg.selection import apply_interactive_filters, prompt_repositories
from driftpkg.session import make_session

_SUBCOMMANDS = frozenset({"download", "deep-unpack"})


def _normalize_argv(argv: list[str] | None) -> list[str]:
    av = list(sys.argv[1:] if argv is None else argv)
    if (
        av
        and av[0] not in _SUBCOMMANDS
        and av[0] not in ("-h", "--help")
    ):
        av = ["download", *av]
    return av


def build_root_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="drift",
        description="Docker registry tools: dump images or recursively unpack archives.",
    )
    sub = root.add_subparsers(dest="command", required=True)

    dl = sub.add_parser(
        "download",
        help="Dump images from a Docker registry (default when the first argument is not a subcommand).",
        description="Docker Registry Dumper + Rebuilder (robust, configurable)",
    )
    add_download_arguments(dl)

    dup = sub.add_parser(
        "deep-unpack",
        help="Recursively unpack nested ZIP/TAR/TAR.GZ/TGZ archives.",
        description=(
            "Recursively unpack ZIP/TAR/TAR.GZ/TGZ archives with multi-pass per level."
        ),
    )
    dup.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Root path to scan (default: current directory)",
    )
    dup.add_argument(
        "-d",
        "--depth",
        type=int,
        default=4,
        help="Max archive nesting depth",
    )
    dup.add_argument(
        "-w",
        "--workers",
        type=int,
        default=default_worker_count(),
        help="Number of parallel workers",
    )
    dup.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose logging",
    )

    return root


def main(argv: list[str] | None = None) -> int:
    argv = _normalize_argv(argv)
    parser = build_root_parser()
    ns = parser.parse_args(argv)

    if ns.command == "deep-unpack":
        return deep_unpack_run(
            ns.path,
            depth=ns.depth,
            workers=ns.workers,
            verbose=ns.verbose,
        )

    config = config_from_args(ns)
    session = make_session(config)

    if ns.repos is None:
        reg = RegistryClient(config.registry, session)
        catalog = reg.get_catalog()
        config = replace(config, repo_filter=prompt_repositories(catalog))

    if ns.interactive:
        config = apply_interactive_filters(
            config, session, tags_arg_present=ns.tags is not None
        )

    app = DriftApp(config, session)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
