from __future__ import annotations

import json
import os

from driftpkg.config import DriftConfig
from driftpkg.downloader import BlobDownloader
from driftpkg.extract import extract_layer
from driftpkg.plan import TagPlan, build_tag_plan, print_plans
from driftpkg.rebuild import build_docker_image
from driftpkg.registry import RegistryClient
from driftpkg.utils import mkdir


class DriftApp:
    def __init__(self, config: DriftConfig, session):
        self._cfg = config
        self._session = session
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

    def execute_tag_plan(self, plan: TagPlan) -> None:
        repo, tag, manifest = plan.repo, plan.tag, plan.manifest
        print(f"\n=== {repo}:{tag} ===")

        base = os.path.join(self._cfg.output, repo, tag)
        archive = os.path.join(base, "archive")
        fs = os.path.join(base, "fs")
        marker = os.path.join(base, ".layers")
        quarantine = os.path.join(base, "quarantine")

        mkdir(archive)
        mkdir(fs)
        mkdir(marker)

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

    def _repo_tag_pairs(self) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        repos = self._catalog_repos()
        print("[+] Repositories to process:", repos if repos else "(none)")
        for repo in repos:
            tags = self._tags_for_repo(repo)
            if not tags:
                print(f"[!] No matching tags for {repo!r}; skip")
                continue
            for tag in tags:
                pairs.append((repo, tag))
        return pairs

    def _confirm_plan(self) -> bool:
        if self._cfg.assume_yes:
            return True
        try:
            line = input("\nProceed with download and extraction? [y/N]: ").strip().lower()
        except EOFError:
            return False
        return line in ("y", "yes")

    def _catalog_repos(self) -> list[str]:
        repos = self._registry.get_catalog()
        rf = self._cfg.repo_filter
        if rf is None:
            return list(repos)
        if not rf:
            return []
        out = [r for r in repos if r in rf]
        missing = rf.difference(frozenset(repos))
        for name in sorted(missing):
            print(f"[!] Repository not in catalog (skipped): {name}")
        return out

    def _tags_for_repo(self, repo: str) -> list[str]:
        tags = self._registry.get_tags(repo)
        tf = self._cfg.tag_filter
        if tf is None:
            return tags
        if not tf:
            return []
        return [t for t in tags if t in tf]

    def _build_plans(self, pairs: list[tuple[str, str]]) -> list[TagPlan]:
        plans: list[TagPlan] = []
        for repo, tag in pairs:
            manifest = self._registry.get_manifest(repo, tag)
            plans.append(
                build_tag_plan(
                    self._session,
                    self._cfg.registry,
                    repo,
                    tag,
                    manifest,
                    self._cfg.chunk_size,
                    self._cfg.blob_timeout,
                )
            )
        return plans

    def run(self) -> None:
        mkdir(self._cfg.output)
        pairs = self._repo_tag_pairs()
        if not pairs:
            print("[!] Nothing to do (no repository/tag pairs).")
            return

        print("\n[+] Building download plan (manifest + HEAD each blob)…")
        plans = self._build_plans(pairs)
        print_plans(plans, self._cfg.chunk_size)

        if not self._confirm_plan():
            print("Aborted (no downloads started).")
            return

        for plan in plans:
            self.execute_tag_plan(plan)
