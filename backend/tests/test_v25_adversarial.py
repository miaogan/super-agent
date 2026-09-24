"""V3-T7 adversarial 子代理测试。

不依赖真实 PG / deepagents / LLM；全部用 fake runner 注入。
覆盖：
- vote 策略：多数投票合并 + 平票取首个 + 自定义 vote_fn（JSON 字段提取）
- judge 策略：法官 runner 注入 + 默认法官 spec 校验
- 非法策略 / quorum 不足 / 无成功输出
- workflow 包导出可见性
"""

from __future__ import annotations

import asyncio

import pytest

from app.workflow import (
    ADVERSARIAL_STRATEGIES,
    MERGE_JUDGE,
    MERGE_VOTE,
    AdversarialOrchestrator,
    AdversarialResult,
    default_vote_fn,
)
from app.workflow.parallel import MERGE_ALL, ParallelError


def _run(coro):
    return asyncio.run(coro)


def _make_runner(outputs: list[str]):
    """返回按子代理顺序返回固定输出的 fake runner。"""
    idx = 0

    async def runner(spec, task, context):
        nonlocal idx
        out = outputs[idx % len(outputs)]
        idx += 1
        return out

    return runner


# ---------------------------------------------------------------------- #
# vote 策略
# ---------------------------------------------------------------------- #


def test_vote_majority_wins():
    """3 个子代理，2 个输出一致 → 多数票胜出。"""
    async def run():
        adv = AdversarialOrchestrator(
            subagents=[
                {"name": "a", "description": "1"},
                {"name": "b", "description": "2"},
                {"name": "c", "description": "3"},
            ],
            strategy=MERGE_VOTE,
            runner=_make_runner(["答案: 42", "答案: 42", "答案: 7"]),
        )
        result = await adv.run_adversarial("1+1=?")
        assert isinstance(result, AdversarialResult)
        assert result.final_output == "答案: 42"
        assert result.winner_vote == "答案: 42"
        assert result.vote_counts == {"答案: 42": 2, "答案: 7": 1}
        assert result.success_count == 3
        assert result.error is None

    _run(run())


def test_vote_tie_takes_first():
    """平票（1:1）→ 取第一个成功输出。"""
    async def run():
        adv = AdversarialOrchestrator(
            subagents=[
                {"name": "a", "description": "1"},
                {"name": "b", "description": "2"},
            ],
            strategy=MERGE_VOTE,
            runner=_make_runner(["AAA", "BBB"]),
        )
        result = await adv.run_adversarial("task")
        assert result.final_output == "AAA"
        assert result.winner_vote == "AAA"

    _run(run())


def test_vote_custom_vote_fn_json_field():
    """自定义 vote_fn：提取 JSON answer 字段投票。"""
    import json

    def vote_fn(step):
        try:
            return json.loads(step.output)["answer"]
        except (ValueError, KeyError):
            return step.output

    async def run():
        adv = AdversarialOrchestrator(
            subagents=[
                {"name": "a", "description": "1"},
                {"name": "b", "description": "2"},
                {"name": "c", "description": "3"},
            ],
            strategy=MERGE_VOTE,
            vote_fn=vote_fn,
            runner=_make_runner(
                ['{"answer": "shanghai"}', '{"answer": "shanghai"}', '{"answer": "beijing"}']
            ),
        )
        result = await adv.run_adversarial("中国最大城市?")
        assert result.final_output == '{"answer": "shanghai"}'
        assert result.winner_vote == "shanghai"

    _run(run())


def test_vote_whitespace_normalized_default_fn():
    """默认 vote_fn 归一化空白：'42' 与 ' 42 ' 算同一票。"""
    async def run():
        adv = AdversarialOrchestrator(
            subagents=[
                {"name": "a", "description": "1"},
                {"name": "b", "description": "2"},
                {"name": "c", "description": "3"},
            ],
            strategy=MERGE_VOTE,
            runner=_make_runner(["  42  ", "42", "7"]),
        )
        result = await adv.run_adversarial("1+1=?")
        assert result.winner_vote == "42"
        assert result.vote_counts["42"] == 2

    _run(run())


# ---------------------------------------------------------------------- #
# judge 策略
# ---------------------------------------------------------------------- #


def test_judge_custom_runner():
    """judge 策略：注入 fake judge_runner，接收全部子代理输出。"""
    captured: dict = {}

    async def judge_runner(judge_spec, task, outputs, context):
        captured["spec_name"] = judge_spec["name"]
        captured["task"] = task
        captured["n_outputs"] = len(outputs)
        captured["agents"] = [o.agent for o in outputs]
        return "法官的最终答案"

    async def run():
        adv = AdversarialOrchestrator(
            subagents=[
                {"name": "a", "description": "1"},
                {"name": "b", "description": "2"},
            ],
            strategy=MERGE_JUDGE,
            judge_spec={"name": "chief-judge", "description": "总法官"},
            judge_runner=judge_runner,
            runner=_make_runner(["A 输出", "B 输出"]),
        )
        result = await adv.run_adversarial("评估两个方案")
        assert result.final_output == "法官的最终答案"
        assert result.judge_agent == "chief-judge"
        assert result.judge_raw == "法官的最终答案"
        assert captured["spec_name"] == "chief-judge"
        assert captured["n_outputs"] == 2
        assert captured["agents"] == ["a", "b"]
        assert "评估两个方案" in captured["task"]
        assert "A 输出" in captured["task"]
        assert "B 输出" in captured["task"]

    _run(run())


def test_judge_default_spec():
    """judge 策略：不传 judge_spec 时用内建默认法官。"""
    async def fake_judge(spec, task, outputs, ctx):
        return "默认法官答案"

    async def run():
        adv = AdversarialOrchestrator(
            subagents=[
                {"name": "a", "description": "1"},
                {"name": "b", "description": "2"},
            ],
            strategy=MERGE_JUDGE,
            judge_runner=fake_judge,
            runner=_make_runner(["x", "y"]),
        )
        assert adv.judge_spec is not None
        assert adv.judge_spec["name"] == "judge"
        result = await adv.run_adversarial("t")
        assert result.final_output == "默认法官答案"
        assert result.judge_agent == "judge"

    _run(run())


# ---------------------------------------------------------------------- #
# 边界与错误
# ---------------------------------------------------------------------- #


def test_invalid_strategy_raises():
    with pytest.raises(ParallelError):
        AdversarialOrchestrator(
            subagents=[{"name": "a", "description": "1"}],
            strategy="bogus",
        )


def test_vote_quorum_insufficient():
    """全部失败 → quorum 不足，返回失败结果。"""
    async def failing_runner(spec, task, context):
        raise RuntimeError("boom")

    async def run():
        adv = AdversarialOrchestrator(
            subagents=[{"name": "a", "description": "1"}],
            strategy=MERGE_VOTE,
            min_success=1,
            runner=failing_runner,
        )
        result = await adv.run_adversarial("t")
        assert result.success_count == 0
        assert result.error is not None
        assert result.final_output == ""

    _run(run())


def test_adversarial_first_all_passthrough():
    """非 vote/judge 策略透传底层（first/all/merge 行为不变）。"""
    async def run():
        adv = AdversarialOrchestrator(
            subagents=[
                {"name": "a", "description": "1"},
                {"name": "b", "description": "2"},
            ],
            strategy=MERGE_ALL,
            runner=_make_runner(["x", "y"]),
        )
        result = await adv.run_adversarial("t")
        assert result.strategy == MERGE_ALL
        assert "x" in result.final_output and "y" in result.final_output

    _run(run())


# ---------------------------------------------------------------------- #
# 包导出可见性
# ---------------------------------------------------------------------- #


def test_exports():
    assert {MERGE_VOTE, MERGE_JUDGE} <= ADVERSARIAL_STRATEGIES
    assert MERGE_VOTE == "vote"
    assert MERGE_JUDGE == "judge"
    assert callable(default_vote_fn)
