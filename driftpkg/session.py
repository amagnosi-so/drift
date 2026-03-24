from __future__ import annotations

import requests

from driftpkg.config import DriftConfig


def make_session(config: DriftConfig) -> requests.Session:
    s = requests.Session()
    if config.proxy:
        s.proxies = {"http": config.proxy, "https": config.proxy}
    s.verify = not config.insecure
    s.headers.update({"User-Agent": config.user_agent})
    return s
