"""Narrow AppArmor relaxation that lets bubblewrap build the fs jail (M4 slice J).

seccomp and the host's LSM are **independent gates, and an operation must pass
both**. `harness/seccomp.py` fixes the syscall filter; it has no effect on
AppArmor. On any host running AppArmor -- Ubuntu/Debian, which is most Linux
Docker -- the daemon applies a generated `docker-default` profile carrying a
literal `deny mount,`. bwrap's second operation after `unshare` is
`mount(NULL, "/", NULL, MS_SLAVE|MS_REC, NULL)`, so the jail fails with:

    bwrap: Failed to make / slave: Permission denied

Note where that lands: *past* `unshare`, at the first mount. That is the
fingerprint `jail.classify_bwrap_failure` uses to tell an LSM denial from a
seccomp/userns one (invariant 37).

**No userns trick escapes it.** AppArmor denies by *profile*, not by uid or
capability. A process in a user namespace holds CAP_SYS_ADMIN over that
namespace -- which is what makes unprivileged bwrap work at all -- but stays
confined by `docker-default`. Root cannot override an LSM denial either, which
also rules out a setuid-root bwrap: that would fix the half that is not failing.

So, exactly as `seccomp.py` does one layer down, we vendor Docker's own profile
with **only** the `mount` rule narrowed:

    mount options=(rw, silent, rslave) -> /,
    mount fstype=tmpfs,
    mount options=(rw, bind),
    mount options=(rw, rbind),
    mount options in (ro, silent, remount, bind, nosuid, nodev, ...),
    pivot_root,
    mount options=(rw, silent, rprivate) -> /oldroot/,

(the exact set is `RELAXED_MOUNT_RULES` below -- measured on a live host, not
read off bwrap's source; do not paraphrase it from here)

Every other rule -- all nine `deny` lines, the signal peers, the ptrace peer
restriction -- carries through byte-for-byte, and `verify_profile` asserts it.
The alternative, `apparmor=unconfined`, drops the *whole* profile to buy one
inner boundary: categorically the trade milestone4.md §16 fork 7 already
rejected in its seccomp form. It stays reachable only as an explicitly-named
operator opt-in (`DEEPAGENTS_JAIL_APPARMOR`).

**Two artifacts, so the diff is the review.** Sync writes both
`apparmor/docker-default.rendered` (upstream, unmodified) and
`apparmor/deepagent-userns` (upstream + our relaxation). `verify_profile` then
checks the strongest property available offline: the shipped profile is
*exactly* `relax_mount(baseline)`. A reader diffing the two files sees one line
become the measured rule set and nothing else.

**Unlike seccomp, this cannot ride along as a `docker run` file argument.** An
AppArmor profile must be compiled into the *host* kernel by root before the
container starts (`scripts/install-apparmor-profile.sh`), and
`--security-opt apparmor=deepagent-userns` merely references it by name. See
`deepagent-image/apparmor/README.md`.

Regenerating the vendored artifacts (dev-time, needs network):

    python3 -m harness apparmor-sync            # refresh from the pinned moby tag
    python3 -m harness apparmor-sync --check    # verify committed files, write nothing

Imports no harness sibling, so `doctor` and the tests both reuse it without a
cycle (same discipline as seccomp.py / mask.py).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Pinned upstream source -- the same moby tag harness/seccomp.py pins for the
# syscall filter, deliberately duplicated rather than imported (this module must
# import no harness sibling). Bump both together, re-run both syncs, and re-read
# both diffs: an upstream profile change is a security-relevant change to our
# base posture.
MOBY_TAG = "v28.0.1"
MOBY_TEMPLATE_URL = (
    f"https://raw.githubusercontent.com/moby/moby/{MOBY_TAG}/profiles/apparmor/template.go"
)

# The profile's name is how `docker run --security-opt apparmor=<name>` selects
# it, and how /proc/self/attr/apparmor/current reports it back. Stable across
# regenerations on purpose: a version-suffixed name would move the launcher
# default and force an uninstall/reinstall on every sync (§14 fork J1). Stale
# loads are diagnosed by the recorded sha instead.
PROFILE_NAME = "deepagent-userns"
PROFILE_FILENAME = "deepagent-userns"
BASELINE_FILENAME = "docker-default.rendered"
PROFILE_ENV = "DEEPAGENTS_APPARMOR_PROFILE"

# Render parameters. moby fills these in at container-create time from what the
# host supports; we pin the modern answers (both macros exist on any AppArmor
# install new enough to matter) so the artifact is reproducible offline.
# DaemonProfile is the peer allowed to signal container processes -- moby reads
# dockerd's own confinement and falls back to "unconfined", which is what an
# ordinary distro dockerd reports.
RENDER_IMPORTS = ("#include <tunables/global>",)
RENDER_INNER_IMPORTS = ("#include <abstractions/base>",)
RENDER_DAEMON_PROFILE = "unconfined"

# The single upstream line we replace.
DENY_MOUNT_RULE = "deny mount,"

# What replaces it: the mount operations bwrap actually performs, and nothing
# else. Each is justified per-rule in apparmor/README.md.
#
# NOTE (milestone4.1.md §13.1): MEASURED, not derived, across TWO live runs. The
# original seven rules were read off bwrap's syscall sequence; four of them were
# wrong and an eighth was missing, and what a live Ubuntu 25.10 / kernel
# 7.0.0-29-generic / Docker 29.7.2 host's `apparmor="DENIED"` log demanded is what
# is here (§13.1a for the round-by-round record). The second run (fork J6) then
# DELETED the `fstype=proc` rule and measured no change, so the set is seven again
# -- narrowed by subtraction, which is the only safe direction to move it.
# Adding a rule here widens the profile -- it must arrive with a justification in
# the README and a denial that demanded it. A bare `mount,` catch-all is
# `unconfined` wearing a costume and verify_profile rejects it.
RELAXED_MOUNT_RULES = (
    # bwrap's first act after unshare. MS_SILENT is set on every mount bwrap
    # makes; AppArmor's `options=` is an EXACT flag-set match, so omitting it
    # denied with info="failed flags match".
    "mount options=(rw, silent, rslave) -> /,",
    "mount fstype=tmpfs,",
    "mount options=(rw, bind),",
    "mount options=(rw, rbind),",
    # The second half of every --ro-bind. `in` (subset), not `=` (exact), because
    # Linux cannot create a read-only bind in one call: bwrap binds, then remounts
    # re-supplying the SOURCE mount's existing flags (nosuid/nodev/relatime/... on
    # the observed host). That set is host- and mount-dependent, so exact matching
    # would need one rule per combination. `rw` is deliberately absent, so this
    # cannot authorize a read-WRITE mount. Be precise about what that does and does
    # not buy: `bind` is inside the set, so as written this is a general *read-only*
    # bind grant, unrestricted by fstype or target. It is not narrower than the
    # `mount options=(rw, bind),` rule above it, which is already unrestricted -- so
    # it widens nothing -- but do not read it as "the ro-remount rule only".
    "mount options in (ro, silent, remount, bind, nosuid, nodev, noexec, noatime, relatime, nodiratime, strictatime),",
    # NO proc rule, and this is a measurement, not a derivation. bwrap DOES mount a
    # fresh procfs at `newroot/proc` pre-pivot. A `mount fstype=proc -> /proc/,` rule
    # shipped here through the first measurement and was deleted in a second one
    # (2026-08-14, fork J6): removing it changed nothing, so it was authorizing
    # nothing -- most likely because its target (`/proc/`) never matched the actual
    # mount point (`newroot/proc`) in the first place. WHICH remaining rule the kernel
    # accepts that mount under is NOT established: none of the rules above is an
    # obvious fit (a fresh procfs is not a bind, and bwrap's rw flags fall outside the
    # `in` set). Do not "restore" a proc rule on the strength of that gap -- the only
    # admissible evidence is an `apparmor="DENIED"` line demanding one. Kept as a
    # comment because "we checked, and the obvious-looking rule is not needed" is the
    # part a future reader would otherwise re-derive. The kernel's own procfs
    # restriction, which is a separate matter entirely, is not an LSM one (§13.7).
    "pivot_root,",
    # bwrap makes the old root rprivate before detaching it, AFTER all setup ops --
    # which is why this denial only surfaced once rules 1-5 were correct and the
    # kernel's procfs gate was held open. Missing entirely from the derived set.
    "mount options=(rw, silent, rprivate) -> /oldroot/,",
)

# Upstream rules whose removal would gut the profile while leaving our mount
# diff looking correct. Checked by name so a quiet deletion cannot pass review
# by hiding in a large regeneration diff.
CRITICAL_DENY_RULES = (
    "deny @{PROC}/sysrq-trigger rwklx,",
    "deny @{PROC}/kcore rwklx,",
    "deny /sys/firmware/** rwklx,",
    "deny /sys/kernel/security/** rwklx,",
    "deny /sys/devices/virtual/powercap/** rwklx,",
)

_HEADER_TEMPLATE = (
    "# GENERATED by 'python3 -m harness apparmor-sync' from moby {tag} -- do not hand-edit.\n"
    "# {what}\n"
    "# holder-profile-sha256: {sha}\n"
)

_SHA_LINE_RE = re.compile(r"^#\s*holder-profile-sha256:\s*([0-9a-f]{64})\s*$")
# A standalone template directive occupying its whole line ({{range}} / {{end}}).
_BLOCK_DIRECTIVE_RE = re.compile(r"^\s*\{\{\s*(?P<body>.+?)\s*\}\}\s*$")
_RANGE_RE = re.compile(r"^range\s+(?P<var>\$\w+)\s*:=\s*\.(?P<field>\w+)$")
_INLINE_DIRECTIVE_RE = re.compile(r"\{\{\s*(?P<body>.+?)\s*\}\}")


class TemplateError(RuntimeError):
    """Upstream's template grew a construct this renderer does not understand.

    Deliberately fatal. A renderer that silently ignores an unknown directive
    emits a profile that looks plausible and enforces something other than what
    upstream wrote -- the worst possible failure mode for a security artifact.
    """


# --- locating the artifacts --------------------------------------------------


def _candidates(filename: str) -> list[Path]:
    """Where a vendored artifact can live, most specific first.

    Two layouts, at different depths (mirrors seccomp.profile_candidates):
    - **repo checkout**: harness/ is at `deepagent-image/project/harness/`, so
      apparmor/ is two levels up. This is the copy an operator loads with
      apparmor_parser.
    - **in-image**: harness/ is at `/project/harness/` and the Dockerfile copies
      the folder to `/project/apparmor/` -- one level up. Without this candidate
      the in-container path resolves to `/apparmor/...` and doctor's artifact
      check fails on every containerized run.
    """
    here = Path(__file__).resolve()
    return [
        here.parents[2] / "apparmor" / filename,  # repo checkout
        here.parents[1] / "apparmor" / filename,  # in-image
    ]


def _resolve(filename: str, override: str | None = None) -> Path:
    if override:
        return Path(override)
    candidates = _candidates(filename)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    # Fall back to the repo path so an error names where the file belongs rather
    # than the last candidate tried.
    return candidates[0]


def profile_path() -> Path:
    """The vendored narrowed profile. Env override wins, then first that exists."""
    return _resolve(PROFILE_FILENAME, os.environ.get(PROFILE_ENV))


def baseline_path() -> Path:
    """The vendored *unmodified* upstream render, kept for the diff property."""
    return _resolve(BASELINE_FILENAME)


def load_text(path: Path) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


# --- header / body / hash ----------------------------------------------------


def split_header(text: str) -> tuple[str, str]:
    """Separate our generated header comment from the profile body.

    The header carries the body's own hash, so it cannot be part of what is
    hashed. Only the leading contiguous run of `#` lines counts -- comments
    inside the profile are upstream's and belong to the body.

    `#include <tunables/global>` is emphatically **not** a comment: it is
    AppArmor's include directive and the profile's first functional line. Eating
    it here would drop it from the hashed body and from the baseline-diff
    comparison, so it is excluded explicitly.
    """
    lines = text.splitlines(keepends=True)
    index = 0
    while index < len(lines):
        stripped = lines[index].lstrip()
        if not stripped.startswith("#") or stripped.startswith("#include"):
            break
        index += 1
    return "".join(lines[:index]), "".join(lines[index:])


def body_sha256(text: str) -> str:
    """Hash of the profile body, normalized for line endings.

    Normalized because a checkout on Windows can rewrite newlines, and a hash
    that flips on clone would make every stale-load diagnostic (§13.2) noise.
    """
    _, body = split_header(text)
    normalized = body.replace("\r\n", "\n").strip() + "\n"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def recorded_sha256(text: str) -> str | None:
    """The sha the header claims, or None when there is no header."""
    header, _ = split_header(text)
    for line in header.splitlines():
        match = _SHA_LINE_RE.match(line.strip())
        if match:
            return match.group(1)
    return None


def _with_header(body: str, what: str) -> str:
    body = body.strip() + "\n"
    header = _HEADER_TEMPLATE.format(
        tag=MOBY_TAG, what=what, sha=body_sha256(body)
    )
    return header + body


# --- upstream template: extract + render -------------------------------------


def extract_template(go_source: str) -> str:
    """Pull the backtick-quoted base template out of moby's template.go.

    Upstream ships AppArmor as a Go text/template that the daemon renders per
    container -- unlike seccomp, there is no finished artifact to download. If
    this raises, moby restructured the file and the sync must be re-read by a
    human rather than papered over.
    """
    marker = re.search(r"const\s+baseTemplate\s*=\s*`", go_source)
    if not marker:
        raise TemplateError(
            "could not find `const baseTemplate = `...`` in moby's template.go -- "
            "upstream changed shape; re-read the file before re-running sync"
        )
    start = marker.end()
    end = go_source.find("`", start)
    if end == -1:
        raise TemplateError("unterminated backtick literal in moby's template.go")
    template = go_source[start:end]
    for required in (DENY_MOUNT_RULE, "{{.Name}}"):
        if required not in template:
            raise TemplateError(
                f"extracted template is missing {required!r} -- either the "
                "extraction grabbed the wrong literal or upstream's profile "
                "changed materially"
            )
    return template


def _substitute_inline(line: str, scope: dict[str, str]) -> str:
    """Expand `{{.Field}}` / `{{$var}}` occurrences inside a line."""

    def replace(match: re.Match[str]) -> str:
        body = match.group("body")
        if body.startswith("."):
            field = body[1:]
            if field == "Name":
                return PROFILE_NAME
            if field == "DaemonProfile":
                return RENDER_DAEMON_PROFILE
            raise TemplateError(f"unsupported template field {{{{{body}}}}}")
        if body in scope:
            return scope[body]
        raise TemplateError(f"unsupported template directive {{{{{body}}}}}")

    return _INLINE_DIRECTIVE_RE.sub(replace, line)


def render_template(template: str) -> str:
    """Render upstream's template with the pinned parameters (§6).

    A deliberately *restricted* evaluator: it supports only the constructs
    moby's template actually uses -- `{{range $v := .Imports}}…{{end}}`,
    `{{$v}}`, `{{.Name}}`, `{{.DaemonProfile}}` -- and raises on anything else.
    Whitespace differs slightly from dockerd's own generated copy (Go emits the
    newlines around directive lines; we drop them). The *rules* do not, which is
    what the profile means.
    """
    ranges = {"Imports": RENDER_IMPORTS, "InnerImports": RENDER_INNER_IMPORTS}
    out: list[str] = []
    lines = template.splitlines()
    index = 0
    saw_range = False

    while index < len(lines):
        line = lines[index]
        block = _BLOCK_DIRECTIVE_RE.match(line)
        if block:
            directive = block.group("body")
            range_match = _RANGE_RE.match(directive)
            if not range_match:
                raise TemplateError(f"unsupported block directive {{{{{directive}}}}}")
            field = range_match.group("field")
            if field not in ranges:
                raise TemplateError(f"unsupported range field .{field}")
            saw_range = True

            # Collect the loop body up to its matching {{end}}. The template has
            # no nested ranges; if one appears, the inner {{range}} lands in the
            # body and trips the unsupported-directive check on the next pass.
            body: list[str] = []
            index += 1
            while index < len(lines):
                inner = _BLOCK_DIRECTIVE_RE.match(lines[index])
                if inner and inner.group("body").strip() == "end":
                    break
                body.append(lines[index])
                index += 1
            else:
                raise TemplateError(f"unterminated {{{{range}}}} over .{field}")

            for item in ranges[field]:
                for body_line in body:
                    out.append(_substitute_inline(body_line, {range_match.group("var"): item}))
            index += 1
            continue

        out.append(_substitute_inline(line, {}))
        index += 1

    if not saw_range:
        # The pinned RENDER_IMPORTS would silently stop being applied.
        raise TemplateError(
            "template contained no {{range}} over .Imports/.InnerImports -- "
            "upstream changed how includes are emitted; re-read before syncing"
        )

    rendered = "\n".join(out)
    # Collapse the blank runs the removed directive lines leave behind.
    rendered = re.sub(r"\n{3,}", "\n\n", rendered)
    return rendered.strip() + "\n"


# --- the relaxation ----------------------------------------------------------


def relax_mount(rendered: str) -> str:
    """Return `rendered` with `deny mount,` replaced by RELAXED_MOUNT_RULES.

    One line becomes len(RELAXED_MOUNT_RULES), at the same indent, and every
    other line passes
    through byte-identical -- so a reader diffing the vendored profile against
    the vendored baseline sees exactly our change and nothing else.

    Raises when the input carries zero or more than one `deny mount,`: both mean
    upstream's shape moved, and a blind edit would produce a profile whose
    meaning nobody checked.
    """
    lines = rendered.splitlines()
    hits = [i for i, line in enumerate(lines) if line.strip() == DENY_MOUNT_RULE]
    if len(hits) != 1:
        raise TemplateError(
            f"expected exactly one {DENY_MOUNT_RULE!r} line in the upstream "
            f"profile, found {len(hits)}"
        )
    at = hits[0]
    indent = lines[at][: len(lines[at]) - len(lines[at].lstrip())]
    # NB: the comment must not contain the literal deny rule -- callers and tests
    # check for its absence by substring, and a comment mentioning it would read
    # as an un-relaxed profile.
    replacement = [
        f"{indent}# holder M4 slice J: the mount operations bubblewrap performs to build",
        f"{indent}# the fs jail, replacing upstream's blanket deny-mount rule. Narrow by",
        f"{indent}# construction and asserted so by apparmor.verify_profile.",
    ] + [f"{indent}{rule}" for rule in RELAXED_MOUNT_RULES]
    lines[at : at + 1] = replacement
    return "\n".join(lines).strip() + "\n"


# --- verification (offline, no kernel, no network) ---------------------------


def _mount_rules(body: str) -> list[str]:
    """Every mount-family rule in a profile body, in file order."""
    found = []
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        if line.startswith("mount") or line.startswith("pivot_root") or line == DENY_MOUNT_RULE:
            found.append(line)
    return found


def verify_profile(text: str, baseline: str | None = None) -> list[str]:
    """Check the profile is upstream's `docker-default` plus exactly our diff.

    Returns human-readable problems; empty means good. This is the regression
    guard behind invariant 38 -- the LSM twin of seccomp's invariant 31. A
    permissive `mount,` catch-all, a widened rule set, a quietly deleted `deny`,
    or a hand-edit all have to fail here (and so in CI and in `harness doctor`)
    rather than sail through because the jail still starts.

    Offline and structural on purpose: it runs in the host test tier, where
    there is neither network nor an AppArmor kernel to ask.
    """
    problems: list[str] = []
    _, body = split_header(text)

    if f"profile {PROFILE_NAME} " not in body:
        problems.append(
            f"profile is not named {PROFILE_NAME!r} -- `--security-opt "
            "apparmor=<name>` selects by name, so a renamed profile is an "
            "unloadable one"
        )

    rules = _mount_rules(body)
    if DENY_MOUNT_RULE in rules:
        problems.append(
            f"{DENY_MOUNT_RULE!r} is still present -- this is stock docker-default; "
            "bwrap will fail at its first mount"
        )
    if any(rule == "mount," for rule in rules):
        problems.append(
            "profile grants a bare `mount,` catch-all -- that permits every mount "
            "operation, which is `apparmor=unconfined` in all but name"
        )
    expected = list(RELAXED_MOUNT_RULES)
    if rules != expected:
        extra = [r for r in rules if r not in expected]
        missing = [r for r in expected if r not in rules]
        detail = []
        if extra:
            detail.append(f"unexpected {extra}")
        if missing:
            detail.append(f"missing {missing}")
        if not detail:
            detail.append(f"order drifted: {rules}")
        problems.append("mount rule set drifted from RELAXED_MOUNT_RULES: " + ", ".join(detail))

    for rule in CRITICAL_DENY_RULES:
        if rule not in body:
            problems.append(
                f"upstream deny rule missing: {rule!r} -- the relaxation must narrow "
                "the mount rule only, not shed other protections"
            )

    claimed = recorded_sha256(text)
    actual = body_sha256(text)
    if claimed is None:
        problems.append(
            "no `# holder-profile-sha256:` header -- the artifact is generated; "
            "regenerate with 'python3 -m harness apparmor-sync'"
        )
    elif claimed != actual:
        problems.append(
            f"body hash {actual} does not match the recorded {claimed} -- the "
            "profile was hand-edited; regenerate rather than patch"
        )

    if baseline is not None:
        _, baseline_body = split_header(baseline)
        try:
            expected_body = relax_mount(baseline_body)
        except TemplateError as exc:
            problems.append(f"vendored upstream baseline is unusable: {exc}")
        else:
            if expected_body.strip() != body.strip():
                problems.append(
                    "profile is not exactly relax_mount(vendored upstream baseline) -- "
                    "something other than the mount rule differs from docker-default; "
                    "diff apparmor/deepagent-userns against apparmor/"
                    f"{BASELINE_FILENAME}"
                )

    return problems


def verify_baseline(text: str) -> list[str]:
    """Sanity-check the vendored *upstream* render before trusting it as a base."""
    problems: list[str] = []
    _, body = split_header(text)
    if DENY_MOUNT_RULE not in body:
        problems.append(
            f"vendored baseline has no {DENY_MOUNT_RULE!r} -- it is not stock "
            "docker-default, so the diff property means nothing"
        )
    for rule in CRITICAL_DENY_RULES:
        if rule not in body:
            problems.append(f"vendored baseline is missing upstream rule {rule!r}")
    claimed = recorded_sha256(text)
    if claimed is not None and claimed != body_sha256(text):
        problems.append("vendored baseline body hash does not match its header")
    return problems


# --- CLI ---------------------------------------------------------------------


def _fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 (pinned https)
        return response.read().decode("utf-8")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def apparmor_sync_main(argv: list[str]) -> int:
    """Dev-time regeneration of the vendored profile. Needs network, no keys."""
    parser = argparse.ArgumentParser(
        prog="harness apparmor-sync",
        description=(
            "Regenerate the vendored narrowed AppArmor profile from moby's "
            "docker-default template."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed artifacts and exit; write nothing, fetch nothing",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="destination for the narrowed profile (default: apparmor/deepagent-userns)",
    )
    args = parser.parse_args(argv)

    out = args.out or profile_path()
    base_out = baseline_path() if args.out is None else args.out.parent / BASELINE_FILENAME

    if args.check:
        try:
            profile = load_text(out)
        except FileNotFoundError:
            print(f"[apparmor] missing vendored profile: {out}", file=sys.stderr)
            return 1
        baseline: str | None
        try:
            baseline = load_text(base_out)
        except FileNotFoundError:
            print(
                f"[apparmor] missing vendored upstream baseline: {base_out} -- "
                "checking structurally only",
                file=sys.stderr,
            )
            baseline = None
        problems = verify_profile(profile, baseline)
        if baseline is not None:
            problems += verify_baseline(baseline)
        for problem in problems:
            print(f"[apparmor] {problem}", file=sys.stderr)
        if problems:
            return 1
        print(
            f"[apparmor] {out} is moby {MOBY_TAG}'s docker-default with exactly "
            f"{len(RELAXED_MOUNT_RULES)} mount rules replacing `{DENY_MOUNT_RULE}`"
        )
        return 0

    try:
        source = _fetch(MOBY_TEMPLATE_URL)
    except (urllib.error.URLError, TimeoutError, UnicodeDecodeError) as exc:
        print(f"[apparmor] could not fetch {MOBY_TEMPLATE_URL}: {exc}", file=sys.stderr)
        return 1

    try:
        rendered = render_template(extract_template(source))
        relaxed = relax_mount(rendered)
    except TemplateError as exc:
        print(f"[apparmor] refusing to write: {exc}", file=sys.stderr)
        return 1

    baseline_text = _with_header(
        rendered, f"Upstream docker-default, rendered UNMODIFIED. Diff base for {PROFILE_FILENAME}."
    )
    profile_text = _with_header(
        relaxed,
        f"docker-default with `{DENY_MOUNT_RULE}` narrowed to the mounts bwrap needs (M4 slice J).",
    )

    problems = verify_profile(profile_text, baseline_text) + verify_baseline(baseline_text)
    if problems:
        for problem in problems:
            print(f"[apparmor] refusing to write: {problem}", file=sys.stderr)
        return 1

    _write(base_out, baseline_text)
    _write(out, profile_text)
    print(f"[apparmor] wrote {base_out} (upstream render, moby {MOBY_TAG})")
    print(f"[apparmor] wrote {out} (+{len(RELAXED_MOUNT_RULES)} mount rules)")
    print(f"[apparmor] load it on the DOCKER DAEMON's host: sudo scripts/install-apparmor-profile.sh")
    return 0
