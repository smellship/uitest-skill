"""多步计划的离线单测：一个会话跑多步、失败即停、历史交接与产物结构。"""

from __future__ import annotations

import json

from fakes import (
    FakeBridge,
    FakeDriver,
    FakeEngine,
    ScriptedSupervisor,
    blocked,
    click,
    done,
    sealed_answers,
    settings_for,
    snapshot,
)

from ui_agent.decide.jev import to_jev_state
from ui_agent.driver.ladder import LadderOutcome
from ui_agent.llm.planner import plan_to_json
from ui_agent.observe.snapshot import build_state
from ui_agent.run.plan_runner import PlanRunner, resolve_step_url
from ui_agent.schema.check import CheckSpec
from ui_agent.schema.plan import PlanStep
from ui_agent.schema.plan import TestPlan as Plan
from ui_agent.schema.state import RecentAction

FORCE_WARNING = "L4 降级：依赖强制/合成事件，真实用户可能做不到（可用性缺陷候选）"


def two_step_plan() -> Plan:
    return Plan(name="登录并查询", url="http://x/", steps=[
        PlanStep(goal="登录", checks=[CheckSpec(kind="text_contains", expected="登录完成")]),
        PlanStep(goal="查询票据", checks=[CheckSpec(kind="text_contains", expected="票据类型")]),
    ])


def make_plan_runner(tmp_path, snapshots, decisions, plan=None, carry=2, engine=None):
    settings = settings_for(tmp_path, max_actions=8)
    driver = FakeDriver(snapshots)
    driver.s = settings
    engine = engine or FakeEngine(decisions)
    runner = PlanRunner(settings, plan or two_step_plan(), driver=driver, engine=engine,
                        verbose=False, history_carry=carry)
    return runner, driver, engine


def test_two_steps_share_one_browser_session(tmp_path):
    runner, driver, engine = make_plan_runner(
        tmp_path,
        [snapshot("a"), snapshot("b", text="登录完成"), snapshot("c", text="票据类型：电子发票")],
        [click(), done(), done()],
    )
    result = runner.run()

    assert result.status == "ok" and result.success
    assert [s.status for s in result.steps] == ["ok", "ok"]
    assert result.detail == "全部 2 步断言通过"
    assert driver.starts == 1 and driver.closed is True
    assert driver.gotos == ["http://x/"]
    assert (result.run_dir / "plan.json").read_text(encoding="utf-8") == plan_to_json(runner.plan)
    assert (result.run_dir / "report.html").exists()
    for step in result.steps:
        assert (result.run_dir / "steps" / step.dir / "result.json").exists()


def test_each_step_gets_its_own_goal_and_history_carries_over(tmp_path):
    runner, _, engine = make_plan_runner(
        tmp_path,
        [snapshot("a"), snapshot("b", text="登录完成"), snapshot("c", text="票据类型：电子发票")],
        [click(), done(), done()],
    )
    runner.run()

    assert engine.goals == ["登录", "登录", "查询票据"]
    # 第 2 步第一次决策必须能看到第 1 步的动作，且带 step 编号
    carried = engine.seen_states[2].recent_actions
    assert [a.action for a in carried] == ["CLICK"] and carried[0].step == 0


def test_history_carry_is_capped(tmp_path):
    plan = two_step_plan()
    plan.steps[0].checks = [CheckSpec(kind="text_contains", expected="登录完成")]
    runner, _, engine = make_plan_runner(
        tmp_path,
        [snapshot("a"), snapshot("b"), snapshot("c", text="登录完成"),
         snapshot("d", text="票据类型：电子发票")],
        [click(1), click(2), done(), done()],
        plan=plan, carry=1,
    )
    result = runner.run()
    assert result.status == "ok"
    # 第 1 步做了两次点击，但只带 1 条历史给第 2 步
    carried = engine.seen_states[3].recent_actions
    assert len(carried) == 1 and carried[0].action == "CLICK" and carried[0].step == 1


def test_failed_step_stops_the_plan_and_marks_the_rest_skipped(tmp_path):
    runner, driver, engine = make_plan_runner(
        tmp_path,
        [snapshot("a", text="首页")],
        [blocked()],
    )
    result = runner.run()

    assert result.status == "blocked" and not result.success
    assert [s.status for s in result.steps] == ["blocked", "skipped"]
    assert result.failed_step.index == 1
    assert "第 1 步未通过" in result.detail and "未执行" in result.steps[1].detail
    assert driver.closed is True
    assert not (result.run_dir / "steps" / result.steps[1].dir).exists()
    summary = json.loads((result.run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["kind"] == "plan" and summary["status"] == "blocked"
    assert [s["status"] for s in summary["steps"]] == ["blocked", "skipped"]


def test_engine_failure_is_recorded_as_that_step_error(tmp_path):
    boom = RuntimeError("缺少 TYPESAFE_API_KEY；请在 .env 中补上后再实跑。")
    runner, driver, _ = make_plan_runner(tmp_path, [snapshot("a")], [], engine=FakeEngine([], error=boom))
    result = runner.run()

    assert result.status == "error" and result.steps[0].status == "error"
    assert "TYPESAFE_API_KEY" in result.steps[0].detail and result.steps[1].status == "skipped"
    assert driver.closed is True


def test_replan_replaces_the_remaining_steps_and_retries_the_same_position(tmp_path):
    steps = [{"goal": "重进首页", "checks": [{"kind": "text_contains", "value": "首页"}]},
             {"goal": "查询票据", "checks": [{"kind": "text_contains", "value": "票据类型"}]}]
    sup = ScriptedSupervisor([{"ruling": "REPLAN", "reason": "第 1 步目标写错了", "steps": steps}])
    settings = settings_for(tmp_path, max_actions=8)
    driver = FakeDriver([snapshot("a", text="首页"), snapshot("b", text="首页"),
                         snapshot("c", text="首页"), snapshot("d", text="票据类型：电子发票")])
    driver.s = settings
    runner = PlanRunner(settings, two_step_plan(), driver=driver,
                        engine=FakeEngine([blocked(), click(), done(), done()]), verbose=False,
                        supervisor=sup)
    result = runner.run()

    assert result.status == "ok" and result.detail == "全部 2 步断言通过"
    assert [s.status for s in result.steps] == ["replanned", "ok", "ok"]
    assert [s.attempt for s in result.steps] == [1, 2, 1]
    assert [s.goal for s in result.steps] == ["登录", "重进首页", "查询票据"]
    retry = result.run_dir / "steps" / result.steps[1].dir
    assert result.steps[1].dir.endswith("-r2") and (retry / "result.json").exists()
    assert sup.asked[0]["trigger"] == "blocked" and sup.asked[0]["remaining"] == ["查询票据"]

    summary = json.loads((result.run_dir / "summary.json").read_text(encoding="utf-8"))
    assert [s["status"] for s in summary["steps"]] == ["replanned", "ok", "ok"]
    assert [s["goal"] for s in summary["replans"][0]["steps"]] == ["重进首页", "查询票据"]
    final = json.loads((result.run_dir / "plan.final.json").read_text(encoding="utf-8"))
    assert [s["goal"] for s in final["steps"]] == ["重进首页", "查询票据"]
    assert [s.goal for s in runner.plan.steps] == ["重进首页", "查询票据"]


def test_step_url_navigates_between_steps(tmp_path):
    plan = two_step_plan()
    plan.steps[1].url = "http://x/report"
    runner, driver, _ = make_plan_runner(
        tmp_path,
        [snapshot("a"), snapshot("b", text="登录完成"), snapshot("c", text="票据类型：电子发票")],
        [click(), done(), done()],
        plan=plan,
    )
    runner.run()
    assert driver.gotos == ["http://x/", "http://x/report"]


def test_resolve_step_url_uses_plan_origin_for_site_paths():
    plan_url = "http://10.100.3.2/bw-main-ui/incomeAlone-ui/income/invoiceCheck"
    assert resolve_step_url("/income/invoiceCheck", plan_url) == "http://10.100.3.2/income/invoiceCheck"
    # 绝对地址原样返回
    assert resolve_step_url("http://y/report", "http://x/a") == "http://y/report"
    # 协议相对地址不掺和，交给浏览器
    assert resolve_step_url("//y/report", "http://x/a") == "//y/report"
    # 没有入口就没有 origin 可借，原样返回（交给 goto 报错，不静默改地址）
    assert resolve_step_url("/a", "") == "/a"


def test_step_url_accepts_site_path_so_cases_do_not_hardcode_host(tmp_path):
    plan = two_step_plan()
    plan.url = "http://10.100.3.2/bw-main-ui/incomeAlone-ui/income/invoiceCheck"
    plan.steps[1].url = "/income/invoiceCheck"
    runner, driver, _ = make_plan_runner(
        tmp_path,
        [snapshot("a"), snapshot("b", text="登录完成"), snapshot("c", text="票据类型：电子发票")],
        [click(), done(), done()],
        plan=plan,
    )
    runner.run()
    assert driver.gotos == [
        "http://10.100.3.2/bw-main-ui/incomeAlone-ui/income/invoiceCheck",
        "http://10.100.3.2/income/invoiceCheck",
    ]


def test_defects_from_steps_are_prefixed_and_aggregated(tmp_path):
    class ForceDriver(FakeDriver):
        def click(self, index: int, allow_force: bool = False) -> LadderOutcome:
            return LadderOutcome(ok=True, level="L4",
                                 warning="L4 降级：依赖强制/合成事件，真实用户可能做不到（可用性缺陷候选）")

    settings = settings_for(tmp_path, max_actions=8)
    driver = ForceDriver([snapshot("a"), snapshot("b", text="登录完成"),
                          snapshot("c"), snapshot("d", text="票据类型：电子发票")])
    driver.s = settings
    runner = PlanRunner(settings, two_step_plan(), driver=driver,
                        engine=FakeEngine([click(), done(), click(), done()]), verbose=False)
    result = runner.run()

    assert result.status == "ok"
    assert result.defect_candidates == [f"第1步 决策#0 CLICK: {FORCE_WARNING}",
                                        f"第2步 决策#0 CLICK: {FORCE_WARNING}"]
    assert json.loads((result.run_dir / "summary.json").read_text(encoding="utf-8"))["defect_candidates"]


def test_vars_override_plan_vars(tmp_path):
    plan = two_step_plan()
    plan.vars = {"账号": "plan-user", "密码": "plan-pass"}
    settings = settings_for(tmp_path)
    runner = PlanRunner(settings, plan, driver=FakeDriver([snapshot("a")]), engine=FakeEngine([]),
                        verbose=False, vars={"密码": "cli-pass"})
    assert runner.vars == {"账号": "plan-user", "密码": "cli-pass"}


def test_summary_records_vars_as_keys_only(tmp_path):
    plan = two_step_plan()
    plan.vars = {"密码": "super-secret"}
    runner, _, _ = make_plan_runner(
        tmp_path,
        [snapshot("a"), snapshot("b", text="登录完成"), snapshot("c", text="票据类型：电子发票")],
        [click(), done(), done()],
        plan=plan,
    )
    result = runner.run()
    text = (result.run_dir / "summary.json").read_text(encoding="utf-8")
    assert json.loads(text)["vars"] == ["密码"] and "super-secret" not in text


def test_step_number_stays_out_of_the_model_payload():
    state = build_state(snapshot("a"), [RecentAction(action="CLICK", step=3)], {})
    payload = to_jev_state(state)["recent_actions"][0]
    assert payload["action"] == "CLICK" and "step" not in payload


def test_run_dir_root_aggregates_trace_and_actions_with_plan_step(tmp_path):
    """平台按运行目录根部读 trace.jsonl 与 actions.json：两处都要在，且标明属于哪一步。"""
    runner, _, _ = make_plan_runner(
        tmp_path,
        [snapshot("a"), snapshot("b", text="登录完成"), snapshot("c", text="票据类型"),
         snapshot("c", text="票据类型")],
        [click(), done(), click(), done()],
    )
    result = runner.run()

    assert result.status == "ok"
    rows = [json.loads(line) for line in
            (result.run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    kinds = [row.get("kind", "decision") for row in rows]
    assert kinds[0] == "run_start" and kinds[-1] == "run_end"
    assert {"step_start", "step_end", "decision", "assert"} <= set(kinds)
    assert [row["seq"] for row in rows] == list(range(1, len(rows) + 1))
    decisions = [row for row in rows if row.get("kind", "decision") == "decision"]
    assert all({"step", "url", "title"} <= set(row) for row in decisions)
    assert {row["plan_step"] for row in decisions} == {1, 2}
    actions = json.loads((result.run_dir / "actions.json").read_text(encoding="utf-8"))
    assert [item["action"] for item in actions] == ["CLICK", "CLICK"]
    assert {item["plan_step"] for item in actions} == {1, 2}
    for step in result.steps:
        assert (result.run_dir / "steps" / step.dir / "trace.jsonl").exists()


class ScriptedPlanAgent:
    """按 kind 脚本答复的假宿主；decide 的回包按各步 questions 现造合法 answers。"""

    def __init__(self, ops: list[tuple[str, str | None]], supervise: list[dict] | None = None):
        self.ops = list(ops)
        self.supervise = list(supervise or [])
        self.bridge = FakeBridge()
        self.bridge.ask = self._ask  # type: ignore[method-assign]

    def _ask(self, kind: str, payload: dict) -> tuple[dict, dict]:
        self.bridge.asks.append({"kind": kind, "payload": payload})
        meta = {"via": "agent", "ask_id": len(self.bridge.asks), "latency_ms": 3}
        if kind == "decide":
            operation, target = self.ops.pop(0)
            return {"answers": sealed_answers(payload["questions"], operation, target)}, meta
        if kind == "supervise":
            return self.supervise.pop(0), meta
        return {"text": "x"}, meta


def test_agent_mode_shares_one_bridge_across_steps(tmp_path):
    """全计划共用一个桥：ask id 连续，不每步新建。"""
    agent = ScriptedPlanAgent([("CLICK", "1"), ("DONE", None), ("DONE", None)])
    settings = settings_for(tmp_path, ui_agent_decider="agent")
    settings = settings.model_copy(update={"ui_agent_supervisor": True,
                                           "ui_agent_supervisor_mode": "agent"})
    driver = FakeDriver([snapshot("a"), snapshot("b", text="登录完成"),
                         snapshot("c", text="票据类型")])
    runner = PlanRunner(settings, two_step_plan(), driver=driver, verbose=False,
                        bridge=agent.bridge)
    result = runner.run()

    assert result.status == "ok"
    _ = driver
    asks = agent.bridge.asks
    assert [ask["kind"] for ask in asks] == ["decide", "decide", "decide"]
    meta_ids = [ask["payload"].get("previous_error") for ask in asks]
    assert meta_ids == [None, None, None], "两步之间不重复答歪，不该带 previous_error"


def test_agent_supervisor_budget_is_shared_across_steps(tmp_path):
    """跨步共享一份监督预算；每步开始 new_step() 只重置「每步」计数。"""
    agent = ScriptedPlanAgent([("BLOCKED", None), ("DONE", None), ("DONE", None)],
                              supervise=[{"ruling": "HINT", "hint": "先看页面"}])
    settings = settings_for(tmp_path, ui_agent_decider="agent")
    settings = settings.model_copy(update={"ui_agent_supervisor": True,
                                           "ui_agent_supervisor_mode": "agent",
                                           "supervisor_per_step": 2, "supervisor_per_run": 6})
    driver = FakeDriver([snapshot("a"), snapshot("b", text="登录完成"),
                         snapshot("c", text="票据类型")])
    runner = PlanRunner(settings, two_step_plan(), driver=driver, verbose=False,
                        bridge=agent.bridge)
    result = runner.run()

    assert result.status == "ok"
    budget = runner.supervisor.budget
    assert budget.per_step == 2 and budget.per_run == 6, "整轮预算不因换步重建"
    assert budget.run_used == 1, "一步用掉一次，跨步累计"


def test_plan_run_start_records_bridge_config(tmp_path):
    agent = ScriptedPlanAgent([("CLICK", "1"), ("DONE", None), ("DONE", None)])
    settings = settings_for(tmp_path, ui_agent_decider="agent")
    settings = settings.model_copy(update={"ui_agent_supervisor": True,
                                           "ui_agent_supervisor_mode": "agent"})
    driver = FakeDriver([snapshot("a"), snapshot("b", text="登录完成"),
                         snapshot("c", text="票据类型")])
    runner = PlanRunner(settings, two_step_plan(), driver=driver, verbose=False,
                        bridge=agent.bridge)
    result = runner.run()

    rows = [json.loads(line) for line in
            (result.run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]
    start = rows[0]
    assert start["decider"] == "agent" and start["supervisor_mode"] == "agent"
    assert start["bridge_dir"] == str(agent.bridge.dir)
    summary = json.loads((result.run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["supervisor_mode"] == "agent"


def test_no_bridge_in_agent_mode_is_an_assembly_error(tmp_path):
    settings = settings_for(tmp_path, ui_agent_decider="agent")
    driver = FakeDriver([snapshot("a")])
    try:
        PlanRunner(settings, two_step_plan(), driver=driver, verbose=False)
    except RuntimeError as exc:
        assert "需要 Agent 桥" in str(exc)
    else:
        raise AssertionError("agent 模式缺桥应当直接抛装配错误")

