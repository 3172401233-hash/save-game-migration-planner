"""组件级转换规则，以及单条迁移边的应用逻辑。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from .exceptions import TransformError


@dataclass
class RawLoss:
    """规则执行或必填校验中发现的、尚未编号的原始损失。"""

    kind: str  # missing_required | drop | collision | rename_source_missing
    component: str
    field: str
    detail: str
    has_default: bool = False
    default: Any = None


class Rule:
    """转换规则基类。每条规则绑定一个组件字段。"""

    name = "rule"

    def __init__(self, field_name: str):
        self.field_name = field_name

    def apply(
        self,
        component_name: str,
        component: Dict[str, Any],
        has_default: bool,
        default: Any,
    ) -> List[RawLoss]:
        raise NotImplementedError


class RenameRule(Rule):
    """字段重命名。目标字段已存在时保留目标值、丢弃源值，并记为碰撞损失。"""

    name = "rename"

    def __init__(self, old_name: str, new_name: str):
        super().__init__(old_name)
        self.old_name = old_name
        self.new_name = new_name

    def apply(
        self,
        component_name: str,
        component: Dict[str, Any],
        has_default: bool,
        default: Any,
    ) -> List[RawLoss]:
        if self.old_name not in component:
            return [
                RawLoss(
                    "rename_source_missing",
                    component_name,
                    self.new_name,
                    f"重命名 {self.old_name} -> {self.new_name} 时源字段不存在",
                    has_default,
                    default,
                )
            ]
        value = component.pop(self.old_name)
        if self.new_name in component:
            return [
                RawLoss(
                    "collision",
                    component_name,
                    self.old_name,
                    f"重命名为 {self.new_name} 时目标字段已存在，保留现有值并丢弃源值 {value!r}",
                )
            ]
        component[self.new_name] = value
        return []


class DropRule(Rule):
    """显式删除字段，必然产生需要确认的丢失损失（字段存在时）。"""

    name = "drop"

    def apply(
        self,
        component_name: str,
        component: Dict[str, Any],
        has_default: bool,
        default: Any,
    ) -> List[RawLoss]:
        if self.field_name not in component:
            return []
        value = component.pop(self.field_name)
        return [
            RawLoss(
                "drop",
                component_name,
                self.field_name,
                f"字段被版本规则显式移除，原值 {value!r} 将丢失",
            )
        ]


class ConvertRule(Rule):
    """对字段值执行转换函数；字段缺失时跳过，异常包装为 TransformError。"""

    name = "convert"

    def __init__(self, field_name: str, transform: Callable[[Any], Any]):
        super().__init__(field_name)
        self.transform = transform

    def apply(
        self,
        component_name: str,
        component: Dict[str, Any],
        has_default: bool,
        default: Any,
    ) -> List[RawLoss]:
        if self.field_name not in component:
            return []
        try:
            component[self.field_name] = self.transform(component[self.field_name])
        except Exception as exc:  # 任何转换异常都视为该步骤失败
            raise TransformError(
                component_name,
                self.field_name,
                str(exc) or exc.__class__.__name__,
                exc,
            ) from exc
        return []


def lookup_default(
    defaults: Optional[Dict[str, Dict[str, Any]]],
    component_name: str,
    field_name: str,
) -> Tuple[bool, Any]:
    """返回 (是否配置了默认值, 默认值)，默认值本身允许为 None。"""

    component_defaults = (defaults or {}).get(component_name)
    if component_defaults is not None and field_name in component_defaults:
        return True, component_defaults[field_name]
    return False, None


def apply_rules(
    save: Dict[str, Any],
    edge: "MigrationEdgeLike",
    defaults: Optional[Dict[str, Dict[str, Any]]],
) -> List[RawLoss]:
    """在给定存档上原地应用一条迁移边的全部转换规则。

    只负责规则本身（重命名/删除/转换），不做必填校验和默认值补救，
    保证同一步骤内的字段重命名不会被先前填入的默认值遮蔽。
    """

    raw_losses: List[RawLoss] = []
    components = save.setdefault("components", {})

    for component_name in sorted(edge.rules):
        component = components.setdefault(component_name, {})
        for rule in edge.rules[component_name]:
            if isinstance(rule, RenameRule):
                has_default, default = lookup_default(
                    defaults, component_name, rule.new_name
                )
            else:
                has_default, default = lookup_default(
                    defaults, component_name, rule.field_name
                )
            raw_losses.extend(
                rule.apply(component_name, component, has_default, default)
            )

    save["version"] = edge.target
    return raw_losses


def check_required_fields(
    save: Dict[str, Any],
    defaults: Optional[Dict[str, Dict[str, Any]]],
    required_fields: Optional[Dict[str, List[str]]],
) -> List[RawLoss]:
    """在一步规则全部执行完之后检查必填字段缺失。"""

    components = save.setdefault("components", {})
    raw_losses: List[RawLoss] = []
    for component_name in sorted(components):
        for field_name in (required_fields or {}).get(component_name, []):
            if field_name in components[component_name]:
                continue
            has_default, default = lookup_default(
                defaults, component_name, field_name
            )
            raw_losses.append(
                RawLoss(
                    "missing_required",
                    component_name,
                    field_name,
                    f"必填字段 {field_name!r} 缺失，不能静默套用默认值",
                    has_default,
                    default,
                )
            )

    return raw_losses


def apply_edge(
    save: Dict[str, Any],
    edge: "MigrationEdgeLike",
    defaults: Optional[Dict[str, Dict[str, Any]]],
    required_fields: Optional[Dict[str, List[str]]],
) -> List[RawLoss]:
    """兼容接口：规则执行 + 必填校验，按正确顺序组合两个阶段。"""

    raw_losses = apply_rules(save, edge, defaults)
    raw_losses.extend(
        check_required_fields(save, defaults, required_fields)
    )
    return raw_losses


class MigrationEdgeLike:  # pragma: no cover - 仅用于类型提示
    source: str
    target: str
    rules: Dict[str, Dict[str, List[Rule]]]
