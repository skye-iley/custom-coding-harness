# Agent instructions

## Two Python stacks

This container runs two separate Python environments. Do not mix them.

| Stack | Location | Used for |
|-------|----------|----------|
| **Harness** | `/opt/venv` | Deep Agents runtime only (`main.py`). First on `PATH`. |
| **Workspace** | `.conda/env/` under the workspace root | Project code, tests, pip/conda installs, R, Node, etc. |

**Never** run `pip install` for project dependencies without activating the workspace conda env first. Unqualified `pip`/`python` in a shell defaults to `/opt/venv` and will pollute the harness.

## Workspace conda setup

On a fresh workspace:

```bash
conda-init-workspace /project/workspace
```

Or from the workspace root if `environment.yml` is present:

```bash
conda-init-workspace
```

## Running project commands

Prefer the wrapper (activates workspace conda automatically):

```bash
./scripts/run-in-env.sh python -m pytest
./scripts/run-in-env.sh pip install -r requirements.txt
./scripts/run-in-env.sh Rscript analysis.R
```

Manual activation:

```bash
source /opt/conda/etc/profile.d/conda.sh
conda activate /project/workspace/.conda/env
python -m pytest
```

## Sandboxed execution (network phases)

`sandbox-exec` wraps a command in a bubblewrap jail whose only writable path is the workspace.
Two phases control network access:

```bash
sandbox-exec install -- pip install -r requirements.txt   # network ALLOWED (fetch deps)
sandbox-exec exec    -- python -m pytest                  # network DENIED  (run code/tests)
```

Use `install` only for dependency resolution; run tests and any agent-authored code under `exec`
so it cannot reach the network. (Requires unprivileged user namespaces — see `design_doc.md` §2.)

## Filesystem

- Work only under the workspace root (`/project/workspace` by default).
- Do not modify `/opt/venv`, `/opt/conda`, or `/project/main.py` unless explicitly asked to change the harness.
