from __future__ import annotations

from driftpkg.app import DriftApp
from driftpkg.config import build_parser, config_from_args
from driftpkg.session import make_session


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    ns = parser.parse_args(argv)
    config = config_from_args(ns)
    session = make_session(config)
    app = DriftApp(config, session)
    app.run()


if __name__ == "__main__":
    main()
