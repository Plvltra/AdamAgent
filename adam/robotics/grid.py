"""真实内存状态转移的离散导航环境；一步一格，不模拟真实硬件安全性。"""

import secrets
from threading import RLock
from typing import Any

from adam.contracts.cancellation import CancellationToken


class GridWorld:
    """宿主固定地图、起点、目标和步数；工具不能改地图、传送或复位急停。"""

    def __init__(
        self,
        width: int,
        height: int,
        start: tuple[int, int],
        goal: tuple[int, int],
        obstacles: frozenset[tuple[int, int]],
        *,
        max_steps: int = 32,
    ) -> None:
        if any(type(value) is not int for value in (width, height, max_steps)) or not (
            1 <= width <= 32 and 1 <= height <= 32 and 1 <= max_steps <= 256
        ):
            raise ValueError("invalid bounded grid or step limit")
        for point in (start, goal, *obstacles):
            if (
                len(point) != 2
                or any(type(value) is not int for value in point)
                or not (0 <= point[0] < width and 0 <= point[1] < height)
            ):
                raise ValueError("point outside grid")
        if start in obstacles or goal in obstacles:
            raise ValueError("start and goal must be free")
        self.width, self.height, self.goal = width, height, goal
        self.obstacles, self.max_steps = frozenset(obstacles), max_steps
        self._position, self._steps, self._revision = start, 0, 0
        self._identity = secrets.token_hex(16)
        self._state = "arrived" if start == goal else "active"
        self._lock = RLock()

    def observe(self) -> dict[str, Any]:
        """返回当前实际位置/地图/剩余步数的副本；坐标x向右、y向下，从0开始。"""
        with self._lock:
            return dict(
                environment_id=self._identity,
                version=f"{self._identity}:{self._revision}",
                width=self.width,
                height=self.height,
                position=list(self._position),
                goal=list(self.goal),
                obstacles=[list(p) for p in sorted(self.obstacles)],
                steps=self._steps,
                remaining=self.max_steps - self._steps,
                state=self._state,
            )

    def move(self, version: str, dx: int, dy: int, cancel: CancellationToken) -> dict[str, Any]:
        """版本匹配后执行一格；撞障碍/边界也耗一步并换版本，旧指令不能重放。"""
        with self._lock:
            cancel.check()
            if version != self.observe()["version"]:
                raise ValueError("stale navigation observation")
            if self._state != "active":
                raise ValueError("navigation is " + self._state)
            if type(dx) is not int or type(dy) is not int or abs(dx) + abs(dy) != 1:
                raise ValueError("move must be one cardinal grid step")
            target = (self._position[0] + dx, self._position[1] + dy)
            blocked = (
                not 0 <= target[0] < self.width
                or not 0 <= target[1] < self.height
                or target in self.obstacles
            )
            self._steps += 1
            self._revision += 1
            if not blocked:
                self._position = target
            if self._position == self.goal:
                self._state = "arrived"
            elif self._steps == self.max_steps:
                self._state = "exhausted"
            return {**self.observe(), "outcome": "blocked" if blocked else "moved"}

    def stop(self) -> dict[str, Any]:
        """立即锁定环境并失效旧版本，后续移动拒绝；再次stop不消耗步数。"""
        with self._lock:
            if self._state == "active":
                self._state = "stopped"
                self._revision += 1
            return self.observe()
