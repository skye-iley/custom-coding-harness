import audit
import metrics
from app import register_user
from bus import EventBus


def test_registering_a_user_is_recorded_in_the_audit_log():
    audit.LOG.clear()
    register_user("Ada")
    assert audit.LOG == [{"name": "Ada"}]


def test_registering_a_user_bumps_the_metrics_counter():
    metrics.COUNT = 0
    register_user("Grace")
    assert metrics.COUNT == 1


def test_the_bus_still_fires_handlers_registered_directly():
    bus = EventBus()
    seen = []
    bus.subscribe("ping", lambda **kw: seen.append(kw))
    bus.publish("ping", n=1)
    assert seen == [{"n": 1}]
