"""Wires the event bus for the app's user-registration flow."""

from audit import wire_audit_log
from bus import EventBus
from metrics import wire_metrics

bus = EventBus()
wire_audit_log(bus)
wire_metrics(bus)


def register_user(name):
    bus.publish("user_created", name=name)
    return {"name": name}
