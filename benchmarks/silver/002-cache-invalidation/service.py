"""Business logic: user profile lookups through the cache."""

from cache import Cache
from store import Store

_store = Store()
_cache = Cache(_store)


def set_profile(user_id, profile):
    _cache.set(user_id, profile)


def get_profile(user_id):
    return _cache.get(user_id)


def remove_profile(user_id):
    _cache.delete(user_id)
