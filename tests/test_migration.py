import copy
import unittest

from migration_planner import (
    ConfirmationRequiredError,
    ConvertRule,
    DropRule,
    MigrationEdge,
    MigrationExecutionError,
    MigrationGraph,
    PlanningError,
    RenameRule,
    execute,
    plan_migration,
    resume_from,
)


def make_save(version, **components):
    return {"version": version, "components": dict(components)}


def make_graph():
    # 主链: v1 -> v2 -> v3 -> v4，v2 处有分叉: v2 -> v2b -> v3
    # 环: v3 -> v3a -> v3
    edges = [
        MigrationEdge(
            "v1",
            "v2",
            rules={
                "player": [RenameRule("name", "nickname")],
                "equipment": [DropRule("legacy_ref")],
            },
            cost=1,
        ),
        MigrationEdge(
            "v2",
            "v3",
            rules={
                "player": [RenameRule("hp", "health")],
            },
            cost=1,
        ),
        MigrationEdge(
            "v2b",
            "v3",
            rules={"player": [RenameRule("hp", "health")]},
            cost=1,
        ),
        MigrationEdge(
            "v2",
            "v2b",
            rules={"player": [ConvertRule("level", lambda value: value + 10)]},
            cost=5,
        ),
        MigrationEdge(
            "v3",
            "v3a",
            rules={"player": [ConvertRule("level", lambda value: value + 1)]},
            cost=2,
        ),
        MigrationEdge(
            "v3a",
            "v3",
            rules={"player": [ConvertRule("level", lambda value: value + 1)]},
            cost=2,
        ),
        MigrationEdge(
            "v3",
            "v4",
            rules={
                "player": [
                    ConvertRule("health", lambda value: value * 2),
                    ConvertRule("level", lambda value: int(value)),
                ]
            },
            cost=1,
        ),
    ]
    return MigrationGraph(edges)


DEFAULTS = {
    "player": {"health": 100, "level": 1},
}

REQUIRED_FIELDS = {
    "player": ["nickname", "level"],
}


class LinearMigrationTest(unittest.TestCase):
    def test_linear_upgrade_renames_and_confirmed_drop(self):
        graph = make_graph()
        save = make_save(
            "v1",
            player={"name": "hero", "hp": 80, "level": 3},
            equipment={"legacy_ref": "old-item-id"},
        )

        plan = plan_migration(save, "v4", graph, DEFAULTS, REQUIRED_FIELDS)

        self.assertTrue(plan.is_reachable)
        self.assertEqual(
            [edge.target for edge in plan.path], ["v2", "v3", "v4"]
        )
        self.assertEqual(plan.total_cost, 3)
        self.assertEqual(save["version"], "v1")  # 规划不修改原存档

        kinds = [loss.kind for loss in plan.losses]
        self.assertIn("drop", kinds)
        drop = next(loss for loss in plan.losses if loss.kind == "drop")
        self.assertEqual((drop.component, drop.field), ("equipment", "legacy_ref"))

        # 未确认时拒绝执行，且不碰原存档
        with self.assertRaises(ConfirmationRequiredError) as ctx:
            execute(save, plan)
        self.assertEqual(ctx.exception.pending_losses[0].id, drop.id)

        plan.confirm([drop.id])
        result = execute(save, plan)
        self.assertEqual(result.save["version"], "v4")
        self.assertEqual(
            result.save["components"]["player"],
            {"nickname": "hero", "health": 160, "level": 3},
        )
        self.assertNotIn("legacy_ref", result.save["components"]["equipment"])
        # 原始存档始终保持不动
        self.assertEqual(save["components"]["player"]["name"], "hero")


class ForkMigrationTest(unittest.TestCase):
    def test_cheapest_path_avoids_expensive_branch(self):
        graph = make_graph()
        save = make_save("v2", player={"hp": 50, "level": 2})

        plan = plan_migration(save, "v3", graph, DEFAULTS, REQUIRED_FIELDS)
        self.assertEqual(
            [(edge.source, edge.target) for edge in plan.path], [("v2", "v3")]
        )
        self.assertEqual(plan.total_cost, 1)

    def test_fork_used_when_cheap_branch_removed(self):
        graph = make_graph()
        save = make_save(
            "v2", player={"hp": 50, "level": 2, "nickname": "hero"}
        )
        small = MigrationGraph(
            [
                edge
                for edge in [
                    MigrationEdge(
                        "v2",
                        "v2b",
                        rules={
                            "player": [
                                ConvertRule("level", lambda value: value + 10)
                            ]
                        },
                        cost=5,
                    ),
                    MigrationEdge(
                        "v2b",
                        "v3",
                        rules={"player": [RenameRule("hp", "health")]},
                    ),
                ]
            ]
        )
        plan = plan_migration(save, "v3", small, DEFAULTS, REQUIRED_FIELDS)
        self.assertEqual(
            [edge.target for edge in plan.path], ["v2b", "v3"]
        )
        plan.confirm([loss.id for loss in plan.losses])
        result = execute(save, plan)
        self.assertEqual(result.save["components"]["player"]["level"], 12)
        self.assertEqual(result.save["version"], "v3")


class CycleGraphTest(unittest.TestCase):
    def test_cycle_does_not_trap_planning(self):
        graph = make_graph()
        save = make_save("v3", player={"nickname": "hero", "health": 10, "level": 1})

        plan = plan_migration(save, "v3", graph, DEFAULTS, REQUIRED_FIELDS)
        self.assertEqual(plan.path, [])
        self.assertEqual(plan.total_cost, 0)

        plan_forward = plan_migration(save, "v4", graph, DEFAULTS, REQUIRED_FIELDS)
        self.assertEqual(
            [edge.target for edge in plan_forward.path], ["v4"]
        )

    def test_unreachable_target_is_blocking(self):
        graph = MigrationGraph(
            [MigrationEdge("v1", "v2", rules={})]
        )
        save = make_save("v1", player={})
        plan = plan_migration(save, "v9", graph)
        self.assertFalse(plan.is_reachable)
        self.assertTrue(plan.blocking_errors)
        with self.assertRaises(PlanningError):
            execute(save, plan)


class RenameCollisionTest(unittest.TestCase):
    def test_rename_keeps_existing_target_and_reports_collision(self):
        graph = MigrationGraph(
            [
                MigrationEdge(
                    "v1",
                    "v2",
                    rules={"player": [RenameRule("hp", "health")]},
                )
            ]
        )
        save = make_save(
            "v1",
            player={"hp": 80, "health": 40, "nickname": "hero", "level": 2},
        )

        plan = plan_migration(save, "v2", graph, DEFAULTS, REQUIRED_FIELDS)
        collision = next(loss for loss in plan.losses if loss.kind == "collision")
        self.assertEqual(collision.field, "hp")

        with self.assertRaises(ConfirmationRequiredError):
            execute(save, plan)
        plan.confirm([collision.id])
        result = execute(save, plan)
        self.assertEqual(result.save["components"]["player"]["health"], 40)
        self.assertNotIn("hp", result.save["components"]["player"])


class MissingFieldTest(unittest.TestCase):
    def test_missing_required_with_default_needs_confirmation(self):
        graph = MigrationGraph(
            [
                MigrationEdge(
                    "v1",
                    "v2",
                    rules={"player": [RenameRule("hp", "health")]},
                )
            ]
        )
        save = make_save("v1", player={"hp": 80, "nickname": "hero", "level": 4})

        plan = plan_migration(save, "v2", graph, DEFAULTS, REQUIRED_FIELDS)
        # rename 成功，health 已存在，无缺失
        self.assertEqual(plan.pending_losses(), [])

        save_missing = make_save(
            "v1", player={"nickname": "hero", "level": 4}
        )
        plan_missing = plan_migration(
            save_missing, "v2", graph, DEFAULTS, REQUIRED_FIELDS
        )
        loss = plan_missing.losses[0]
        self.assertEqual(loss.kind, "rename_source_missing")
        self.assertTrue(loss.has_default)
        self.assertEqual(loss.default, 100)

        # 未确认不能套默认值
        with self.assertRaises(ConfirmationRequiredError):
            execute(save_missing, plan_missing)
        plan_missing.confirm([loss.id])
        result = execute(save_missing, plan_missing)
        self.assertEqual(
            result.save["components"]["player"]["health"], 100
        )
        self.assertIn("写入已确认的默认值", result.applied_remedies[0])
        # 规划模拟不预填默认值，确认前没有任何副作用
        self.assertNotIn(
            "health", plan_missing.simulated_save["components"]["player"]
        )

    def test_missing_required_without_default_is_blocking(self):
        graph = MigrationGraph(
            [MigrationEdge("v1", "v2", rules={})]
        )
        save = make_save("v1", player={"nickname": "hero"})
        plan = plan_migration(save, "v2", graph, None, REQUIRED_FIELDS)
        self.assertTrue(plan.blocking_errors)
        with self.assertRaises(PlanningError):
            execute(save, plan)


class TransformFailureTest(unittest.TestCase):
    def _failing_graph(self):
        graph = MigrationGraph(
            [
                MigrationEdge("v2", "v3", rules={}),
                MigrationEdge(
                    "v3",
                    "v4",
                    rules={
                        "player": [
                            ConvertRule("level", lambda value: int(value)),
                        ]
                    },
                )
            ]
        )
        return graph

    def _failing_plan(self):
        graph = self._failing_graph()
        save = make_save(
            "v2",
            player={"nickname": "hero", "health": 10, "level": "not-a-number"},
        )
        plan = plan_migration(save, "v4", graph, DEFAULTS, REQUIRED_FIELDS)
        return graph, save, plan

    def test_rollback_returns_untouched_original(self):
        graph, save, plan = self._failing_plan()
        original_snapshot = copy.deepcopy(save)
        self.assertEqual([edge.target for edge in plan.path], ["v3", "v4"])
        self.assertTrue(plan.transform_errors)

        with self.assertRaises(MigrationExecutionError) as ctx:
            execute(save, plan, on_failure="rollback")
        error = ctx.exception.transform_error
        self.assertEqual(error.step_index, 1)
        self.assertEqual(error.edge, ("v3", "v4"))
        self.assertEqual(save, original_snapshot)

    def test_checkpoint_returns_recoverable_state(self):
        graph = self._failing_graph()
        save = make_save(
            "v2",
            player={"nickname": "hero", "health": 10, "level": "oops"},
        )
        plan = plan_migration(save, "v4", graph, DEFAULTS, REQUIRED_FIELDS)
        self.assertTrue(plan.transform_errors)

        state = execute(save, plan, on_failure="checkpoint")
        self.assertEqual(state.failed_step, 1)
        self.assertEqual(state.save["version"], "v3")
        self.assertEqual(state.save["components"], save["components"])
        self.assertEqual(state.error.edge, ("v3", "v4"))

        # 修复规则后从检查点继续
        plan.path[1].rules["player"][0] = ConvertRule(
            "level", lambda value: len(value)
        )
        result = state.resume()
        self.assertEqual(result.save["version"], "v4")
        self.assertEqual(result.save["components"]["player"]["level"], 4)

    def test_resume_helper_matches_method(self):
        graph, save, plan = self._failing_plan()
        state = execute(save, plan, on_failure="checkpoint")
        plan.path[1].rules["player"][0] = ConvertRule(
            "level", lambda value: 0
        )
        result = resume_from(state)
        self.assertEqual(result.save["components"]["player"]["level"], 0)


class LossReportTest(unittest.TestCase):
    def test_intermediate_default_must_not_shadow_later_rename(self):
        graph = MigrationGraph(
            [
                MigrationEdge("v1", "v2", rules={}),
                MigrationEdge(
                    "v2",
                    "v3",
                    rules={"player": [RenameRule("hp", "health")]},
                ),
            ]
        )
        save = make_save("v1", player={"hp": 80, "level": 2})
        defaults = {"player": {"health": 100}}
        required = {"player": ["health", "level"]}

        plan = plan_migration(save, "v3", graph, defaults, required)
        loss = plan.losses[0]
        self.assertEqual(loss.kind, "missing_required")
        self.assertEqual(loss.step_index, 0)

        plan.confirm([loss.id])
        result = execute(save, plan)
        # 真实的 hp=80 经重命名保留，默认值 100 不得遮蔽
        self.assertEqual(
            result.save["components"]["player"]["health"], 80
        )
        self.assertEqual(
            result.save["components"]["player"]["level"], 2
        )

    def test_report_is_deterministic_and_serializable(self):
        graph = make_graph()
        save = make_save(
            "v1",
            player={"name": "hero", "hp": 80, "level": 3},
            equipment={"legacy_ref": "x"},
        )
        plan = plan_migration(save, "v4", graph, DEFAULTS, REQUIRED_FIELDS)
        report = plan.loss_report()

        self.assertEqual(report["source_version"], "v1")
        self.assertEqual(report["target_version"], "v4")
        self.assertEqual(
            report["path"], ["v1 -> v2", "v2 -> v3", "v3 -> v4"]
        )
        self.assertEqual([loss["id"] for loss in report["losses"]], ["L001"])
        self.assertEqual(report["pending_count"], 1)

        plan.confirm(["L001"])
        self.assertEqual(plan.loss_report()["pending_count"], 0)
        with self.assertRaises(ValueError):
            plan.confirm(["L999"])


if __name__ == "__main__":
    unittest.main()
