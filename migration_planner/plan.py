"""迁移规划：最低代价路径 + 全量损失报告 + 确定性模拟结果。"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .exceptions import PlanningError, TransformError
from .graph import MigrationEdge, MigrationGraph
from .rules import (
    RawLoss,
    apply_rules,
    check_required_fields,
)

# 可通过确认后继续的损失类型；不可解析的损失会阻断规划
_RESOLVABLE_KINDS = {
    "missing_required",
    "drop",
    "collision",
    "rename_source_missing",
}


@dataclass
class LossEntry:
    """损失报告中的一项，id 是显式确认使用的稳定标识。"""

    id: str
    kind: str
    component: str
    field: str
    detail: str
    step_index: int
    edge: str
    has_default: bool = False
    default: Any = None
    confirmed: bool = False


@dataclass
class MigrationPlan:
    source_version: str
    target_version: str
    path: List[MigrationEdge]
    total_cost: int
    losses: List[LossEntry] = field(default_factory=list)
    simulated_save: Dict[str, Any] = field(default_factory=dict)
    blocking_errors: List[str] = field(default_factory=list)
    transform_errors: List[str] = field(default_factory=list)
    defaults: Optional[Dict[str, Dict[str, Any]]] = None
    required_fields: Optional[Dict[str, List[str]]] = None
    confirmed_loss_ids: List[str] = field(default_factory=list)

    @property
    def is_reachable(self) -> bool:
        return self.target_version == self.source_version or bool(self.path)

    def pending_losses(self) -> List[LossEntry]:
        return [loss for loss in self.losses if not loss.confirmed]

    def confirm(self, loss_ids: List[str]) -> None:
        known = {loss.id for loss in self.losses}
        unknown = [loss_id for loss_id in loss_ids if loss_id not in known]
        if unknown:
            raise ValueError(f"未知的损失标识: {', '.join(unknown)}")
        for loss in self.losses:
            if loss.id in loss_ids:
                loss.confirmed = True
        self.confirmed_loss_ids = [
            loss.id for loss in self.losses if loss.confirmed
        ]

    def loss_report(self) -> Dict[str, Any]:
        return {
            "source_version": self.source_version,
            "target_version": self.target_version,
            "total_cost": self.total_cost,
            "path": [f"{edge.source} -> {edge.target}" for edge in self.path],
            "loss_count": len(self.losses),
            "pending_count": len(self.pending_losses()),
            "blocking_errors": list(self.blocking_errors),
            "transform_errors": list(self.transform_errors),
            "losses": [
                {
                    "id": loss.id,
                    "kind": loss.kind,
                    "component": loss.component,
                    "field": loss.field,
                    "detail": loss.detail,
                    "step": loss.step_index,
                    "edge": loss.edge,
                    "has_default": loss.has_default,
                    "default": loss.default,
                    "confirmed": loss.confirmed,
                }
                for loss in self.losses
            ],
        }


def plan_migration(
    save: Dict[str, Any],
    target_version: str,
    graph: MigrationGraph,
    defaults: Optional[Dict[str, Dict[str, Any]]] = None,
    required_fields: Optional[Dict[str, List[str]]] = None,
) -> MigrationPlan:
    """生成最低代价迁移规划，包含完整损失报告和模拟存档。

    规划阶段不修改原始存档；模拟存档只演示转换规则本身的结果，
    不预填任何默认值。默认值/丢弃补救仅在损失被显式确认后由执行引擎写入，
    避免规划期副作用遮蔽后续步骤的重命名或碰撞检测。
    """

    source_version = save.get("version")
    if source_version is None:
        raise PlanningError("存档缺少 version 字段，无法确定当前版本")

    if source_version not in graph.nodes():
        plan = MigrationPlan(
            source_version=source_version,
            target_version=target_version,
            path=[],
            total_cost=0,
            defaults=copy.deepcopy(defaults),
            required_fields=copy.deepcopy(required_fields),
        )
        plan.blocking_errors.append(
            f"当前版本 {source_version!r} 不在迁移图中"
        )
        plan.simulated_save = copy.deepcopy(save)
        return plan

    if target_version not in graph.nodes():
        path, total_cost = [], -1
    else:
        path, total_cost = graph.cheapest_path(source_version, target_version)

    plan = MigrationPlan(
        source_version=source_version,
        target_version=target_version,
        path=path,
        total_cost=total_cost,
        defaults=copy.deepcopy(defaults),
        required_fields=copy.deepcopy(required_fields),
    )

    if source_version != target_version and not path:
        plan.blocking_errors.append(
            f"版本 {source_version} 到 {target_version} 之间不存在可达路径"
        )
        plan.simulated_save = copy.deepcopy(save)
        return plan

    simulated = copy.deepcopy(save)
    loss_counter = 0

    for step_index, edge in enumerate(path):
        try:
            rule_losses = apply_rules(simulated, edge, defaults)
            required_losses = check_required_fields(
                simulated, defaults, required_fields
            )
            raw_losses = rule_losses + required_losses
        except TransformError as exc:
            exc.step_index = step_index
            exc.edge = (edge.source, edge.target)
            # 确定性转换失败记入报告但不阻断规划：执行阶段由引擎
            # 按 rollback / checkpoint 策略处理，保证不会半写入
            plan.transform_errors.append(
                f"步骤 {step_index}（{edge.source} -> {edge.target}）"
                f"存在必然失败的转换: {exc}"
            )
            plan.simulated_save = simulated
            return plan
        for raw in raw_losses:
            loss_counter += 1
            entry = LossEntry(
                id=f"L{loss_counter:03d}",
                kind=raw.kind,
                component=raw.component,
                field=raw.field,
                detail=raw.detail,
                step_index=step_index,
                edge=f"{edge.source} -> {edge.target}",
                has_default=raw.has_default,
                default=copy.deepcopy(raw.default),
            )
            plan.losses.append(entry)

            if raw.kind not in _RESOLVABLE_KINDS:
                plan.blocking_errors.append(
                    f"{entry.id}: 不支持的损失类型 {raw.kind}"
                )
                continue

            if raw.kind in ("missing_required", "rename_source_missing"):
                # 规划阶段不预填默认值：否则会遮蔽后续步骤的重命名/碰撞。
                # 默认值只在损失被显式确认后，由执行引擎写入。
                if not raw.has_default:
                    plan.blocking_errors.append(
                        f"{entry.id}: 组件 {raw.component!r} 的必填字段 "
                        f"{raw.field!r} 缺失且没有配置默认值，无法继续"
                    )

    plan.simulated_save = simulated
    return plan
