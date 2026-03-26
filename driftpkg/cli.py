from __future__ import annotations

from dataclasses import replace

from driftpkg.app import DriftApp
from driftpkg.config import build_parser, config_from_args
from driftpkg.registry import RegistryClient
from driftpkg.selection import apply_interactive_filters, prompt_repositories
from driftpkg.session import make_session


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    ns = parser.parse_args(argv)
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


if __name__ == "__main__":
    main()
