"""A thin request-handling layer over the service module."""

import service


def handle_delete_request(user_id):
    service.remove_profile(user_id)
    return {"status": "deleted", "user_id": user_id}


def handle_get_request(user_id):
    profile = service.get_profile(user_id)
    if profile is None:
        return {"status": "not_found", "user_id": user_id}
    return {"status": "ok", "profile": profile}
