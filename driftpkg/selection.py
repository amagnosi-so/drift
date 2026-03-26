from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from driftpkg.config import DriftConfig
from driftpkg.registry import RegistryClient

NameFilter = frozenset[str] | None


def parse_pick_line(line: str, items: list[str]) -> list[str]:
    """Parse interactive selection: indices, ranges, or literal names (comma-separated)."""
    line = line.strip()
    if not line or line.lower() in ("all", "*"):
        return list(items)
    out: list[str] = []
    seen: set[str] = set()
    for raw in line.split(","):
        raw = raw.strip()
        if not raw:
            continue
        if "-" in raw and all(p.strip().isdigit() for p in raw.split("-", 1)):
            lo_s, hi_s = raw.split("-", 1)
            lo, hi = int(lo_s.strip()), int(hi_s.strip())
            if lo > hi:
                lo, hi = hi, lo
            for idx in range(lo, hi + 1):
                if 1 <= idx <= len(items):
                    name = items[idx - 1]
                    if name not in seen:
                        seen.add(name)
                        out.append(name)
        elif raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(items):
                name = items[idx - 1]
                if name not in seen:
                    seen.add(name)
                    out.append(name)
        else:
            if raw not in seen:
                seen.add(raw)
                out.append(raw)
    return out


def prompt_repositories(catalog: list[str]) -> NameFilter:
    if not catalog:
        print("[!] Catalog is empty.")
        return frozenset()
    print("\nRepositories in catalog:")
    for i, name in enumerate(catalog, 1):
        print(f"  {i:3}) {name}")
    print("Select: numbers and/or ranges (e.g. 1,3,5-7), exact names, or 'all' / empty = all")
    try:
        line = input("repos> ").strip()
    except EOFError:
        print("[!] No input (EOF); treating as no repositories selected.")
        return frozenset()
    if not line or line.lower() in ("all", "*"):
        return None
    picked = parse_pick_line(line, catalog)
    cat_set = frozenset(catalog)
    resolved: list[str] = []
    seen: set[str] = set()
    for n in picked:
        if n in cat_set:
            if n not in seen:
                seen.add(n)
                resolved.append(n)
        else:
            print(f"[!] Unknown repository {n!r} (ignored)")
    if not resolved:
        print("[!] No valid repositories selected.")
        return frozenset()
    return frozenset(resolved)


def _interactive_tag_pick(
    repos: list[str],
    get_tags: Callable[[str], list[str]],
) -> NameFilter:
    if not repos:
        return frozenset()
    union: list[str] = []
    seen: set[str] = set()
    for r in repos:
        for t in get_tags(r):
            if t not in seen:
                seen.add(t)
                union.append(t)
    union.sort(key=lambda x: x.lower())
    if not union:
        print("[!] No tags found for the selected repositories.")
        return frozenset()
    print("\nTags (union across selected repositories):")
    for i, t in enumerate(union, 1):
        print(f"  {i:3}) {t}")
    print("Select: numbers and/or ranges, exact tag names, or 'all' / empty = all")
    line = input("tags> ").strip()
    if not line or line.lower() in ("all", "*"):
        return None
    picked = parse_pick_line(line, union)
    tag_set = frozenset(union)
    resolved: list[str] = []
    seen.clear()
    for t in picked:
        if t in tag_set:
            if t not in seen:
                seen.add(t)
                resolved.append(t)
        else:
            print(f"[!] Unknown tag {t!r} (ignored)")
    if not resolved:
        print("[!] No valid tags selected.")
        return frozenset()
    return frozenset(resolved)


def apply_interactive_filters(
    config: DriftConfig,
    session,
    *,
    tags_arg_present: bool,
) -> DriftConfig:
    """With ``--interactive``, prompt for tags if ``--tag`` was not on the CLI.

    Repository scope is chosen earlier (CLI ``--repo`` / ``all``, or the mandatory pre-plan
    prompt when ``--repo`` is omitted).
    """
    if tags_arg_present:
        return config

    reg = RegistryClient(config.registry, session)
    catalog = reg.get_catalog()
    rf = config.repo_filter
    if rf is None:
        repos_for_tag_prompt = list(catalog)
    elif not rf:
        repos_for_tag_prompt = []
    else:
        repos_for_tag_prompt = sorted(r for r in catalog if r in rf)

    if not repos_for_tag_prompt:
        tf: NameFilter = frozenset()
    else:
        tf = _interactive_tag_pick(repos_for_tag_prompt, reg.get_tags)

    return replace(config, tag_filter=tf)
