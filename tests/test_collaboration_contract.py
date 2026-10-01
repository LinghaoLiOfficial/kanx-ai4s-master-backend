from fastapi.routing import APIRoute

from kanx_ai4s_master.modules.collaboration.router import INVITE_TTL, _digest, router
from kanx_ai4s_master.modules.email.service import render_template


def test_invitation_tokens_are_stored_as_digests() -> None:
    assert _digest("secret-token") != "secret-token"
    assert len(_digest("secret-token")) == 64
    assert INVITE_TTL.days == 7


def test_group_invitation_email_contains_confirmation_link() -> None:
    subject, body = render_template(
        "group_invite",
        {
            "url": "https://app.test/workspace/invitations/token",
            "group_name": "Research",
            "inviter": "Lin",
        },
    )
    assert subject == "Group invitation"
    assert "Research" in body
    assert "/workspace/invitations/token" in body


def test_collaboration_routes_are_registered() -> None:
    paths = {route.path for route in router.routes if isinstance(route, APIRoute)}
    assert "/groups" in paths
    assert "/groups/{group_id}/members" in paths
    assert "/invitations/{token}/{action}" in paths
    assert "/invitations/by-id/{invitation_id}/{action}" in paths
    assert "/notifications" in paths


def test_user_search_accepts_single_character_queries() -> None:
    route = next(route for route in router.routes if isinstance(route, APIRoute) and route.path == "/users/search")
    query = next(parameter for parameter in route.dependant.query_params if parameter.name == "q")
    min_length = next(item.min_length for item in query.field_info.metadata if hasattr(item, "min_length"))
    assert min_length == 1
