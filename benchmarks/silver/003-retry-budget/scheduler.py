"""Runs a batch of jobs that all draw from ONE shared retry budget."""

from budget import RetryBudget
from worker import run_job


def run_batch(jobs, total_retries):
    shared = RetryBudget(total_retries)
    results = []
    for job in jobs:
        # BUG: builds a FRESH RetryBudget(total_retries) per job instead of
        # reusing `shared` -- every job gets its own private allowance, so a
        # five-job batch with a budget of 2 can spend 10 retries total, not 2.
        results.append(run_job(job, RetryBudget(total_retries)))
    return results
