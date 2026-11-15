"""迁移执行引擎：显式确认、原子步骤、回滚与可恢复中间状态。"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .exceptions import (
    ConfirmationRequiredError,
    MigrationExecutionError,
    PlanningError,
    TransformError,
)
from .plan import MigrationPlan
from .rules import apply_rules, check_required_fields


@dataclass
class MigrationResult:
    save: Dict[str, Any]
    plan: MigrationPlan
    applied_remedies: List[str] = field(default_factory=list)


@dataclass
class RecoverableState:
    """失败时返回的可恢复中间状态。

    save 是最后一个成功步骤之后的完整存档快照，可直接持久化；
    failed_step 是未能完成、未产生任何部分写入的步骤序号。
    """

    save: Dict[str, Any]
    plan: MigrationPlan
    failed_step: int
    error: TransformError

    def resume(self) -> MigrationResult:
        """修复转换规则后从失败步骤继续，沿用同一份确认。"""

        return resume_from(self)


def _require_executable(plan: MigrationPlan) -> None:
    if not plan.is_reachable:
        raise PlanningError(
            "目标版本不可达: " + "; ".join(plan.blocking_errors)
        )
    if plan.blocking_errors:
        raise PlanningError(
            "规划存在不可解析的阻断问题: " + "; ".join(plan.blocking_errors)
        )
    pending = plan.pending_losses()
    if pending:
        raise ConfirmationRequiredError(pending)


def _apply_all_remedies(
    save: Dict[str, Any],
    plan: MigrationPlan,
    applied_remedies: List[str],
) -> None:
    """所有规则步骤成功后统一补救。

    默认值推迟到最后写入，避免中间步骤的默认值遮蔽后续重命名产生的
    真实数据；字段在最终存档中已存在时不再写入默认值。
    """

    for loss in plan.losses:
        component = save.setdefault("components", {}).setdefault(
            loss.component, {}
        )
        if loss.kind in ("missing_required", "rename_source_missing"):
            if loss.field in component:
                continue
            component[loss.field] = copy.deepcopy(loss.default)
            applied_remedies.append(
                f"{loss.id}: 写入已确认的默认值 -> {loss.component}.{loss.field}"
            )
        else:
            applied_remedies.append(
                f"{loss.id}: 确认丢弃 -> {loss.component}.{loss.field}"
            )


def _run_step(
    working_save: Dict[str, Any],
    plan: MigrationPlan,
    step_index: int,
) -> None:
    edge = plan.path[step_index]
    try:
        apply_rules(working_save, edge, plan.defaults)
    except TransformError as exc:
        exc.step_index = step_index
        exc.edge = (edge.source, edge.target)
        raise

    raw_required = check_required_fields(
        working_save, plan.defaults, plan.required_fields
    )
    step_losses = [
        loss for loss in plan.losses if loss.step_index == step_index
    ]
    # 规划与执行必须对同一步骤得出一致的 missing_required 损失集合；
    # rename_source_missing 由规则自身产生，不依赖必填配置
    required_ids = {
        (loss.component, loss.field) for loss in raw_required
    }
    planned_required_ids = {
        (loss.component, loss.field)
        for loss in step_losses
        if loss.kind == "missing_required"
    }
    if required_ids != planned_required_ids:
        raise PlanningError(
            f"步骤 {step_index} 的必填字段校验结果与规划不一致，"
            "请基于当前存档重新生成规划"
        )


def _finalize(
    working_save: Dict[str, Any],
    plan: MigrationPlan,
    applied_remedies: List[str],
) -> MigrationResult:
    _apply_all_remedies(working_save, plan, applied_remedies)
    return MigrationResult(
        save=working_save,
        plan=plan,
        applied_remedies=applied_remedies,
    )


def execute(
    save: Dict[str, Any],
    plan: MigrationPlan,
    on_failure: str = "rollback",
) -> MigrationResult:
    """按规划执行迁移。

    on_failure:
      - "rollback": 任意步骤失败时完整回滚，抛出 MigrationExecutionError，
        调用方传入的存档对象不会被修改；
      - "checkpoint": 任意步骤失败时返回 RecoverableState，其中包含
        最后一个成功步骤之后的完整快照，修复后调用 resume() 即可继续。
    """

    if on_failure not in ("rollback", "checkpoint"):
        raise ValueError("on_failure 只能是 'rollback' 或 'checkpoint'")
    _require_executable(plan)

    working_save = copy.deepcopy(save)
    applied_remedies: List[str] = []

    for step_index in range(len(plan.path)):
        step_snapshot = copy.deepcopy(working_save)
        try:
            _run_step(working_save, plan, step_index)
        except TransformError as exc:
            if on_failure == "rollback":
                raise MigrationExecutionError(exc) from exc
            return RecoverableState(
                save=step_snapshot,
                plan=plan,
                failed_step=step_index,
                error=exc,
            )

    return _finalize(working_save, plan, applied_remedies)


def resume_from(state: RecoverableState) -> MigrationResult:
    """从可恢复检查点继续执行剩余步骤。"""

    plan = state.plan
    _require_executable(plan)

    working_save = copy.deepcopy(state.save)
    applied_remedies: List[str] = []
    for step_index in range(state.failed_step, len(plan.path)):
        try:
            _run_step(working_save, plan, step_index)
        except TransformError as exc:
            raise MigrationExecutionError(exc) from exc

    return _finalize(working_save, plan, applied_remedies)
