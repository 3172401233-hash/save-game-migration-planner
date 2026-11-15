# save-game-migration-planner

Python 游戏存档迁移规划库。给定当前版本、版本迁移图、组件转换规则、字段默认值和
必填字段，找出代价最低的可达迁移路径，输出确定性的损失报告；所有数据损失必须
显式确认后才能执行，执行失败时要么完整回滚，要么返回可恢复的中间状态。

## 存档结构

```python
save = {
    "version": "v1",
    "components": {
        "player": {"hp": 80, "level": 3},
        "equipment": {"legacy_ref": "sword-001"},
    },
}
```

## 快速开始

```python
from migration_planner import (
    ConvertRule, DropRule, MigrationEdge, MigrationGraph,
    RenameRule, execute, plan_migration,
)

graph = MigrationGraph([
    MigrationEdge("v1", "v2", rules={
        "equipment": [DropRule("legacy_ref")],
    }),
    MigrationEdge("v2", "v3", rules={
        "player": [
            RenameRule("hp", "health"),
            ConvertRule("level", lambda value: value + 1),
        ],
    }),
])

defaults = {"player": {"health": 100}}
required_fields = {"player": ["health", "level"]}

plan = plan_migration(save, "v3", graph, defaults, required_fields)
print(plan.loss_report())

# 未确认的损失会抛 ConfirmationRequiredError，不会修改存档
plan.confirm([loss.id for loss in plan.losses])
result = execute(save, plan)
```

## 核心概念

- `MigrationGraph`：版本有向图，支持分叉与环；`cheapest_path` 使用 Dijkstra
  求最低累计代价路径，代价相同时按边的插入顺序破并，结果确定。
- `RenameRule`：字段重命名。源字段缺失时报 `rename_source_missing`；目标字段
  已存在时保留目标值、丢弃源值，报 `collision`。
- `DropRule`：显式删除字段，字段存在时报 `drop` 损失。
- `ConvertRule`：值转换函数，字段缺失时跳过；抛出的任何异常都会被包装成
  `TransformError`，失败步骤不产生部分写入。
- 必填字段：每步规则全部执行完后再校验。缺失且有默认值时报
  `missing_required`（可确认后补默认值）；没有默认值则进入 `blocking_errors`，
  规划不可执行。

## 损失确认语义

- 规划阶段绝不修改原始存档，也不预填默认值；`simulated_save` 只反映规则转换。
- 每一项损失有稳定标识 `L001`、`L002`……，必须调用 `plan.confirm([...])`
  逐项确认，执行时若仍有未确认损失会抛 `ConfirmationRequiredError`。
- 默认值补救推迟到所有规则步骤成功之后统一写入；若字段经过后续重命名已经
  获得真实值，则不再写默认值，避免“组件悄悄变成默认值”。
- `drop` 与 `collision` 的补救方式就是确认丢弃；`missing_required` 与
  `rename_source_missing` 在确认后写入已配置的默认值。

## 失败策略与回滚边界

`execute(save, plan, on_failure=...)` 支持两种策略：

- `on_failure="rollback"`（默认）
  - 执行前对存档做深拷贝，整个迁移在副本上进行。
  - 任意步骤转换失败时抛 `MigrationExecutionError`，丢弃全部中间状态。
  - 回滚边界：调用方传入的 `save` 对象在任何情况下都保持原样，不存在
    “写坏一半”的存档。
- `on_failure="checkpoint"`
  - 每个步骤开始前保存快照；某步失败时返回 `RecoverableState`。
  - 检查点边界：`state.save` 是最后一个**完整成功**步骤之后的存档，
    失败步骤本身不会留下部分写入；`state.failed_step` 指向失败步骤序号。
  - 修复转换规则后调用 `state.resume()`（或 `resume_from(state)`）从失败
    步骤继续，已确认的损失仍然有效。

规划期可以静态判定的转换异常会出现在 `plan.transform_errors` 中（不阻断规划，
执行时仍由上述策略处理）；不可达版本、无默认值的必填缺失等出现在
`blocking_errors` 中，这类规划调用 `execute` 会直接抛 `PlanningError`。

## 测试

```bash
python -m unittest
```

覆盖场景：线性升级、版本分叉选路、环图不会陷入死循环、字段重命名与碰撞、
缺字段（有/无默认值）、转换异常的完整回滚与检查点恢复、损失报告确定性。
