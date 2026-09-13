import json

from technocore_agent.sonnet import team


def test_team_request_message_matches_rules():
    assert json.loads(team.team_request_message("sonnet-2", "xav-1", "room-1")) == {
        "type": "sonnet.team-request.v1", "contest_id": "sonnet-2", "game_id": "xav-1", "request_id": "room-1"}


def test_game_id_shape_is_enforced():
    for bad in ("", "-abc", "Abc", "a" * 17, "a b"):
        try:
            team.team_request_message("sonnet-2", bad, "r")
        except ValueError:
            continue
        raise AssertionError(bad)


def test_roster_message_keeps_member_order_and_requires_4_to_8():
    members = [f"did:key:z6Mk{c * 44}" for c in "abcd"]
    data = json.loads(team.roster_message("sonnet-2", "xav-1", "d-sonnet-2-team-xav-1", 2, members, "roster-1"))
    assert data["members"] == members and data["room_generation"] == 2 and data["poem_room"] == "d-sonnet-2-team-xav-1"
    for n in (3, 9):
        try:
            team.roster_message("sonnet-2", "xav-1", "d-sonnet-2-team-xav-1", 2, members[:1] * n, "r")
        except ValueError:
            continue
        raise AssertionError(n)


def test_withdraw_message():
    assert json.loads(team.withdraw_message("sonnet-2", "xav-1", "wd-1")) == {
        "type": "sonnet.withdraw.v1", "contest_id": "sonnet-2", "game_id": "xav-1", "request_id": "wd-1"}
