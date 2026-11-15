"""版本迁移有向图与最低代价寻路。"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .rules import Rule


@dataclass
class MigrationEdge:
    """一条版本迁移边。

    rules 按组件名分组，例如 {"player": [RenameRule("name", "nickname")]}。
    cost 是规划寻路时使用的代价，不代表数据损失的严重程度。
    """

    source: str
    target: str
    rules: Dict[str, List[Rule]] = field(default_factory=dict)
    cost: int = 1

    def __post_init__(self) -> None:
        if self.cost < 0:
            raise ValueError(f"迁移边 {self.source} -> {self.target} 的代价不能为负数")
        if self.source == self.target:
            raise ValueError("迁移边的起点和终点不能相同")


class MigrationGraph:
    def __init__(self, edges: Optional[List[MigrationEdge]] = None):
        self._adjacency: Dict[str, List[MigrationEdge]] = {}
        self._edge_keys: set = set()
        for edge in edges or []:
            self.add_edge(edge)

    def add_edge(self, edge: MigrationEdge) -> None:
        key = (edge.source, edge.target)
        if key in self._edge_keys:
            raise ValueError(f"迁移边 {edge.source} -> {edge.target} 重复定义")
        self._edge_keys.add(key)
        self._adjacency.setdefault(edge.source, []).append(edge)
        self._adjacency.setdefault(edge.target, [])

    def nodes(self) -> List[str]:
        return list(self._adjacency)

    def outgoing(self, version: str) -> List[MigrationEdge]:
        return list(self._adjacency.get(version, []))

    def cheapest_path(self, source: str, target: str) -> Tuple[List[MigrationEdge], int]:
        """Dijkstra 最低代价路径。

        代价相同时按插入顺序（边序号）破并，保证规划结果确定；
        支持包含环的图，环只会增加累计代价，不会造成死循环。
        """

        if source not in self._adjacency:
            raise KeyError(f"当前版本 {source!r} 不在迁移图中")
        if target not in self._adjacency:
            raise KeyError(f"目标版本 {target!r} 不在迁移图中")
        if source == target:
            return [], 0

        # 堆元素: (累计代价, 插入序号, 版本, 路径)
        counter = 0
        heap: List[Tuple[int, int, str, List[MigrationEdge]]] = [
            (0, counter, source, [])
        ]
        best: Dict[str, int] = {source: 0}

        while heap:
            total_cost, _, version, path = heapq.heappop(heap)
            if total_cost > best.get(version, total_cost):
                continue
            if version == target:
                return path, total_cost
            for edge in self._adjacency.get(version, []):
                next_cost = total_cost + edge.cost
                if next_cost < best.get(edge.target, float("inf")):
                    best[edge.target] = next_cost
                    counter += 1
                    heapq.heappush(
                        heap,
                        (next_cost, counter, edge.target, path + [edge]),
                    )

        return [], -1
