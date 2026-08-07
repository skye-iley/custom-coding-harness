#!/usr/bin/env python3
"""M4 slice H runtime gate: prove the bwrap fs jail actually holds in this image.

Driven from `smoke.{sh,ps1}` rather than pytest because the check only means
anything when the container was started with
`--security-opt seccomp=seccomp/userns.json` -- a `docker run` concern a test
inside the container cannot set for itself.

What this asserts, using the harness's own `jail.bwrap_args` (NOT hand-rolled
binds, so a regression in the real argv builder is caught) and with **no docker
mask applied**, so the jail is the only enforcer in play:

  1. bwrap can unshare under the vendored profile               (the §3 hard gate)
  2. a masked path reads EMPTY inside the jail                   (invariant 5, 3rd leg)
  3. an unmasked file reads byte-identical inside the jail       (invariant 8)
  4. the workspace stays writable                                (live edits still land)
  5. /project is read-only                                       (agent can't rewrite harness code)

Exit codes:
  0  all checks passed
  77 SKIPPED -- this host cannot create user namespaces at all (e.g. the profile
     was not passed, or a kernel/runtime that disallows nesting). Environmental,
     not a regression. smoke turns this into a hard failure under JAIL_CHECK=1.
  1  a check FAILED -- a real boundary regression
"""
import os
import subprocess
import sys

sys.path.insert(0, "/project")
from harness import jail  # noqa: E402

EXIT_SKIP = 77

# Deliberately NOT under /tmp: bwrap_args emits `--tmpfs /tmp` *after* the binds,
# so a fixture staged there is overmounted and every check below silently passes
# for the wrong reason (the tree vanished rather than the mask working).
WS = "/project/workspace"
STATE = "/project/state"
SECRET_BODY = "SUPER_SECRET=leaked\n"

failures = []


def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{(' -- ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def main():
    os.makedirs(WS, exist_ok=True)
    os.makedirs(STATE, exist_ok=True)
    secret = os.path.join(WS, ".env")
    with open(secret, "w") as fh:
        fh.write(SECRET_BODY)
    plain = os.path.join(WS, "visible.txt")
    with open(plain, "w") as fh:
        fh.write("not a secret\n")
    os.chdir(WS)

    args = jail.bwrap_args(
        WS,
        STATE,
        [{"relpath": ".env", "type": "file"}],
        empty_file=jail.ensure_empty_file(STATE),
        project_root="/project",
    )

    def run(argv):
        return subprocess.run(["bwrap", *args, *argv], capture_output=True, text=True)

    # 1. the gate itself.
    probe = run(["/usr/bin/id", "-u"])
    if probe.returncode != 0:
        err = probe.stderr.strip()
        if "namespace" in err.lower() or "unshare" in err.lower():
            print("SKIPPED: this host cannot create user namespaces.")
            print(f"  bwrap said: {err[:200]}")
            print("  Expected when the seccomp profile was not passed to docker run.")
            return EXIT_SKIP
        check("1 bwrap runs under the vendored seccomp profile", False, err[:200])
        return 1
    check("1 bwrap runs under the vendored seccomp profile", True, f"uid {probe.stdout.strip()}")

    # Control: outside the jail the secret is real. If this is already empty the
    # fixture is broken and check 2 would pass for the wrong reason.
    with open(secret) as fh:
        outside = fh.read()
    check("  (control) secret is non-empty OUTSIDE the jail", outside == SECRET_BODY,
          f"{len(outside)} bytes")

    # 2. masked read is empty INSIDE -- with no docker mask in play.
    r = run(["/usr/bin/cat", secret])
    check("2 masked path reads empty inside the jail", r.returncode == 0 and r.stdout == "",
          f"rc={r.returncode} bytes={len(r.stdout)}")

    # 3. unmasked read is byte-identical.
    r = run(["/usr/bin/cat", plain])
    check("3 unmasked file is byte-identical inside", r.stdout == "not a secret\n",
          f"rc={r.returncode} {r.stdout!r}")

    # 4. workspace still writable.
    r = run(["/usr/bin/touch", os.path.join(WS, ".jail-write-probe")])
    check("4 workspace is writable inside", r.returncode == 0, r.stderr.strip()[:120])

    # 5. /project read-only.
    r = run(["/usr/bin/touch", "/project/.should-not-exist"])
    check("5 /project is read-only inside", r.returncode != 0
          and "read-only" in r.stderr.lower(), r.stderr.strip()[:120])

    if failures:
        print(f"\nJAIL CHECK FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("\nJAIL CHECK PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
