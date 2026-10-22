"""离线单测：快照的跨帧去重（微前端会让两个帧看到同一批节点）。

真实案例：wujie 把子应用 DOM 挂在主文档 `WUJIE-APP` 的 shadowRoot 里，
子帧（`.../incomeAlone-ui/...`）与主帧是同一批节点的两个视图。
旧逻辑逐帧扫描、逐帧打索引：子帧那轮把主帧刚打上的标记全覆盖掉，
但 `_frames` 又指回子帧 —— 主帧的 30 个索引自此全部定位不到。
"""

from __future__ import annotations

from fakes import settings_for

from ui_agent.driver.playwright_ import INDEX_MARK, Driver


class FakeDom:
    """一棵 DOM。多个帧可以指向同一个对象，用来模拟微前端的"同一批节点"。"""

    def __init__(self, elements: list[dict]):
        self.elements = elements
        self.marks: set[str] = set()


class FakeFrame:
    """只实现 Driver.snapshot 用到的面：url / evaluate。"""

    def __init__(self, url: str, dom: FakeDom | None = None, elements: list[dict] | None = None):
        self.url = url
        self.dom = dom if dom is not None else FakeDom(elements or [])
        self.scans = 0
        self.clears = 0

    def evaluate(self, script, args=None):
        args = args or {}
        if "op" in args:  # marks.js
            marks = self.dom.marks
            seen = len(marks)
            if args["op"] == "clear":
                marks.clear()
                self.clears += 1
            return seen
        # snapshot.js
        self.scans += 1
        start = args.get("start", 0)
        budget = args.get("maxElements", 250)
        picked = self.dom.elements[:budget]
        out = []
        for offset, element in enumerate(picked, start=start + 1):
            item = dict(element, index=offset)
            self.dom.marks.add(str(offset))
            out.append(item)
        return {
            "frame_url": self.url,
            "page": {"url": self.url, "title": f"T:{self.url}", "text": f"text-{self.url}",
                     "viewport": {"w": 800, "h": 600}, "scroll": {"x": 0, "y": 0, "max": 0}},
            "elements": out,
            "fingerprint": f"fp-{self.url}-{len(out)}",
        }


class FakePage:
    def __init__(self, url: str, frames: list[FakeFrame], main: FakeFrame | None = None):
        self.url = url
        self.frames = frames
        self.main_frame = main if main is not None else frames[0]


def element(label: str, role: str = "textbox") -> dict:
    return {"role": role, "label": label, "rect": [0, 0, 10, 10], "in_viewport": True,
            "occluded_by": "", "disabled": False, "operations": ["CLICK"]}


def wired(tmp_path, frames: list[FakeFrame], main: FakeFrame | None = None) -> Driver:
    driver = Driver(settings_for(tmp_path))
    driver.page = FakePage(url=frames[0].url, frames=frames, main=main or frames[0])
    return driver


class TestSharedDomAcrossFrames:
    def test_frame_sharing_the_same_nodes_is_skipped(self, tmp_path):
        """主帧 30 个 + 共用节点的子帧 30 个 → 只编主帧那份，子帧跳过。"""
        shared = FakeDom([element(f"字段{i}") for i in range(30)])
        main = FakeFrame("http://x/bw-main-ui/page", dom=shared)
        sub = FakeFrame("http://x/page", dom=shared)
        driver = wired(tmp_path, [main, sub])

        raw = driver.snapshot()

        assert len(raw["elements"]) == 30
        assert [el["index"] for el in raw["elements"]] == list(range(1, 31))
        assert all(el["frame"] == "" for el in raw["elements"])
        assert main.scans == 1
        assert sub.scans == 0, "共用节点的子帧不该再扫一遍"

    def test_sub_frame_keeps_main_frame_indexes_alive(self, tmp_path):
        """去重的真正目的：每个索引都要指回真正能点到它的那一帧。"""
        shared = FakeDom([element("发票号码"), element("金额合计"), element("开票日期")])
        main = FakeFrame("http://x/bw-main-ui/page", dom=shared)
        sub = FakeFrame("http://x/page", dom=shared)
        driver = wired(tmp_path, [main, sub])

        raw = driver.snapshot()

        for el in raw["elements"]:
            assert driver.frame_of(el["index"]) is main
        assert driver.locator.__self__ is driver  # 定位器只经过 frame_of，这里确认对象没被换掉

    def test_frame_with_its_own_nodes_is_still_scanned(self, tmp_path):
        """真正独立的帧（内容不同的 iframe）照旧编进索引，不能被误跳过。"""
        main = FakeFrame("http://x/main", elements=[element("主帧输入"), element("主帧按钮", "button")])
        sub = FakeFrame("http://x/child", elements=[element("子帧输入")])
        driver = wired(tmp_path, [main, sub])

        raw = driver.snapshot()

        assert [el["label"] for el in raw["elements"]] == ["主帧输入", "主帧按钮", "子帧输入"]
        assert raw["elements"][0]["frame"] == ""
        assert raw["elements"][2]["frame"] == "http://x/child"
        assert driver.frame_of(3) is sub
        assert sub.scans == 1

    def test_marks_are_cleared_before_every_scan(self, tmp_path):
        """上一轮残留标记会让"这帧扫过没"误判，所以每轮开头必须清。"""
        shared = FakeDom([element("A")])
        main = FakeFrame("http://x/bw-main-ui/page", dom=shared)
        sub = FakeFrame("http://x/page", dom=shared)
        driver = wired(tmp_path, [main, sub])

        first = driver.snapshot()
        second = driver.snapshot()

        assert first["elements"] == second["elements"], "两轮快照应当一致（清标记后可重复）"
        assert main.clears == 2 and sub.clears == 2
        assert main.scans == 2
        assert sub.scans == 0

    def test_marks_js_is_scoped_to_one_frame(self, tmp_path):
        """clear 只清本次求值的那一帧：这正是必须由 Python 逐帧清的原因。"""
        left = FakeDom([element("左")])
        right = FakeDom([element("右")])
        main = FakeFrame("http://x/left", dom=left)
        sub = FakeFrame("http://x/right", dom=right)
        driver = wired(tmp_path, [main, sub])

        driver._clear_marks()

        assert left.marks == set() and right.marks == set()
        assert main.clears == 1 and sub.clears == 1

    def test_mark_constant_is_the_one_the_guard_looks_for(self, tmp_path):
        """标记名只该有一个来源：快照写、守卫读、定位器查的不是三份字面量。"""
        assert INDEX_MARK == "data-uiagent-idx"
        main = FakeFrame("http://x/main", elements=[element("A")])
        driver = wired(tmp_path, [main])

        raw = driver.snapshot()

        assert str(raw["elements"][0]["index"]) in main.dom.marks


class TestFrameBudget:
    def test_budget_is_shared_across_frames(self, tmp_path):
        """max_elements 是全局预算，主帧先扫吃满后，后续帧不该再挤进来。"""
        main = FakeFrame("http://x/main", elements=[element(f"主{i}") for i in range(5)])
        sub = FakeFrame("http://x/child", elements=[element(f"子{i}") for i in range(5)])
        driver = wired(tmp_path, [main, sub])
        driver.s = driver.s.model_copy(update={"max_elements": 3})

        raw = driver.snapshot()

        assert [el["label"] for el in raw["elements"]] == ["主0", "主1", "主2"]
        assert sub.scans == 0
