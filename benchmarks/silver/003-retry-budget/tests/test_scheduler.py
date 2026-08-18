from budget import RetryBudget
from scheduler import run_batch
from worker import run_job


def _flaky(fail_times):
    """A job that raises `fail_times` times, then succeeds."""
    state = {"calls": 0}

    def job():
        state["calls"] += 1
        if state["calls"] <= fail_times:
            raise RuntimeError("not yet")

    return job


def _always_fails():
    raise RuntimeError("nope")


def test_the_retry_budget_is_shared_across_the_whole_batch():
    # Each job needs exactly 2 retries to succeed (the 3rd call succeeds). A
    # budget of 2 total retries can fund only ONE of the two jobs if it's
    # really shared -- the second job gets whatever is left over, which is
    # nothing.
    jobs = [_flaky(2), _flaky(2)]
    results = run_batch(jobs, total_retries=2)
    assert results == [True, False]


def test_a_single_job_still_succeeds_within_its_budget():
    budget = RetryBudget(3)
    job = _flaky(2)
    assert run_job(job, budget) is True
    assert budget.remaining == 1  # started at 3, spent 2


def test_a_job_that_never_succeeds_exhausts_the_budget_and_reports_failure():
    budget = RetryBudget(2)
    assert run_job(_always_fails, budget) is False
    assert budget.remaining == 0
