"""Registry API path encoding (names with '/' must appear as %2F in /v2/<name>/...)."""

from __future__ import annotations

from urllib.parse import quote


def encode_repo(repo: str) -> str:
    """Encode repository name for use inside /v2/<name>/... paths.

    Forward slashes in names (e.g. ``akeyless/gateway``) become ``%2F`` so the path is not
    mistaken for extra segments (which would 404).
    """
    return quote(repo, safe="")


def encode_manifest_reference(ref: str) -> str:
    """Encode tag or digest for .../manifests/<reference> (keep ``:`` for ``sha256:...``)."""
    return quote(ref, safe=":")
