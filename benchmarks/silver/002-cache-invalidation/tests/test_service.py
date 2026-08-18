import api
import service


def test_a_deleted_profile_is_reported_not_found_through_the_api():
    service.set_profile("u1", {"name": "Ada"})
    api.handle_delete_request("u1")
    assert api.handle_get_request("u1") == {"status": "not_found", "user_id": "u1"}


def test_setting_and_reading_a_profile_still_works_through_the_api():
    service.set_profile("u2", {"name": "Grace"})
    assert api.handle_get_request("u2") == {"status": "ok", "profile": {"name": "Grace"}}


def test_the_store_itself_is_unaffected_by_the_cache_layer():
    from store import Store
    s = Store()
    s.set("k", 1)
    s.delete("k")
    assert s.get("k") is None
