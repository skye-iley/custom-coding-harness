"""The benchmark dataset format (Milestone 8, slice B3).

One JSON object per line. Tier 1 points each instance at a **local directory**
with its own git history — that is what makes the ladder free and offline. `repo`
and a real `base_commit` are reserved for tier 3 (clone-at-commit) and parse as
optional today, so the format does not change under tiers 2 and 3.

```json
{
  "instance_id": "gold-001-off-by-one",
  "workspace": "benchmarks/gold/001",
  "base_commit": "HEAD",
  "task_prompt": "The paginator returns one row too many on the last page. Fix it.",
  "fail_to_pass": ["pytest tests/test_paginate.py::test_last_page"],
  "pass_to_pass": ["pytest tests/test_paginate.py"]
}
```

`fail_to_pass` / `pass_to_pass` are literal pytest node-id commands and are
**carried, not run** — scoring is the official evaluation harness's job
(`milestone8.md` §9), and a scorer written here would be a number nobody else
could compare against.

Stdlib only: no yaml, no pydantic, no runtime stack.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


class DatasetError(ValueError):
    """A dataset that cannot be trusted. Always names the line.

    Malformed lines are **fatal, not skipped**. A dataset is authored, so a bad
    line is a bug in the file — and a sweep that silently ran 4 of 5 instances
    would report a pass rate over a set nobody chose. That is the same class of
    silent partiality invariant 18 exists to forbid.
    """


@dataclass(frozen=True)
class Instance:
    """One benchmark task."""

    instance_id: str
    workspace: str
    task_prompt: str
    base_commit: str = "HEAD"
    fail_to_pass: tuple[str, ...] = field(default_factory=tuple)
    pass_to_pass: tuple[str, ...] = field(default_factory=tuple)
    # Tier 3's clone-at-commit source. Parsed and carried so the format is stable
    # across tiers; nothing in tier 1 reads it.
    repo: str | None = None

    def resolve_workspace(self, dataset_path: Path | str) -> Path:
        """The instance directory, resolved **relative to the dataset file**.

        Relative to the dataset rather than the process CWD so a dataset is
        relocatable as a unit and a sweep does not depend on where it was
        launched from.
        """
        ws = Path(self.workspace)
        if ws.is_absolute():
            return ws
        return (Path(dataset_path).resolve().parent / ws).resolve()


_REQUIRED = ("instance_id", "workspace", "task_prompt")
_KNOWN = (*_REQUIRED, "base_commit", "fail_to_pass", "pass_to_pass", "repo")


def _as_commands(value, where: str, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return tuple(value)
    raise DatasetError(f"{where}: {key} must be a string or a list of strings")


def parse_instance(obj: dict, where: str) -> Instance:
    """One parsed object, or `DatasetError` naming what is wrong and where."""
    if not isinstance(obj, dict):
        raise DatasetError(f"{where}: expected a JSON object")
    missing = [k for k in _REQUIRED if not str(obj.get(k) or "").strip()]
    if missing:
        raise DatasetError(f"{where}: missing required field(s) {', '.join(missing)}")
    unknown = sorted(set(obj) - set(_KNOWN))
    if unknown:
        # Loud rather than ignored: a typo'd key (`task_promt`) would otherwise
        # fall through to "missing task_prompt" one field later, or worse, be
        # silently dropped once a future field makes it optional.
        raise DatasetError(f"{where}: unknown field(s) {', '.join(unknown)}")
    return Instance(
        instance_id=str(obj["instance_id"]).strip(),
        workspace=str(obj["workspace"]).strip(),
        task_prompt=str(obj["task_prompt"]),
        base_commit=str(obj.get("base_commit") or "HEAD").strip(),
        fail_to_pass=_as_commands(obj.get("fail_to_pass"), where, "fail_to_pass"),
        pass_to_pass=_as_commands(obj.get("pass_to_pass"), where, "pass_to_pass"),
        repo=(str(obj["repo"]).strip() if obj.get("repo") else None),
    )


def load_dataset(path: Path | str) -> list[Instance]:
    """Parse a jsonl dataset. Blank lines and `#` comments are skipped.

    Duplicate `instance_id`s are rejected: both output files are keyed by it, and
    invariant 18 requires every instance to appear in each **exactly once**.
    """
    path = Path(path)
    if not path.is_file():
        raise DatasetError(f"dataset not found: {path}")
    out: list[Instance] = []
    seen: set[str] = set()
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        where = f"{path.name}:{lineno}"
        try:
            obj = json.loads(line)
        except ValueError as exc:
            raise DatasetError(f"{where}: not valid JSON ({exc})") from exc
        inst = parse_instance(obj, where)
        if inst.instance_id in seen:
            raise DatasetError(f"{where}: duplicate instance_id {inst.instance_id!r}")
        seen.add(inst.instance_id)
        out.append(inst)
    if not out:
        raise DatasetError(f"{path}: no instances")
    return out


def select(instances: list[Instance], *, only: tuple[str, ...] = (),
           limit: int | None = None) -> list[Instance]:
    """Apply `--only` then `--limit`, preserving dataset order.

    An `--only` id that matches nothing is an error, not an empty sweep: the
    usual cause is a typo, and silently running zero instances then reporting a
    clean sweep is the worst possible answer.
    """
    chosen = list(instances)
    if only:
        wanted = set(only)
        chosen = [i for i in chosen if i.instance_id in wanted]
        missing = sorted(wanted - {i.instance_id for i in chosen})
        if missing:
            raise DatasetError(f"--only names unknown instance(s): {', '.join(missing)}")
    if limit is not None:
        if limit < 1:
            raise DatasetError(f"--limit must be at least 1, got {limit}")
        chosen = chosen[:limit]
    return chosen
