# Follow-up: fix `project/workspace/project/workspace` nesting bug

Generated while fixing the nested-workspace bug autonomously. Captures the root cause, the fix,
what was verified vs. not, and judgment calls made without user sign-off. Delete sections once
resolved/accepted.

## Root cause

Two tools share one real directory but expose two different path namespaces, and neither tool's
response tells the model about the mismatch:

- `LocalShellBackend(root_dir=workspace, virtual_mode=True, ...)` (`harness/agent.py`, in
  `build_agent`) gives the agent two ways to touch files:
  1. **Shell `execute()`** — runs with `cwd=root_dir`. `pwd` inside it prints the *real* absolute
     host/container path, e.g. `/project/workspace`.
  2. **Structured file tools** (`write_file`, `read_file`, `ls`, `glob`, `grep`) — backed by
     `FilesystemBackend._resolve_path` with `virtual_mode=True`. Every path passed to these tools,
     absolute-looking or not, is treated as **already relative to `root_dir`**: `_resolve_path`
     does `(self.cwd / vpath.lstrip("/")).resolve()`. So `/foo.py` and `foo.py` both correctly land
     at `{root_dir}/foo.py`.

  The bug: if the model shells out, sees `pwd` print `/project/workspace`, and later reuses that
  *real* string as a path argument to a file tool (instead of a root-relative virtual path), it
  gets silently re-anchored under `root_dir` a second time:
  `_resolve_path("/project/workspace/foo.py")` → `{root_dir}` + `/project/workspace/foo.py` →
  `/project/workspace/project/workspace/foo.py`. Exactly the reported symptom.

  Why the model "doesn't notice": `write_file`'s success response just echoes back the path the
  model supplied — it never surfaces the real resolved disk path, so there's no signal in the tool
  output that anything went wrong. This is also openly documented as a known sharp edge in
  deepagents' own `LocalShellBackend` deprecation warning text (confirmed by pulling the installed
  wheel, `deepagents==0.6.7`, and reading `backends/filesystem.py:_resolve_path` directly — see
  Verification below).

- **Direct evidence found in this repo**: `deepagent-image/project/workspace/project/workspace/
  reverse_text.py` (untracked, `?? deepagent-image/project/workspace/project/` in `git status`) is
  a duplicate of `deepagent-image/project/workspace/reverse_text.py` with extra comments — i.e. the
  model wrote the file correctly once, then later in the same/another turn wrote a refined version
  to the doubly-nested path. Left in place untouched (untracked test residue, not source) — your
  call whether to delete it.

- `project/AGENTS.md` already has a guidance section telling the model not to prepend
  `/project/workspace` itself. That's a soft prompt-level patch on top of the same structural
  conflict; it clearly isn't sufficient on its own (the bug still happened), which is why a
  structural fix was needed.

## Fix

`deepagent-image/project/harness/agent.py`: added `_WorkspaceShellBackend(LocalShellBackend)`,
overriding `_resolve_path` to strip a literal `root_dir`-prefix from an incoming path (matched on a
`/`-boundary, not a string prefix, so `/project/workspace2/...` is *not* mistaken for
`/project/workspace/...`) before delegating to the parent's virtual resolution. Both path
conventions ("use a virtual path" and "use the real absolute path you saw via `pwd`") now resolve
to the same place instead of nesting. `build_agent` now constructs `_WorkspaceShellBackend` instead
of `LocalShellBackend` directly; nothing else changed.

Left `project/AGENTS.md`'s existing "don't prepend the workspace path" guidance in place as
defense-in-depth / documentation of intent — it costs nothing and helps if the agent ever
introspects path handling.

## Decision: normalize instead of disabling `virtual_mode`

Considered setting `virtual_mode=False` instead (then absolute paths would be used as-is, matching
what `pwd` shows, with no namespace mismatch at all). Rejected: `FilesystemBackend.ls`/`glob`
default to listing path `"/"`, which `virtual_mode=False` would resolve to the **real filesystem
root**, not the workspace — i.e. `ls("/")` would list the container's actual `/` instead of the
workspace. That's a regression in the tools' usability (and a containment loss for those specific
tools) for no benefit, since the shell tool already has unrestricted host access regardless of
`virtual_mode` (per deepagents' own docs: "`virtual_mode` does not restrict shell commands"). Kept
`virtual_mode=True` and fixed the path-normalization mismatch directly instead.

## Verification

No `pip`/`venv` available in this sandbox (no network-installed Python packages possible beyond
plain `curl`), so the real `deepagents` package could not be `pip install`ed and the harness could
not be run end-to-end here. To still verify against the *real* shipped code rather than guessing:

1. Downloaded the actual `deepagents==0.6.7` wheel from PyPI (matches
   `requirements.txt`'s `deepagents>=0.6.7,<0.7.0` floor) via `curl` + the PyPI JSON API, and read
   `backends/filesystem.py` / `backends/local_shell.py` directly to confirm the exact
   `_resolve_path` algorithm described above.
2. Wrote a standalone test reproducing that algorithm verbatim (copied from the wheel, not
   reimplemented from memory) plus the new `_WorkspaceShellBackend._resolve_path` override (copied
   from the actual edited `agent.py`), and ran 8 cases: plain relative paths, virtual absolute
   paths, the exact bug pattern (both absolute and relative forms), the bare workspace root, a
   similarly-named-but-different directory (`workspace2`, to check the fix doesn't over-strip), and
   an unrelated absolute virtual path. All 8 passed, and the "buggy (old)" column for the bug-
   pattern cases reproduced the exact reported nesting
   (`/project/workspace/project/workspace/foo.py`) before the fix and the correct path after.
3. `python3 -m py_compile harness/agent.py` passes.

**Not verified**: an actual end-to-end run of `run-docker.ps1`/`run-docker.sh` against the built
image, since this sandbox can't build/run Docker. If you can, worth a quick manual check: start a
session, ask the agent to `pwd` then write a file using that exact printed path, and confirm it
lands at the workspace root instead of nesting.

## Missing info filled in by assumption

- Didn't ask before deciding the untracked `workspace/project/` artifact and `workspace/
  reverse_text.py` should be left alone rather than deleted — they're untracked test residue, not
  reversible to recover if wrong, so left for you to clean up or keep as a regression-test fixture.
