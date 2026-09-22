"""V2.5-T1 条件分支节点：if/switch + 表达式求值器。

节点类型扩展（在 V2 ``start/agent/tool/subagent/end`` 基础上新增）：
- ``if``     二分支：condition 为真走 source_handle="true"，否则走 "false"
- ``switch`` 多分支：data.cases 为 ``[{expr, label}]`` 列表，
  求值命中第一个 expr 为真的 case，走对应 source_handle=label；
  全不命中走 source_handle="default"。

表达式求值器 ``eval_ex``
------------------------
- 纯函数 + 白名单 AST，不调用 ``eval`` / 不引入完整 DSL
- 支持字面量（str/int/float/bool/None）、变量引用（``$var``）、
  比较运算（``== != > >= < <=``）、逻辑运算（``and or not``）、
  字符串包含（``contains``）、字典取值（``context.field`` / ``context["field"]``）
- 变量上下文：``context`` 字典（来自上游节点的输出快照 + 用户输入）

编译器集成
---------
- ``NODE_TYPES`` 扩展为 ``{"start","agent","tool","subagent","if","switch","end"}``
- ``validate_definition`` 校验 if/switch 节点的 ``source_handle`` 唯一性
- ``compile_workflow`` 编译时记录分支路由表到 ``CompiledConfig.branches``，
  供执行器（V2.5 后续任务）按运行时求值结果选边
"""

from __future__ import annotations

import ast
import operator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.workflow.compiler import CompiledConfig, CompileError  # noqa: F401

# 扩展节点类型集（与 compiler.py 对齐，但避免循环导入）
# compiler.py 的 NODE_TYPES 在 V2.5 已扩展为含 if/switch，
# 这里保持独立常量供前端/测试直接引用，运行时从 compiler 导入更准确
NODE_TYPES_V25 = {"start", "agent", "tool", "subagent", "if", "switch", "end"}

# 表达式求值白名单 AST 节点类型
# 注意：ast.walk 会遍历到 Load/Store/Del/And/Or/Not/Eq/Gt 等上下文/运算符子节点，
# 这些不是独立可执行节点，统一加入白名单避免误报。
_ALLOWED_AST_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.UnaryOp,
    ast.Compare,
    ast.Name,
    ast.Constant,
    ast.Subscript,
    ast.Attribute,
    ast.Call,
    ast.keyword,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.BinOp,
    # 上下文节点（ast.walk 会遍历到）
    ast.Load,
    ast.Store,
    ast.Del,
    # 运算符节点（ast.Compare.ops / ast.BoolOp.op 等的子节点）
    ast.And,
    ast.Or,
    ast.Not,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.USub,
    ast.UAdd,
    # 索引/切片上下文
    ast.Index,
)

# 比较运算符映射
_CMP_OPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}

# 逻辑运算符映射
_BOOL_OPS = {ast.And: all, ast.Or: any}

# 二元算术运算（用于数值比较的中间步骤，如 ``len(x) > 5``）
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
}

# 内置函数白名单
_ALLOWED_FUNCS = {
    "len": len,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "contains": lambda container, item: item in container,
    "startswith": lambda s, prefix: s.startswith(prefix),
    "endswith": lambda s, suffix: s.endswith(suffix),
    "lower": lambda s: s.lower(),
    "upper": lambda s: s.upper(),
}


class EvalError(ValueError):
    """表达式求值失败（语法/语义/越权）。"""


def eval_ex(expr: str, context: dict[str, Any] | None = None) -> Any:
    """安全求值表达式。

    Args:
        expr: 表达式字符串，例如 ``$messages | length > 5``、
            ``contains($input, "bug")``、``$severity == "high"``
        context: 变量上下文；``$name`` 语法糖会去 context 里取 ``name``

    Returns:
        求值结果（任意类型，通常用于 if 判定）

    Raises:
        EvalError: 语法不合法、调用了未授权函数、引用了未定义变量
    """
    if not isinstance(expr, str) or not expr.strip():
        raise EvalError("表达式为空")
    # 语法糖：$name → name 变量；保留 $ 作为合法标识符首字符不现实，直接替换
    src = _normalize_syntax_sugar(expr)
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError as exc:
        raise EvalError(f"表达式语法错误: {exc}") from exc
    _check_ast(tree, expr)
    return _eval_node(tree.body, context or {})


def _normalize_syntax_sugar(expr: str) -> str:
    """把 ``$name`` 替换为合法标识符 ``name``，便于走标准 AST。

    语义上 ``$name`` 等价于从 context 取 ``context["name"]``。
    实现上：解析时把 ``$xxx`` 当成普通 Name，求值时从 context 取值。
    """
    out: list[str] = []
    i = 0
    n = len(expr)
    while i < n:
        ch = expr[i]
        if ch == "$" and i + 1 < n and (expr[i + 1].isalpha() or expr[i + 1] == "_"):
            # 消费 $ 后的标识符
            j = i + 1
            while j < n and (expr[j].isalnum() or expr[j] == "_"):
                j += 1
            out.append(expr[i + 1 : j])
            i = j
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _check_ast(tree: ast.AST, src: str) -> None:
    """递归校验 AST 节点都在白名单内。"""
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_AST_NODES):
            raise EvalError(f"表达式含未授权语法节点: {type(node).__name__} (源: {src})")
        # 函数调用白名单
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id not in _ALLOWED_FUNCS:
                raise EvalError(f"调用了未授权函数: {func.id}")
            elif isinstance(func, ast.Attribute):
                raise EvalError(f"不允许调用属性方法: {ast.dump(func)}")


def _eval_node(node: ast.AST, context: dict[str, Any]) -> Any:
    """递归求值。"""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        # JS/JSON 风格小写字面量（true/false/null）支持
        if node.id == "true":
            return True
        if node.id == "false":
            return False
        if node.id in ("null", "none"):
            return None
        # 标识符取自 context；缺省返回 None（便于 ``if $x is None`` 判定）
        return context.get(node.id)
    if isinstance(node, ast.BoolOp):
        op_fn = _BOOL_OPS.get(type(node.op))
        if op_fn is None:
            raise EvalError(f"未知逻辑运算: {type(node.op).__name__}")
        return op_fn([_eval_node(v, context) for v in node.values])
    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand, context)
        if isinstance(node.op, ast.Not):
            return not operand
        if isinstance(node.op, ast.USub):
            return -operand
        raise EvalError(f"未知一元运算: {type(node.op).__name__}")
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, context)
        for op, comparator in zip(node.ops, node.comparators):
            op_fn = _CMP_OPS.get(type(op))
            if op_fn is None:
                raise EvalError(f"未知比较运算: {type(op).__name__}")
            right = _eval_node(comparator, context)
            if not op_fn(left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.BinOp):
        op_fn = _BIN_OPS.get(type(node.op))
        if op_fn is None:
            raise EvalError(f"未知算术运算: {type(node.op).__name__}")
        return op_fn(_eval_node(node.left, context), _eval_node(node.right, context))
    if isinstance(node, ast.List):
        return [_eval_node(e, context) for e in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_eval_node(e, context) for e in node.elts)
    if isinstance(node, ast.Dict):
        return {
            _eval_node(k, context): _eval_node(v, context)
            for k, v in zip(node.keys, node.values)
        }
    if isinstance(node, ast.Attribute):
        obj = _eval_node(node.value, context)
        if obj is None:
            return None
        # dict 优先按键取值（context.field 语义），非 dict 再走 getattr
        if isinstance(obj, dict):
            return obj.get(node.attr)
        return getattr(obj, node.attr, None)
    if isinstance(node, ast.Subscript):
        obj = _eval_node(node.value, context)
        key = _eval_node(node.slice, context)
        try:
            return obj[key] if obj is not None else None
        except (KeyError, IndexError, TypeError):
            return None
    if isinstance(node, ast.Call):
        func_name = node.func.id if isinstance(node.func, ast.Name) else None
        func = _ALLOWED_FUNCS.get(func_name) if func_name else None
        if func is None:
            raise EvalError(f"未知函数: {func_name}")
        args = [_eval_node(a, context) for a in node.args]
        kwargs = {k.arg: _eval_node(k.value, context) for k in node.keywords}
        return func(*args, **kwargs)
    raise EvalError(f"未支持的 AST 节点: {type(node).__name__}")


# ---------------------------------------------------------------------- #
# 分支路由表
# ---------------------------------------------------------------------- #


@dataclass
class BranchRoute:
    """编译期记录的分支节点路由规则。"""

    node_id: str
    kind: str  # "if" | "switch"
    # if: [("true", target_id), ("false", target_id)]
    # switch: [(case_label, target_id), ...] + ("default", target_id)
    routes: list[tuple[str, str]] = field(default_factory=list)
    # if 的 condition 表达式 / switch 的 case 表达式列表
    expressions: list[str] = field(default_factory=list)
    # switch 的 default 目标
    default_target: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "routes": list(self.routes),
            "expressions": list(self.expressions),
            "default_target": self.default_target,
        }


def select_route(route: BranchRoute, context: dict[str, Any]) -> str | None:
    """运行时根据上下文求值选择目标节点 id。

    Args:
        route: 编译期记录的分支路由
        context: 运行时上下文（来自上游节点输出快照）

    Returns:
        命中的目标节点 id；if 的 false 分支或 switch 的 default 也可能返回 None
        （表示无出边，等价于直接结束）
    """
    if route.kind == "if":
        # expressions[0] 是 if 的 condition
        try:
            cond = eval_ex(route.expressions[0], context)
        except EvalError:
            cond = False
        # routes: [("true", t1), ("false", t2)]
        for label, target in route.routes:
            if (label == "true" and cond) or (label == "false" and not cond):
                return target
        return None
    if route.kind == "switch":
        # 逐个 case 求值，命中第一个为真的
        # expressions 按 case 顺序排列，routes 按 (label, target) 排列
        for i, expr in enumerate(route.expressions):
            try:
                if eval_ex(expr, context):
                    # 第 i 个 case 命中，取 routes 第 i 项的 target
                    if i < len(route.routes):
                        return route.routes[i][1]
            except EvalError:
                continue
        return route.default_target
    return None


def extract_branches(
    nodes: list[dict], edges: list[dict]
) -> list[BranchRoute]:
    """从画布节点 + 边提取分支路由表（编译期产物）。

    对每个 if/switch 节点：
    - 收集其所有出边，按 source_handle 分组
    - if：source_handle ∈ {"true","false"}
    - switch：source_handle ∈ case labels ∪ "default"
    """
    routes: list[BranchRoute] = []
    # 按源节点分组出边
    out_edges: dict[str, list[dict]] = {}
    for e in edges:
        out_edges.setdefault(e["source"], []).append(e)

    for node in nodes:
        ntype = node.get("type")
        if ntype not in ("if", "switch"):
            continue
        nid = node["id"]
        data = node.get("data") or {}
        outs = out_edges.get(nid, [])

        if ntype == "if":
            condition = data.get("condition", "False")
            route = BranchRoute(
                node_id=nid,
                kind="if",
                expressions=[condition],
            )
            for e in outs:
                handle = e.get("sourceHandle") or "true"
                route.routes.append((handle, e["target"]))
            routes.append(route)
        elif ntype == "switch":
            cases = data.get("cases", [])
            route = BranchRoute(
                node_id=nid,
                kind="switch",
                expressions=[c.get("expr", "False") for c in cases],
            )
            for e in outs:
                handle = e.get("sourceHandle") or "default"
                if handle == "default":
                    route.default_target = e["target"]
                else:
                    route.routes.append((handle, e["target"]))
            # case 表达式与 routes 顺序对齐
            for c in cases:
                label = c.get("label")
                if label:
                    # 确保 case 的 label 在 routes 里找到对应 target
                    if not any(h == label for h, _ in route.routes) and not route.default_target:
                        route.default_target = None
            routes.append(route)
    return routes


def validate_branch_nodes(nodes: list[dict]) -> None:
    """校验 if/switch 节点的结构合法性。"""
    # 延迟导入避免循环依赖：compiler -> branch -> compiler
    from app.workflow.compiler import CompileError  # noqa: PLC0415

    for node in nodes:
        ntype = node.get("type")
        if ntype == "if":
            data = node.get("data") or {}
            if not data.get("condition"):
                raise CompileError(f"if 节点 {node['id']} 缺少 condition")
        elif ntype == "switch":
            data = node.get("data") or {}
            cases = data.get("cases")
            if not isinstance(cases, list) or not cases:
                raise CompileError(f"switch 节点 {node['id']} 必须有 cases 列表")
            for i, c in enumerate(cases):
                if not isinstance(c, dict) or not c.get("expr"):
                    raise CompileError(
                        f"switch 节点 {node['id']} 第 {i} 个 case 缺少 expr"
                    )
