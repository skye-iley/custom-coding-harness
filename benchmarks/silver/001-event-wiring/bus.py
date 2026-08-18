"""A tiny synchronous event bus."""


class EventBus:
    def __init__(self):
        self._handlers = {}

    def subscribe(self, event_name, handler):
        self._handlers.setdefault(event_name, []).append(handler)

    def unsubscribe(self, event_name, handler):
        handlers = self._handlers.get(event_name, [])
        if handler in handlers:
            handlers.remove(handler)

    def publish(self, event_name, **payload):
        for handler in list(self._handlers.get(event_name, [])):
            handler(**payload)
