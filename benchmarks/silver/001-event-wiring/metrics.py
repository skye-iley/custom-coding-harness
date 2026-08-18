"""Counts user registrations, independent of the audit log."""

COUNT = 0


def _bump(**_payload):
    global COUNT
    COUNT += 1


def wire_metrics(bus):
    bus.subscribe("user_created", _bump)
