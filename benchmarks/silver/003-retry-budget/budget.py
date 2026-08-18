"""A shared retry budget: only so many retries total, across every job."""


class RetryBudget:
    def __init__(self, total):
        self.remaining = total

    def take(self):
        """Consume one retry from the budget.

        Returns True if one was available, False if the budget is exhausted.
        """
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True
