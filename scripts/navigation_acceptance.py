"""固定四场景的脚本/真实模型导航对照；按环境终态评分，失败保留且不自动重跑。"""

import argparse
import json
from collections import deque
from pathlib import Path
from time import monotonic
from typing import Any

import httpx

from adam.bootstrap import build_application
from adam.config.settings import (
    AgentConfig,
    AppConfig,
    LLMConfig,
    ReflectionConfig,
    RulesConfig,
    SkillsConfig,
    ToolDiscoveryConfig,
)
from adam.contracts.cancellation import CancellationToken
from adam.llm.client import LLMClient
from adam.robotics.grid import GridWorld
from adam.session.storage import SessionStore

CASES = {
    "obstacle": (4, 3, (0, 1), (3, 1), frozenset({(1, 1)}), 12, "arrived"),
    "no_path": (3, 3, (0, 1), (2, 1), frozenset({(1, 0), (1, 1), (1, 2)}), 8, "stopped"),
    "step_limit": (4, 1, (0, 0), (3, 0), frozenset(), 2, "exhausted"),
    "emergency_stop": (4, 1, (0, 0), (3, 0), frozenset(), 8, "stopped"),
}


class RecordedWorld(GridWorld):
    """只增加外部轨迹与急停故障注入，移动和碰撞仍由原GridWorld计算。"""

    def __init__(self, case: str) -> None:
        width, height, start, goal, obstacles, limit, _ = CASES[case]
        super().__init__(width, height, start, goal, obstacles, max_steps=limit)
        self.case, self.trajectory = case, []

    def move(self, version: str, dx: int, dy: int, cancel: CancellationToken) -> dict[str, Any]:
        """急停场景在第一次真实移动后由宿主注入stop，不假造传感反馈。"""
        try:
            result = super().move(version, dx, dy, cancel)
            if self.case == "emergency_stop" and result["steps"] == 1:
                result = {**self.stop(), "outcome": "host_emergency_stop"}
            self.trajectory.append(dict(dx=dx, dy=dy, result=result))
            return result
        except BaseException as error:
            self.trajectory.append(
                dict(dx=dx, dy=dy, error=type(error).__name__, result=self.observe())
            )
            raise


def route(world: GridWorld) -> list[tuple[int, int]] | None:
    """脚本基线用BFS找最短路径；不调用模型或改动环境。"""
    initial = world.observe()
    start, goal = tuple(initial["position"]), tuple(initial["goal"])
    obstacles = {tuple(point) for point in initial["obstacles"]}
    queue = deque([(start, [])])
    seen = {start}
    while queue:
        point, moves = queue.popleft()
        if point == goal:
            return moves
        for dx, dy in ((1, 0), (0, 1), (-1, 0), (0, -1)):
            target = (point[0] + dx, point[1] + dy)
            if (
                0 <= target[0] < initial["width"]
                and 0 <= target[1] < initial["height"]
                and target not in obstacles
                and target not in seen
            ):
                seen.add(target)
                queue.append((target, [*moves, (dx, dy)]))
    return None


def passed(case: str, final: dict[str, Any], error: str | None = None) -> bool:
    """只按场景预定终态/位置/实际步数判定；异常或声称到达不能替代证据。"""
    if error or final["state"] != CASES[case][-1]:
        return False
    if case == "obstacle":
        return final["position"] == list(CASES[case][3])
    if case == "no_path":
        return final["position"] == list(CASES[case][2]) and final["steps"] == 0
    if case == "step_limit":
        return final["steps"] == 2 and final["position"] == [2, 0]
    return final["steps"] == 1 and final["position"] == [1, 0]


def script_case(case: str) -> dict[str, Any]:
    """在新环境执行BFS基线；无路主动停止，环境耗尽/急停后不再移动。"""
    world = RecordedWorld(case)
    started = monotonic()
    moves = route(world)
    if moves is None:
        world.stop()
    else:
        for dx, dy in moves:
            if world.observe()["state"] != "active":
                break
            world.move(world.observe()["version"], dx, dy, CancellationToken())
    final = world.observe()
    return dict(
        case=case,
        strategy="script",
        final=final,
        trajectory=world.trajectory,
        passed=passed(case, final),
        seconds=monotonic() - started,
        calls=0,
    )


def model_case(case: str, url: str, model: str, directory: Path) -> dict[str, Any]:
    """新会话通过原发现/工具循环跑一次；HTTP正文与真实工具事件独立保存。"""
    directory.mkdir(parents=True, exist_ok=False)
    world, requests, responses = RecordedWorld(case), [], []

    def request_hook(request: httpx.Request) -> None:
        """只保存请求正文，不记录认证头。"""
        requests.append(json.loads(request.content))
        save(directory / "requests.json", requests)

    def response_hook(response: httpx.Response) -> None:
        """保存所有真实响应，失败也不丢弃。"""
        response.read()
        responses.append(dict(status=response.status_code, body=response.text))
        save(directory / "responses.json", responses)

    config = AppConfig(
        LLMConfig(url, model, "local-not-a-secret", timeout_seconds=180, stream=False),
        directory / "sessions",
        'Only control this 2D simulation. First discover_tools groups=["navigation"], then nav_observe. '
        "x goes right, y goes down. Move one cardinal step with the latest version. Avoid obstacles. "
        "Navigate toward the goal until the environment is terminal. If no geometric path exists, nav_stop. "
        "If remaining steps cannot reach the goal, still attempt progress until the environment reports exhausted. "
        "If the host reports stopped, never move again. One move per response; inspect actual feedback. "
        "Do not use unrelated tools. Finish with a brief truthful account, not a proposed route.",
        agent=AgentConfig(
            max_model_requests=12,
            max_tool_calls=12,
            max_session_tokens=100000,
            max_output_tokens=400,
        ),
        reflection=ReflectionConfig(enabled=False),
        tool_discovery=ToolDiscoveryConfig(mode="on_demand"),
        rules=RulesConfig(enabled=False),
        skills=SkillsConfig(enabled=False),
    )
    result: dict[str, Any] = dict(case=case, strategy="model", error=None)
    started = monotonic()
    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(
            trust_env=False, event_hooks={"request": [request_hook], "response": [response_hook]}
        ) as http,
    ):
        app = build_application(
            config, store, LLMClient(config.llm, http), directory, navigation_world=world
        )
        try:
            app.select_mode("act")
            result["answer"] = app.send(
                "Run this navigation task to its terminal state using actual tools. Observe first."
            )
        except Exception as error:
            result["error"] = f"{type(error).__name__}: {error}"
        finally:
            # 在app.close的自动急停之前评分，不能把清理动作冒充模型主动停止。
            result.update(
                final=world.observe(), trajectory=world.trajectory, seconds=monotonic() - started
            )
            if app.session:
                save(directory / "events.json", store.load_events(app.session.session_id))
            app.close()
    result["passed"] = passed(case, result["final"], result["error"])
    result["calls"] = len(requests)
    usage = [
        json.loads(item["body"]).get("usage", {}) for item in responses if item["status"] == 200
    ]
    result["input_tokens"] = sum(item.get("prompt_tokens", 0) for item in usage)
    result["output_tokens"] = sum(item.get("completion_tokens", 0) for item in usage)
    result["cost"] = None
    save(directory / "result.json", result)
    return result


def save(path: Path, value: object) -> None:
    """固定验收目录内保存JSON；上层拒绝复用输出目录，避免覆盖原失败。"""
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    """连接显式本地服务；障碍可复用已核对的C10.3一次实测，不重复请求模型。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reuse-obstacle", type=Path)
    args = parser.parse_args()
    if not args.url.startswith("http://127.0.0.1:"):
        raise ValueError("only an explicit local model service is supported")
    args.output.mkdir(parents=True, exist_ok=False)
    rows = []
    for case in CASES:
        rows.append(script_case(case))
        if case == "obstacle" and args.reuse_obstacle:
            prior = json.loads(args.reuse_obstacle.read_text(encoding="utf-8"))
            expected = RecordedWorld(case).observe()
            if any(
                prior["initial"][key] != expected[key]
                for key in ("width", "height", "position", "goal", "obstacles", "remaining")
            ):
                raise ValueError("reused obstacle scenario differs")
            rows.append(
                dict(
                    case=case,
                    strategy="model",
                    final=prior["final"],
                    passed=passed(case, prior["final"], prior.get("error")),
                    calls=prior["request_count"],
                    seconds=prior["elapsed_seconds"],
                    reused_from=str(args.reuse_obstacle),
                )
            )
        else:
            rows.append(model_case(case, args.url, args.model, args.output / case))
        save(args.output / "results.json", rows)
        print(json.dumps(rows[-2:], ensure_ascii=False), flush=True)
    save(
        args.output / "summary.json",
        {
            strategy: dict(
                success=sum(row["passed"] for row in rows if row["strategy"] == strategy), total=4
            )
            for strategy in ("script", "model")
        },
    )


if __name__ == "__main__":
    main()
