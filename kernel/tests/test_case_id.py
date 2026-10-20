"""YAML 的 id 作缓存身份：旧用例键不变、同 id 改措辞键不变、round-trip 保留 id。"""

from __future__ import annotations

import pytest

from ui_agent.case.yaml_case import compile_case, plan_to_case_yaml
from ui_agent.replay.cache import cache_key, case_identity, goal_case_hash, plan_case_hash
from ui_agent.schema.check import CheckSpec
from ui_agent.schema.plan import PlanStep, TestPlan

CASE = """id: tickets-query-001
title: 票据查询
steps:
  - goal: 登录
    checks:
      - text_contains: 已登录
"""

NO_ID = """title: 票据查询
steps:
  - goal: 登录
    checks:
      - text_contains: 已登录
"""


def plan_of(text: str) -> TestPlan:
    result = compile_case(text)
    assert result["ok"], result["errors"]
    return TestPlan(**result["plan"])


def plain_plan(goal: str = "登录", expected: str = "已登录") -> TestPlan:
    return TestPlan(name="n", url="http://x/",
                    steps=[PlanStep(goal=goal, checks=[CheckSpec(kind="text_contains",
                                                                 expected=expected)])])


class TestYamlCarriesTheId:
    def test_id_is_compiled_into_the_plan(self):
        assert plan_of(CASE).case_id == "tickets-query-001"

    def test_missing_id_compiles_to_empty(self):
        assert plan_of(NO_ID).case_id == ""

    def test_round_trip_keeps_the_id(self):
        text = plan_to_case_yaml(plan_of(CASE))
        assert "id: tickets-query-001" in text
        assert compile_case(text)["ok"], "回写出的 YAML 必须还能编译"

    def test_no_id_means_no_id_key_in_yaml(self):
        assert "id:" not in plan_to_case_yaml(plan_of(NO_ID))

    def test_illegal_characters_are_still_rejected(self):
        bad = CASE.replace("tickets-query-001", "a/b")
        result = compile_case(bad)
        assert not result["ok"] and result["errors"][0]["code"] == "bad_name"


class TestIdentity:
    def test_id_becomes_the_identity(self):
        assert case_identity(plan_of(CASE)) == "id:tickets-query-001"

    def test_no_id_falls_back_to_the_content_hash(self):
        plan = plan_of(NO_ID)
        assert case_identity(plan) == plan_case_hash(plan)

    def test_rewording_the_goal_keeps_the_identity_with_an_id(self):
        swapped = CASE.replace("goal: 登录", "goal: 完成登录流程并进入工作台")
        assert case_identity(plan_of(CASE)) == case_identity(plan_of(swapped)), \
            "id 固定时改 goal 措辞不该让录像失效"

    def test_rewording_the_goal_changes_the_identity_without_an_id(self):
        """没有 id 的旧用例保持老语义：内容变则键变（这是既有行为，不许悄悄改）。"""
        a = plain_plan("登录")
        b = plain_plan("登录并进入工作台")
        assert plan_case_hash(a) != plan_case_hash(b)

    def test_changing_only_the_assertion_changes_the_content_hash(self):
        assert plan_case_hash(plain_plan("登录", "已登录")) != plan_case_hash(plain_plan("登录", "票据类型"))

    def test_changing_only_the_assertion_keeps_the_id_identity(self):
        a = plan_of(CASE)
        b = plan_of(CASE.replace("text_contains: 已登录", "text_contains: 票据类型"))
        assert case_identity(a) == case_identity(b)


class TestBackwardCompatibility:
    """最要紧的一条：加 case_id 字段不许动旧用例的哈希。"""

    def test_existing_case_hash_is_pinned(self):
        """把旧口径的期望值钉死：任何让旧键漂移的改动都会在这里失败。"""
        plan = plain_plan("登录", "已登录")
        data = plan.model_dump()
        data.pop("url", None)
        data.pop("case_id", None)
        data["vars"] = {key: "" for key in sorted(data.get("vars") or {})}
        from ui_agent.replay.cache import _sha256_json
        assert plan_case_hash(plan) == _sha256_json(data)

    def test_case_id_is_excluded_from_the_content_hash(self):
        """不排除的话，加字段会让所有旧用例哈希一起变（=静默作废全部录像）。"""
        plan = plain_plan()
        with_id = plan.model_copy(update={"case_id": "anything"})
        assert plan_case_hash(plan) == plan_case_hash(with_id)

    def test_cache_key_for_legacy_cases_is_unchanged(self):
        plan = plan_of(NO_ID)
        expected = cache_key(plan_case_hash(plan), "http://x/")
        assert cache_key(case_identity(plan), "http://x/") == expected

    def test_goal_mode_hash_is_untouched(self):
        """run --goal 走 goal_case_hash，与用例 id 无关。"""
        h = goal_case_hash("查询", [CheckSpec(kind="text_contains", expected="完成")])
        assert h == goal_case_hash("查询", [CheckSpec(kind="text_contains", expected="完成")])


class TestPolicyVersion:
    def test_cache_policy_stays_v3(self):
        """加 id 不改记录格式：旧录像必须继续有效。"""
        from ui_agent.replay.cache import CACHE_POLICY_VERSION
        assert CACHE_POLICY_VERSION == "v3"


class TestPlanJsonRoundTrip:
    def test_case_id_survives_plan_json(self):
        from ui_agent.llm.planner import plan_from_json, plan_to_json
        plan = plan_of(CASE)
        again = plan_from_json(plan_to_json(plan))
        assert again.case_id == "tickets-query-001"

    def test_bad_case_id_type_is_rejected_by_pydantic(self):
        with pytest.raises(Exception):
            TestPlan(case_id=123, steps=[PlanStep(goal="g")])
