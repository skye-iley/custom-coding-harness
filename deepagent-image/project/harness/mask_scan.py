"""CLI wrapper for mask resolution — invoked as ``python3 -m harness mask-scan``.

Calls ``mask.resolve``, prints the §9.3 stdout grammar, writes the snapshot,
and warns on protection reduction. No matcher logic here.
"""

from __future__ import annotations

import sys
from pathlib import Path

from harness import archive
from harness.mask import MODE_DENY, format_scan_lines, resolve


def mask_scan_main(argv: list[str]) -> int:
    """Run the mask scan and emit the §9.3 grammar on stdout.

    Args:
        argv: Remaining CLI args (unused currently; reserved for future flags).

    Returns:
        0 on success, 1 on error.

    Reads workspace from ``AGENT_WORKSPACE`` env var and state dir from
    ``DEEPAGENTS_STATE_DIR`` (falling back to archive.state_dir heuristic).
    """
    workspace_raw = argv[0] if argv else None
    state_dir_raw = argv[1] if len(argv) > 1 else None

    if workspace_raw:
        workspace = Path(workspace_raw)
    else:
        import os
        raw = os.environ.get("AGENT_WORKSPACE", "/project/workspace")
        workspace = Path(raw)

    if state_dir_raw:
        state_dir = Path(state_dir_raw)
    else:
        import os
        raw = os.environ.get("DEEPAGENTS_STATE_DIR", "")
        if raw:
            state_dir = Path(raw)
        else:
            state_dir = archive.state_dir(workspace)

    if not workspace.is_dir():
        print(f"mask-scan: workspace {workspace} is not a directory", file=sys.stderr)
        return 1

    state_dir.mkdir(parents=True, exist_ok=True)

    try:
        result = resolve(str(workspace), str(state_dir))
    except SystemExit as exc:
        print(f"mask-scan: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"mask-scan: error: {exc}", file=sys.stderr)
        return 1

    for line in format_scan_lines(result):
        print(line)

    if result.warnings:
        for w in result.warnings:
            print(f"mask-scan: warning: {w}", file=sys.stderr)

    if result.protection_reduced:
        print(
            f"mask-scan: protection reduced — {len(result.reduced_paths)} path(s) "
            f"no longer masked: {', '.join(result.reduced_paths)}",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(mask_scan_main(sys.argv[1:]))
