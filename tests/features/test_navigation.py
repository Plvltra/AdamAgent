"""实际网格状态与原工具循环；模型HTTP为固定路线，真实模型验收另留证。"""

import pytest

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted
from adam.robotics.grid import GridWorld
from tests.features.test_browser_actions import application
from tests.features.test_desktop_actions import config
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response


def test_original_tool_loop_moves_real_environment_around_obstacle(tmp_path):
    world = GridWorld(4, 3, (0, 1), (3, 1), frozenset({(1, 1)}), max_steps=8)
    route = [(0, -1), (1, 0), (1, 0), (1, 0), (0, 1)]

    def respond(body, number):
        if number == 1:
            return tool_response([call("observe", "nav_observe")])
        if number <= 6:
            dx, dy = route[number - 2]
            return tool_response(
                [
                    call(
                        "move" + str(number),
                        "nav_move",
                        version=outputs(body)[-1]["version"],
                        dx=dx,
                        dy=dy,
                    )
                ]
            )
        assert outputs(body)[-1]["state"] == "arrived"
        return answer("arrived")

    with application(tmp_path, respond, config(tmp_path), navigation_world=world) as (
        app,
        store,
        bodies,
    ):
        app.select_mode("act")
        assert app.send("navigate") == "arrived"
        assert world.observe()["position"] == [3, 1] and world.observe()["steps"] == 5
        assert app.session is not None
        turns = store.load_turns(app.session.session_id)
        assert len(turns) == 1 and len(bodies) == 7
        results = outputs(bodies[-1])
        assert [item["position"] for item in results] == [
            [0, 1],
            [0, 0],
            [1, 0],
            [2, 0],
            [3, 0],
            [3, 1],
        ]


def test_collision_spends_step_and_old_version_cannot_repeat():
    world = GridWorld(3, 2, (0, 0), (2, 0), frozenset({(1, 0)}), max_steps=2)
    old = world.observe()["version"]
    result = world.move(old, 1, 0, CancellationToken())
    assert result["position"] == [0, 0] and result["outcome"] == "blocked" and result["steps"] == 1
    with pytest.raises(ValueError, match="stale"):
        world.move(old, 1, 0, CancellationToken())
    result = world.move(result["version"], -1, 0, CancellationToken())
    assert result["state"] == "exhausted" and result["steps"] == 2
    with pytest.raises(ValueError, match="exhausted"):
        world.move(result["version"], 0, 1, CancellationToken())


def test_cancel_and_emergency_stop_never_move_or_reset():
    world = GridWorld(2, 2, (0, 0), (1, 1), frozenset())
    before, cancel = world.observe(), CancellationToken()
    cancel.cancel()
    with pytest.raises(RequestInterrupted):
        world.move(before["version"], 1, 0, cancel)
    assert world.observe() == before
    stopped = world.stop()
    assert stopped["state"] == "stopped" and stopped["steps"] == 0
    assert world.stop() == stopped
    with pytest.raises(ValueError, match="stopped"):
        world.move(stopped["version"], 1, 0, CancellationToken())


def test_plan_mode_does_not_move_and_close_stops_environment(tmp_path):
    world = GridWorld(2, 1, (0, 0), (1, 0), frozenset())

    def respond(body, number):
        if number == 1:
            return tool_response([call("observe", "nav_observe")])
        if number == 2:
            return tool_response(
                [call("move", "nav_move", version=outputs(body)[-1]["version"], dx=1, dy=0)]
            )
        assert outputs(body)[-1]["status"] == "mode_blocked"
        return answer("blocked")

    with application(tmp_path, respond, config(tmp_path), navigation_world=world) as (app, _, _):
        app.select_mode("plan")
        assert app.send("navigate") == "blocked"
        assert world.observe()["steps"] == 0
    assert world.observe()["state"] == "stopped"


def test_invalid_moves_and_returned_map_do_not_mutate_environment():
    world = GridWorld(2, 2, (0, 0), (1, 1), frozenset({(1, 0)}))
    snapshot = world.observe()
    snapshot["position"][0] = 99
    snapshot["obstacles"].clear()
    with pytest.raises(ValueError, match="cardinal"):
        world.move(snapshot["version"], 1, 1, CancellationToken())
    assert world.observe()["position"] == [0, 0]
    assert world.observe()["obstacles"] == [[1, 0]] and world.observe()["steps"] == 0
