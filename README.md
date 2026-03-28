# Drift

**Drift: Docker Registry Image Fetching Tool**

Dump images from a Docker Registry HTTP API (v2), extract layers to a filesystem tree, and optionally rebuild local images with `docker import`.

## Requirements

- Python 3.10+
- For `--rebuild` / `-b`: Docker CLI and `tar` on `PATH`

## Install

From a clone of this repository:

```bash
pip install .
```

Editable install while developing:

```bash
pip install -e .
```

After installation, the console script **`drift`** is on your `PATH`. You can also run the package as a module:

```bash
python -m driftpkg --help
```

### Install with pipx

The project is a normal **PEP 517** app with **`[project.scripts]`** entry points, so **pipx** can install it into an isolated venv and link the executables onto your `PATH`.

From a clone (replace with your path or use `.` from the repo root):

```bash
pipx install /path/to/drift
```

Or directly from GitHub:

```bash
pipx install git+https://github.com/amagnosi-so/drift.git
```

pipx links the **`drift`** command on your `PATH`.

Upgrade / uninstall:

```bash
pipx upgrade drift
pipx uninstall drift
```

If you previously installed under the old name, run `pipx uninstall drift-registry-dumper` once, then `pipx install drift` (or install from a path / git URL as above).

## Usage

Minimal example (TLS verify on, output under `./dump`):

```bash
drift -r https://registry.example.com
```

### Choosing repositories and tags

**Repositories**

- If you **omit** **`--repo` / `-S`**, the tool fetches `_catalog` and **prompts you once** (before the download plan) to pick repositories—same UI as below: numbers, names, or **`all`** / **`*`** / Enter for every repo.
- To process the whole catalog **without** that prompt, pass **`--repo all`** or **`-S '*'`** explicitly (automation-friendly).
- Otherwise **`--repo` / `-S`** (alias **`--image`**) filters to one or more names (repeat or comma-separated).

**Repository names with `/`**

Paths under **`/v2/<name>/...`** encode the name so slashes are **`%2F`** (e.g. `akeyless/gateway` → `akeyless%2Fgateway`). Otherwise the registry treats extra path segments and requests **404**.

**Tags**

- **`--tag` / `-G`** filters tags (repeat, comma-separated, or **`all`** / **`*`**).
- If you omit **`--tag`** and pass **`--interactive` / `-x`**, you get a **tag** picker after the repo step (union of tags across selected repos).
- Omitting **`--tag`** without **`-x`** means **all tags** for each selected repository.

Examples:

```bash
# No -S: prompted for repos; then all tags for those repos
drift -r https://registry.example

# All repos, no repo prompt; all tags
drift -r https://registry.example -S all

# Non-interactive: these repos, all tags
drift -r https://registry.example -S my/app -S other/service

# Interactive tags only (repos already set on CLI)
drift -r https://registry.example -S my/app -x
```

**Note:** **`-i`** is used for **`--jitter-inline-min-ms`**; use **`-S`** / **`--image`** for repository selection.

### Pre-download plan and confirmation

Before any blob is fetched, the tool builds a **plan** for every selected `repo:tag`: it reads each **manifest**, then **`HEAD`s each blob** (config + layers) for `Content-Length`. It prints a **table** with digest, type (config/layer), size, and **parallel part count** per layer (`ceil(size / chunk-size)`). The summary includes a **naive total** across tags and a **unique-digest total** (helpful when several tags share layers—the second tag often skips real network I/O if the blob file already exists and verifies).

By default it then asks: **`Proceed with download and extraction? [y/N]`**. Pass **`--yes` / `-y`** to skip that prompt (for automation or piped runs). The jitter tuning flags **`--jitter-pool-loop-min-ms` / `-z`** and **`--jitter-pool-loop-max-ms` / `-Z`** are used so **`-y` stays reserved for “yes, proceed”.**

## What the tool does

1. **Lists repositories** via `GET /v2/_catalog`, then **tags** per repo via `GET /v2/<encoded-name>/tags/list` (names with `/` are percent-encoded, e.g. `%2F`).
2. For each `repo:tag`, fetches the **manifest** with `Accept` covering **Docker v2** and **OCI** image manifests (and list/index types), so registries that only store OCI manifests still return **200** instead of a misleading **404**.
3. **Downloads the config blob** (image JSON) with a **single streaming** HTTP GET (resume supported via `Range` if a `.part` file exists).
4. **Downloads each layer blob** using a **parallel, ranged** strategy: the blob is split into fixed-size parts; each part is written by one worker using many smaller HTTP sub-requests with `Range: bytes=...` headers until the part’s byte range is complete.
5. **Verifies** each layer part and the merged blob against the registry digest (SHA-256).
6. **Extracts** gzip/tar layers into a per-image filesystem tree (with optional quarantine on failure).
7. If **`--rebuild` / `-b`** is set, builds a local image with **`docker import`** and Dockerfile-like `--change` options derived from the config JSON.

Layer downloads use **resume metadata** (`.meta.json` and `*.parts/`) so interrupted runs can continue.

---

## Behaviour: fragmented vs full range reads

**Layer** download (parallel path) does not always read the entire HTTP body that was asked for in one shot.

- **`--fragmented` / `-f` (default)**  
  For each sub-request, the client sends a `Range` covering up to **`--chunk-size` / `-c`** bytes (or whatever is left in the current part). It then **stops reading the response** after a **limited** number of bytes:
  - `read_limit = max(floor(request_size × partial_ratio), min_partial)`, capped so it never exceeds `request_size`.
  - Defaults: **`--partial-ratio` / `-P`** = `0.5`, **`--min-partial` / `-m`** = `512K`.  
  The file offset advances only by what was actually read, so the next sub-request continues where the stream left off. Many small reads against overlapping/large ranges produce the “request a range, only consume part” pattern.

- **`--no-fragmented` / `-N`**  
  Each sub-request still uses `Range`, but the client reads **the full** `request_size` for that sub-request (no early cut-off). Throughput and server behaviour will differ; this is the straightforward ranged download mode.

**Config blobs** (non-parallel `download_blob`) always stream the full response for each request; **fragmented** options apply only to the **parallel layer** path.

---

## Behaviour: adaptive throttle and workers

During **parallel layer** downloads, an internal **adaptive controller** adjusts:

- **Effective bytes/sec cap** used while writing to disk (`--base-rate` / `-R` → up to `--max-bytes-per-sec` / `-M`).
- **Thread pool size** (`--base-workers` / `-w` → up to `--max-workers` / `-W`).

Rules (simplified):

- On **successful completion** of a large chunk download, every **10** successes, if recent errors are low: rate increases by ~**20%** (capped at `-M`); worker count increases by **1** (capped at `-W`).
- On **errors**: rate halves (floored at **200 KiB/s**); workers decrease by **1** (minimum **1**).

So `-w` / `-W` and `-R` / `-M` shape how aggressive downloads are allowed to become after the tool observes stable progress.

---

## Behaviour: jitter and backoff

**Jitter** is optional random sleep between certain steps so traffic is less perfectly periodic. **`--jitter` / `-j`** turns it on, **`--no-jitter` / `-J`** turns it off (default is **on**).

Where it applies (parallel layer path unless `-J`):

| When | Flags (min / max) | Role |
|------|-------------------|------|
| Occasionally inside the read loop | `-q` / `--jitter-inline-prob`, `-i`, `-I` | Small random pauses while streaming |
| After each successful sub-request | `-d`, `-E` | Pause before the next `Range` request in the same part |
| When a part is >80% complete (inner loop) | `-l`, `-L` | Extra delay near the end of a part |
| When overall part completion >80% (worker) | `-v`, `-X` | Delay after finishing work in a loaded phase |
| Between outer pool passes while parts remain | `-z`, `-Z` | Pause before re-submitting work to the executor |

On **sub-request failure** (before retrying that sub-range), the tool sleeps a random duration between **`--micro-backoff-min-s` / `-A`** and **`--micro-backoff-max-s` / `-C`**.

---

## Configuration reference

Sizes accept plain bytes (`5242880`) or suffixes: **`K`**, **`M`**, **`G`**, **`T`** (optional trailing `B`). Example: `5M`, `512K`.

### Registry and output

| Long | Short | Default | Purpose |
|------|-------|---------|---------|
| `--registry` | `-r` | *(required)* | Registry base URL (trailing `/` stripped). |
| `--output` | `-o` | `dump` | Root directory for all dumped images. |
| `--proxy` | `-p` | *(none)* | Proxy URL for `requests` (`http://`, `https://`, or **`socks5[h]://`** — `PySocks` is bundled so SOCKS does not need an extra install). |
| `--insecure` | `-k` | off | Skip TLS certificate verification (urllib3 “Unverified HTTPS request” warnings are disabled for this mode). |
| `--user-agent` | `-u` | `registry-dumper/3.0` | `User-Agent` header on registry calls. |

### Scope (which repos / tags)

| Long | Short | Default | Purpose |
|------|-------|---------|---------|
| `--repo` / `--image` | `-S` | *(prompt)* | Omit: prompted once before the plan from `_catalog`. Or pass repo names, or `all` / `*` alone for every repo with no prompt. |
| `--tag` | `-G` | *(all)* | Limit to these tags (repeat or comma-separated). Use `all` or `*` alone for every tag. |
| `--interactive` | `-x` | off | Prompt on stdin to pick repos and/or tags after listing the catalog (skips prompts for axes already set via `-S` / `-G`, including `all`). |
| `--yes` | `-y` | off | After the plan table, do not ask for confirmation; start downloads immediately. |

### Rebuild and verification

| Long | Short | Default | Purpose |
|------|-------|---------|---------|
| `--rebuild` | `-b` | off | After extract, run `docker import` to create a local image. |
| `--image-prefix` | `-n` | `recovered` | Image name prefix: `{prefix}/{repo}:{tag}` (lowercased). |
| `--verify-diffid` | `-D` | off | Reserved for future uncompressed-layer / diff-id checks; currently unused. |

### Fragmented ranged reads (layers only)

| Long | Short | Default | Purpose |
|------|-------|---------|---------|
| `--fragmented` | `-f` | *(default mode)* | Enable partial consumption of each ranged response (see above). |
| `--no-fragmented` | `-N` | — | Disable partial reads; consume full each sub-request range. |
| `--partial-ratio` | `-P` | `0.5` | With fragmented mode: fraction in `(0, 1]` of each sub-request size to read before stopping (then capped by `request_size`). |
| `--min-partial` | `-m` | `512K` | With fragmented mode: minimum bytes to read per sub-request (still capped by `request_size`). |

### Chunking, HTTP, and retries (layers)

| Long | Short | Default | Purpose |
|------|-------|---------|---------|
| `--chunk-size` | `-c` | `5M` | Size of each **parallel part** and maximum size of each **sub-request** range within a part. |
| `--subrequest-timeout` | `-t` | `15` | Per ranged `GET` timeout (seconds). |
| `--stream-chunk` | `-s` | `8192` | `requests` `iter_content` read size (bytes). |
| `--chunk-retries` | `-e` | `5` | Attempts per **part** file if the whole chunk download fails. |

### Parallel download recovery (layers)

When a ranged read fails mid-stream (e.g. **SSL `DECRYPTION_FAILED_OR_BAD_RECORD_MAC`**), the tool must not leave garbage bytes in the part file. It **truncates** back to the last good byte offset and retries that sub-range (default), or **`restart-part`** discards the whole part file and retries from scratch.

| Long | Short | Default | Purpose |
|------|-------|---------|---------|
| `--subrange-recover` | — | `truncate` | `truncate` \| `restart-part` — see above. |
| `--digest-mismatch` | — | `retry-problematic` | After merge, if the blob SHA-256 is wrong: `full` = delete all parts and restart; `retry-problematic` = re-fetch parts that had subrange/size problems first, or **all** parts if none were flagged. |
| `--digest-mismatch-rounds` | — | `5` | Max **digest verify** retry passes for `retry-problematic` before falling back to a full part-tree reset (`0` = full reset on first bad digest). |

`.meta.json` stores **`completed`** and **`problematic`** part indices so you can see which parts were flaky. Logs spell out **full reset** vs **partial re-download** and the reason.

### Simple blob download (config JSON)

| Long | Short | Default | Purpose |
|------|-------|---------|---------|
| `--blob-timeout` | `-T` | `30` | Timeout (seconds) for streaming config blob download. |
| `--blob-max-retries` | `-a` | `60` | Max full attempts for that download (includes hash retry). |

### Adaptive throttle and workers (layers)

| Long | Short | Default | Purpose |
|------|-------|---------|---------|
| `--base-workers` | `-w` | `1` | Initial worker count for parallel layer download. |
| `--max-workers` | `-W` | `1` | Upper cap on workers (`must be ≥ base-workers`). |
| `--base-rate` | `-R` | `1M` | Starting effective throttle (bytes/s) before adaptation. |
| `--max-bytes-per-sec` | `-M` | `2M` | Maximum effective throttle (bytes/s) after adaptation. |

### Jitter master switch

| Long | Short | Default | Purpose |
|------|-------|---------|---------|
| `--jitter` | `-j` | *(on)* | Enable jitter (default). |
| `--no-jitter` | `-J` | — | Disable all optional jitter sleeps. |

### Jitter tuning (only if jitter enabled)

| Long | Short | Default | Purpose |
|------|-------|---------|---------|
| `--jitter-inline-prob` | `-q` | `0.05` | Probability of an inline micro-jitter each read iteration. |
| `--jitter-inline-min-ms` | `-i` | `5` | Inline jitter lower bound (ms). |
| `--jitter-inline-max-ms` | `-I` | `30` | Inline jitter upper bound (ms). |
| `--jitter-between-sub-min-ms` | `-d` | `50` | Between sub-requests (ms). |
| `--jitter-between-sub-max-ms` | `-E` | `200` | Between sub-requests (ms). |
| `--jitter-inner-near-min-ms` | `-l` | `200` | When a part is >80% done (inner, ms). |
| `--jitter-inner-near-max-ms` | `-L` | `800` | Same (ms). |
| `--jitter-worker-near-min-ms` | `-v` | `300` | When many parts done (worker, ms). |
| `--jitter-worker-near-max-ms` | `-X` | `1200` | Same (ms). |
| `--jitter-pool-loop-min-ms` | `-z` | `200` | Between executor passes (ms). |
| `--jitter-pool-loop-max-ms` | `-Z` | `800` | Between executor passes (ms). |
| `--micro-backoff-min-s` | `-A` | `1` | Sub-request error backoff (seconds). |
| `--micro-backoff-max-s` | `-C` | `3` | Sub-request error backoff (seconds). |

For a one-line list of every flag, run:

```bash
drift --help
```

---

## Project layout

- `driftpkg/` — library package (`BlobDownloader`, `DriftApp`, CLI parsing, etc.)
- `drift.py` — thin script for running from a source tree without installing (`python drift.py …`)

## License

MIT
