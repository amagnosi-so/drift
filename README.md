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

After publishing to PyPI (or with a VCS URL):

```bash
pipx install drift-registry-dumper
# or, e.g.:
# pipx install git+https://github.com/you/drift.git
```

pipx exposes two equivalent commands (use the long name if another `drift` is already on your `PATH`):

- `drift`
- `drift-registry-dumper` (same as `drift`, name matches the distribution on PyPI)

Upgrade / uninstall:

```bash
pipx upgrade drift-registry-dumper
pipx uninstall drift-registry-dumper
```

## Usage

Minimal example (TLS verify on, output under `./dump`):

```bash
drift -r https://registry.example.com
```

## What the tool does

1. **Lists repositories** via `GET /v2/_catalog`, then **tags** per repo via `GET /v2/{name}/tags/list`.
2. For each `repo:tag`, fetches the **manifest** (`application/vnd.docker.distribution.manifest.v2+json`).
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
| Between outer pool passes while parts remain | `-y`, `-Y` | Pause before re-submitting work to the executor |

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
| `--insecure` | `-k` | off | Skip TLS certificate verification. |
| `--user-agent` | `-u` | `registry-dumper/3.0` | `User-Agent` header on registry calls. |

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
| `--jitter-pool-loop-min-ms` | `-y` | `200` | Between executor passes (ms). |
| `--jitter-pool-loop-max-ms` | `-Y` | `800` | Between executor passes (ms). |
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
