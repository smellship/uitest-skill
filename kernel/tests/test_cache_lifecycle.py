"""断言生命周期：仅收尾断言没过时**保留录像**，动作失败仍淘汰；--relearn 主动重录。

这一组盯的是「用户改了断言就把好录像删掉」这个坑：用例 id 固定 ⇒ 缓存键不变 ⇒
回放仍会命中那条录像，所以淘汰它等于白白丢掉一份本来正确的录像。
"""

from __future__ import annotations

from fakes import FakeDriver, FakeEngine, done, read_trace, settings_for, snapshot

from ui_agent.replay.cache import CacheStore, case_identity
from ui_agent.replay.session import open_session
from ui_agent.run.runner import Runner
from ui_agent.schema.check import CheckSpec
from ui_agent.schema.decision import Decision
from ui_agent.schema.plan import PlanStep, TestPlan
from ui_agent.verify.asserts import parse_check

CHECKS = ["text_contains=查询完成"]


def plan_for(goal: str = "查询", expected: str = "查询完成", case_id: str = "case-1") -> TestPlan:
    return TestPlan(case_id=case_id, name="t", url="http://x/",
                    steps=[PlanStep(goal=goal,
                                    checks=[CheckSpec(kind="text_contains", expected=expected)])])


def run_goal(tmp_path, snapshots, decisions, checks=CHECKS, engine=None, case_id="case-1",
             relearn=False, **overrides):
    settings = settings_for(tmp_path, **{"ui_agent_cache": True, **overrides})
    plan = plan_for(expected=checks[0].split("=", 1)[1], case_id=case_id)
    session = open_session(settings, case_hash=case_identity(plan), base_url="http://x/",
                           plan_name="t", relearn=relearn)
    assert session is not None, "本组用例必须开着缓存"
    driver = FakeDriver(snapshots)
    runner = Runner(settings, "查询", "http://x/", checks=[parse_check(c) for c in checks],
                    task="t", driver=driver, engine=engine or FakeEngine(decisions),
                    verbose=False, cache=session)
    result = runner.run()
    session.run_id = result.run_dir.name
    outcome = session.finalize()
    return result, session, outcome


def record_once(tmp_path):
    """先录一份：CLICK + DONE 且断言通过。"""
    result, session, outcome = run_goal(
        tmp_path, [snapshot("a"), snapshot("b", text="查询完成")],
        [Decision(operation="CLICK", target="1", target_element=1, confidence=0.9), done()])
    assert result.status == "ok" and outcome.word == "saved"
    return session


class TestCheckFailedKeepsTheRecording:
    def test_changed_assertion_does_not_evict(self, tmp_path):
        session = record_once(tmp_path)
        before = CacheStore(settings_for(tmp_path).cache_dir).load(session.entry.key)
        assert before is not None and len(before.records) == 2

        # 用户把断言改严了：id 不变 ⇒ 键不变 ⇒ 回放仍命中，但收尾断言不成立
        result, session2, outcome = run_goal(
            tmp_path, [snapshot("a"), snapshot("b")], [done()],
            checks=["text_contains=查询完成且已归档"], ui_agent_decider="off",
            ui_agent_supervisor=False, ui_agent_assert_timeout_ms=0)

        assert result.status == "check_failed"
        assert outcome.evicted_steps == 0, "仅断言没过不许淘汰录像"
        after = CacheStore(settings_for(tmp_path).cache_dir).load(session2.entry.key)
        assert len(after.records) == 2, "录像原样留着：改回/放宽断言后还能直接回放"

    def test_loosening_the_assertion_again_replays_cleanly(self, tmp_path):
        """改严失败一次后，把断言改回来仍应零模型回放成功。"""
        record_once(tmp_path)
        run_goal(tmp_path, [snapshot("a"), snapshot("b")], [done()],
                 checks=["text_contains=不成立"], ui_agent_decider="off",
                 ui_agent_supervisor=False, ui_agent_assert_timeout_ms=0)

        result, _, outcome = run_goal(
            tmp_path, [snapshot("a"), snapshot("b", text="查询完成")], [],
            ui_agent_decider="off", ui_agent_supervisor=False)
        assert result.status == "ok" and outcome.hits == 2

    def test_retry_miss_reason_is_still_recorded(self, tmp_path):
        record_once(tmp_path)
        _, _, outcome = run_goal(
            tmp_path, [snapshot("a"), snapshot("b")], [done()],
            checks=["text_contains=不成立"], ui_agent_decider="off",
            ui_agent_supervisor=False, ui_agent_assert_timeout_ms=0)
        assert outcome.miss_reasons.get("retry") == 1


def failing_click_driver(snapshots):
    """点不动的驱动：动作执行失败（不是断言失败）。"""
    from ui_agent.driver.ladder import LadderOutcome

    class FailingDriver(FakeDriver):
        def click(self, index: int, allow_force: bool = False):
            return LadderOutcome(ok=False, level="L0", detail="元素不可达")

    return FailingDriver(snapshots)


class TestActionFailureStillEvicts:
    def test_execution_failure_evicts_the_variant(self, tmp_path):
        """动作执行不出来 = 录像确实不成立，照旧淘汰（防投毒）。"""
        record_once(tmp_path)

        settings = settings_for(tmp_path, ui_agent_cache=True)
        session = open_session(settings, case_hash=case_identity(plan_for()),
                               base_url="http://x/", plan_name="t")
        runner = Runner(settings, "查询", "http://x/", checks=[parse_check(CHECKS[0])],
                        task="t", driver=failing_click_driver([snapshot("a"), snapshot("a")]),
                        engine=FakeEngine([]), verbose=False, cache=session)
        result = runner.run()
        outcome = session.finalize()

        assert result.status != "ok"
        assert outcome.evicted_steps == 1, "动作失败要淘汰那条录像"
        entry = CacheStore(settings.cache_dir).load(session.entry.key)
        assert entry.for_step(1) == []

    def test_zero_model_action_failure_stops_cleanly(self, tmp_path):
        record_once(tmp_path)

        settings = settings_for(tmp_path, ui_agent_cache=True, ui_agent_decider="off",
                                ui_agent_supervisor=False)
        session = open_session(settings, case_hash=case_identity(plan_for()),
                               base_url="http://x/", plan_name="t")
        runner = Runner(settings, "查询", "http://x/", checks=[parse_check(CHECKS[0])],
                        task="t", driver=failing_click_driver([snapshot("a")]),
                        engine=FakeEngine([]), verbose=False, cache=session)
        result = runner.run()
        session.finalize()
        assert result.status != "ok"


class TestRelearn:
    def test_relearn_ignores_existing_recordings(self, tmp_path):
        session = record_once(tmp_path)
        key = session.entry.key

        settings = settings_for(tmp_path, ui_agent_cache=True)
        fresh = open_session(settings, case_hash=case_identity(plan_for()), base_url="http://x/",
                             plan_name="t", relearn=True)
        assert fresh.entry.key == key and fresh.entry.records == [], "重录＝当作没录过"

    def test_relearn_rewrites_the_entry(self, tmp_path):
        record_once(tmp_path)
        settings = settings_for(tmp_path, ui_agent_cache=True)
        session = open_session(settings, case_hash=case_identity(plan_for()), base_url="http://x/",
                               plan_name="t", relearn=True)
        driver = FakeDriver([snapshot("a", labels=["查询(新)"]),
                             snapshot("b", text="查询完成", labels=["查询(新)"])])
        runner = Runner(settings, "查询", "http://x/", checks=[parse_check(CHECKS[0])],
                        task="t", driver=driver,
                        engine=FakeEngine([Decision(operation="CLICK", target="1",
                                                    target_element=1, confidence=0.9), done()]),
                        verbose=False, cache=session)
        result = runner.run()
        outcome = session.finalize()

        assert result.status == "ok" and outcome.word == "saved"
        entry = CacheStore(settings.cache_dir).load(session.entry.key)
        assert [rec.target_label for rec in entry.records] == ["查询(新)", ""]

    def test_relearn_off_by_default(self, tmp_path):
        record_once(tmp_path)
        settings = settings_for(tmp_path, ui_agent_cache=True)
        session = open_session(settings, case_hash=case_identity(plan_for()), base_url="http://x/",
                               plan_name="t")
        assert len(session.entry.records) == 2, "默认要照常读已有录像"


class TestTrace:
    def test_decision_rows_carry_via(self, tmp_path):
        """HTTP 模型的决策标 via=http；agent 才是 agent；回放命中是 cache；没有决策是空。"""
        http_done = done()
        http_done.model = "jev-latest"
        result, _, _ = run_goal(tmp_path, [snapshot("a"), snapshot("b", text="查询完成")],
                                [Decision(operation="CLICK", target="1", target_element=1,
                                          confidence=0.9, model="jev-latest"), http_done])
        assert {row["via"] for row in read_trace(result.run_dir)} == {"http"}

    def test_fake_engine_decisions_have_no_via(self, tmp_path):
        """没有 model 的决策（测试替身）不该被谎报成某个来源。"""
        result, _, _ = run_goal(tmp_path, [snapshot("a"), snapshot("b", text="查询完成")],
                                [Decision(operation="CLICK", target="1", target_element=1,
                                          confidence=0.9), done()])
        assert {row["via"] for row in read_trace(result.run_dir)} == {""}

    def test_replayed_decisions_carry_via_cache(self, tmp_path):
        record_once(tmp_path)
        result, _, _ = run_goal(tmp_path, [snapshot("a"), snapshot("b", text="查询完成")], [],
                                ui_agent_decider="off", ui_agent_supervisor=False)
        assert {row["via"] for row in read_trace(result.run_dir)} == {"cache"}
