# IMMEDIATE_TODO — Fix: native-Linux bind-mount permission failure (sqlite checkpointer)

Status: **built.** Auto host-uid mapping on native Linux (`scripts/lib/hostmap.sh` +
`run-docker.sh`), launcher-knobs docs, and the decision-matrix unit test
(`project/tests/test_hostmap.py`) are in. Priority was: high — harness did not run out-of-the-box
on native (non-WSL) Linux.

## Symptom

On a fresh install on **native Linux** (bare metal / VM, *not* WSL2, *not* Docker Desktop), the
first turn crashes when the harness opens the LangGraph `SqliteSaver` checkpointer. Errors surface
as one of:

- `sqlite3.OperationalError: unable to open database file`
- `sqlite3.OperationalError: attempt to write a readonly database`

Works fine on Windows/WSL2 and macOS/Docker Desktop, so it reads like a "sqlite" or "fresh install"
bug but is neither.

## Root cause — Docker bind-mount UID mismatch

- The image runs as `USER agent`, uid **10001** — `deepagent-image/Dockerfile:30`, `:96`.
- The checkpointer DB is `/project/state/checkpoints.sqlite`; `/project/state` is a **bind mount**
  from a host dir created by `mkdir -p "$STATE_HOST_DIR"` running as the **host user** (typically
  uid 1000, mode 755) — `deepagent-image/scripts/run-docker.sh:226-228`, mounted at `:258`, path
  wired via `-e DEEPAGENTS_STATE_DIR=/project/state` `:256`.
- On **native Linux**, bind mounts preserve **real host ownership**. Inside the container
  `/project/state` is owned by uid 1000, mode 755 → agent (uid 10001) has read+execute but **no
  write**.
- `SqliteSaver.from_conn_string(...)` must **create** `checkpoints.sqlite` in that dir and then run
  `setup()` (`CREATE TABLE ...`) — `deepagent-image/project/harness/cli.py:636-637`, `:674`.
  Creating a file needs write permission on the parent dir → `EACCES` → the sqlite errors above.
- The image's own `chown -R agent:agent /project` (`Dockerfile:90`) is **masked at run time**: the
  bind mount overlays `/project/state`, replacing image ownership with the host dir's.

### Why WSL2 / Docker Desktop are unaffected

Docker Desktop and WSL2 route mounts through a VM filesystem (virtiofs / gRPC-FUSE / 9p) that
**squashes ownership** so mounts appear owned by the container user. Native Linux uses the real
kernel VFS with real UIDs → mismatch. This is exactly the "works on WSL, fails on bare Linux"
report.

### Scope note

The same defect also affects the **workspace mount** (`run-docker.sh:257`) — the agent cannot write
a host-owned workspace either. The checkpointer just fails first and loudest because it writes
immediately on turn 1. The fix below (map the container to the host uid) resolves **both** the state
dir and the workspace.

## Existing partial mitigation (insufficient)

`MAP_HOST_USER=1` already exists — `run-docker.sh:43-50` — and runs the container as the host
uid:gid (`HOST_UID`/`HOST_GID` default to `id -u`/`id -g`), which makes both host-owned mounts
writable. Problems:

1. It is **opt-in and unset by default**, so a fresh native-Linux run fails out of the box.
2. It is **undiscoverable**: it lives only in a script comment. It is *not* in `.env.example`
   (correctly so — it is a **host-side launcher** var read before `docker run`, whereas `.env` is
   forwarded *into* the container via `--env-file` `:253`; a `.env` entry would never reach the code
   that reads it). Siblings `CPUS`/`MEMORY`/`PIDS_LIMIT`/`SAVE_WORKSPACE`/`EPHEMERAL` are absent for
   the same reason. There is no documented surface for launcher knobs at all.

## The fix

### 1. Auto-enable host-uid mapping on native Linux (primary)

Make `run-docker.sh` **default to host-uid mapping when, and only when, the Docker engine does NOT
squash mount ownership** — i.e. a native Linux engine. Keep WSL2 / Docker Desktop / macOS on the
current path (no mapping), since mapping there is unnecessary and mildly harmful (it redirects
`HOME=/tmp` `:48` and runs as a uid with no matching named user).

**Precedence (explicit wins):**

| `MAP_HOST_USER` | Behavior |
|-----------------|----------|
| `1` (explicit)  | Force mapping on (current behavior). |
| `0` (explicit)  | Force mapping off (escape hatch, even on native Linux). |
| unset           | **Auto-detect**: native-Linux engine → map; otherwise → don't. |

**Detection (host side, in `run-docker.sh`), map only if ALL hold:**

- `uname -s` == `Linux` (rules out macOS).
- Host is **not WSL**: `/proc/version` (or `/proc/sys/kernel/osrelease`) does **not** match
  `-i microsoft` / `WSL`.
- Docker engine is **not Docker Desktop**: `docker info --format '{{.OperatingSystem}}'` does **not**
  contain `Docker Desktop` (native engine returns the distro string, e.g. `Ubuntu 24.04`).

If any check fails → treat as squashed → no mapping (unchanged path). Run `docker info` once and
cache; keep the probe quiet.

**Testability:** extract the decision into a **pure** shell function, e.g.
`_should_map_host_user <uname> <is_wsl> <docker_os> <map_host_user_env>` returning 0/1, so the choice
can be unit-tested with stubbed inputs without a live Docker daemon. The surrounding code only does
I/O (calls `uname`/`docker info`) and passes results in.

### 2. Document the launcher knobs (secondary)

Add a **launcher environment** reference (host-side vars, not `.env`) covering `MAP_HOST_USER`,
`HOST_UID`, `HOST_GID`, `CPUS`, `MEMORY`, `PIDS_LIMIT`, `SAVE_WORKSPACE`, `EPHEMERAL`, `NET_JAIL`.
Place it in `deepagent-image/CLAUDE.md` (Commands section) and/or the script's usage/`--help`
banner. Explicitly state: these are **not** `.env` vars. Do **not** add them to `.env.example`.

## Files to change

- `deepagent-image/scripts/run-docker.sh` — add auto-detect + pure decision function; default the
  mapping on native Linux.
- `deepagent-image/scripts/run-docker.ps1` — **keep the pair in sync** (repo convention). Windows is
  always Docker Desktop (squashed), so this is effectively a no-op, but mirror the comment/precedence
  so the two scripts don't drift. No behavior change on Windows.
- Docs: `deepagent-image/CLAUDE.md` (launcher-knobs table) and/or the script usage banner.

## Tests / verification

- **Unit (pure decision fn):** assert the 3×3 matrix — `MAP_HOST_USER` in {unset, 0, 1} × engine in
  {native-linux, wsl, docker-desktop} — yields the precedence table above. Runnable on any host, no
  daemon.
- **Manual on the native-Linux VM:** fresh clone → build → `./scripts/run-docker.sh "hi"` with
  `MAP_HOST_USER` **unset**. Expected: checkpointer opens, no sqlite error, `checkpoints.sqlite`
  written under `deepagent-image/project/state/<ws-key>/` owned by the host uid. Confirm the
  container ran mapped: `docker inspect <c> --format '{{.Config.User}}'` → `1000:1000` (not `agent`).
- **Regression note (repo rule):** ship the fix with the unit test above so the detection can't
  silently regress.

## Acceptance criteria

1. Fresh native-Linux install runs turn 1 with **no** manual env var and **no** sqlite/permission
   error; `checkpoints.sqlite` and `past.sqlite` are created and writable.
2. WSL2 / Docker Desktop / macOS behavior is **byte-for-byte unchanged** (no mapping applied).
3. `MAP_HOST_USER=1` and `MAP_HOST_USER=0` still force the respective behavior on any host.
4. Launcher knobs are documented in a discoverable, correctly-layered place (not `.env.example`).
5. `.ps1` and `.sh` remain in sync.

## Out of scope

- The bubblewrap / overlayfs workspace jail (`docs/features/workspace_visibility.md`).
- Any change to the Python harness (`cli.py`/`archive.py`) — the state/DB path handling is correct;
  this is purely a host-side mount-ownership problem.
- Named-volume or root-entrypoint-chown alternatives (considered, rejected in favor of uid mapping
  which also fixes the workspace mount and keeps the host-visible per-workspace state path).
