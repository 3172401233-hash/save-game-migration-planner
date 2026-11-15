"""迁移规划器的异常类型。"""

from __future__ import annotations

from typing import List, Optional, Tuple


class MigrationError(Exception):
    """所有迁移相关异常的基类。"""


class PlanningError(MigrationError):
    """规划阶段失败：版本不可达、存在不可解析的损失等。"""


class ConfirmationRequiredError(MigrationError):
    """存在未显式确认的数据损失，执行被拒绝。"""

    def __init__(self, pending_losses: List[object]):
        self.pending_losses = list(pending_losses)
        ids = ", ".join(loss.id for loss in self.pending_losses)
        super().__init__(
            f"存在 {len(self.pending_losses)} 项未确认的数据损失（{ids}），"
            "请在损失报告中逐项确认后再执行迁移。"
        )


class TransformError(MigrationError):
    """单条转换规则抛出异常，失败步骤不产生任何半写入数据。"""

    def __init__(
        self,
        component: str,
        field: str,
        reason: str,
        original: BaseException,
        step_index: Optional[int] = None,
        edge: Optional[Tuple[str, str]] = None,
    ):
        self.component = component
        self.field = field
        self.reason = reason
        self.original = original
        self.step_index = step_index
        self.edge = edge
        location = f"（步骤 {step_index}: {edge[0]} -> {edge[1]}）" if edge else ""
        super().__init__(
            f"组件 {component!r} 的字段 {field!r} 转换失败{location}: {reason}"
        )


class MigrationExecutionError(MigrationError):
    """回滚模式下执行失败，全部中间状态已丢弃。"""

    def __init__(self, transform_error: TransformError):
        self.transform_error = transform_error
        super().__init__(
            f"迁移已完整回滚，原始存档未被修改。原因: {transform_error}"
        )
