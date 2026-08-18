"""Hard stops for a run: the step, wall-clock and turn bounds (Milestone 8, B1).

Stdlib only, no langchain — the same split ``telemetry.py``/``TelemetryMiddleware``
and ``rawtrace.py``/``RawTraceMiddleware`` already use, and for the same reason:
the arithmetic stays in the host test tier while the one class that needs the
langchain base (``cli.DeadlineMiddleware``) lives beside the other middleware.

**Why this exists at all** (``milestone8.md`` §3). A headless benchmark instance is
*one* turn, so a turn-count cap bounds nothing: the runaway is the ReAct loop
*inside* that turn. The only thing bounding it today is LangGraph's
``recursion_limit``, which the harness never sets, and whose inherited default on
the pinned version is ``10007`` — no bound at all on a free local model where no
cost accrues to stop it. Hence three bounds, not one:

===============  =======================================  ========================
Knob             Bounds                                   Seam
===============  =======================================  ========================
``--max-steps``  the ReAct loop inside one turn           ``config["recursion_limit"]``
``--max-seconds`` wall clock for the whole session        ``DeadlineMiddleware.before_model``
``--max-turns``  turns in a session                       ``Deadline``-free counter, checked in ``run_turn``
===============  =======================================  ========================

**Absent, not infinite.** Every bound here takes ``None`` to mean *no bound*, and
``None`` must stay structural: ``cli.main`` does not put ``recursion_limit`` in the
graph config at all when ``--max-steps`` is unset, no deadline object is built when
``--max-seconds`` is unset, and no counter is compared when ``--max-turns`` is. A
very large number would look the same in a passing test and be a behaviour change
(M7 invariant 18's lesson).

**Why a step-boundary clock and not a signal or a watchdog thread.** ``SIGALRM``
does not exist on Windows and the host tier must stay cross-platform; a watchdog
thread cannot interrupt a blocking HTTP read without cancelling mid-write. A
step-boundary check overshoots by at most one model call, which for a benchmark
bound is the right trade — the number that matters is "this instance did not run
for an hour", not "this instance stopped at 600.000s".
"""

from __future__ import annotations

import time

# The value of a record's `stop_reason`, telling the three bounds apart under one
# `stopped` outcome. A sweep must be able to ask *which* ceiling it is measuring:
# one outcome with three causes cannot answer that, and the three lead to
# different actions (raise the bound / fix the loop / the run was simply long).
STOP_STEPS = "steps"
STOP_SECONDS = "seconds"
STOP_TURNS = "turns"
STOP_REASONS = (STOP_STEPS, STOP_SECONDS, STOP_TURNS)

# The process exit code for a run a bound stopped, distinct from 0 (finished) and
# from 1 (the harness broke) for the same reason `OUTCOME_STOPPED` is distinct
# from `error`: a sweep reading only exit codes must still be able to tell "did
# not converge" from "crashed". Numbered next to `interrupt.EXIT_INTERRUPT_ABORT`
# (42), which is the same kind of labelled non-failure exit.
EXIT_STOPPED = 43


class DeadlineExceeded(Exception):
    """``--max-seconds`` elapsed; raised at the next step boundary.

    Lives here rather than in a shared errors module, following the convention
    the rest of the harness already uses — an exception belongs with its
    subsystem (``BudgetExceeded`` in ``cost.py``, ``HaltTurn`` in ``hitl.py``,
    ``PathGuardDenied`` in ``pathguard.py``). ``cost.py`` in particular is the
    *wrong* home despite owning ``BudgetExceeded``: a clock bound is not cost, and
    ``cost.py`` sits under an acyclic import guard a new concern should not be
    pushed through (``milestone8.md`` §12 fork 2).
    """

    stop_reason = STOP_SECONDS

    def __init__(self, limit_seconds: float, elapsed_seconds: float):
        self.limit_seconds = limit_seconds
        self.elapsed_seconds = elapsed_seconds
        super().__init__(
            f"session deadline of {limit_seconds:g}s exceeded "
            f"({elapsed_seconds:.1f}s elapsed)"
        )


class TurnLimitExceeded(Exception):
    """``--max-turns`` reached; raised instead of starting one more turn."""

    stop_reason = STOP_TURNS

    def __init__(self, limit_turns: int):
        self.limit_turns = limit_turns
        super().__init__(f"turn limit of {limit_turns} reached")


class Deadline:
    """A session wall-clock bound, checked at step boundaries.

    Monotonic (``time.monotonic``), never the wall calendar: a clock adjustment
    mid-sweep must not shorten or extend a bound. ``clock`` is injectable so the
    arithmetic is testable without sleeping.

    ``seconds=None`` is not constructed by ``cli`` at all (see the module note on
    absent-not-infinite); the class still tolerates it so a caller holding an
    optional deadline can call ``check()`` unconditionally.
    """

    def __init__(self, seconds: float | None, clock=time.monotonic):
        self.seconds = seconds
        self._clock = clock
        self.started = clock()

    def elapsed(self) -> float:
        return self._clock() - self.started

    def remaining(self) -> float | None:
        """Seconds left, or ``None`` when unbounded. Can go negative — a caller
        printing it wants to know by how much the bound was blown."""
        if self.seconds is None:
            return None
        return self.seconds - self.elapsed()

    def expired(self) -> bool:
        if self.seconds is None:
            return False
        return self.elapsed() >= self.seconds

    def check(self) -> None:
        """Raise ``DeadlineExceeded`` if the bound has been crossed."""
        if self.expired():
            raise DeadlineExceeded(self.seconds, self.elapsed())


class TurnCounter:
    """How many turns a session has started, against ``--max-turns``.

    ``begin()`` is called at the top of every turn and is the whole protocol:
    it raises when the limit is already reached, and otherwise counts this turn.
    So ``--max-turns K`` lets exactly ``K`` turns *run* and refuses the ``K+1``th
    — the refusal is the stop event, and it is what carries ``stop_reason:
    "turns"`` into the ledger.

    Checked at the top of the turn rather than at its end deliberately: marking a
    turn that ran to completion as ``stopped`` would claim the harness cut it off
    when it did not, and the point of the outcome split is that a record says what
    actually happened.
    """

    def __init__(self, limit: int | None):
        self.limit = limit
        self.count = 0

    def exhausted(self) -> bool:
        if self.limit is None:
            return False
        return self.count >= self.limit

    def check(self) -> None:
        if self.exhausted():
            raise TurnLimitExceeded(self.limit)

    def begin(self) -> int:
        """Start a turn: raise if none is left, else count it and return its
        1-based index."""
        self.check()
        self.count += 1
        return self.count


def stop_reason_for(exc: BaseException) -> str | None:
    """Which bound stopped this turn, or ``None`` when the exception is not a stop.

    ``GraphRecursionError`` is matched **by name** rather than imported: this
    module is stdlib-only by contract (it is what keeps the arithmetic in the host
    test tier), and langgraph is not importable there. The name is also the stable
    part — the class has moved between langgraph modules across versions.
    """
    reason = getattr(exc, "stop_reason", None)
    if reason in STOP_REASONS:
        return reason
    if type(exc).__name__ == "GraphRecursionError":
        return STOP_STEPS
    return None


def is_stop(exc: BaseException) -> bool:
    """True when the turn ended because a bound the operator set fired."""
    return stop_reason_for(exc) is not None
