from __future__ import annotations

import json
import os

from driftpkg.config import DriftConfig
from driftpkg.downloader import BlobDownloader
from driftpkg.extract import extract_layer
from driftpkg.rebuild import build_docker_image
from driftpkg.registry import RegistryClient
from driftpkg.utils import mkdir


class DriftApp:
    def __init__(self, config: DriftConfig, session):
        self._cfg = config
        self._registry = RegistryClient(config.registry, session)
        self._downloader = BlobDownloader(config, session)

    def download_config(self, repo: str, digest: str, archive_dir: str, base_dir: str) -> str:
        blob = self._downloader.download_blob(repo, digest, archive_dir)
        config_path = os.path.join(base_dir, "config.json")
        with open(blob, "rb") as f:
            data = json.load(f)
        with open(config_path, "w") as f:
            json.dump(data, f, indent=2)
        return config_path

    def process_image(self, repo: str, tag: str) -> None:
        print(f"\n=== {repo}:{tag} ===")

        base = os.path.join(self._cfg.output, repo, tag)
        archive = os.path.join(base, "archive")
        fs = os.path.join(base, "fs")
        marker = os.path.join(base, ".layers")
        quarantine = os.path.join(base, "quarantine")

        mkdir(archive)
        mkdir(fs)
        mkdir(marker)

        manifest = self._registry.get_manifest(repo, tag)
        with open(os.path.join(base, "manifest.json"), "w") as f:
            json.dump(manifest, f, indent=2)

        config_path = None
        config_digest = manifest.get("config", {}).get("digest")
        if config_digest:
            config_path = self.download_config(repo, config_digest, archive, base)

        for layer in manifest.get("layers", []):
            blob = self._downloader.parallel_download_blob(repo, layer["digest"], archive)
            extract_layer(blob, fs, marker, quarantine)

        print(f"[✓] FS ready: {fs}")

        if self._cfg.rebuild and config_path:
            name = f"{self._cfg.image_prefix}/{repo}:{tag}".lower()
            build_docker_image(fs, config_path, name)

    def run(self) -> None:
        mkdir(self._cfg.output)
        repos = self._registry.get_catalog()
        print("[+] Repos:", repos)
        for repo in repos:
            for tag in self._registry.get_tags(repo):
                self.process_image(repo, tag)
