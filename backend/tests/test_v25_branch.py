"""V2.5-T1 条件分支节点单元测试。

覆盖：
- 表达式求值器 ``eval_ex``：字面量、变量、比较、逻辑、内置函数、语法糖
- 分支路由 ``select_route``：if 二分支、switch 多分支、default 兜底
- 编译器集成：NODE_TYPES 扩展、CompiledConfig.branches 提取、validate_definition
"""

from __future__ import annotations

import pytest

from app.workflow.branch import (
    BranchRoute,
    EvalError,
    eval_ex,
    extract_branches,
    select_route,
    validate_branch_nodes,
)
from app.workflow.compiler import (
    CompileError,
    CompiledConfig,
    NODE_TYPES,
    compile_workflow,
    validate_definition,
)


# ---------------------------------------------------------------------- #
# eval_ex 表达式求值器
# ---------------------------------------------------------------------- #


class TestEvalEx:
    """表达式求值器测试。"""

    def test_literal(self):
        assert eval_ex("True") is True
        assert eval_ex("False") is False
        assert eval_ex("1") == 1
        assert eval_ex("1.5") == 1.5
        assert eval_ex("'hello'") == "hello"

    def test_variable_syntax_sugar(self):
        """$name 语法糖等价于 context["name"]。"""
        ctx = {"input": "有 bug", "severity": "high"}
        assert eval_ex("$input", ctx) == "有 bug"
        assert eval_ex("$severity", ctx) == "high"

    def test_variable_missing_returns_none(self):
        assert eval_ex("$undefined", {}) is None

    def test_comparison_eq(self):
        ctx = {"severity": "high"}
        assert eval_ex("$severity == 'high'", ctx) is True
        assert eval_ex("$severity == 'low'", ctx) is False

    def test_comparison_numeric(self):
        ctx = {"count": 10}
        assert eval_ex("$count > 5", ctx) is True
        assert eval_ex("$count >= 10", ctx) is True
        assert eval_ex("$count < 5", ctx) is False
        assert eval_ex("$count != 5", ctx) is True

    def test_logical_and(self):
        ctx = {"a": True, "b": False}
        assert eval_ex("$a and $b", ctx) is False
        assert eval_ex("$a or $b", ctx) is True

    def test_logical_not(self):
        ctx = {"flag": False}
        assert eval_ex("not $flag", ctx) is True

    def test_contains_function(self):
        ctx = {"input": "用户报告了一个 bug"}
        assert eval_ex('contains($input, "bug")', ctx) is True
        assert eval_ex('contains($input, "feature")', ctx) is False

    def test_len_function(self):
        ctx = {"items": [1, 2, 3, 4, 5]}
        assert eval_ex("len($items) > 3", ctx) is True
        assert eval_ex("len($items) >= 5", ctx) is True

    def test_dict_subscript(self):
        ctx = {"meta": {"priority": "high"}}
        assert eval_ex('$meta["priority"] == "high"', ctx) is True

    def test_attribute_access(self):
        ctx = {"obj": type("X", (), {"name": "test"})()}
        assert eval_ex('$obj.name == "test"', ctx) is True

    def test_compound_expression(self):
        ctx = {"input": "bug", "severity": "high", "count": 10}
        expr = 'contains($input, "bug") and $severity == "high" and $count > 5'
        assert eval_ex(expr, ctx) is True

    def test_empty_expression_raises(self):
        with pytest.raises(EvalError):
            eval_ex("")

    def test_syntax_error_raises(self):
        with pytest.raises(EvalError):
            eval_ex("$unclosed[")

    def test_unauthorized_function_raises(self):
        with pytest.raises(EvalError):
            eval_ex("__import__('os')")

    def test_attribute_method_call_raises(self):
        with pytest.raises(EvalError):
            eval_ex("'x'.upper()")

    def test_chained_comparison(self):
        ctx = {"x": 5}
        assert eval_ex("1 < $x < 10", ctx) is True
        assert eval_ex("1 < $x < 3", ctx) is False

    def test_non_string_expr_raises(self):
        with pytest.raises(EvalError):
            eval_ex(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------- #
# select_route 分支路由选择
# ---------------------------------------------------------------------- #


class TestSelectRoute:
    """分支路由选择测试。"""

    def test_if_true_branch(self):
        route = BranchRoute(
            node_id="n1",
            kind="if",
            expressions=["$severity == 'high'"],
            routes=[("true", "n_high"), ("false", "n_low")],
        )
        ctx = {"severity": "high"}
        assert select_route(route, ctx) == "n_high"

    def test_if_false_branch(self):
        route = BranchRoute(
            node_id="n1",
            kind="if",
            expressions=["$severity == 'high'"],
            routes=[("true", "n_high"), ("false", "n_low")],
        )
        ctx = {"severity": "low"}
        assert select_route(route, ctx) == "n_low"

    def test_if_eval_error_falls_to_false(self):
        route = BranchRoute(
            node_id="n1",
            kind="if",
            expressions=["$unclosed["],
            routes=[("true", "n_high"), ("false", "n_low")],
        )
        ctx = {}
        # 求值失败时 cond=False，走 false 分支
        assert select_route(route, ctx) == "n_low"

    def test_switch_first_case_matches(self):
        route = BranchRoute(
            node_id="n1",
            kind="switch",
            expressions=["$level == 1", "$level == 2", "$level == 3"],
            routes=[("case_1", "n_a"), ("case_2", "n_b"), ("case_3", "n_c")],
            default_target="n_default",
        )
        ctx = {"level": 1}
        assert select_route(route, ctx) == "n_a"

    def test_switch_second_case_matches(self):
        route = BranchRoute(
            node_id="n1",
            kind="switch",
            expressions=["$level == 1", "$level == 2", "$level == 3"],
            routes=[("case_1", "n_a"), ("case_2", "n_b"), ("case_3", "n_c")],
            default_target="n_default",
        )
        ctx = {"level": 2}
        assert select_route(route, ctx) == "n_b"

    def test_switch_default_when_no_match(self):
        route = BranchRoute(
            node_id="n1",
            kind="switch",
            expressions=["$level == 1", "$level == 2"],
            routes=[("case_1", "n_a"), ("case_2", "n_b")],
            default_target="n_default",
        )
        ctx = {"level": 99}
        assert select_route(route, ctx) == "n_default"

    def test_switch_eval_error_skips_case(self):
        route = BranchRoute(
            node_id="n1",
            kind="switch",
            expressions=["$bad[", "$level == 2"],
            routes=[("case_1", "n_a"), ("case_2", "n_b")],
            default_target="n_default",
        )
        ctx = {"level": 2}
        # 第一个 case 求值失败被跳过，第二个命中
        assert select_route(route, ctx) == "n_b"

    def test_unknown_kind_returns_none(self):
        route = BranchRoute(
            node_id="n1",
            kind="unknown",
            expressions=[],
            routes=[],
        )
        assert select_route(route, {}) is None


# ---------------------------------------------------------------------- #
# validate_branch_nodes 节点校验
# ---------------------------------------------------------------------- #


class TestValidateBranchNodes:
    """if/switch 节点校验测试。"""

    def test_if_missing_condition_raises(self):
        nodes = [{"id": "n1", "type": "if", "data": {}}]
        with pytest.raises(CompileError):
            validate_branch_nodes(nodes)

    def test_switch_missing_cases_raises(self):
        nodes = [{"id": "n1", "type": "switch", "data": {}}]
        with pytest.raises(CompileError):
            validate_branch_nodes(nodes)

    def test_switch_case_missing_expr_raises(self):
        nodes = [{"id": "n1", "type": "switch", "data": {"cases": [{"label": "a"}]}}]
        with pytest.raises(CompileError):
            validate_branch_nodes(nodes)

    def test_valid_if_passes(self):
        nodes = [{"id": "n1", "type": "if", "data": {"condition": "$x > 5"}}]
        validate_branch_nodes(nodes)  # 不抛异常即通过

    def test_valid_switch_passes(self):
        nodes = [
            {
                "id": "n1",
                "type": "switch",
                "data": {
                    "cases": [
                        {"label": "a", "expr": "$x == 1"},
                        {"label": "b", "expr": "$x == 2"},
                    ]
                },
            }
        ]
        validate_branch_nodes(nodes)

    def test_non_branch_nodes_ignored(self):
        nodes = [
            {"id": "n1", "type": "start"},
            {"id": "n2", "type": "agent", "data": {"system_prompt": "x"}},
            {"id": "n3", "type": "end"},
        ]
        validate_branch_nodes(nodes)


# ---------------------------------------------------------------------- #
# 编译器集成测试
# ---------------------------------------------------------------------- #


class TestCompilerIntegration:
    """编译器与 if/switch 集成测试。"""

    def test_node_types_extended(self):
        assert "if" in NODE_TYPES
        assert "switch" in NODE_TYPES

    def test_compile_if_workflow(self):
        """编译含 if 节点的画布。"""
        definition = {
            "nodes": [
                {"id": "start", "type": "start", "data": {}},
                {"id": "agent_a", "type": "agent", "data": {"system_prompt": "分支A"}},
                {"id": "agent_b", "type": "agent", "data": {"system_prompt": "分支B"}},
                {"id": "if1", "type": "if", "data": {"condition": "$severity == 'high'"}},
                {"id": "end", "type": "end", "data": {}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "if1"},
                {
                    "id": "e2",
                    "source": "if1",
                    "target": "agent_a",
                    "sourceHandle": "true",
                },
                {
                    "id": "e3",
                    "source": "if1",
                    "target": "agent_b",
                    "sourceHandle": "false",
                },
                {"id": "e4", "source": "agent_a", "target": "end"},
                {"id": "e5", "source": "agent_b", "target": "end"},
            ],
        }
        cfg = compile_workflow(definition)
        assert len(cfg.branches) == 1
        branch = cfg.branches[0]
        assert branch.kind == "if"
        assert branch.expressions == ["$severity == 'high'"]
        assert ("true", "agent_a") in branch.routes
        assert ("false", "agent_b") in branch.routes

    def test_compile_switch_workflow(self):
        """编译含 switch 节点的画布。"""
        definition = {
            "nodes": [
                {"id": "start", "type": "start", "data": {}},
                {
                    "id": "switch1",
                    "type": "switch",
                    "data": {
                        "cases": [
                            {"label": "low", "expr": "$level < 3"},
                            {"label": "mid", "expr": "$level < 7"},
                            {"label": "high", "expr": "$level >= 7"},
                        ]
                    },
                },
                {"id": "agent_low", "type": "agent", "data": {"system_prompt": "低"}},
                {"id": "agent_mid", "type": "agent", "data": {"system_prompt": "中"}},
                {"id": "agent_high", "type": "agent", "data": {"system_prompt": "高"}},
                {"id": "agent_default", "type": "agent", "data": {"system_prompt": "默认"}},
                {"id": "end", "type": "end", "data": {}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "switch1"},
                {"id": "e2", "source": "switch1", "target": "agent_low", "sourceHandle": "low"},
                {"id": "e3", "source": "switch1", "target": "agent_mid", "sourceHandle": "mid"},
                {"id": "e4", "source": "switch1", "target": "agent_high", "sourceHandle": "high"},
                {
                    "id": "e5",
                    "source": "switch1",
                    "target": "agent_default",
                    "sourceHandle": "default",
                },
                {"id": "e6", "source": "agent_low", "target": "end"},
                {"id": "e7", "source": "agent_mid", "target": "end"},
                {"id": "e8", "source": "agent_high", "target": "end"},
                {"id": "e9", "source": "agent_default", "target": "end"},
            ],
        }
        cfg = compile_workflow(definition)
        assert len(cfg.branches) == 1
        branch = cfg.branches[0]
        assert branch.kind == "switch"
        assert len(branch.expressions) == 3
        assert branch.default_target == "agent_default"

    def test_compile_v2_workflow_still_works(self):
        """V2 原有节点类型仍可正常编译。"""
        definition = {
            "nodes": [
                {"id": "start", "type": "start", "data": {}},
                {"id": "agent1", "type": "agent", "data": {"system_prompt": "你好"}},
                {"id": "end", "type": "end", "data": {}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "agent1"},
                {"id": "e2", "source": "agent1", "target": "end"},
            ],
        }
        cfg = compile_workflow(definition)
        assert cfg.branches == []  # 无分支节点
        assert "你好" in cfg.system_prompt

    def test_compiled_config_to_dict_includes_branches(self):
        cfg = CompiledConfig()
        d = cfg.to_dict()
        assert "branches" in d
        assert d["branches"] == []


# ---------------------------------------------------------------------- #
# extract_branches 直接测试
# ---------------------------------------------------------------------- #


class TestExtractBranches:
    """分支路由提取测试。"""

    def test_no_branches_returns_empty(self):
        nodes = [{"id": "n1", "type": "agent"}]
        edges = []
        assert extract_branches(nodes, edges) == []

    def test_if_with_only_true_branch(self):
        """if 节点只有 true 分支（false 无出边）。"""
        nodes = [
            {"id": "if1", "type": "if", "data": {"condition": "$x"}},
            {"id": "n_a", "type": "agent"},
        ]
        edges = [{"id": "e1", "source": "if1", "target": "n_a", "sourceHandle": "true"}]
        routes = extract_branches(nodes, edges)
        assert len(routes) == 1
        assert routes[0].routes == [("true", "n_a")]
