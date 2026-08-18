from cache import Cache


def test_put_and_get():
    c = Cache(capacity=3)
    c.put("a", 1)
    assert c.get("a") == 1
    assert c.get("missing") is None


def test_overwriting_does_not_grow_the_cache():
    c = Cache(capacity=3)
    c.put("a", 1)
    c.put("a", 2)
    assert len(c) == 1
    assert c.get("a") == 2


def test_recent_keys_survive_eviction():
    c = Cache(capacity=3)
    for key, value in [("a", 1), ("b", 2), ("c", 3), ("d", 4)]:
        c.put(key, value)
    assert c.keys() == ["b", "c", "d"]
    assert [c.get(k) for k in ("b", "c", "d")] == [2, 3, 4]


def test_evicted_key_is_really_gone():
    c = Cache(capacity=3)
    for key, value in [("a", 1), ("b", 2), ("c", 3), ("d", 4)]:
        c.put(key, value)
    assert c.get("a") is None
