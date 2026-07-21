"""Tests for harness/resilience.py (Milestone 3 slice P1).

Pure: resilience.py is stdlib-only (no langchain), so the classification and
backoff math run on a bare host. The retry loop takes injected ``sleep`` / ``rand``
so it is deterministic and instant here — no real time passes.
"""

from __future__ import annotations

import pytest

from _bootstrap import _load

res = _load("harness.resilience")


# --- classification ----------------------------------------------------------

class _Statused(Exception):
    def __init__(self, msg, status_code):
        super().__init__(msg)
        self.status_code = status_code


class _Response:
    def __init__(self, status_code):
        self.status_code = status_code


class _WithResponse(Exception):
    def __init__(self, msg, status_code):
        super().__init__(msg)
        self.response = _Response(status_code)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retryable_statuses(status):
    assert res.is_retryable(_Statused("boom", status))


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_non_retryable_client_statuses(status):
    assert not res.is_retryable(_Statused("nope", status))


def test_status_read_off_nested_response():
    assert res.is_retryable(_WithResponse("upstream", 503))
    assert not res.is_retryable(_WithResponse("bad request", 400))


def test_bool_attr_not_mistaken_for_status():
    # A truthy non-int `status`-ish attr must not be read as HTTP 1.
    class E(Exception):
        code = True

    assert not res.is_retryable(E("weird"))


@pytest.mark.parametrize(
    "msg",
    [
        "Connection reset by peer",
        "Read timed out.",
        "Service Unavailable",
        "the model is overloaded, please try again",
        "Error code: 503 - upstream",
    ],
)
def test_retryable_by_message(msg):
    assert res.is_retryable(Exception(msg))


def test_embedded_4xx_other_than_429_not_retryable():
    assert not res.is_retryable(Exception("Error code: 404 - not found"))


@pytest.mark.parametrize(
    "msg",
    [
        "This model's maximum context length is 8192 tokens",
        "context_length_exceeded",
        "Please reduce the length of the messages",
        "input is too long for the context window",
    ],
)
def test_context_overflow_detected(msg):
    assert res.is_context_overflow(Exception(msg))
    # Overflow is NOT a blind-retry case — it needs trimming.
    assert not res.is_retryable(Exception(msg))


def test_plain_error_is_neither():
    exc = Exception("invalid api key")
    assert not res.is_retryable(exc)
    assert not res.is_context_overflow(exc)


# --- env knobs ---------------------------------------------------------------

def test_env_defaults():
    assert res.max_retries_from_env({}) == 3
    assert res.retry_base_from_env({}) == 0.5


def test_env_overrides():
    assert res.max_retries_from_env({"DEEPAGENTS_MAX_RETRIES": "5"}) == 5
    assert res.retry_base_from_env({"DEEPAGENTS_RETRY_BASE": "1.5"}) == 1.5


def test_env_invalid_falls_back():
    assert res.max_retries_from_env({"DEEPAGENTS_MAX_RETRIES": "abc"}) == 3
    assert res.retry_base_from_env({"DEEPAGENTS_RETRY_BASE": "-2"}) == 0.5
    assert res.max_retries_from_env({"DEEPAGENTS_MAX_RETRIES": "-1"}) == 0


# --- backoff math ------------------------------------------------------------

def test_compute_delay_full_jitter_bounds():
    # rand=1 -> the ceiling; rand=0 -> zero. Ceiling doubles per attempt.
    assert res.compute_delay(0, 0.5, rand=lambda: 1.0) == 0.5
    assert res.compute_delay(1, 0.5, rand=lambda: 1.0) == 1.0
    assert res.compute_delay(2, 0.5, rand=lambda: 1.0) == 2.0
    assert res.compute_delay(3, 0.5, rand=lambda: 0.0) == 0.0


def test_compute_delay_capped():
    # A huge attempt is capped, not astronomically large.
    assert res.compute_delay(100, 1.0, rand=lambda: 1.0) == pytest.approx(20.0)


# --- retry loop --------------------------------------------------------------

def test_retry_succeeds_first_try_no_sleep():
    slept = []
    calls = []

    def fn():
        calls.append(1)
        return "ok"

    out = res.retry_call(fn, max_retries=3, base=0.5, sleep=slept.append)
    assert out == "ok"
    assert len(calls) == 1
    assert slept == []


def test_retry_then_succeed():
    slept = []
    state = {"n": 0}

    def fn():
        state["n"] += 1
        if state["n"] < 3:
            raise _Statused("overloaded", 503)
        return "recovered"

    out = res.retry_call(
        fn, max_retries=3, base=0.5, sleep=slept.append, rand=lambda: 1.0
    )
    assert out == "recovered"
    assert state["n"] == 3
    assert slept == [0.5, 1.0]  # two backoffs before the third, successful call


def test_retry_exhausts_and_reraises_last():
    slept = []

    def fn():
        raise _Statused("still 429", 429)

    with pytest.raises(_Statused):
        res.retry_call(fn, max_retries=2, base=0.5, sleep=slept.append, rand=lambda: 0.0)
    assert len(slept) == 2  # max_retries backoffs, then re-raise


# --- retry_after_seconds (reactive: honor the server's wait) -----------------

import datetime as _dt


class _RetryDelayAttr(Exception):
    def __init__(self, msg, retry_delay):
        super().__init__(msg)
        self.retry_delay = retry_delay
        self.status_code = 429


class _Duration:  # protobuf-Duration-like
    def __init__(self, seconds):
        self.seconds = seconds


class _HeaderResp:
    def __init__(self, headers):
        self.headers = headers


class _WithHeaders(Exception):
    def __init__(self, msg, headers):
        super().__init__(msg)
        self.response = _HeaderResp(headers)
        self.status_code = 429


def test_retry_after_from_timedelta_attr():
    exc = _RetryDelayAttr("quota", _dt.timedelta(seconds=47))
    assert res.retry_after_seconds(exc) == 47.0


def test_retry_after_from_protobuf_duration():
    exc = _RetryDelayAttr("quota", _Duration(seconds=12))
    assert res.retry_after_seconds(exc) == 12.0


def test_retry_after_from_numeric_attr():
    exc = _RetryDelayAttr("quota", 8)
    assert res.retry_after_seconds(exc) == 8.0


def test_retry_after_from_header():
    assert res.retry_after_seconds(_WithHeaders("429", {"Retry-After": "30"})) == 30.0


def test_retry_after_from_google_proto_text():
    msg = "429 Resource exhausted. retry_delay { seconds: 53 }"
    assert res.retry_after_seconds(_Statused(msg, 429)) == 53.0


def test_retry_after_from_phrase():
    assert res.retry_after_seconds(_Statused("Rate limited, retry in 19s", 429)) == 19.0


def test_retry_after_none_when_absent():
    assert res.retry_after_seconds(_Statused("boom", 500)) is None


def test_retry_call_uses_server_delay_over_jitter():
    slept = []
    state = {"n": 0}

    def fn():
        state["n"] += 1
        if state["n"] < 2:
            raise _RetryDelayAttr("quota", _dt.timedelta(seconds=17))
        return "ok"

    out = res.retry_call(fn, max_retries=3, base=0.5, sleep=slept.append, rand=lambda: 1.0)
    assert out == "ok"
    assert slept == [17.0]  # server delay, not the 0.5 jittered backoff


def test_retry_call_caps_absurd_server_delay():
    def fn():
        raise _RetryDelayAttr("quota", _Duration(seconds=99999))

    slept = []
    with pytest.raises(_RetryDelayAttr):
        res.retry_call(fn, max_retries=1, base=0.5, sleep=slept.append)
    assert slept == [120.0]  # _SERVER_DELAY_CAP_SECONDS


def test_non_retryable_propagates_immediately():
    slept = []
    calls = []

    def fn():
        calls.append(1)
        raise _Statused("bad key", 401)

    with pytest.raises(_Statused):
        res.retry_call(fn, max_retries=5, base=0.5, sleep=slept.append)
    assert len(calls) == 1  # no retry on a non-retryable status
    assert slept == []


def test_context_overflow_not_retried_by_loop():
    calls = []

    def fn():
        calls.append(1)
        raise Exception("maximum context length exceeded")

    with pytest.raises(Exception, match="context"):
        res.retry_call(fn, max_retries=5, base=0.5, sleep=lambda d: None)
    assert len(calls) == 1


def test_on_retry_observer_called():
    seen = []

    def fn():
        raise _Statused("503", 503)

    with pytest.raises(_Statused):
        res.retry_call(
            fn,
            max_retries=2,
            base=0.5,
            sleep=lambda d: None,
            rand=lambda: 1.0,
            on_retry=lambda attempt, exc, delay: seen.append((attempt, delay)),
        )
    assert seen == [(1, 0.5), (2, 1.0)]


# --- trim stopgap ------------------------------------------------------------

def test_trim_messages_keeps_last_n():
    msgs = list(range(10))
    assert res.trim_messages(msgs, 3) == [7, 8, 9]
    assert res.trim_messages(msgs, 0) == []
    assert res.trim_messages(msgs, 100) == msgs
