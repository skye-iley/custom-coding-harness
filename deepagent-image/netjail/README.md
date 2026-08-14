# NetJail — deny-all-egress network jail with an allowlist

NetJail runs the agent container on an `--internal` Docker network: **no route to
the host, no route to the internet.** From that locked box you punch exactly the
holes you declare — nothing else is reachable, by construction (not by firewall
rules you have to get right, and not by trusting which host ports happen to be
bound where).

It is **opt-in** and off by default. Without it, `run-docker` gives the agent
normal bridge networking (host reachable via `host.docker.internal`).

```bash
# Linux / bash
NET_JAIL=1 ./scripts/run-docker.sh "your task"
```
```powershell
# Windows / PowerShell
.\scripts\run-docker.ps1 -NetJail "your task"
```

## How it works

```
        ┌─────────────────── deepagent-jail (--internal: no gateway) ───────────────────┐
        │                                                                                │
        │   agent container ── TCP ──▶ deepagent-fwd-ollama ─┐                           │
        │        │                    deepagent-proxy ─┐     │                           │
        └────────┼───────────────────────────────────┼──────┼───────────────────────────┘
                 │  (agent has NO other route)        │      │  also attached to:
                 ▼                                    ▼      ▼
                (nothing)                     deepagent-egress (normal bridge, host route)
                                                     │      │
                                                     ▼      ▼
                                          host:PORT (socat)   internet:443 (proxy, allowlisted)
```

- The **agent** is attached only to `deepagent-jail` (`--internal`). It can resolve
  and reach the sidecars by name (Docker DNS), and nothing else — no host IP, no
  public DNS route.
- **Host-service forwarders** (`socat`) each relay one TCP port to a service on
  the Docker host. One per line in `host-services.txt`.
- The **egress proxy** (`tinyproxy`) is a domain-allowlisted HTTP(S) forward
  proxy. The agent hands it a hostname via `HTTP(S)_PROXY`; the proxy resolves and
  connects only if the host matches `allowed-domains.txt`, else denies. Git, pip,
  and npm all honor `HTTP(S)_PROXY`.

Because the agent itself never routes to the internet or resolves public names —
the proxy does — the deny-by-default holds even if the agent is compromised. Its
blast radius is exactly the ports and domains you listed.

---

## Adding a custom permission

Two files. One line each. No script edits.

### 1. Reach a service on the Docker HOST → `host-services.txt`

Format: `<name>  <port>`

```text
ollama    11434
postgres  5432
```

- The agent reaches it as `http://deepagent-fwd-<name>:<port>` (e.g.
  `http://deepagent-fwd-postgres:5432`).
- The **host daemon must listen on the docker bridge**, not only `127.0.0.1`
  (loopback is unreachable from any container). For Ollama:
  `OLLAMA_HOST=0.0.0.0:11434 ollama serve`.
- `ollama` is special-cased: when listed, the agent's `OLLAMA_HOST` is auto-set to
  its forwarder, so jail runs need no `project/.env` change.
- **Revoke:** delete the line.

### 2. Reach a domain on the INTERNET → `allowed-domains.txt`

Plain domains, one per line. Subdomains included automatically (`example.com`
also allows `api.example.com`). Everything not listed is denied.

```text
github.com
api.github.com
pypi.org
files.pythonhosted.org
registry.npmjs.org
```

Common bundles (ship commented; uncomment to enable):

| Need            | Domains |
|-----------------|---------|
| git / gh (HTTPS)| `github.com`, `api.github.com`, `codeload.github.com`, `objects.githubusercontent.com` |
| pip / PyPI      | `pypi.org`, `files.pythonhosted.org` |
| npm             | `registry.npmjs.org` |
| conda           | `conda.anaconda.org`, `repo.anaconda.com` |

- **Revoke:** delete or re-comment the line.

That's the whole model: `host-services.txt` = host ports, `allowed-domains.txt` =
internet domains. Anything absent from both is unreachable.

---

## git / pip / npm inside the jail

- **Local git** (commit, branch, status, diff) needs no network — works in the
  jail unchanged.
- **Remote git** (`git fetch`/`push`, `gh pr create`) hits `github.com:443`, so it
  needs the GitHub domains (shipped **enabled** in `allowed-domains.txt`). Use
  HTTPS remotes + a token (`GH_TOKEN`), not SSH — SSH is not proxied by default.
- **pip / npm / conda** need their registry domains uncommented. They read
  `HTTP_PROXY`/`HTTPS_PROXY`, which the jail injects automatically.

To allow **git-over-SSH** instead: add `ConnectPort 22` to `tinyproxy.conf`, list
the git host, and configure git's `core.sshCommand` to tunnel through the proxy —
simpler to just use HTTPS + token.

---

## Config files

| File | Tracked? | Purpose |
|------|----------|---------|
| `host-services.txt`          | no (gitignored) | host-port forwarders (one `name port` per line) |
| `host-services.txt.example`  | **yes** | shipped defaults for the above; used verbatim while the live file is absent |
| `allowed-domains.txt`        | no (gitignored) | egress domain allowlist (one domain per line) |
| `allowed-domains.txt.example`| **yes** | shipped defaults for the above; used verbatim while the live file is absent |
| `tinyproxy.conf`             | **yes** | proxy settings; the allowlist Filter is generated from `allowed-domains.txt` at run time |

**The two allowlists are local files, not tracked ones.** Only
`host-services.txt.example` / `allowed-domains.txt.example` are committed; the
live pair is gitignored. This is because `harness config security` edits them in
place, which made every allowlist experiment a dirty tracked file — one
`git add -A` away from shipping someone's local grant to everyone.

You do **not** have to copy anything. A reader (`run-docker`, `smoke`) uses the
live file when it exists and falls through to the `.example` otherwise, so a
fresh clone runs on the shipped defaults with no setup step. The live file is
materialized only by a **write**: `harness config security`, or your own
`cp host-services.txt.example host-services.txt` before hand-editing. Seeding
copies the template whole, so your local copy keeps its comments and
commented-out examples.

Consequence worth knowing: once a live file exists it **fully replaces** the
template — later additions to the shipped defaults (a new provider domain, say)
won't reach you. Diff them after a pull if a jailed run starts failing on
something new: `diff allowed-domains.txt allowed-domains.txt.example`.

Env overrides (both scripts): `NETJAIL_JAIL_NET`, `NETJAIL_EGRESS_NET`,
`NETJAIL_SOCAT_IMAGE`, `NETJAIL_PROXY_IMAGE`.

## Operational notes

- Sidecars (`deepagent-fwd-*`, `deepagent-proxy`) are torn down when `run-docker`
  exits. The two networks are left in place (idempotent); remove manually with
  `docker network rm deepagent-jail deepagent-egress`.
- **Not concurrent-safe:** sidecar names are fixed, so don't run two jailed
  sessions at once on the same host.
- **The model API must be allowlisted** or a cloud-model agent cannot start — see
  the model-provider block in `allowed-domains.txt` (uncomment the line for your
  `DEEPAGENTS_MODEL` provider). Local models via `host-services.txt` need nothing.
- **Telemetry noise is expected:** if LangSmith tracing is on, the agent tries to
  reach `api.smith.langchain.com`, which is denied (`403 Filtered`) unless you
  allowlist it. It's harmless (the task still runs); to silence it, set
  `LANGCHAIN_TRACING_V2=false` in `project/.env`.
- If the proxy container exits immediately, inspect `docker logs deepagent-proxy`
  and adjust `tinyproxy.conf` (`User`/`Group`/`PidFile`) for your
  `NETJAIL_PROXY_IMAGE` — directive requirements vary by image.
- **Verify the jail** (from another shell while a jailed session runs):
  ```bash
  # agent can reach an allowlisted domain via the proxy:
  docker exec <agent> sh -c 'HTTPS_PROXY=$HTTPS_PROXY curl -sS https://github.com -o /dev/null && echo OK'
  # agent CANNOT reach a non-listed domain:
  docker exec <agent> sh -c 'curl -sS --max-time 5 https://example.com; echo rc=$?'
  # agent CANNOT reach the host directly (no route off the internal net):
  docker exec <agent> sh -c 'curl -sS --max-time 5 http://host.docker.internal:11434; echo rc=$?'
  ```

## Status

**Core mechanics verified** on Docker Desktop (Windows, Linux containers):

- isolation — a container on `deepagent-jail` cannot reach the host or the
  internet (both time out);
- allowlist — a listed domain (github.com) returns 200 through the proxy while an
  unlisted one (example.com) is denied at the CONNECT tunnel (403);
- forwarder — the socat sidecar relays a jail-side connection through to the host.

Hardening: if the proxy fails to load our allowlist config (e.g. a mount that
didn't land), `run-docker` aborts rather than handing the agent an open proxy —
the jail **fails closed**, never open.

Default proxy image is `kalaksi/tinyproxy` (Docker Hub); the upstream
`ghcr.io/tinyproxy/tinyproxy` is blocked on some networks. Override either sidecar
image with `NETJAIL_PROXY_IMAGE` / `NETJAIL_SOCAT_IMAGE`.

Still recommended: run the verify probes above against your own host once,
especially after changing the proxy image — directive support and default config
fallback vary by image (the fail-closed check guards the worst case).
