from __future__ import annotations

from typing import Any

import requests


class RegistryClient:
    def __init__(self, registry: str, session: requests.Session):
        self._registry = registry.rstrip("/")
        self._session = session

    @property
    def base(self) -> str:
        return self._registry

    def get_catalog(self) -> list[str]:
        r = self._session.get(f"{self._registry}/v2/_catalog")
        return r.json().get("repositories", [])

    def get_tags(self, repo: str) -> list[str]:
        r = self._session.get(f"{self._registry}/v2/{repo}/tags/list")
        if r.status_code != 200:
            return []
        return r.json().get("tags", [])

    def get_manifest(self, repo: str, tag: str) -> dict[str, Any]:
        r = self._session.get(
            f"{self._registry}/v2/{repo}/manifests/{tag}",
            headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"},
        )
        r.raise_for_status()
        return r.json()
