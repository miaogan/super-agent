"""V2-T2 Workflow 编译器：DAG 拓扑排序 + 环检测 + 合并 deepagents 配置。

输入：画布 JSON ``{"nodes": [...], "edges": [...]}``
输出：``CompiledConfig``，可直接传给 ``build_agent`` 组装 deep agent。

节点 schema（与前端 Vue Flow 对齐）::

    {
      "id": "n1",
      "type": "agent",
      "data": {
        "system_prompt": "你是代码助手",
        "model": "auto",
        "tools": ["sandbox", "memory"],
        "skills": ["greet"]
      },
      "position": {"x": 100, "y": 100}
    }

边 schema::

    {"id": "e1", "source": "n1", "target": "n2"}

编译规则
--------
1. **入口检测**：必须有且仅有一个 ``start`` 节点，且无入边。
2. **出口检测**：必须有至少一个 ``end`` 节点，且无出边。
3. **环检测**：Kahn 算法做拓扑排序，剩余节点 > 0 即有环。
4. **合并策略**：从 start 做正向 BFS，按拓扑序合并所有 ``agent`` 节点的
   ``system_prompt``（拼接）、``tools``（并集）、``skills``（并集）；
   ``model`` 取最后一个 agent 节点的值（用户在画布末端决定的模型优先）。
5. **子代理**：``subagent`` 节点的 ``data.subagents`` 累加到编译产物的
   ``subagents`` 列表，供 ``SubagentOrchestrator`` 使用（V2-T4）。
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from app.workflow.branch import (
    BranchRoute,
    extract_branches,
    validate_branch_nodes,
)
from app.workflow.hil import (
    ApprovalNodeSpec,
    validate_approval_nodes,
)

# 支持的节点类型（V2.5 扩展：if / switch / approval）
NODE_TYPES = {
    "start",
    "agent",
    "tool",
    "subagent",
    "if",
    "switch",
    "approval",
    "end",
}


class CompileError(ValueError):
    """编译失败（DAG 不合法）。"""


@dataclass
class CompiledConfig:
    """编译产物：可直接喂给 ``build_agent``。"""

    system_prompt: str = ""
    model: str = "auto"
    tools: list[str] = field(default_factory=list)  # 工具名集合
    skills: list[str] = field(default_factory=list)  # skill 名集合
    subagents: list[dict[str, Any]] = field(default_factory=list)
    # V2.5：分支路由表（if/switch 节点的编译期元信息）
    branches: list[BranchRoute] = field(default_factory=list)
    # V2.5-T2：审批节点 spec 列表（按拓扑序）
    approvals: list[ApprovalNodeSpec] = field(default_factory=list)
    # 原始拓扑序（调试用）
    topological_order: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "system_prompt": self.system_prompt,
            "model": self.model,
            "tools": self.tools,
            "skills": self.skills,
            "subagents": self.subagents,
            "branches": [b.to_dict() for b in self.branches],
            "approvals": [
                {
                    "node_id": a.node_id,
                    "message": a.message,
                    "assignee": a.assignee,
                    "timeout_seconds": a.timeout_seconds,
                    "on_approve": a.on_approve,
                    "on_reject": a.on_reject,
                }
                for a in self.approvals
            ],
            "topological_order": self.topological_order,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


# ---------------------------------------------------------------------- #
# 校验
# ---------------------------------------------------------------------- #


def validate_definition(definition: dict[str, Any] | str) -> tuple[list[dict], list[dict]]:
    """解析 + 校验画布 JSON，返回 (nodes, edges)。

    Raises:
        CompileError: 结构不合法。
    """
    if isinstance(definition, str):
        try:
            definition = json.loads(definition)
        except json.JSONDecodeError as exc:
            raise CompileError(f"definition 不是合法 JSON: {exc}") from exc

    if not isinstance(definition, dict):
        raise CompileError("definition 必须是对象")
    nodes = definition.get("nodes")
    edges = definition.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise CompileError("definition 必须含 nodes 和 edges 数组")

    # 节点 id 唯一 + 类型合法
    ids: set[str] = set()
    for n in nodes:
        nid = n.get("id")
        if not nid:
            raise CompileError(f"节点缺少 id: {n}")
        if nid in ids:
            raise CompileError(f"节点 id 重复: {nid}")
        ids.add(nid)
        ntype = n.get("type")
        if ntype not in NODE_TYPES:
            raise CompileError(f"节点 {nid} 类型非法: {ntype}")

    # 边端点存在
    for e in edges:
        if e.get("source") not in ids or e.get("target") not in ids:
            raise CompileError(f"边 {e.get('id')} 端点不存在")

    # start 唯一 + 无入边；end 至少一个 + 无出边
    starts = [n for n in nodes if n["type"] == "start"]
    if len(starts) != 1:
        raise CompileError(f"必须有且仅有一个 start 节点，当前 {len(starts)} 个")
    ends = [n for n in nodes if n["type"] == "end"]
    if not ends:
        raise CompileError("必须至少有一个 end 节点")

    # V2.5：if/switch 节点结构校验
    validate_branch_nodes(nodes)
    # V2.5-T2：approval 节点结构校验
    validate_approval_nodes(nodes)

    in_deg: dict[str, int] = {n["id"]: 0 for n in nodes}
    out_deg: dict[str, int] = {n["id"]: 0 for n in nodes}
    for e in edges:
        out_deg[e["source"]] += 1
        in_deg[e["target"]] += 1
    if in_deg[starts[0]["id"]] != 0:
        raise CompileError("start 节点不能有入边")
    for end in ends:
        if out_deg[end["id"]] != 0:
            raise CompileError(f"end 节点 {end['id']} 不能有出边")

    return nodes, edges


# ---------------------------------------------------------------------- #
# 拓扑排序 + 环检测
# ---------------------------------------------------------------------- #


def _topological_sort(
    nodes: list[dict], edges: list[dict]
) -> tuple[list[str], list[str]]:
    """Kahn 算法：返回 (拓扑序, 环内节点 id)。

    环内节点 = 排序后入度仍 > 0 的节点。
    """
    in_deg: dict[str, int] = {n["id"]: 0 for n in nodes}
    adj: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    for e in edges:
        adj[e["source"]].append(e["target"])
        in_deg[e["target"]] += 1

    queue = deque([nid for nid, d in in_deg.items() if d == 0])
    order: list[str] = []
    while queue:
        nid = queue.popleft()
        order.append(nid)
        for nxt in adj[nid]:
            in_deg[nxt] -= 1
            if in_deg[nxt] == 0:
                queue.append(nxt)

    cyclic = [nid for nid, d in in_deg.items() if d > 0]
    return order, cyclic


# ---------------------------------------------------------------------- #
# 编译
# ---------------------------------------------------------------------- #


def compile_workflow(definition: dict[str, Any] | str) -> CompiledConfig:
    """编译画布 JSON → CompiledConfig。

    Raises:
        CompileError: 结构/环/入口出口不合法。
    """
    nodes, edges = validate_definition(definition)
    order, cyclic = _topological_sort(nodes, edges)
    if cyclic:
        raise CompileError(f"存在环，涉及节点: {cyclic}")

    # 按拓扑序合并 agent / subagent / tool 节点
    node_map = {n["id"]: n for n in nodes}
    cfg = CompiledConfig(topological_order=order)
    # 从 start 开始的正向遍历（拓扑序已保证）
    for nid in order:
        node = node_map[nid]
        ntype = node.get("type")
        data = node.get("data") or {}
        if ntype == "start":
            # start 节点 data.system_prompt 作为前缀（可选）
            prefix = data.get("system_prompt", "").strip()
            if prefix:
                cfg.system_prompt = prefix + "\n\n"
        elif ntype == "agent":
            sp = data.get("system_prompt", "").strip()
            if sp:
                cfg.system_prompt += sp + "\n---\n"
            model = data.get("model", "auto")
            if model:
                cfg.model = model
            for t in data.get("tools", []):
                if t not in cfg.tools:
                    cfg.tools.append(t)
            for s in data.get("skills", []):
                if s not in cfg.skills:
                    cfg.skills.append(s)
        elif ntype == "subagent":
            subs = data.get("subagents", [])
            for sub in subs:
                if sub not in cfg.subagents:
                    cfg.subagents.append(sub)
        elif ntype == "tool":
            # 独立 tool 节点也并入 tools 集合
            t = data.get("tool")
            if t and t not in cfg.tools:
                cfg.tools.append(t)

    # 去掉末尾的分隔符
    cfg.system_prompt = cfg.system_prompt.rstrip("\n-").rstrip()
    if not cfg.system_prompt:
        cfg.system_prompt = "你是一个有用的助手。"

    # V2.5：提取 if/switch 分支路由表
    cfg.branches = extract_branches(nodes, edges)
    # V2.5-T2：提取 approval 节点 spec（按拓扑序）
    cfg.approvals = [
        ApprovalNodeSpec.from_node(node_map[nid])
        for nid in order
        if node_map[nid].get("type") == "approval"
    ]
    return cfg
