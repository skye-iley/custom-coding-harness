"""A read-through cache in front of a Store.

Reads are served from the cache when present; writes go to both the cache and
the store; deletes must clear the cache entry too, or a deleted key keeps
serving its last cached value forever.
"""


class Cache:
    def __init__(self, store):
        self._store = store
        self._cache = {}

    def get(self, key):
        if key not in self._cache:
            self._cache[key] = self._store.get(key)
        return self._cache[key]

    def set(self, key, value):
        self._store.set(key, value)
        self._cache[key] = value

    def delete(self, key):
        # BUG: forgets to remove `key` from self._cache, so a deleted key
        # keeps serving its last cached value through get() even though the
        # backing store no longer has it.
        self._store.delete(key)
