"""Records every event the app cares about, for later inspection."""

LOG = []


def record(**payload):
    LOG.append(payload)


def wire_audit_log(bus):
    # BUG: subscribes under "user.created" (dot), but the app publishes
    # "user_created" (underscore) -- the handler is registered, just never
    # under the name that actually fires.
    bus.subscribe("user.created", record)
