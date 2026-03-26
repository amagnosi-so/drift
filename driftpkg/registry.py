from __future__ import annotations

from typing import Any

import requests

from driftpkg.registry_paths import encode_manifest_reference, encode_repo

# Docker v2 + OCI; registries may only store OCI manifests and return 404 if Accept excludes them.
_MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.oci.image.index.v1+json",
    )
)


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
        name = encode_repo(repo)
        r = self._session.get(f"{self._registry}/v2/{name}/tags/list")
        if r.status_code != 200:
            return []
        return r.json().get("tags", [])

    def get_manifest(self, repo: str, tag: str) -> dict[str, Any]:
        name = encode_repo(repo)
        ref = encode_manifest_reference(tag)
        r = self._session.get(
            f"{self._registry}/v2/{name}/manifests/{ref}",
            headers={"Accept": _MANIFEST_ACCEPT},
        )
        r.raise_for_status()
        return r.json()
