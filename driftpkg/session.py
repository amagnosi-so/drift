from __future__ import annotations

import requests
import urllib3

from driftpkg.config import DriftConfig


def make_session(config: DriftConfig) -> requests.Session:
    if config.insecure:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    s = requests.Session()
    if config.proxy:
        s.proxies = {"http": config.proxy, "https": config.proxy}
    s.verify = not config.insecure
    s.headers.update({"User-Agent": config.user_agent})
    return s
