"""存档迁移规划器：版本图寻路、组件转换、损失确认与回滚。"""

from .engine import (
    MigrationExecutionError,
    MigrationResult,
    RecoverableState,
    execute,
    resume_from,
)
from .exceptions import (
    ConfirmationRequiredError,
    MigrationError,
    PlanningError,
    TransformError,
)
from .graph import MigrationEdge, MigrationGraph
from .plan import LossEntry, MigrationPlan, plan_migration
from .rules import (
    apply_rules,
    check_required_fields,
    ConvertRule,
    DropRule,
    RawLoss,
    RenameRule,
    Rule,
    apply_edge,
    lookup_default,
)

__all__ = [
    "ConfirmationRequiredError",
    "ConvertRule",
    "DropRule",
    "LossEntry",
    "MigrationEdge",
    "MigrationError",
    "MigrationExecutionError",
    "MigrationGraph",
    "MigrationPlan",
    "MigrationResult",
    "PlanningError",
    "RawLoss",
    "RecoverableState",
    "RenameRule",
    "Rule",
    "TransformError",
    "apply_edge",
    "apply_rules",
    "check_required_fields",
    "execute",
    "lookup_default",
    "plan_migration",
    "resume_from",
]
