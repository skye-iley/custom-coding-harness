"""A tiny bounded cache."""


class Cache:
    """Keeps at most `capacity` entries, evicting the oldest first.

    Two properties callers rely on:
      * an evicted key is really gone -- `get` returns the default for it
      * evicting one key does NOT disturb the others
    """

    def __init__(self, capacity=3):
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self.capacity = capacity
        self._data = {}
        self._order = []

    def put(self, key, value):
        if key not in self._data:
            self._order.append(key)
        self._data[key] = value
        # BUG: eviction drops the key from the ordering but leaves the value in
        # `_data`, so an evicted key is still returned by `get`.
        while len(self._order) > self.capacity:
            self._order.pop(0)

    def get(self, key, default=None):
        return self._data.get(key, default)

    def keys(self):
        return list(self._order)

    def __len__(self):
        return len(self._order)
