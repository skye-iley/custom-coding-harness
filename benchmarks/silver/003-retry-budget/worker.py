"""Runs one job against a shared retry budget."""


def run_job(job, budget):
    """Call `job()` (a zero-arg callable that raises on failure) until it
    succeeds or the budget runs out. Returns True on success, False if the
    budget was exhausted first.
    """
    while True:
        try:
            job()
            return True
        except Exception:
            if not budget.take():
                return False
