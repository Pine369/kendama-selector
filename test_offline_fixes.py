"""
离线单元测试 - 覆盖本轮"最小必要修复"涉及的纯逻辑函数。

只测试:
1. main.filter_by_brand_whitelist() 大小写无关匹配
2. main.filter_already_evaluated() / main._load_seen_prices() 当日去重
3. main.run_daily_summary() 推送失败时保留 daily_pool.json,成功时清理
4. ai_filter._append_to_daily_pool() 原子写入
5. ai_filter.assign_tag() 利润五档判定(与 rules.md 第七条一致)
6. scraper_health 抓取健康检查(连续 N 轮 0 条告警一次,直到恢复)
7. main.feedback_link() 反馈链接签名 / feedback_server.py 签名校验 + XSS 转义
8. ai_filter.calculate_cost() / calculate_profit() 成本与利润边界
9. ai_filter._enrich_with_profit() 跳过缺有效 URL 的候选,保留缺图片的候选
10. db.py SQLite Phase 1 影子写入(listings/price_history/evaluations/scan_runs)
11. evaluations.brand / evaluations.source_category 的写入与 category 字段透传
12. main.py 命令行入口(--once / --platform / --keyword / --max-items)
13. 负利润评估留档(ai_filter._enrich_with_profit/evaluate_with_ai 返回 (pushable, all_evaluated),
    main.run_scan 的 scan_runs.evaluated_count 包含被跳过的负利润商品)
14. main._clean_llm_candidates():LLM 前候选清洗(无效 URL 剔除、同轮同 URL 去重保留第一条)
15. Sprint: feedback_events 追加留档(feedback_server.py 双写 + --migrate-feedback 幂等导入)、
    价格变动识别(main._detect_price_drop / db.get_previous_price)与 【降价】 展示信号透传
16. 复盘与偏好信号 Sprint: evaluations 降价列的安全补列迁移、reporting.py 周报
    (--weekly-review) 与偏好信号(--refresh-signals)生成、四类维护命令互斥规则
17. 反馈统计口径与报告可信度收口补丁: 周报窗口内/外反馈数拆分展示、"买入"纳入
    正向映射、空 URL 历史测试数据不参与偏好结论、周报去掉 dict repr、维护命令
    终端日志不重复
18. 运行接口与文档收口: main.print_status()(--status 只读摘要,不创建数据库)、
    reporting.generate_weekly_review() 按 --days 生成不同文件名(不覆盖默认 7 天报告)、
    SKILL.md / README.md 的文档完整性

不联网、不调用真实 DeepSeek / SiliconFlow / 飞书,不启动浏览器,不读取 .env 内容。
feedback_server.py 相关测试使用临时 SQLite 文件,不污染真实 feedback.db;
db.py / reporting.py 相关测试全部使用临时目录下的 sqlite 文件,不创建/不污染真实 kendama.db;
命令行相关测试全程 mock 掉 run_scan/scrape_multiple_keywords/schedule/time,
不会真的抓取、循环或 sleep。
运行方式:
    python -m unittest test_offline_fixes.py -v
"""
import os
import io
import json
import shutil
import sqlite3
import tempfile
import unittest
import contextlib
from datetime import datetime, timedelta
from unittest import mock
from urllib.parse import urlparse, parse_qs

import main
import ai_filter
import scraper_health
import feedback_server
import reporting
import db


class TestBrandWhitelistCaseInsensitive(unittest.TestCase):
    def test_mixed_case_and_lowercase_brands_all_match(self):
        items = [
            {"title": "sulab FC漆 新品けん玉"},       # 白名单本身已是小写
            {"title": "Cereal けん玉 套剑 新品"},       # 首字母大写
            {"title": "CEREAL kendama new"},           # 全大写
            {"title": "cereal kendama used"},           # 全小写
            {"title": "WEKENS 日产款 单剣"},
            {"title": "Wekens けん玉"},
            {"title": "wekens kendama"},
            {"title": "无关商品 完全不搭边"},
        ]
        result = main.filter_by_brand_whitelist(items)
        matched = {item["title"] for item in result}

        for expected in [
            "sulab FC漆 新品けん玉",
            "Cereal けん玉 套剑 新品",
            "CEREAL kendama new",
            "cereal kendama used",
            "WEKENS 日产款 单剣",
            "Wekens けん玉",
            "wekens kendama",
        ]:
            self.assertIn(expected, matched)

        self.assertNotIn("无关商品 完全不搭边", matched)
        self.assertEqual(len(result), 7)

    def test_valid_brands_content_untouched(self):
        # 白名单本身的大小写不应被修复逻辑改写
        self.assertIn("Cereal", main.VALID_BRANDS)
        self.assertIn("Wekens", main.VALID_BRANDS)
        self.assertIn("sulab", main.VALID_BRANDS)


class TestDedupBeforeLLM(unittest.TestCase):
    def test_same_url_same_price_is_skipped(self):
        seen = {"https://a.example/item1": 18000}
        items = [{"title": "t1", "price": "¥18,000", "url": "https://a.example/item1"}]
        result, skipped = main.filter_already_evaluated(items, seen)
        self.assertEqual(result, [])
        self.assertEqual(skipped, 1)

    def test_same_url_different_price_is_kept(self):
        seen = {"https://a.example/item1": 18000}
        items = [{"title": "t1", "price": "¥16,000", "url": "https://a.example/item1"}]
        result, skipped = main.filter_already_evaluated(items, seen)
        self.assertEqual(len(result), 1)
        self.assertEqual(skipped, 0)

    def test_new_url_is_kept(self):
        seen = {"https://a.example/item1": 18000}
        items = [{"title": "t2", "price": "¥9,000", "url": "https://b.example/item2"}]
        result, skipped = main.filter_already_evaluated(items, seen)
        self.assertEqual(len(result), 1)
        self.assertEqual(skipped, 0)

    def test_load_seen_prices_reads_pool_and_ignores_bad_entries(self):
        tmpdir = tempfile.mkdtemp()
        try:
            pool_path = os.path.join(tmpdir, "daily_pool.json")
            with open(pool_path, "w", encoding="utf-8") as f:
                json.dump([
                    {"url": "https://a.example/item1", "price_jpy": 18000},
                    {"url": "https://a.example/item2", "price_jpy": "not-a-number"},
                    {"price_jpy": 5000},  # 缺 url,应被忽略
                ], f)
            seen = main._load_seen_prices(pool_path)
            self.assertEqual(seen, {"https://a.example/item1": 18000})
        finally:
            shutil.rmtree(tmpdir)

    def test_load_seen_prices_missing_file_returns_empty(self):
        seen = main._load_seen_prices(os.path.join(tempfile.gettempdir(), "does_not_exist.json"))
        self.assertEqual(seen, {})


class TestDailySummaryKeepsPoolOnFailure(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_cwd = os.getcwd()
        os.chdir(self.tmpdir)
        with open(main.DAILY_POOL_FILE, "w", encoding="utf-8") as f:
            json.dump([{"url": "https://a.example/x", "price_jpy": 1000}], f)

    def tearDown(self):
        os.chdir(self._orig_cwd)
        shutil.rmtree(self.tmpdir)

    def test_push_failure_keeps_file(self):
        with mock.patch.object(main, "generate_daily_summary",
                                return_value=[{"url": "x", "estimated_profit": 1}]), \
             mock.patch.object(main, "push_summary", return_value=False):
            main.run_daily_summary()
        self.assertTrue(os.path.exists(main.DAILY_POOL_FILE))

    def test_push_success_removes_file(self):
        with mock.patch.object(main, "generate_daily_summary",
                                return_value=[{"url": "x", "estimated_profit": 1}]), \
             mock.patch.object(main, "push_summary", return_value=True):
            main.run_daily_summary()
        self.assertFalse(os.path.exists(main.DAILY_POOL_FILE))


class TestAtomicDailyPoolWrite(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_cwd = os.getcwd()
        os.chdir(self.tmpdir)

    def tearDown(self):
        os.chdir(self._orig_cwd)
        shutil.rmtree(self.tmpdir)

    def test_append_produces_readable_json(self):
        ai_filter._append_to_daily_pool([{"url": "https://a.example/1", "price_jpy": 1000}])
        ai_filter._append_to_daily_pool([{"url": "https://a.example/2", "price_jpy": 2000}])

        with open(ai_filter.DAILY_POOL_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(len(data), 2)
        self.assertEqual(
            {d["url"] for d in data},
            {"https://a.example/1", "https://a.example/2"},
        )

    def test_no_leftover_temp_files(self):
        ai_filter._append_to_daily_pool([{"url": "https://a.example/1", "price_jpy": 1000}])
        leftovers = [f for f in os.listdir(".") if f.startswith(".daily_pool_")]
        self.assertEqual(leftovers, [])


class TestAssignTagFiveTiers(unittest.TestCase):
    def test_profit_boundaries(self):
        cases = [
            (-1, None),
            (0, "盲盒"),
            (9.99, "盲盒"),
            (10, "观望"),
            (79.99, "观望"),
            (80, "推荐"),
            (149.99, "推荐"),
            (150, "强推"),
        ]
        for profit, expected in cases:
            with self.subTest(profit=profit):
                self.assertEqual(ai_filter.assign_tag(profit, False), expected)
                # is_gold_mine 不应再影响判定结果
                self.assertEqual(ai_filter.assign_tag(profit, True), expected)


class TestRulesDocAndGitignore(unittest.TestCase):
    """纯文本层面的一致性检查,不涉及任何网络或真实调用。"""

    def setUp(self):
        base = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(base, "rules.md"), "r", encoding="utf-8") as f:
            self.rules_text = f.read()
        with open(os.path.join(base, ".gitignore"), "r", encoding="utf-8") as f:
            self.gitignore_lines = [line.strip() for line in f.readlines()]

    def test_rules_md_no_longer_claims_ai_outputs_final_tag(self):
        self.assertNotIn('只输出"推荐"以上等级', self.rules_text)
        self.assertIn("assign_tag()", self.rules_text.split("## 三、AI 评估指令")[1])

    def test_gitignore_covers_dot_venv_without_removing_existing_rules(self):
        self.assertIn(".venv/", self.gitignore_lines)
        for existing in [".env", "venv/", "__pycache__/", "*.log", "*.json",
                          "*.db", "rules.md", "cases.md"]:
            self.assertIn(existing, self.gitignore_lines)

    def test_gitignore_covers_scraper_health_file(self):
        self.assertIn("scraper_health.json", self.gitignore_lines)

    def test_gitignore_covers_kendama_db(self):
        self.assertIn("kendama.db", self.gitignore_lines)


class TestScraperHealthCounting(unittest.TestCase):
    """连续 0 计数 + 告警一次直到恢复,纯内存状态,不落盘。"""

    def test_full_zero_recover_zero_sequence(self):
        state = {}

        # 第 1 次 0 条: 计数 1, 不告警
        state, alert1 = scraper_health.update_platform_counts(state, {"Mercari": 0})
        self.assertEqual(state["Mercari"]["consecutive_zero"], 1)
        self.assertEqual(alert1, [])

        # 第 2 次 0 条: 计数 2, 不告警
        state, alert2 = scraper_health.update_platform_counts(state, {"Mercari": 0})
        self.assertEqual(state["Mercari"]["consecutive_zero"], 2)
        self.assertEqual(alert2, [])

        # 第 3 次 0 条: 计数 3, 触发一次告警
        state, alert3 = scraper_health.update_platform_counts(state, {"Mercari": 0})
        self.assertEqual(state["Mercari"]["consecutive_zero"], 3)
        self.assertEqual(alert3, ["Mercari"])
        self.assertTrue(state["Mercari"]["alerted"])

        # 第 4 次仍 0: 计数 4, 不重复告警
        state, alert4 = scraper_health.update_platform_counts(state, {"Mercari": 0})
        self.assertEqual(state["Mercari"]["consecutive_zero"], 4)
        self.assertEqual(alert4, [])

        # 恢复 >0: 计数清零, 告警状态清除
        state, alert5 = scraper_health.update_platform_counts(state, {"Mercari": 5})
        self.assertEqual(state["Mercari"]["consecutive_zero"], 0)
        self.assertFalse(state["Mercari"]["alerted"])
        self.assertEqual(alert5, [])

        # 恢复后再次连续 3 次 0: 允许再次告警
        state, _ = scraper_health.update_platform_counts(state, {"Mercari": 0})
        state, _ = scraper_health.update_platform_counts(state, {"Mercari": 0})
        state, alert6 = scraper_health.update_platform_counts(state, {"Mercari": 0})
        self.assertEqual(alert6, ["Mercari"])

    def test_platforms_are_independent(self):
        state = {}
        state, alert = scraper_health.update_platform_counts(
            state, {"Mercari": 0, "Yahoo": 10, "Rakuten": 0}
        )
        self.assertEqual(alert, [])
        self.assertEqual(state["Yahoo"]["consecutive_zero"], 0)
        self.assertEqual(state["Mercari"]["consecutive_zero"], 1)
        self.assertEqual(state["Rakuten"]["consecutive_zero"], 1)


class TestScraperHealthPersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "scraper_health.json")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_missing_file_starts_from_empty_state(self):
        state = scraper_health.load_health_state(self.path)
        self.assertEqual(state, {})

    def test_corrupted_file_does_not_crash_and_reinitializes(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        state = scraper_health.load_health_state(self.path)
        self.assertEqual(state, {})

    def test_non_dict_json_is_treated_as_corrupted(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump([1, 2, 3], f)
        state = scraper_health.load_health_state(self.path)
        self.assertEqual(state, {})

    def test_write_then_read_roundtrip(self):
        state = {"Mercari": {"consecutive_zero": 3, "alerted": True}}
        scraper_health.save_health_state(self.path, state)
        reloaded = scraper_health.load_health_state(self.path)
        self.assertEqual(reloaded, state)

    def test_write_leaves_no_temp_files(self):
        scraper_health.save_health_state(self.path, {"Mercari": {"consecutive_zero": 1, "alerted": False}})
        leftovers = [f for f in os.listdir(self.tmpdir) if f.startswith(".scraper_health_")]
        self.assertEqual(leftovers, [])


class TestCheckScraperHealthIntegration(unittest.TestCase):
    """main.check_scraper_health() 的最小集成测试: 用 category 字段统计平台数量,
    只在达到阈值时调用一次 post_to_feishu,不做真实推送。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_cwd = os.getcwd()
        os.chdir(self.tmpdir)

    def tearDown(self):
        os.chdir(self._orig_cwd)
        shutil.rmtree(self.tmpdir)

    def _items_for(self, platform, n):
        return [{"category": f"[{platform}] けん玉", "title": f"item{i}"} for i in range(n)]

    def test_alert_fires_only_on_third_consecutive_zero_round(self):
        with mock.patch.object(main, "post_to_feishu", return_value=True) as mocked_push:
            main.check_scraper_health([])  # 全部平台本轮都是 0 条(第 1 轮)
            main.check_scraper_health([])  # 第 2 轮
            self.assertEqual(mocked_push.call_count, 0)
            main.check_scraper_health([])  # 第 3 轮,三个平台同时达到阈值
            self.assertEqual(mocked_push.call_count, len(main.PLATFORMS))

    def test_non_zero_platform_is_not_counted_as_zero(self):
        with mock.patch.object(main, "post_to_feishu", return_value=True) as mocked_push:
            main.check_scraper_health(self._items_for("Mercari", 5))
            main.check_scraper_health(self._items_for("Mercari", 5))
            main.check_scraper_health(self._items_for("Mercari", 5))
        # Mercari 一直有结果,不应触发告警;其余平台三轮都是 0,应告警
        state = scraper_health.load_health_state(main.SCRAPER_HEALTH_FILE)
        self.assertEqual(state["Mercari"]["consecutive_zero"], 0)
        self.assertGreaterEqual(mocked_push.call_count, 2)


class TestFeedbackLinkSigning(unittest.TestCase):
    """main.feedback_link() 在密钥未配置时不能生成可写入数据库的链接。"""

    def test_no_signing_secret_returns_none(self):
        with mock.patch.object(main, "FEEDBACK_URL", "https://example.com/feedback"), \
             mock.patch.object(main, "FEEDBACK_SIGNING_SECRET", None):
            link = main.feedback_link({"url": "https://a.example/x"}, "选的好", "符合预期")
        self.assertIsNone(link)

    def test_placeholder_signing_secret_returns_none(self):
        # 必须和 env.example 里的占位符文本一致,确认默认值不会被当成"已配置"
        with mock.patch.object(main, "FEEDBACK_URL", "https://example.com/feedback"), \
             mock.patch.object(main, "FEEDBACK_SIGNING_SECRET", "your_random_secret_here"):
            link = main.feedback_link({"url": "https://a.example/x"}, "选的好", "符合预期")
        self.assertIsNone(link)

    def test_configured_secret_produces_link_verifiable_by_feedback_server(self):
        with mock.patch.object(main, "FEEDBACK_URL", "https://example.com/feedback"), \
             mock.patch.object(main, "FEEDBACK_SIGNING_SECRET", "test-secret"):
            link = main.feedback_link({"url": "https://a.example/x"}, "选的好", "符合预期")

        self.assertIsNotNone(link)
        qs = parse_qs(urlparse(link).query)
        item_id_value = qs["id"][0]
        sig = qs["sig"][0]

        with mock.patch.object(feedback_server, "SIGNING_SECRET", "test-secret"):
            self.assertTrue(
                feedback_server.signature_valid(item_id_value, "选的好", "符合预期", sig)
            )


class TestFeedbackServerSecurity(unittest.TestCase):
    """feedback_server.py 的签名校验 + XSS 转义,使用临时 SQLite 文件,不碰真实 feedback.db。

    注意:feedback_server.py 现在会在合法反馈写入旧库后,追加写 kendama.db.feedback_events,
    所以这里必须同时把 db.DB_FILE 也指向临时文件,否则会污染真实 kendama.db。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "feedback_test.db")
        self.kendama_db_path = os.path.join(self.tmpdir, "kendama_test.db")
        self._secret_patch = mock.patch.object(feedback_server, "SIGNING_SECRET", "test-secret")
        self._db_patch = mock.patch.object(feedback_server, "DB_FILE", self.db_path)
        self._kendama_db_patch = mock.patch.object(db, "DB_FILE", self.kendama_db_path)
        self._secret_patch.start()
        self._db_patch.start()
        self._kendama_db_patch.start()
        feedback_server.init_db()
        db.init_db()
        self.client = feedback_server.app.test_client()

    def tearDown(self):
        self._secret_patch.stop()
        self._db_patch.stop()
        self._kendama_db_patch.stop()
        shutil.rmtree(self.tmpdir)

    def _rows(self):
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute("SELECT id, action, reason FROM feedback").fetchall()
        conn.close()
        return rows

    def test_valid_signature_writes_to_db(self):
        sig = feedback_server.expected_signature("item1", "选的好", "符合预期")
        resp = self.client.get("/feedback", query_string={
            "id": "item1", "action": "选的好", "reason": "符合预期",
            "url": "https://a.example/x", "sig": sig,
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._rows(), [("item1", "选的好", "符合预期")])

    def test_tampered_action_is_rejected(self):
        sig = feedback_server.expected_signature("item1", "选的好", "符合预期")
        resp = self.client.get("/feedback", query_string={
            "id": "item1", "action": "放弃", "reason": "符合预期",
            "url": "https://a.example/x", "sig": sig,
        })
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self._rows(), [])

    def test_tampered_item_id_is_rejected(self):
        sig = feedback_server.expected_signature("item1", "选的好", "符合预期")
        resp = self.client.get("/feedback", query_string={
            "id": "item2", "action": "选的好", "reason": "符合预期",
            "url": "https://a.example/x", "sig": sig,
        })
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self._rows(), [])

    def test_missing_signature_is_rejected(self):
        resp = self.client.get("/feedback", query_string={
            "id": "item1", "action": "选的好", "reason": "符合预期",
            "url": "https://a.example/x",
        })
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self._rows(), [])

    def test_unconfigured_secret_rejects_even_with_a_signature(self):
        with mock.patch.object(feedback_server, "SIGNING_SECRET", None):
            resp = self.client.get("/feedback", query_string={
                "id": "item1", "action": "选的好", "reason": "符合预期",
                "url": "https://a.example/x", "sig": "anything",
            })
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self._rows(), [])

    def test_xss_payload_is_escaped_in_response_body(self):
        malicious = "<script>alert(1)</script>"
        sig = feedback_server.expected_signature("item1", malicious, "符合预期")
        resp = self.client.get("/feedback", query_string={
            "id": "item1", "action": malicious, "reason": "符合预期",
            "url": "https://a.example/x", "sig": sig,
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertIn("&lt;script&gt;", body)


class TestCalculateCostAndProfit(unittest.TestCase):
    """calculate_cost() / calculate_profit() 的真实分段公式,不改动业务公式本身。

    汇率通过 mock 固定为 0.05(JPY_TO_CNY 本身就是环境变量参数化的值,
    这里只是为了让边界数字整除、方便断言,不涉及修改成本/利润公式)。
    """

    def setUp(self):
        self._rate_patch = mock.patch.object(ai_filter, "JPY_TO_CNY", 0.05)
        self._rate_patch.start()

    def tearDown(self):
        self._rate_patch.stop()

    def test_below_tax_threshold_not_taxed(self):
        # base = 4000*0.05 = 200, tax_base = 40 < 50 → 不征税
        base, cost, taxed = ai_filter.calculate_cost(4000)
        self.assertEqual(base, 200)
        self.assertFalse(taxed)
        self.assertEqual(cost, 250)  # 200 + 50

    def test_just_below_threshold_not_taxed(self):
        # base = 4999*0.05 = 249.95, tax_base = 49.99 < 50 → 不征税
        base, cost, taxed = ai_filter.calculate_cost(4999)
        self.assertEqual(base, 249.95)
        self.assertFalse(taxed)
        self.assertEqual(cost, 299.95)

    def test_exact_threshold_boundary_is_taxed(self):
        # base = 5000*0.05 = 250, tax_base = 50,代码用 `< 50` 判断,
        # 恰好等于 50 时落入 else 分支,即征税(边界值本身就是关键断言)
        base, cost, taxed = ai_filter.calculate_cost(5000)
        self.assertEqual(base, 250)
        self.assertTrue(taxed)
        self.assertEqual(cost, 332.5)  # 250 + 50 + 250*0.13

    def test_above_threshold_is_taxed(self):
        # base = 8000*0.05 = 400, tax_base = 80 >= 50 → 征税
        base, cost, taxed = ai_filter.calculate_cost(8000)
        self.assertEqual(base, 400)
        self.assertTrue(taxed)
        self.assertEqual(cost, 502)  # 400 + 50 + 400*0.13

    def test_positive_profit_when_ref_price_above_cost(self):
        # price_jpy=4000 → cost=250; ref_price=400 → profit=150
        profit, base, cost, taxed = ai_filter.calculate_profit(4000, 400)
        self.assertEqual(cost, 250)
        self.assertEqual(profit, 150)

    def test_zero_profit_when_ref_price_equals_cost(self):
        profit, base, cost, taxed = ai_filter.calculate_profit(4000, 250)
        self.assertEqual(cost, 250)
        self.assertEqual(profit, 0)

    def test_negative_profit_when_ref_price_below_cost(self):
        profit, base, cost, taxed = ai_filter.calculate_profit(4000, 100)
        self.assertEqual(cost, 250)
        self.assertEqual(profit, -150)


class TestEnrichWithProfitSkipsInvalidUrl(unittest.TestCase):
    """_enrich_with_profit() 应跳过缺有效 URL 的候选,保留缺图片但有 URL 的候选。"""

    def setUp(self):
        self._rate_patch = mock.patch.object(ai_filter, "JPY_TO_CNY", 0.05)
        self._rate_patch.start()

    def tearDown(self):
        self._rate_patch.stop()

    def _valid_item(self, **overrides):
        item = {
            "title": "sulab FC漆",
            "brand": "sulab",
            "price_jpy": 4000,
            "domestic_ref_price": 400,  # profit=150 → 推荐/强推区间,必定通过标签过滤
            "is_gold_mine": False,
            "url": "https://jp.mercari.com/item/m123",
            "img_url": "https://static.mercdn.net/img.jpg",
            "reason": "测试用例",
        }
        item.update(overrides)
        return item

    def test_missing_url_is_skipped(self):
        item = self._valid_item(url="")
        del item["url"]
        pushable, all_evaluated = ai_filter._enrich_with_profit([item])
        self.assertEqual(pushable, [])
        self.assertEqual(all_evaluated, [])

    def test_empty_or_non_http_url_is_skipped(self):
        for bad_url in ["", "   ", "not-a-url", "ftp://example.com/x"]:
            with self.subTest(bad_url=bad_url):
                item = self._valid_item(url=bad_url)
                pushable, all_evaluated = ai_filter._enrich_with_profit([item])
                self.assertEqual(pushable, [])
                self.assertEqual(all_evaluated, [])

    def test_valid_url_missing_image_is_kept(self):
        item = self._valid_item(img_url="")
        pushable, all_evaluated = ai_filter._enrich_with_profit([item])
        self.assertEqual(len(pushable), 1)
        self.assertEqual(pushable[0]["img_url"], "")
        self.assertEqual(pushable[0]["tag"], "强推")
        self.assertEqual(all_evaluated, pushable)

    def test_normal_candidate_is_unaffected(self):
        item = self._valid_item()
        pushable, all_evaluated = ai_filter._enrich_with_profit([item])
        self.assertEqual(len(pushable), 1)
        self.assertEqual(pushable[0]["url"], "https://jp.mercari.com/item/m123")
        self.assertEqual(pushable[0]["img_url"], "https://static.mercdn.net/img.jpg")
        self.assertEqual(pushable[0]["estimated_profit"], 150)
        self.assertEqual(pushable[0]["tag"], "强推")
        self.assertEqual(all_evaluated, pushable)


class TestDbListingsAndPriceHistory(unittest.TestCase):
    """db.py 的 listings / price_history / evaluations / scan_runs 基础行为,
    全部用临时目录下的 sqlite 文件,不碰真实 kendama.db。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "kendama_test.db")
        db.init_db(db_file=self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_same_url_does_not_create_duplicate_listing(self):
        id1 = db.get_or_create_listing(
            "Mercari", "https://jp.mercari.com/item/m1", "abc123",
            "title1", "img1", "2026-01-01T00:00:00", db_file=self.db_path,
        )
        id2 = db.get_or_create_listing(
            "Mercari", "https://jp.mercari.com/item/m1", "abc123",
            "title1-updated", "img2", "2026-01-02T00:00:00", db_file=self.db_path,
        )
        self.assertEqual(id1, id2)

        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        title, last_seen = conn.execute(
            "SELECT title, last_seen_at FROM listings WHERE id = ?", (id1,)
        ).fetchone()
        conn.close()
        self.assertEqual(count, 1)
        self.assertEqual(title, "title1-updated")
        self.assertEqual(last_seen, "2026-01-02T00:00:00")

    def test_legacy_url_hash_is_saved(self):
        listing_id = db.get_or_create_listing(
            "Mercari", "https://jp.mercari.com/item/m2", "deadbeef0001",
            "title", "img", "2026-01-01T00:00:00", db_file=self.db_path,
        )
        conn = sqlite3.connect(self.db_path)
        saved_hash = conn.execute(
            "SELECT legacy_url_hash FROM listings WHERE id = ?", (listing_id,)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(saved_hash, "deadbeef0001")

    def test_different_scan_runs_can_each_record_a_price(self):
        listing_id = db.get_or_create_listing(
            "Mercari", "https://jp.mercari.com/item/m3", "hash3",
            "t", "i", "2026-01-01T00:00:00", db_file=self.db_path,
        )
        run1 = db.create_scan_run("2026-01-01T00:00:00", db_file=self.db_path)
        run2 = db.create_scan_run("2026-01-01T01:00:00", db_file=self.db_path)
        db.record_price(listing_id, run1, 4000, "¥4,000", "2026-01-01T00:00:00", db_file=self.db_path)
        db.record_price(listing_id, run2, 3800, "¥3,800", "2026-01-01T01:00:00", db_file=self.db_path)

        conn = sqlite3.connect(self.db_path)
        prices = sorted(
            r[0] for r in conn.execute(
                "SELECT price_jpy FROM price_history WHERE listing_id = ?", (listing_id,)
            ).fetchall()
        )
        conn.close()
        self.assertEqual(prices, [3800, 4000])

    def test_same_scan_run_does_not_duplicate_price(self):
        listing_id = db.get_or_create_listing(
            "Mercari", "https://jp.mercari.com/item/m4", "hash4",
            "t", "i", "2026-01-01T00:00:00", db_file=self.db_path,
        )
        run_id = db.create_scan_run("2026-01-01T00:00:00", db_file=self.db_path)
        db.record_price(listing_id, run_id, 4000, "¥4,000", "2026-01-01T00:00:00", db_file=self.db_path)
        db.record_price(listing_id, run_id, 3999, "¥3,999", "2026-01-01T00:00:01", db_file=self.db_path)

        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT price_jpy FROM price_history WHERE listing_id = ? AND scan_run_id = ?",
            (listing_id, run_id),
        ).fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 4000)

    def test_evaluation_links_to_listing_and_scan_run(self):
        listing_id = db.get_or_create_listing(
            "Mercari", "https://jp.mercari.com/item/m5", "hash5",
            "t", "i", "2026-01-01T00:00:00", db_file=self.db_path,
        )
        run_id = db.create_scan_run("2026-01-01T00:00:00", db_file=self.db_path)
        ok = db.record_evaluation(
            listing_id, run_id, price_jpy=4000, domestic_ref_price=400, is_gold_mine=False,
            reason="test", estimated_profit=170, total_cost=230, taxed=False, tag="强推",
            selected_for_push=True, evaluated_at="2026-01-01T00:00:00", db_file=self.db_path,
        )
        self.assertTrue(ok)

        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            """
            SELECT l.url, e.tag
            FROM evaluations e
            JOIN listings l ON l.id = e.listing_id
            JOIN scan_runs r ON r.id = e.scan_run_id
            WHERE e.listing_id = ? AND e.scan_run_id = ?
            """,
            (listing_id, run_id),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "https://jp.mercari.com/item/m5")
        self.assertEqual(row[1], "强推")

    def test_selected_for_push_distinguishes_evaluations(self):
        listing_id = db.get_or_create_listing(
            "Mercari", "https://jp.mercari.com/item/m6", "hash6",
            "t", "i", "2026-01-01T00:00:00", db_file=self.db_path,
        )
        run_id = db.create_scan_run("2026-01-01T00:00:00", db_file=self.db_path)
        db.record_evaluation(listing_id, run_id, 4000, 400, False, "r", 170, 230, False,
                              "强推", True, "2026-01-01T00:00:00", db_file=self.db_path)
        db.record_evaluation(listing_id, run_id, 4000, 400, False, "r", 20, 230, False,
                              "盲盒", False, "2026-01-01T00:00:01", db_file=self.db_path)

        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT tag, selected_for_push FROM evaluations ORDER BY id"
        ).fetchall()
        conn.close()
        self.assertEqual(rows, [("强推", 1), ("盲盒", 0)])

    def test_db_error_returns_none_instead_of_raising(self):
        # platform 是 NOT NULL,传 None 会触发真实 sqlite3.IntegrityError,
        # db.py 必须捕获并返回 None,而不是让异常向上传播。
        result = db.get_or_create_listing(
            None, "https://jp.mercari.com/item/m7", "hash7",
            "t", "i", "2026-01-01T00:00:00", db_file=self.db_path,
        )
        self.assertIsNone(result)

    def test_new_connection_enables_foreign_keys_and_busy_timeout(self):
        conn = db._connect_db(self.db_path)
        try:
            fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(fk, 1)
        self.assertEqual(timeout, 5000)

    def test_wal_mode_enabled_after_init(self):
        conn = sqlite3.connect(self.db_path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        self.assertEqual(mode.lower(), "wal")


class TestRecordEvaluationsSelectedForPush(unittest.TestCase):
    """main._record_evaluations() 应该只给真正进入 top_items 的条目标 selected_for_push=1。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "kendama_test.db")
        db.init_db(db_file=self.db_path)
        self._db_patch = mock.patch.object(db, "DB_FILE", self.db_path)
        self._db_patch.start()

    def tearDown(self):
        self._db_patch.stop()
        shutil.rmtree(self.tmpdir)

    def test_only_top_items_marked_selected_for_push(self):
        run_id = db.create_scan_run("2026-01-01T00:00:00")
        item_a = {"url": "https://a.example/1", "price_jpy": 4000, "domestic_ref_price": 400,
                  "is_gold_mine": False, "reason": "r", "estimated_profit": 170,
                  "total_cost": 230, "taxed": False, "tag": "强推"}
        item_b = {"url": "https://a.example/2", "price_jpy": 3000, "domestic_ref_price": 200,
                  "is_gold_mine": False, "reason": "r", "estimated_profit": 20,
                  "total_cost": 180, "taxed": False, "tag": "盲盒"}
        url_to_listing_id = {
            item_a["url"]: db.get_or_create_listing(
                "Mercari", item_a["url"], "h1", "t1", "i1", "2026-01-01T00:00:00"),
            item_b["url"]: db.get_or_create_listing(
                "Mercari", item_b["url"], "h2", "t2", "i2", "2026-01-01T00:00:00"),
        }

        main._record_evaluations([item_a, item_b], [item_a], url_to_listing_id, run_id)

        conn = sqlite3.connect(self.db_path)
        rows = dict(conn.execute("SELECT tag, selected_for_push FROM evaluations").fetchall())
        conn.close()
        self.assertEqual(rows["强推"], 1)
        self.assertEqual(rows["盲盒"], 0)


class TestPhase1ShadowWriteIntegration(unittest.TestCase):
    """main.run_scan() 端到端:确认影子写入落库,且原有推送流程完全不受影响,
    数据库不可用时扫描主流程也不会崩溃。全程 mock 抓取/LLM/推送,不发真实请求。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_cwd = os.getcwd()
        os.chdir(self.tmpdir)
        self.db_path = os.path.join(self.tmpdir, "kendama_test.db")
        self._db_patch = mock.patch.object(db, "DB_FILE", self.db_path)
        self._db_patch.start()
        db.init_db()

    def tearDown(self):
        self._db_patch.stop()
        os.chdir(self._orig_cwd)
        shutil.rmtree(self.tmpdir)

    def _raw_item(self, platform, title, price, url):
        return {
            "title": title, "price": price, "url": url,
            "img_url": "https://img.example/x.jpg", "category": f"[{platform}] けん玉",
        }

    def _enriched_item(self, url):
        return {
            "title": "sulab FC漆 新品", "brand": "sulab", "price_jpy": 4000,
            "domestic_ref_price": 400, "is_gold_mine": False, "url": url,
            "img_url": "https://img.example/x.jpg", "reason": "test",
            "price_cny": 180.0, "total_cost": 230.0,
            "estimated_profit": 170, "taxed": False, "tag": "强推",
            "category": "[Mercari] けん玉",
        }

    def test_full_scan_flow_writes_shadow_tables_without_affecting_push(self):
        url = "https://jp.mercari.com/item/m1"
        raw_items = [self._raw_item("Mercari", "sulab FC漆 新品", "¥4,000", url)]
        enriched_item = self._enriched_item(url)

        with mock.patch.object(main, "scrape_multiple_keywords", return_value=raw_items), \
             mock.patch.object(main, "evaluate_with_ai", return_value=([enriched_item], [enriched_item])), \
             mock.patch.object(main, "push_items") as mocked_push, \
             mock.patch.object(main, "check_scraper_health"):
            main.run_scan()

        mocked_push.assert_called_once_with([enriched_item])

        conn = sqlite3.connect(self.db_path)
        listings = conn.execute("SELECT platform, url, legacy_url_hash FROM listings").fetchall()
        prices = conn.execute("SELECT price_jpy, raw_price_text FROM price_history").fetchall()
        evals = conn.execute(
            "SELECT tag, selected_for_push, brand, source_category FROM evaluations"
        ).fetchall()
        run = conn.execute(
            "SELECT status, raw_item_count, brand_matched_count, llm_input_count, "
            "evaluated_count, candidate_count FROM scan_runs"
        ).fetchall()
        conn.close()

        self.assertEqual(listings, [("Mercari", url, main.item_id(raw_items[0]))])
        self.assertEqual(prices, [(4000, "¥4,000")])
        self.assertEqual(evals, [("强推", 1, "sulab", "[Mercari] けん玉")])
        self.assertEqual(run, [("ok", 1, 1, 1, 1, 1)])

    def test_db_unavailable_does_not_break_scan_or_push(self):
        url = "https://jp.mercari.com/item/m2"
        raw_items = [self._raw_item("Mercari", "sulab FC漆 新品", "¥4,000", url)]
        enriched_item = self._enriched_item(url)

        with mock.patch.object(main, "scrape_multiple_keywords", return_value=raw_items), \
             mock.patch.object(main, "evaluate_with_ai", return_value=([enriched_item], [enriched_item])), \
             mock.patch.object(main, "push_items") as mocked_push, \
             mock.patch.object(main, "check_scraper_health"), \
             mock.patch.object(db, "create_scan_run", return_value=None):
            try:
                main.run_scan()
            except Exception as e:
                self.fail(f"run_scan() 不应该因为数据库不可用而抛出异常: {e}")

        mocked_push.assert_called_once_with([enriched_item])


class TestEvaluationBrandAndCategory(unittest.TestCase):
    """evaluations.brand / evaluations.source_category:正常写入、缺失不报错、可聚合查询。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "kendama_test.db")
        db.init_db(db_file=self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _listing(self, url, n):
        return db.get_or_create_listing(
            "Mercari", url, f"hash{n}", f"title{n}", "img",
            "2026-01-01T00:00:00", db_file=self.db_path,
        )

    def test_brand_and_category_are_written_for_normal_evaluation(self):
        run_id = db.create_scan_run("2026-01-01T00:00:00", db_file=self.db_path)
        listing_id = self._listing("https://a.example/1", 1)

        ok = db.record_evaluation(
            listing_id, run_id, 4000, 400, False, "r", 170, 230, False, "强推", True,
            "2026-01-01T00:00:00", brand="sulab", source_category="[Mercari] けん玉",
            db_file=self.db_path,
        )
        self.assertTrue(ok)

        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT brand, source_category FROM evaluations").fetchone()
        conn.close()
        self.assertEqual(row, ("sulab", "[Mercari] けん玉"))

    def test_missing_brand_does_not_fail_write(self):
        run_id = db.create_scan_run("2026-01-01T00:00:00", db_file=self.db_path)
        listing_id = self._listing("https://a.example/2", 2)

        ok = db.record_evaluation(
            listing_id, run_id, 4000, 400, False, "r", 170, 230, False, "强推", True,
            "2026-01-01T00:00:00", brand=None, source_category="[Mercari] けん玉",
            db_file=self.db_path,
        )
        self.assertTrue(ok)

        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT brand FROM evaluations").fetchone()
        conn.close()
        self.assertIsNone(row[0])

    def test_missing_category_does_not_break_main_flow(self):
        # main._record_evaluations() 面对完全没有 category 键的 enriched item 时不应报错
        with mock.patch.object(db, "DB_FILE", self.db_path):
            run_id = db.create_scan_run("2026-01-01T00:00:00")
            listing_id = db.get_or_create_listing(
                "Mercari", "https://a.example/3", "hash3", "t", "i", "2026-01-01T00:00:00",
            )
            item_without_category = {
                "url": "https://a.example/3", "brand": "sulab", "price_jpy": 4000,
                "domestic_ref_price": 400, "is_gold_mine": False, "reason": "r",
                "estimated_profit": 170, "total_cost": 230, "taxed": False, "tag": "强推",
            }
            main._record_evaluations(
                [item_without_category], [item_without_category],
                {"https://a.example/3": listing_id}, run_id,
            )

        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT brand, source_category FROM evaluations").fetchone()
        conn.close()
        self.assertEqual(row, ("sulab", None))

    def test_aggregate_by_brand_and_source_category(self):
        run_id = db.create_scan_run("2026-01-01T00:00:00", db_file=self.db_path)
        rows = [
            ("https://a.example/10", "sulab", "[Mercari] けん玉"),
            ("https://a.example/11", "sulab", "[Yahoo] Kendama"),
            ("https://a.example/12", "krom", "[Mercari] けん玉"),
        ]
        for i, (url, brand, category) in enumerate(rows, start=10):
            listing_id = self._listing(url, i)
            db.record_evaluation(
                listing_id, run_id, 4000, 400, False, "r", 170, 230, False, "推荐", False,
                "2026-01-01T00:00:00", brand=brand, source_category=category,
                db_file=self.db_path,
            )

        conn = sqlite3.connect(self.db_path)
        by_brand = dict(conn.execute(
            "SELECT brand, COUNT(*) FROM evaluations GROUP BY brand ORDER BY brand"
        ).fetchall())
        by_category = dict(conn.execute(
            "SELECT source_category, COUNT(*) FROM evaluations GROUP BY source_category ORDER BY source_category"
        ).fetchall())
        conn.close()

        self.assertEqual(by_brand, {"krom": 1, "sulab": 2})
        self.assertEqual(by_category, {"[Mercari] けん玉": 2, "[Yahoo] Kendama": 1})


class TestEvaluateWithAiCategoryTransplant(unittest.TestCase):
    """ai_filter.evaluate_with_ai():LLM 输出本身不带 category,需要按 url 从原始候选透传回来。"""

    def test_category_backfilled_from_input_when_missing_from_llm_output(self):
        input_items = [{
            "title": "sulab FC漆 新品", "price": "¥4,000",
            "url": "https://jp.mercari.com/item/m1",
            "img_url": "https://img.example/x.jpg",
            "category": "[Mercari] けん玉",
        }]
        llm_output = json.dumps([{
            "title": "sulab FC漆 新品", "brand": "sulab", "price_jpy": 4000,
            "domestic_ref_price": 400, "is_gold_mine": False,
            "url": "https://jp.mercari.com/item/m1",
            "img_url": "https://img.example/x.jpg", "reason": "test",
        }], ensure_ascii=False)

        with mock.patch.object(ai_filter, "call_ai", return_value=llm_output), \
             mock.patch.object(ai_filter, "_append_to_daily_pool"):
            top_items, enriched = ai_filter.evaluate_with_ai(input_items)

        self.assertEqual(len(enriched), 1)
        self.assertEqual(enriched[0]["category"], "[Mercari] けん玉")
        self.assertEqual(enriched[0]["brand"], "sulab")
        self.assertEqual(top_items, enriched)


def _parse_args_silently(argv):
    """调用 main.parse_args(),把 argparse 出错时打印的 usage/error 文本吞掉,保持测试输出干净。"""
    with contextlib.redirect_stderr(io.StringIO()):
        return main.parse_args(argv)


class TestCliArgParsing(unittest.TestCase):
    """main.parse_args() 本身的解析与校验规则,不涉及真正执行扫描。"""

    def test_no_args_defaults_to_continuous_mode(self):
        args = main.parse_args([])
        self.assertFalse(args.once)
        self.assertIsNone(args.platforms)
        self.assertIsNone(args.keywords)
        self.assertIsNone(args.max_items)

    def test_once_flag_alone_parses(self):
        args = main.parse_args(["--once"])
        self.assertTrue(args.once)
        self.assertIsNone(args.platforms)
        self.assertIsNone(args.keywords)
        self.assertIsNone(args.max_items)

    def test_platform_is_repeatable_and_ordered(self):
        args = main.parse_args(["--once", "--platform", "Mercari", "--platform", "Yahoo"])
        self.assertEqual(args.platforms, ["Mercari", "Yahoo"])

    def test_keyword_is_repeatable_and_ordered(self):
        args = main.parse_args(["--once", "--keyword", "Kendama", "--keyword", "けん玉"])
        self.assertEqual(args.keywords, ["Kendama", "けん玉"])

    def test_max_items_parses_as_positive_int(self):
        args = main.parse_args(["--once", "--max-items", "30"])
        self.assertEqual(args.max_items, 30)

    def test_unknown_platform_errors(self):
        with self.assertRaises(SystemExit):
            _parse_args_silently(["--once", "--platform", "NotAPlatform"])

    def test_max_items_zero_errors(self):
        with self.assertRaises(SystemExit):
            _parse_args_silently(["--once", "--max-items", "0"])

    def test_max_items_negative_errors(self):
        with self.assertRaises(SystemExit):
            _parse_args_silently(["--once", "--max-items", "-5"])

    def test_max_items_non_numeric_errors(self):
        with self.assertRaises(SystemExit):
            _parse_args_silently(["--once", "--max-items", "abc"])

    def test_scope_args_without_once_error_individually(self):
        for bad_argv in (
            ["--platform", "Mercari"],
            ["--keyword", "Kendama"],
            ["--max-items", "10"],
        ):
            with self.subTest(argv=bad_argv):
                with self.assertRaises(SystemExit):
                    _parse_args_silently(bad_argv)


class TestRunScanScopeOverrides(unittest.TestCase):
    """run_scan() 的 keywords/platforms/max_items_per_platform 覆盖是否正确传给 scraper。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_cwd = os.getcwd()
        os.chdir(self.tmpdir)
        self.db_path = os.path.join(self.tmpdir, "kendama_test.db")
        self._db_patch = mock.patch.object(db, "DB_FILE", self.db_path)
        self._db_patch.start()
        db.init_db()

    def tearDown(self):
        self._db_patch.stop()
        os.chdir(self._orig_cwd)
        shutil.rmtree(self.tmpdir)

    def test_overrides_are_passed_through_to_scraper(self):
        with mock.patch.object(main, "scrape_multiple_keywords", return_value=[]) as mocked_scrape, \
             mock.patch.object(main, "check_scraper_health"):
            main.run_scan(keywords=["Kendama"], platforms=["Mercari"], max_items_per_platform=30)

        mocked_scrape.assert_called_once_with(
            ["Kendama"], max_items_per_platform=30, platforms=["Mercari"]
        )

    def test_no_overrides_uses_config_yaml_defaults(self):
        with mock.patch.object(main, "scrape_multiple_keywords", return_value=[]) as mocked_scrape, \
             mock.patch.object(main, "check_scraper_health"):
            main.run_scan()

        mocked_scrape.assert_called_once_with(
            main.BROAD_KEYWORDS, max_items_per_platform=main.MAX_ITEMS, platforms=None
        )


class TestOnceModeEntryPoint(unittest.TestCase):
    """main.main() 在 --once 模式下:只跑一次、不注册 schedule、正确传参给 run_scan()。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_cwd = os.getcwd()
        os.chdir(self.tmpdir)

    def tearDown(self):
        os.chdir(self._orig_cwd)
        shutil.rmtree(self.tmpdir)

    def test_once_with_scope_args_calls_run_scan_once_without_scheduling(self):
        with mock.patch.object(main, "run_scan") as mocked_scan, \
             mock.patch.object(main, "schedule") as mocked_schedule, \
             mock.patch.object(main, "diagnose_feedback_config"), \
             mock.patch.object(db, "init_db", return_value=True):
            main.main(["--once", "--platform", "Mercari", "--keyword", "Kendama", "--max-items", "10"])

        mocked_scan.assert_called_once_with(
            keywords=["Kendama"], platforms=["Mercari"], max_items_per_platform=10
        )
        mocked_schedule.every.assert_not_called()

    def test_once_without_scope_args_calls_run_scan_with_none_defaults(self):
        with mock.patch.object(main, "run_scan") as mocked_scan, \
             mock.patch.object(main, "schedule") as mocked_schedule, \
             mock.patch.object(main, "diagnose_feedback_config"), \
             mock.patch.object(db, "init_db", return_value=True):
            main.main(["--once"])

        mocked_scan.assert_called_once_with(keywords=None, platforms=None, max_items_per_platform=None)
        mocked_schedule.every.assert_not_called()


class TestDefaultModeUnchanged(unittest.TestCase):
    """main.main() 不带参数时,必须仍然是:立即扫描一次 → 注册两个定时任务 → 进入循环。"""

    def test_default_mode_scans_once_then_registers_schedule_and_loops(self):
        with mock.patch.object(main, "run_scan") as mocked_scan, \
             mock.patch.object(main, "schedule") as mocked_schedule, \
             mock.patch.object(main, "time") as mocked_time, \
             mock.patch.object(main, "diagnose_feedback_config"), \
             mock.patch.object(db, "init_db", return_value=True):
            # 用 KeyboardInterrupt 让 while True 循环在第一次 sleep 时就干净退出,
            # 不用真的等待/真的进入死循环。
            mocked_time.sleep.side_effect = KeyboardInterrupt()
            main.main([])

        mocked_scan.assert_called_once_with()
        self.assertEqual(mocked_schedule.every.call_count, 2)


class TestEnrichWithProfitRetainsNegativeProfit(unittest.TestCase):
    """_enrich_with_profit() 返回 (pushable, all_evaluated):
    正利润/零利润走原有规则,负利润仍完成计算但只进 all_evaluated,tag='跳过'。"""

    def setUp(self):
        self._rate_patch = mock.patch.object(ai_filter, "JPY_TO_CNY", 0.05)
        self._rate_patch.start()

    def tearDown(self):
        self._rate_patch.stop()

    def _item(self, ref_price, **overrides):
        # price_jpy=4000, rate=0.05 → base=200, tax_base=40<50 → 不征税, cost=250
        item = {
            "title": "krom 基础款", "brand": "krom", "price_jpy": 4000,
            "domestic_ref_price": ref_price, "is_gold_mine": False,
            "url": "https://jp.mercari.com/item/neg1",
            "img_url": "https://img.example/x.jpg", "reason": "测试用例",
        }
        item.update(overrides)
        return item

    def test_positive_profit_item_appears_in_both_lists_with_normal_tag(self):
        item = self._item(ref_price=400)  # profit=150 → 强推
        pushable, all_evaluated = ai_filter._enrich_with_profit([item])
        self.assertEqual(len(pushable), 1)
        self.assertEqual(pushable[0]["tag"], "强推")
        self.assertEqual(all_evaluated, pushable)

    def test_zero_profit_item_kept_under_existing_rules(self):
        item = self._item(ref_price=250)  # profit=0 → 盲盒,维持现有规则,不是"跳过"
        pushable, all_evaluated = ai_filter._enrich_with_profit([item])
        self.assertEqual(len(pushable), 1)
        self.assertEqual(pushable[0]["tag"], "盲盒")
        self.assertEqual(pushable[0]["estimated_profit"], 0)
        self.assertEqual(all_evaluated, pushable)

    def test_negative_profit_item_recorded_as_skipped_but_not_pushable(self):
        item = self._item(ref_price=100)  # profit=-150 → 跳过
        pushable, all_evaluated = ai_filter._enrich_with_profit([item])
        self.assertEqual(pushable, [])
        self.assertEqual(len(all_evaluated), 1)
        skipped = all_evaluated[0]
        self.assertEqual(skipped["tag"], "跳过")
        self.assertEqual(skipped["estimated_profit"], -150)
        self.assertEqual(skipped["total_cost"], 250)
        self.assertFalse(skipped["taxed"])
        self.assertEqual(skipped["brand"], "krom")


class TestEvaluateWithAiPersistsNegativeProfit(unittest.TestCase):
    """evaluate_with_ai() 端到端:负利润商品进 all_evaluated,但不进 daily_pool/top_items。"""

    def test_negative_profit_excluded_from_daily_pool_and_top_items(self):
        input_items = [
            {"title": "sulab FC漆", "price": "¥4,000", "url": "https://a.example/pos",
             "img_url": "https://img.example/pos.jpg", "category": "[Mercari] けん玉"},
            {"title": "krom 基础款", "price": "¥4,000", "url": "https://a.example/neg",
             "img_url": "https://img.example/neg.jpg", "category": "[Mercari] けん玉"},
        ]
        llm_output = json.dumps([
            {"title": "sulab FC漆", "brand": "sulab", "price_jpy": 4000,
             "domestic_ref_price": 400, "is_gold_mine": False,
             "url": "https://a.example/pos", "img_url": "https://img.example/pos.jpg",
             "reason": "正利润"},
            {"title": "krom 基础款", "brand": "krom", "price_jpy": 4000,
             "domestic_ref_price": 100, "is_gold_mine": False,
             "url": "https://a.example/neg", "img_url": "https://img.example/neg.jpg",
             "reason": "负利润"},
        ], ensure_ascii=False)

        with mock.patch.object(ai_filter, "call_ai", return_value=llm_output), \
             mock.patch.object(ai_filter, "JPY_TO_CNY", 0.05), \
             mock.patch.object(ai_filter, "_append_to_daily_pool") as mocked_pool:
            top_items, all_evaluated = ai_filter.evaluate_with_ai(input_items)

        self.assertEqual(len(all_evaluated), 2)
        self.assertEqual(len(top_items), 1)
        self.assertEqual(top_items[0]["url"], "https://a.example/pos")

        neg = next(i for i in all_evaluated if i["url"] == "https://a.example/neg")
        self.assertEqual(neg["tag"], "跳过")
        self.assertLess(neg["estimated_profit"], 0)

        # daily_pool 只应该拿到正利润的那一条
        mocked_pool.assert_called_once()
        pool_arg = mocked_pool.call_args[0][0]
        self.assertEqual([i["url"] for i in pool_arg], ["https://a.example/pos"])


class TestScanRunCountsIncludeSkippedEvaluations(unittest.TestCase):
    """main.run_scan() 端到端:scan_runs.evaluated_count 包含负利润商品,
    candidate_count 只统计最终 top_items;evaluations 里负利润行 selected_for_push=0。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_cwd = os.getcwd()
        os.chdir(self.tmpdir)
        self.db_path = os.path.join(self.tmpdir, "kendama_test.db")
        self._db_patch = mock.patch.object(db, "DB_FILE", self.db_path)
        self._db_patch.start()
        db.init_db()

    def tearDown(self):
        self._db_patch.stop()
        os.chdir(self._orig_cwd)
        shutil.rmtree(self.tmpdir)

    def test_evaluated_count_includes_skipped_candidate_count_only_top_items(self):
        pos_url = "https://jp.mercari.com/item/pos1"
        neg_url = "https://jp.mercari.com/item/neg1"
        raw_items = [
            {"title": "sulab FC漆", "price": "¥4,000", "url": pos_url,
             "img_url": "https://img.example/pos.jpg", "category": "[Mercari] けん玉"},
            {"title": "krom 基础款", "price": "¥4,000", "url": neg_url,
             "img_url": "https://img.example/neg.jpg", "category": "[Mercari] けん玉"},
        ]
        pos_item = {
            "title": "sulab FC漆", "brand": "sulab", "price_jpy": 4000,
            "domestic_ref_price": 400, "is_gold_mine": False, "url": pos_url,
            "img_url": "https://img.example/pos.jpg", "reason": "test",
            "price_cny": 180.0, "total_cost": 230.0, "estimated_profit": 170,
            "taxed": False, "tag": "强推", "category": "[Mercari] けん玉",
        }
        neg_item = {
            "title": "krom 基础款", "brand": "krom", "price_jpy": 4000,
            "domestic_ref_price": 100, "is_gold_mine": False, "url": neg_url,
            "img_url": "https://img.example/neg.jpg", "reason": "test",
            "price_cny": 180.0, "total_cost": 230.0, "estimated_profit": -30,
            "taxed": False, "tag": "跳过", "category": "[Mercari] けん玉",
        }

        with mock.patch.object(main, "scrape_multiple_keywords", return_value=raw_items), \
             mock.patch.object(main, "evaluate_with_ai",
                                return_value=([pos_item], [pos_item, neg_item])), \
             mock.patch.object(main, "push_items") as mocked_push, \
             mock.patch.object(main, "check_scraper_health"):
            main.run_scan()

        mocked_push.assert_called_once_with([pos_item])

        conn = sqlite3.connect(self.db_path)
        run = conn.execute("SELECT evaluated_count, candidate_count FROM scan_runs").fetchone()
        evals = conn.execute(
            "SELECT tag, selected_for_push, estimated_profit FROM evaluations ORDER BY estimated_profit"
        ).fetchall()
        conn.close()

        self.assertEqual(run, (2, 1))
        self.assertEqual(evals, [("跳过", 0, -30), ("强推", 1, 170)])


class TestCleanLlmCandidates(unittest.TestCase):
    """main._clean_llm_candidates(): 品牌白名单之后、历史过滤之前的清洗。"""

    def test_invalid_url_is_excluded(self):
        items = [{"title": "t1", "price": "¥4,000", "url": "not-a-url"}]
        cleaned, stats = main._clean_llm_candidates(items)
        self.assertEqual(cleaned, [])
        self.assertEqual(stats["invalid_url_count"], 1)
        self.assertEqual(stats["duplicate_url_count"], 0)
        self.assertEqual(stats["price_conflict_count"], 0)

    def test_same_url_same_price_dedup_to_one(self):
        items = [
            {"title": "t1", "price": "¥4,000", "url": "https://a.example/1"},
            {"title": "t1-again", "price": "¥4,000", "url": "https://a.example/1"},
        ]
        cleaned, stats = main._clean_llm_candidates(items)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0]["title"], "t1")  # 保留抓取顺序中第一条
        self.assertEqual(stats["duplicate_url_count"], 1)
        self.assertEqual(stats["price_conflict_count"], 0)

    def test_same_url_different_price_keeps_first_and_counts_conflict(self):
        items = [
            {"title": "first", "price": "¥4,000", "url": "https://a.example/1"},
            {"title": "second", "price": "¥3,500", "url": "https://a.example/1"},
        ]
        cleaned, stats = main._clean_llm_candidates(items)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0]["title"], "first")
        self.assertEqual(stats["duplicate_url_count"], 1)
        self.assertEqual(stats["price_conflict_count"], 1)

    def test_same_title_different_url_both_kept(self):
        items = [
            {"title": "同标题", "price": "¥4,000", "url": "https://a.example/1"},
            {"title": "同标题", "price": "¥4,000", "url": "https://a.example/2"},
        ]
        cleaned, stats = main._clean_llm_candidates(items)
        self.assertEqual(len(cleaned), 2)
        self.assertEqual(stats["duplicate_url_count"], 0)

    def test_stats_account_for_every_input_item(self):
        items = [
            {"title": "t1", "price": "¥4,000", "url": "https://a.example/1"},
            {"title": "t1-dup", "price": "¥4,000", "url": "https://a.example/1"},
            {"title": "t2", "price": "¥1,000", "url": "bad-url"},
            {"title": "t3", "price": "¥2,000", "url": "https://a.example/3"},
        ]
        cleaned, stats = main._clean_llm_candidates(items)
        total_accounted = len(cleaned) + stats["invalid_url_count"] + stats["duplicate_url_count"]
        self.assertEqual(total_accounted, len(items))
        self.assertEqual(len(cleaned), 2)
        self.assertEqual(stats["invalid_url_count"], 1)
        self.assertEqual(stats["duplicate_url_count"], 1)

    def test_history_filter_still_works_after_cleaning(self):
        candidates = [
            {"title": "t1", "price": "¥18,000", "url": "https://a.example/1"},
            {"title": "t1-dup", "price": "¥18,000", "url": "https://a.example/1"},
            {"title": "t2", "price": "¥9,000", "url": "https://a.example/2"},
        ]
        cleaned, _ = main._clean_llm_candidates(candidates)
        seen_prices = {"https://a.example/1": 18000}  # 历史记录: item1 价格未变
        fresh, skipped = main.filter_already_evaluated(cleaned, seen_prices)

        self.assertEqual(len(cleaned), 2)  # 同轮去重后剩 2 条
        self.assertEqual(len(fresh), 1)     # item1 被历史过滤跳过,只剩 item2
        self.assertEqual(fresh[0]["url"], "https://a.example/2")
        self.assertEqual(skipped, 1)


class TestCleanLlmCandidatesIntegration(unittest.TestCase):
    """main.run_scan() 端到端: llm_input_count 与真正送入 evaluate_with_ai() 的数量一致,
    同轮重复 URL 不会导致 listings/price_history 出现重复记录。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_cwd = os.getcwd()
        os.chdir(self.tmpdir)
        self.db_path = os.path.join(self.tmpdir, "kendama_test.db")
        self._db_patch = mock.patch.object(db, "DB_FILE", self.db_path)
        self._db_patch.start()
        db.init_db()

    def tearDown(self):
        self._db_patch.stop()
        os.chdir(self._orig_cwd)
        shutil.rmtree(self.tmpdir)

    def test_llm_input_count_matches_what_evaluate_with_ai_receives(self):
        raw_items = [
            {"title": "sulab FC漆", "price": "¥4,000", "url": "https://a.example/1",
             "img_url": "https://img.example/1.jpg", "category": "[Mercari] けん玉"},
            {"title": "sulab FC漆 dup", "price": "¥4,000", "url": "https://a.example/1",
             "img_url": "https://img.example/1.jpg", "category": "[Yahoo] Kendama"},
            {"title": "sulab 另一件", "price": "¥5,000", "url": "https://a.example/2",
             "img_url": "https://img.example/2.jpg", "category": "[Mercari] けん玉"},
        ]

        with mock.patch.object(main, "scrape_multiple_keywords", return_value=raw_items), \
             mock.patch.object(main, "evaluate_with_ai", return_value=([], [])) as mocked_eval, \
             mock.patch.object(main, "check_scraper_health"):
            main.run_scan()

        mocked_eval.assert_called_once()
        passed_candidates = mocked_eval.call_args[0][0]
        self.assertEqual(len(passed_candidates), 2)  # 同 URL 去重后只剩 2 条

        conn = sqlite3.connect(self.db_path)
        llm_input_count = conn.execute("SELECT llm_input_count FROM scan_runs").fetchone()[0]
        conn.close()
        self.assertEqual(llm_input_count, len(passed_candidates))

    def test_duplicate_url_within_round_writes_listing_and_price_once(self):
        raw_items = [
            {"title": "sulab FC漆", "price": "¥4,000", "url": "https://a.example/dup",
             "img_url": "https://img.example/1.jpg", "category": "[Mercari] けん玉"},
            {"title": "sulab FC漆", "price": "¥4,000", "url": "https://a.example/dup",
             "img_url": "https://img.example/1.jpg", "category": "[Yahoo] Kendama"},
        ]

        with mock.patch.object(main, "scrape_multiple_keywords", return_value=raw_items), \
             mock.patch.object(main, "evaluate_with_ai", return_value=([], [])), \
             mock.patch.object(main, "check_scraper_health"):
            main.run_scan()

        conn = sqlite3.connect(self.db_path)
        listings_count = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        price_count = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
        conn.close()
        self.assertEqual(listings_count, 1)
        self.assertEqual(price_count, 1)


class TestFeedbackEventsAppend(unittest.TestCase):
    """feedback_server.py: 合法反馈继续写旧库,并追加写 kendama.db.feedback_events;
    非法签名两个库都不写;新库写入失败不影响反馈接口原有成功行为。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.legacy_db_path = os.path.join(self.tmpdir, "feedback_test.db")
        self.kendama_db_path = os.path.join(self.tmpdir, "kendama_test.db")
        self._secret_patch = mock.patch.object(feedback_server, "SIGNING_SECRET", "test-secret")
        self._legacy_db_patch = mock.patch.object(feedback_server, "DB_FILE", self.legacy_db_path)
        self._kendama_db_patch = mock.patch.object(db, "DB_FILE", self.kendama_db_path)
        self._secret_patch.start()
        self._legacy_db_patch.start()
        self._kendama_db_patch.start()
        feedback_server.init_db()
        db.init_db()
        self.client = feedback_server.app.test_client()

    def tearDown(self):
        self._secret_patch.stop()
        self._legacy_db_patch.stop()
        self._kendama_db_patch.stop()
        shutil.rmtree(self.tmpdir)

    def _legacy_rows(self):
        conn = sqlite3.connect(self.legacy_db_path)
        rows = conn.execute("SELECT id, action, reason FROM feedback ORDER BY ts").fetchall()
        conn.close()
        return rows

    def _events(self):
        conn = sqlite3.connect(self.kendama_db_path)
        rows = conn.execute(
            "SELECT legacy_item_id, url, action, reason, source FROM feedback_events ORDER BY id"
        ).fetchall()
        conn.close()
        return rows

    def _click(self, item_id, action, reason, url="https://a.example/x"):
        sig = feedback_server.expected_signature(item_id, action, reason)
        return self.client.get("/feedback", query_string={
            "id": item_id, "action": action, "reason": reason, "url": url, "sig": sig,
        })

    def test_valid_feedback_still_writes_legacy_db(self):
        resp = self._click("item1", "选的好", "符合预期")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._legacy_rows(), [("item1", "选的好", "符合预期")])

    def test_valid_feedback_appends_feedback_event(self):
        self._click("item1", "选的好", "符合预期")
        events = self._events()
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0], ("item1", "https://a.example/x", "选的好", "符合预期", "live")
        )

    def test_repeated_valid_feedback_on_same_url_keeps_multiple_events(self):
        self._click("item1", "选的好", "符合预期")
        self._click("item1", "放弃", "价格倒挂")

        events = self._events()
        self.assertEqual(len(events), 2)  # 两次点击都留痕,不是覆盖
        # 旧库仍然维持覆盖写入语义,只保留最后一条
        self.assertEqual(self._legacy_rows(), [("item1", "放弃", "价格倒挂")])

    def test_kendama_write_failure_does_not_break_feedback_endpoint(self):
        with mock.patch.object(db, "record_feedback_event", side_effect=Exception("boom")):
            resp = self._click("item1", "选的好", "符合预期")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._legacy_rows(), [("item1", "选的好", "符合预期")])
        self.assertEqual(self._events(), [])  # 新库这次确实没写成功,但不影响旧库/响应

    def test_invalid_signature_writes_neither_db(self):
        resp = self.client.get("/feedback", query_string={
            "id": "item1", "action": "选的好", "reason": "符合预期",
            "url": "https://a.example/x", "sig": "bad-signature",
        })
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self._legacy_rows(), [])
        self.assertEqual(self._events(), [])


class TestMigrateFeedbackToKendamaDb(unittest.TestCase):
    """main.migrate_feedback_to_kendama_db(): 一次性、幂等地把旧 feedback.db 导入
    kendama.db.feedback_events,不修改旧库。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_cwd = os.getcwd()
        os.chdir(self.tmpdir)
        self.kendama_db_path = os.path.join(self.tmpdir, "kendama_test.db")
        self._db_patch = mock.patch.object(db, "DB_FILE", self.kendama_db_path)
        self._db_patch.start()
        db.init_db()

        legacy_conn = sqlite3.connect(main.FEEDBACK_DB_FILE)
        legacy_conn.execute(
            "CREATE TABLE feedback (id TEXT PRIMARY KEY, url TEXT, action TEXT, reason TEXT, ts TEXT)"
        )
        legacy_conn.executemany(
            "INSERT INTO feedback VALUES (?, ?, ?, ?, ?)",
            [
                ("legacy1", "https://a.example/1", "选的好", "符合预期", "2026-06-01T00:00:00"),
                ("legacy2", "https://a.example/2", "放弃", "价格倒挂", "2026-06-02T00:00:00"),
            ],
        )
        legacy_conn.commit()
        legacy_conn.close()

    def tearDown(self):
        self._db_patch.stop()
        os.chdir(self._orig_cwd)
        shutil.rmtree(self.tmpdir)

    def _events(self):
        conn = sqlite3.connect(self.kendama_db_path)
        rows = conn.execute(
            "SELECT legacy_item_id, url, action, source FROM feedback_events ORDER BY legacy_item_id"
        ).fetchall()
        conn.close()
        return rows

    def test_import_creates_events_for_all_legacy_rows(self):
        imported, skipped = main.migrate_feedback_to_kendama_db()
        self.assertEqual(imported, 2)
        self.assertEqual(skipped, 0)
        events = self._events()
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e[3] == "legacy_import" for e in events))

    def test_repeated_import_is_idempotent(self):
        main.migrate_feedback_to_kendama_db()
        imported_second, skipped_second = main.migrate_feedback_to_kendama_db()
        self.assertEqual(imported_second, 0)
        self.assertEqual(skipped_second, 2)
        self.assertEqual(len(self._events()), 2)  # 没有产生重复行

    def test_legacy_feedback_db_is_not_modified(self):
        conn = sqlite3.connect(main.FEEDBACK_DB_FILE)
        before = conn.execute("SELECT * FROM feedback ORDER BY id").fetchall()
        conn.close()

        main.migrate_feedback_to_kendama_db()

        conn = sqlite3.connect(main.FEEDBACK_DB_FILE)
        after = conn.execute("SELECT * FROM feedback ORDER BY id").fetchall()
        conn.close()
        self.assertEqual(before, after)


class TestMigrateFeedbackCliEntryPoint(unittest.TestCase):
    """--migrate-feedback 的参数校验与 main() 分发,不扫描、不注册定时任务。"""

    def test_migrate_feedback_flag_parses_alone(self):
        args = main.parse_args(["--migrate-feedback"])
        self.assertTrue(args.migrate_feedback)

    def test_migrate_feedback_with_once_errors(self):
        with self.assertRaises(SystemExit):
            _parse_args_silently(["--migrate-feedback", "--once"])

    def test_migrate_feedback_with_platform_errors(self):
        with self.assertRaises(SystemExit):
            _parse_args_silently(["--migrate-feedback", "--once", "--platform", "Mercari"])

    def test_migrate_feedback_mode_does_not_scan_or_schedule(self):
        with mock.patch.object(main, "migrate_feedback_to_kendama_db",
                                return_value=(3, 1)) as mocked_migrate, \
             mock.patch.object(main, "run_scan") as mocked_scan, \
             mock.patch.object(main, "schedule") as mocked_schedule, \
             mock.patch.object(db, "init_db", return_value=True):
            main.main(["--migrate-feedback"])

        mocked_migrate.assert_called_once()
        mocked_scan.assert_not_called()
        mocked_schedule.every.assert_not_called()


class TestDetectPriceDrop(unittest.TestCase):
    """main._detect_price_drop() / db.get_previous_price(): 降价判定规则。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "kendama_test.db")
        db.init_db(db_file=self.db_path)
        self._db_patch = mock.patch.object(db, "DB_FILE", self.db_path)
        self._db_patch.start()

    def tearDown(self):
        self._db_patch.stop()
        shutil.rmtree(self.tmpdir)

    def _listing_with_price(self, url, price_jpy):
        listing_id = db.get_or_create_listing(
            "Mercari", url, f"hash-{url}", "t", "i", "2026-01-01T00:00:00"
        )
        run_id = db.create_scan_run("2026-01-01T00:00:00")
        db.record_price(listing_id, run_id, price_jpy, f"¥{price_jpy}", "2026-01-01T00:00:00")
        return listing_id

    def test_no_history_means_no_drop(self):
        listing_id = db.get_or_create_listing(
            "Mercari", "https://a.example/new", "hash-new", "t", "i", "2026-01-01T00:00:00"
        )
        current_run = db.create_scan_run("2026-01-02T00:00:00")
        result = main._detect_price_drop(listing_id, current_run, 4000)
        self.assertFalse(result["is_price_drop"])
        self.assertIsNone(result["previous_price_jpy"])

    def test_drop_of_500_jpy_triggers_even_below_pct_threshold(self):
        # 20000 → 19500: -500 日元, -2.5%(低于 5% 门槛,但金额门槛已达标)
        listing_id = self._listing_with_price("https://a.example/1", 20000)
        current_run = db.create_scan_run("2026-01-02T00:00:00")
        result = main._detect_price_drop(listing_id, current_run, 19500)
        self.assertTrue(result["is_price_drop"])
        self.assertEqual(result["previous_price_jpy"], 20000)
        self.assertEqual(result["price_drop_jpy"], 500)

    def test_drop_of_5_pct_triggers_even_below_jpy_threshold(self):
        # 1000 → 940: -60 日元(低于 500 门槛),-6%(达标)
        listing_id = self._listing_with_price("https://a.example/2", 1000)
        current_run = db.create_scan_run("2026-01-02T00:00:00")
        result = main._detect_price_drop(listing_id, current_run, 940)
        self.assertTrue(result["is_price_drop"])
        self.assertEqual(result["price_drop_jpy"], 60)
        self.assertEqual(result["price_drop_pct"], 6.0)

    def test_price_increase_does_not_trigger(self):
        listing_id = self._listing_with_price("https://a.example/3", 4000)
        current_run = db.create_scan_run("2026-01-02T00:00:00")
        result = main._detect_price_drop(listing_id, current_run, 4500)
        self.assertFalse(result["is_price_drop"])
        self.assertEqual(result["previous_price_jpy"], 4000)

    def test_unchanged_price_does_not_trigger(self):
        listing_id = self._listing_with_price("https://a.example/4", 4000)
        current_run = db.create_scan_run("2026-01-02T00:00:00")
        result = main._detect_price_drop(listing_id, current_run, 4000)
        self.assertFalse(result["is_price_drop"])

    def test_small_drop_below_both_thresholds_does_not_trigger(self):
        # 10000 → 9700: -300 日元, -3%,两个门槛都没达标
        listing_id = self._listing_with_price("https://a.example/5", 10000)
        current_run = db.create_scan_run("2026-01-02T00:00:00")
        result = main._detect_price_drop(listing_id, current_run, 9700)
        self.assertFalse(result["is_price_drop"])


class TestPriceDropFieldsTransplant(unittest.TestCase):
    """降价字段必须从 main.py 算好的候选透传到 evaluate_with_ai() 返回的 enriched item,
    不能被 LLM 输出覆盖或丢失;负利润 + 降价的商品仍然不进 top_items/daily_pool。"""

    def test_price_drop_fields_survive_llm_round_trip(self):
        input_items = [{
            "title": "sulab FC漆", "price": "¥4,000", "url": "https://a.example/pos",
            "img_url": "https://img.example/pos.jpg", "category": "[Mercari] けん玉",
            "previous_price_jpy": 5000, "price_drop_jpy": 1000, "price_drop_pct": 20.0,
            "is_price_drop": True,
        }]
        llm_output = json.dumps([{
            "title": "sulab FC漆", "brand": "sulab", "price_jpy": 4000,
            "domestic_ref_price": 400, "is_gold_mine": False,
            "url": "https://a.example/pos", "img_url": "https://img.example/pos.jpg",
            "reason": "正利润",
        }], ensure_ascii=False)

        with mock.patch.object(ai_filter, "call_ai", return_value=llm_output), \
             mock.patch.object(ai_filter, "JPY_TO_CNY", 0.05), \
             mock.patch.object(ai_filter, "_append_to_daily_pool"):
            top_items, all_evaluated = ai_filter.evaluate_with_ai(input_items)

        self.assertEqual(len(top_items), 1)
        self.assertTrue(top_items[0]["is_price_drop"])
        self.assertEqual(top_items[0]["previous_price_jpy"], 5000)
        self.assertEqual(top_items[0]["price_drop_jpy"], 1000)
        self.assertEqual(top_items[0]["price_drop_pct"], 20.0)

    def test_negative_profit_price_drop_item_not_in_top_items_or_daily_pool(self):
        input_items = [{
            "title": "krom 基础款", "price": "¥4,000", "url": "https://a.example/neg",
            "img_url": "https://img.example/neg.jpg", "category": "[Mercari] けん玉",
            "previous_price_jpy": 5000, "price_drop_jpy": 1000, "price_drop_pct": 20.0,
            "is_price_drop": True,
        }]
        llm_output = json.dumps([{
            "title": "krom 基础款", "brand": "krom", "price_jpy": 4000,
            "domestic_ref_price": 100, "is_gold_mine": False,
            "url": "https://a.example/neg", "img_url": "https://img.example/neg.jpg",
            "reason": "负利润但降价",
        }], ensure_ascii=False)

        with mock.patch.object(ai_filter, "call_ai", return_value=llm_output), \
             mock.patch.object(ai_filter, "JPY_TO_CNY", 0.05), \
             mock.patch.object(ai_filter, "_append_to_daily_pool") as mocked_pool:
            top_items, all_evaluated = ai_filter.evaluate_with_ai(input_items)

        self.assertEqual(top_items, [])  # 不进入推送候选
        self.assertEqual(len(all_evaluated), 1)
        self.assertEqual(all_evaluated[0]["tag"], "跳过")
        self.assertTrue(all_evaluated[0]["is_price_drop"])  # 降价信息仍保留,只是不推送
        mocked_pool.assert_not_called()


class TestPriceDropCardAnnotation(unittest.TestCase):
    """飞书卡片(单条 + 汇总)在 is_price_drop 时明确显示【降价 ¥N / P%】,否则不显示。"""

    def _base_item(self, **overrides):
        item = {
            "estimated_profit": 170, "brand": "sulab", "title": "t", "price_jpy": 4000,
            "price_cny": 180.0, "total_cost": 230.0, "domestic_ref_price": 400,
            "taxed": False, "tag": "强推", "reason": "r", "img_url": "i", "url": "u",
        }
        item.update(overrides)
        return item

    def test_item_card_shows_drop_annotation_when_flagged(self):
        item = self._base_item(is_price_drop=True, price_drop_jpy=800, price_drop_pct=8.2)
        card = main.build_item_card(item)
        content = card["card"]["elements"][0]["content"]
        self.assertIn("降价 ¥800 / 8.2%", content)

    def test_item_card_no_annotation_when_not_a_drop(self):
        item = self._base_item(is_price_drop=False)
        card = main.build_item_card(item)
        content = card["card"]["elements"][0]["content"]
        self.assertNotIn("降价", content)

    def test_summary_card_shows_drop_annotation(self):
        item = self._base_item(is_price_drop=True, price_drop_jpy=800, price_drop_pct=8.2)
        card = main.build_summary_card([item])
        content = card["card"]["elements"][0]["content"]
        self.assertIn("降价 ¥800 / 8.2%", content)

    def test_summary_card_no_annotation_when_not_a_drop(self):
        item = self._base_item(is_price_drop=False)
        card = main.build_summary_card([item])
        content = card["card"]["elements"][0]["content"]
        self.assertNotIn("降价", content)


class TestEvaluationsColumnMigration(unittest.TestCase):
    """db.init_db() 必须能安全、幂等地给已存在的旧 evaluations 表补齐降价相关列,
    不丢失、不覆盖、不重建已有历史行(模拟这几列引入之前就存在的真实 kendama.db)。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "kendama_test.db")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _create_legacy_schema(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE scan_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL, finished_at TEXT,
                raw_item_count INTEGER, brand_matched_count INTEGER,
                llm_input_count INTEGER, evaluated_count INTEGER,
                candidate_count INTEGER, status TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL, url TEXT NOT NULL UNIQUE,
                legacy_url_hash TEXT NOT NULL, title TEXT NOT NULL,
                img_url TEXT NOT NULL DEFAULT '',
                first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id INTEGER NOT NULL, scan_run_id INTEGER NOT NULL,
                price_jpy INTEGER NOT NULL, domestic_ref_price REAL NOT NULL,
                is_gold_mine INTEGER NOT NULL, reason TEXT,
                estimated_profit REAL NOT NULL, total_cost REAL NOT NULL,
                taxed INTEGER NOT NULL, tag TEXT NOT NULL,
                selected_for_push INTEGER NOT NULL, evaluated_at TEXT NOT NULL,
                brand TEXT, source_category TEXT
            )
        """)
        conn.execute(
            "INSERT INTO listings (platform, url, legacy_url_hash, title, img_url, "
            "first_seen_at, last_seen_at) VALUES "
            "('Mercari', 'https://a.example/old', 'hash-old', 't', 'i', "
            "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
        )
        conn.execute("INSERT INTO scan_runs (started_at) VALUES ('2026-01-01T00:00:00')")
        conn.execute(
            "INSERT INTO evaluations (listing_id, scan_run_id, price_jpy, domestic_ref_price, "
            "is_gold_mine, reason, estimated_profit, total_cost, taxed, tag, selected_for_push, "
            "evaluated_at, brand, source_category) VALUES (1, 1, 4000, 400, 0, 'old row', 170, "
            "230, 0, '强推', 1, '2026-01-01T00:00:00', 'sulab', '[Mercari] けん玉')"
        )
        conn.commit()
        conn.close()

    def test_missing_drop_columns_are_added_safely(self):
        self._create_legacy_schema()
        ok = db.init_db(db_file=self.db_path)
        self.assertTrue(ok)

        conn = sqlite3.connect(self.db_path)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(evaluations)").fetchall()}
        conn.close()
        for col in ("previous_price_jpy", "price_drop_jpy", "price_drop_pct", "is_price_drop"):
            self.assertIn(col, columns)

    def test_old_row_preserved_with_is_price_drop_default_zero(self):
        self._create_legacy_schema()
        db.init_db(db_file=self.db_path)

        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT reason, brand, is_price_drop, previous_price_jpy FROM evaluations WHERE id = 1"
        ).fetchone()
        count = conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0]
        conn.close()

        self.assertEqual(count, 1)  # 没有丢失、没有重建
        self.assertEqual(row[0], "old row")
        self.assertEqual(row[1], "sulab")
        self.assertEqual(row[2], 0)  # 旧行默认值
        self.assertIsNone(row[3])

    def test_migration_is_idempotent(self):
        self._create_legacy_schema()
        db.init_db(db_file=self.db_path)
        ok_second = db.init_db(db_file=self.db_path)  # 再跑一次不应报错、不应重复补列
        self.assertTrue(ok_second)

        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    def test_new_evaluation_drop_fields_are_written(self):
        self._create_legacy_schema()
        db.init_db(db_file=self.db_path)

        ok = db.record_evaluation(
            listing_id=1, scan_run_id=1, price_jpy=4000, domestic_ref_price=400,
            is_gold_mine=False, reason="new row", estimated_profit=170, total_cost=230,
            taxed=False, tag="强推", selected_for_push=True, evaluated_at="2026-01-02T00:00:00",
            brand="sulab", source_category="[Mercari] けん玉",
            previous_price_jpy=5000, price_drop_jpy=1000, price_drop_pct=20.0,
            is_price_drop=True, db_file=self.db_path,
        )
        self.assertTrue(ok)

        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT previous_price_jpy, price_drop_jpy, price_drop_pct, is_price_drop "
            "FROM evaluations WHERE reason = 'new row'"
        ).fetchone()
        conn.close()
        self.assertEqual(row, (5000, 1000, 20.0, 1))


class TestWeeklyReviewGeneration(unittest.TestCase):
    """reporting.generate_weekly_review(): 空库/只有扫描数据/带反馈数据三种场景都能生成。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "kendama_test.db")
        self.reports_dir = os.path.join(self.tmpdir, "reports")
        db.init_db(db_file=self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_empty_database_generates_report_without_crashing(self):
        path, summary = reporting.generate_weekly_review(
            days=7, db_file=self.db_path, reports_dir=self.reports_dir
        )
        self.assertTrue(os.path.exists(path))
        self.assertEqual(summary["scan_runs"], 0)
        self.assertEqual(summary["evaluations"], 0)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("每周复盘报告", content)

    def _seed_scan_and_evaluations(self):
        now = datetime.now().isoformat(timespec="seconds")
        run_id = db.create_scan_run(now, db_file=self.db_path)
        db.finish_scan_run(
            run_id, now, "ok", raw_item_count=100, brand_matched_count=20,
            llm_input_count=15, evaluated_count=10, candidate_count=2, db_file=self.db_path,
        )
        listing_a = db.get_or_create_listing(
            "Mercari", "https://a.example/1", "h1", "t1", "i1", now, db_file=self.db_path
        )
        listing_b = db.get_or_create_listing(
            "Mercari", "https://a.example/2", "h2", "t2", "i2", now, db_file=self.db_path
        )
        db.record_evaluation(
            listing_a, run_id, 4000, 400, False, "r", 170, 230, False, "强推", True, now,
            brand="sulab", source_category="[Mercari] けん玉",
            is_price_drop=True, previous_price_jpy=5000, price_drop_jpy=1000, price_drop_pct=20.0,
            db_file=self.db_path,
        )
        db.record_evaluation(
            listing_b, run_id, 4000, 100, False, "r", -130, 230, False, "跳过", False, now,
            brand="krom", source_category="[Yahoo] Kendama", db_file=self.db_path,
        )
        return run_id, listing_a, listing_b

    def test_scan_only_data_generates_correct_funnel_and_profit_sections(self):
        self._seed_scan_and_evaluations()
        path, summary = reporting.generate_weekly_review(
            days=7, db_file=self.db_path, reports_dir=self.reports_dir
        )
        with open(path, encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(summary["scan_runs"], 1)
        self.assertEqual(summary["evaluations"], 2)
        self.assertIn("raw_item_count 总和: 100", content)
        self.assertIn("sulab", content)
        self.assertIn("krom", content)

    def test_feedback_data_included_in_review(self):
        run_id, listing_a, listing_b = self._seed_scan_and_evaluations()
        now = datetime.now().isoformat(timespec="seconds")
        db.record_feedback_event(
            listing_id=listing_a, legacy_item_id="x1", url="https://a.example/1",
            action="选的好", reason="符合预期", source="live",
            dedupe_key="live:1", created_at=now, db_file=self.db_path,
        )
        db.record_feedback_event(
            listing_id=None, legacy_item_id="x2", url="https://a.example/unknown",
            action="放弃", reason="旧记录", source="legacy_import",
            dedupe_key="legacy:2", created_at=now, db_file=self.db_path,
        )

        path, summary = reporting.generate_weekly_review(
            days=7, db_file=self.db_path, reports_dir=self.reports_dir
        )
        self.assertEqual(summary["feedback_events_in_window"], 2)
        self.assertEqual(summary["feedback_events_total"], 2)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("数据库中 feedback_events 总数(不限时间窗口): 2", content)
        self.assertIn("统计窗口内 feedback_events 数: 2", content)
        self.assertIn("可关联到 listing 的反馈数: 1", content)
        self.assertIn("无法关联的反馈数: 1", content)
        self.assertIn("legacy_import", content)

    def test_data_quality_section_detects_fk_check_and_missing_fields(self):
        self._seed_scan_and_evaluations()
        path, _ = reporting.generate_weekly_review(
            days=7, db_file=self.db_path, reports_dir=self.reports_dir
        )
        with open(path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("PRAGMA foreign_key_check", content)
        self.assertIn("已推送但没有任何反馈的候选数量", content)
        self.assertIn("缺少 brand 或 source_category 的 evaluations 数", content)
        self.assertIn("首次出现且无价格历史的 listings 数", content)


class TestWeeklyReviewCliDoesNotTouchRealWork(unittest.TestCase):
    """--weekly-review 只生成报告,不抓取、不调 LLM、不推飞书。"""

    def test_weekly_review_only_calls_reporting_module(self):
        with mock.patch.object(reporting, "generate_weekly_review",
                                return_value=("reports/x.md", {"scan_runs": 0})) as mocked_gen, \
             mock.patch.object(main, "scrape_multiple_keywords") as mocked_scrape, \
             mock.patch.object(main, "evaluate_with_ai") as mocked_llm, \
             mock.patch.object(main, "post_to_feishu") as mocked_feishu, \
             mock.patch.object(db, "init_db", return_value=True):
            main.main(["--weekly-review"])

        mocked_gen.assert_called_once()
        mocked_scrape.assert_not_called()
        mocked_llm.assert_not_called()
        mocked_feishu.assert_not_called()

    def test_weekly_review_with_days_passes_through(self):
        with mock.patch.object(reporting, "generate_weekly_review",
                                return_value=("reports/x.md", {})) as mocked_gen, \
             mock.patch.object(db, "init_db", return_value=True):
            main.main(["--weekly-review", "--days", "14"])
        mocked_gen.assert_called_once_with(days=14)


class TestSignalsGeneration(unittest.TestCase):
    """reporting.generate_signals_report(): 样本不足时明确标注,每条结论带样本数,不修改数据库。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "kendama_test.db")
        self.signals_path = os.path.join(self.tmpdir, "personalized_signals.md")
        db.init_db(db_file=self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _seed(self, feedback_rows):
        """feedback_rows: [(brand, action, price_jpy, is_drop), ...]。"""
        now = datetime.now().isoformat(timespec="seconds")
        run_id = db.create_scan_run(now, db_file=self.db_path)
        for i, (brand, action, price_jpy, is_drop) in enumerate(feedback_rows):
            url = f"https://a.example/{i}"
            listing_id = db.get_or_create_listing(
                "Mercari", url, f"h{i}", f"t{i}", "i", now, db_file=self.db_path
            )
            db.record_evaluation(
                listing_id, run_id, price_jpy, 400, False, "r", 170, 230, False,
                "强推", True, now, brand=brand, source_category="[Mercari] けん玉",
                is_price_drop=is_drop, db_file=self.db_path,
            )
            db.record_feedback_event(
                listing_id=listing_id, legacy_item_id=f"leg{i}", url=url,
                action=action, reason="r", source="live",
                dedupe_key=f"live:{i}", created_at=now, db_file=self.db_path,
            )

    def test_insufficient_sample_is_explicitly_labeled(self):
        self._seed([("sulab", "选的好", 4000, False)])  # 只有 1 条,低于默认门槛 3
        path, _ = reporting.generate_signals_report(
            db_file=self.db_path, signals_file=self.signals_path
        )
        with open(path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("样本不足,不形成结论", content)

    def test_each_conclusion_carries_sample_count(self):
        self._seed([
            ("sulab", "选的好", 4000, False),
            ("sulab", "选的好", 4000, False),
            ("sulab", "选的好", 4000, False),
        ])  # 3 条全部正向,达到门槛且占比 100%
        path, _ = reporting.generate_signals_report(
            db_file=self.db_path, signals_file=self.signals_path
        )
        with open(path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("sulab: 样本 3 条", content)

    def test_refresh_signals_does_not_modify_database(self):
        self._seed([
            ("sulab", "选的好", 4000, False),
            ("sulab", "放弃", 4000, False),
        ])
        conn = sqlite3.connect(self.db_path)
        before = {
            "listings": conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0],
            "evaluations": conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0],
            "feedback_events": conn.execute("SELECT COUNT(*) FROM feedback_events").fetchone()[0],
        }
        conn.close()

        reporting.generate_signals_report(db_file=self.db_path, signals_file=self.signals_path)

        conn = sqlite3.connect(self.db_path)
        after = {
            "listings": conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0],
            "evaluations": conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0],
            "feedback_events": conn.execute("SELECT COUNT(*) FROM feedback_events").fetchone()[0],
        }
        conn.close()
        self.assertEqual(before, after)

    def test_signals_file_states_not_auto_injected_into_prompt(self):
        self._seed([("sulab", "选的好", 4000, False)])
        path, _ = reporting.generate_signals_report(
            db_file=self.db_path, signals_file=self.signals_path
        )
        with open(path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("本轮不会被自动注入 LLM prompt", content)


class TestRefreshSignalsCliDoesNotTouchRealWork(unittest.TestCase):
    def test_refresh_signals_only_calls_reporting_module(self):
        with mock.patch.object(reporting, "generate_signals_report",
                                return_value=("personalized_signals.md", {})) as mocked_gen, \
             mock.patch.object(main, "scrape_multiple_keywords") as mocked_scrape, \
             mock.patch.object(main, "evaluate_with_ai") as mocked_llm, \
             mock.patch.object(main, "post_to_feishu") as mocked_feishu, \
             mock.patch.object(db, "init_db", return_value=True):
            main.main(["--refresh-signals"])

        mocked_gen.assert_called_once()
        mocked_scrape.assert_not_called()
        mocked_llm.assert_not_called()
        mocked_feishu.assert_not_called()


class TestMaintenanceCommandsMutualExclusivity(unittest.TestCase):
    """--migrate-feedback / --weekly-review / --refresh-signals 互斥,且都不能与
    --once / --platform / --keyword / --max-items 同时使用。"""

    def test_weekly_review_and_refresh_signals_together_errors(self):
        with self.assertRaises(SystemExit):
            _parse_args_silently(["--weekly-review", "--refresh-signals"])

    def test_weekly_review_and_migrate_feedback_together_errors(self):
        with self.assertRaises(SystemExit):
            _parse_args_silently(["--weekly-review", "--migrate-feedback"])

    def test_weekly_review_with_once_errors(self):
        with self.assertRaises(SystemExit):
            _parse_args_silently(["--weekly-review", "--once"])

    def test_refresh_signals_with_platform_errors(self):
        with self.assertRaises(SystemExit):
            _parse_args_silently(["--refresh-signals", "--once", "--platform", "Mercari"])

    def test_days_without_weekly_review_errors(self):
        with self.assertRaises(SystemExit):
            _parse_args_silently(["--days", "3"])

    def test_days_zero_errors(self):
        with self.assertRaises(SystemExit):
            _parse_args_silently(["--weekly-review", "--days", "0"])

    def test_weekly_review_alone_parses(self):
        args = main.parse_args(["--weekly-review"])
        self.assertTrue(args.weekly_review)
        self.assertIsNone(args.days)

    def test_weekly_review_with_days_parses(self):
        args = main.parse_args(["--weekly-review", "--days", "14"])
        self.assertEqual(args.days, 14)

    def test_refresh_signals_alone_parses(self):
        args = main.parse_args(["--refresh-signals"])
        self.assertTrue(args.refresh_signals)


class TestWeeklyReviewFeedbackWindowBreakdown(unittest.TestCase):
    """周报必须把"窗口内 0 条"和"数据库里其实有历史反馈"分开展示,
    不能把窗口外的历史数据写成误导性的"总数 0"。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "kendama_test.db")
        self.reports_dir = os.path.join(self.tmpdir, "reports")
        db.init_db(db_file=self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_outside_window_feedback_is_shown_separately_with_explanation(self):
        old_ts = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
        db.record_feedback_event(
            listing_id=None, legacy_item_id="old1", url="https://a.example/old",
            action="放弃", reason="很久以前", source="legacy_import",
            dedupe_key="legacy:old1", created_at=old_ts, db_file=self.db_path,
        )

        path, summary = reporting.generate_weekly_review(
            days=7, db_file=self.db_path, reports_dir=self.reports_dir
        )
        self.assertEqual(summary["feedback_events_in_window"], 0)
        self.assertEqual(summary["feedback_events_total"], 1)

        with open(path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("数据库中 feedback_events 总数(不限时间窗口): 1", content)
        self.assertIn("统计窗口内 feedback_events 数: 0", content)
        self.assertIn("统计窗口外历史 feedback_events 数: 1", content)
        self.assertIn("本统计窗口内暂无反馈", content)
        self.assertIn("数据库中仍有 1 条", content)
        # 必须是"这不代表反馈数据缺失或系统未生效"这种明确的否定说法,
        # 而不是把"窗口内 0 条"直接断言成"反馈数据缺失"或"系统未生效"。
        self.assertIn("不代表反馈数据缺失或系统未生效", content)

    def test_action_mapping_counts_buy_in_as_positive_and_ignores_unmapped(self):
        now = datetime.now().isoformat(timespec="seconds")
        run_id = db.create_scan_run(now, db_file=self.db_path)
        listing_id = db.get_or_create_listing(
            "Mercari", "https://a.example/buy", "h1", "t1", "i1", now, db_file=self.db_path
        )
        db.record_evaluation(
            listing_id, run_id, 4000, 400, False, "r", 170, 230, False, "强推", True, now,
            brand="sulab", source_category="[Mercari] けん玉", db_file=self.db_path,
        )
        db.record_feedback_event(
            listing_id=listing_id, legacy_item_id="buy1", url="https://a.example/buy",
            action="买入", reason="旧标签", source="legacy_import",
            dedupe_key="legacy:buy1", created_at=now, db_file=self.db_path,
        )
        db.record_feedback_event(
            listing_id=listing_id, legacy_item_id="mystery1", url="https://a.example/buy",
            action="未知动作", reason="不认识", source="legacy_import",
            dedupe_key="legacy:mystery1", created_at=now, db_file=self.db_path,
        )

        path, _ = reporting.generate_weekly_review(
            days=7, db_file=self.db_path, reports_dir=self.reports_dir
        )
        with open(path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("映射后正向反馈数: 1, 负向反馈数: 0", content)
        self.assertIn("未知动作: 1", content)  # 出现在"按 action 原样统计"里,没被强行归类

    def test_empty_url_feedback_counted_in_total_but_excluded_from_preference_stats(self):
        now = datetime.now().isoformat(timespec="seconds")
        run_id = db.create_scan_run(now, db_file=self.db_path)
        listing_id = db.get_or_create_listing(
            "Mercari", "https://a.example/normal", "h1", "t1", "i1", now, db_file=self.db_path
        )
        db.record_evaluation(
            listing_id, run_id, 4000, 400, False, "r", 170, 230, False, "强推", True, now,
            brand="sulab", source_category="[Mercari] けん玉", db_file=self.db_path,
        )
        db.record_feedback_event(
            listing_id=listing_id, legacy_item_id="normal1", url="https://a.example/normal",
            action="选的好", reason="ok", source="live",
            dedupe_key="live:normal1", created_at=now, db_file=self.db_path,
        )
        db.record_feedback_event(
            listing_id=None, legacy_item_id="test", url="",
            action="放弃", reason="测试脏数据", source="legacy_import",
            dedupe_key="legacy:test", created_at=now, db_file=self.db_path,
        )

        path, summary = reporting.generate_weekly_review(
            days=7, db_file=self.db_path, reports_dir=self.reports_dir
        )
        self.assertEqual(summary["feedback_events_in_window"], 2)  # 空 URL 那条仍计入总数
        with open(path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn(
            "空 URL 的 feedback_events 数(不限时间窗口,历史测试/脏数据,不参与偏好结论): 1",
            content,
        )
        # 正负向统计只应该来自那条正常记录("选的好"),空 URL 的"放弃"不计入
        self.assertIn("映射后正向反馈数: 1, 负向反馈数: 0", content)
        # 品牌分布也不应该被空 URL 记录污染
        self.assertIn("sulab: 1", content)


class TestWeeklyReviewNoRawDictRepr(unittest.TestCase):
    """周报不应该直接打印 Python dict 的 repr,应该渲染成 Markdown 列表。"""

    def test_report_does_not_contain_python_dict_repr(self):
        tmpdir = tempfile.mkdtemp()
        try:
            db_path = os.path.join(tmpdir, "kendama_test.db")
            reports_dir = os.path.join(tmpdir, "reports")
            db.init_db(db_file=db_path)
            now = datetime.now().isoformat(timespec="seconds")
            run_id = db.create_scan_run(now, db_file=db_path)
            listing_id = db.get_or_create_listing(
                "Mercari", "https://a.example/1", "h1", "t1", "i1", now, db_file=db_path
            )
            db.record_evaluation(
                listing_id, run_id, 4000, 400, False, "r", 170, 230, False, "强推", True, now,
                brand="sulab", source_category="[Mercari] けん玉", db_file=db_path,
            )
            path, _ = reporting.generate_weekly_review(
                days=7, db_file=db_path, reports_dir=reports_dir
            )
            with open(path, encoding="utf-8") as f:
                content = f.read()
            # Python dict repr 的典型特征: 花括号里紧跟带引号的 key,例如 {'强推': 2}
            self.assertNotRegex(content, r"\{'[^']+':\s*[\d']")
        finally:
            shutil.rmtree(tmpdir)


class TestSignalsColdStartMessage(unittest.TestCase):
    """personalized_signals.md 在可关联反馈为 0 时,必须给出明确的冷启动说明,
    而不只是干巴巴地列出空分组。"""

    def test_zero_linked_feedback_shows_cold_start_explanation(self):
        tmpdir = tempfile.mkdtemp()
        try:
            db_path = os.path.join(tmpdir, "kendama_test.db")
            signals_path = os.path.join(tmpdir, "personalized_signals.md")
            db.init_db(db_file=db_path)
            # 有反馈,但 listing_id 全部无法关联(与真实场景一致)
            db.record_feedback_event(
                listing_id=None, legacy_item_id="x1", url="https://a.example/unmatched",
                action="放弃", reason="r", source="legacy_import",
                dedupe_key="legacy:x1", created_at=datetime.now().isoformat(timespec="seconds"),
                db_file=db_path,
            )
            path, summary = reporting.generate_signals_report(
                db_file=db_path, signals_file=signals_path
            )
            self.assertEqual(summary["linked_feedback"], 0)
            with open(path, encoding="utf-8") as f:
                content = f.read()
            self.assertIn("当前没有可关联到近期扫描商品的有效反馈", content)
            self.assertIn("并不代表反馈功能失效", content)
        finally:
            shutil.rmtree(tmpdir)


class TestMaintenanceCommandsLogOnce(unittest.TestCase):
    """--weekly-review / --refresh-signals 的"已生成"提示只应该出现一次,不重复刷屏。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_cwd = os.getcwd()
        os.chdir(self.tmpdir)
        self.db_path = os.path.join(self.tmpdir, "kendama_test.db")
        self._db_patch = mock.patch.object(db, "DB_FILE", self.db_path)
        self._db_patch.start()

    def tearDown(self):
        self._db_patch.stop()
        os.chdir(self._orig_cwd)
        shutil.rmtree(self.tmpdir)

    def test_weekly_review_reports_generation_message_only_once(self):
        with self.assertLogs(level="INFO") as captured:
            main.main(["--weekly-review"])
        occurrences = sum(1 for line in captured.output if "周报已生成" in line)
        self.assertEqual(occurrences, 1)

    def test_refresh_signals_reports_generation_message_only_once(self):
        with self.assertLogs(level="INFO") as captured:
            main.main(["--refresh-signals"])
        occurrences = sum(1 for line in captured.output if "偏好信号已生成" in line)
        self.assertEqual(occurrences, 1)


class TestStatusCommand(unittest.TestCase):
    """main.print_status() / --status: 只读输出摘要,不初始化/创建数据库,
    不抓取、不调用 LLM、不推飞书。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_cwd = os.getcwd()
        os.chdir(self.tmpdir)
        self.db_path = os.path.join(self.tmpdir, "kendama_test.db")
        self._db_patch = mock.patch.object(db, "DB_FILE", self.db_path)
        self._db_patch.start()

    def tearDown(self):
        self._db_patch.stop()
        os.chdir(self._orig_cwd)
        shutil.rmtree(self.tmpdir)

    def test_status_does_not_create_database_when_missing(self):
        self.assertFalse(os.path.exists(self.db_path))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            main.print_status()
        self.assertFalse(os.path.exists(self.db_path))
        self.assertIn("尚未初始化", buf.getvalue())

    def test_status_outputs_key_summary_on_full_database(self):
        db.init_db(db_file=self.db_path)
        now = datetime.now().isoformat(timespec="seconds")
        run_id = db.create_scan_run(now, db_file=self.db_path)
        db.finish_scan_run(
            run_id, now, "ok", raw_item_count=100, brand_matched_count=20,
            llm_input_count=15, evaluated_count=10, candidate_count=2, db_file=self.db_path,
        )
        listing_id = db.get_or_create_listing(
            "Mercari", "https://a.example/1", "h1", "t1", "i1", now, db_file=self.db_path
        )
        db.record_evaluation(
            listing_id, run_id, 4000, 400, False, "r", 170, 230, False, "强推", True, now,
            brand="sulab", source_category="[Mercari] けん玉",
            is_price_drop=True, previous_price_jpy=5000, price_drop_jpy=1000, price_drop_pct=20.0,
            db_file=self.db_path,
        )
        db.record_feedback_event(
            listing_id=listing_id, legacy_item_id="x1", url="https://a.example/1",
            action="选的好", reason="ok", source="live",
            dedupe_key="live:1", created_at=now, db_file=self.db_path,
        )

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            main.print_status()
        output = buf.getvalue()

        self.assertIn("kendama.db 是否存在: 是", output)
        self.assertIn(f"id={run_id}", output)
        self.assertIn("listings 总数: 1", output)
        self.assertIn("price_history 总数: 0", output)
        self.assertIn("evaluations 总数: 1", output)
        self.assertIn("feedback_events 总数: 1", output)
        self.assertIn("强推: 1", output)
        self.assertIn("selected_for_push=1 总数: 1", output)
        self.assertIn("is_price_drop=1 总数: 1", output)
        self.assertIn("PRAGMA foreign_key_check: 无异常", output)
        self.assertIn("最近周报文件: 不存在", output)
        self.assertIn("personalized_signals.md 是否存在: 否", output)
        self.assertIn("daily_pool.json 是否存在: 否", output)

    def test_status_reports_daily_pool_candidate_and_unique_url_counts(self):
        db.init_db(db_file=self.db_path)
        pool = [
            {"url": "https://a.example/1", "estimated_profit": 10},
            {"url": "https://a.example/1", "estimated_profit": 12},
            {"url": "https://a.example/2", "estimated_profit": 20},
        ]
        with open("daily_pool.json", "w", encoding="utf-8") as f:
            json.dump(pool, f)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            main.print_status()
        output = buf.getvalue()
        self.assertIn("daily_pool.json 是否存在: 是,候选 3 条,不同 URL 2 个", output)

    def test_status_does_not_scrape_call_llm_or_push_feishu(self):
        db.init_db(db_file=self.db_path)
        with mock.patch.object(main, "scrape_multiple_keywords") as mocked_scrape, \
             mock.patch.object(main, "evaluate_with_ai") as mocked_llm, \
             mock.patch.object(main, "post_to_feishu") as mocked_feishu:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                main.main(["--status"])
        mocked_scrape.assert_not_called()
        mocked_llm.assert_not_called()
        mocked_feishu.assert_not_called()


class TestStatusMutualExclusivity(unittest.TestCase):
    """--status 必须与其他运行/维护参数全部互斥。"""

    def test_status_alone_parses(self):
        args = main.parse_args(["--status"])
        self.assertTrue(args.status)

    def test_status_with_once_errors(self):
        with self.assertRaises(SystemExit):
            _parse_args_silently(["--status", "--once"])

    def test_status_with_weekly_review_errors(self):
        with self.assertRaises(SystemExit):
            _parse_args_silently(["--status", "--weekly-review"])

    def test_status_with_migrate_feedback_errors(self):
        with self.assertRaises(SystemExit):
            _parse_args_silently(["--status", "--migrate-feedback"])

    def test_status_with_refresh_signals_errors(self):
        with self.assertRaises(SystemExit):
            _parse_args_silently(["--status", "--refresh-signals"])

    def test_status_with_platform_errors(self):
        with self.assertRaises(SystemExit):
            _parse_args_silently(["--status", "--once", "--platform", "Mercari"])

    def test_status_with_keyword_errors(self):
        with self.assertRaises(SystemExit):
            _parse_args_silently(["--status", "--once", "--keyword", "Kendama"])

    def test_status_with_max_items_errors(self):
        with self.assertRaises(SystemExit):
            _parse_args_silently(["--status", "--once", "--max-items", "10"])


class TestWeeklyReviewFilenameByDays(unittest.TestCase):
    """不同 --days 取值生成不同文件名,默认 7 天文件名不变、也不会被其他天数覆盖。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "kendama_test.db")
        self.reports_dir = os.path.join(self.tmpdir, "reports")
        db.init_db(db_file=self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_default_seven_days_uses_plain_filename(self):
        path, _ = reporting.generate_weekly_review(
            days=7, db_file=self.db_path, reports_dir=self.reports_dir
        )
        today = datetime.now().strftime("%Y%m%d")
        self.assertEqual(os.path.basename(path), f"weekly_review_{today}.md")

    def test_custom_days_uses_suffixed_filename_and_does_not_overwrite_default(self):
        default_path, _ = reporting.generate_weekly_review(
            days=7, db_file=self.db_path, reports_dir=self.reports_dir
        )
        with open(default_path, encoding="utf-8") as f:
            default_content_before = f.read()

        path_14, _ = reporting.generate_weekly_review(
            days=14, db_file=self.db_path, reports_dir=self.reports_dir
        )
        path_30, _ = reporting.generate_weekly_review(
            days=30, db_file=self.db_path, reports_dir=self.reports_dir
        )

        today = datetime.now().strftime("%Y%m%d")
        self.assertEqual(os.path.basename(path_14), f"weekly_review_{today}_d14.md")
        self.assertEqual(os.path.basename(path_30), f"weekly_review_{today}_d30.md")
        self.assertNotEqual(path_14, default_path)
        self.assertNotEqual(path_30, default_path)
        self.assertNotEqual(path_14, path_30)

        with open(default_path, encoding="utf-8") as f:
            default_content_after = f.read()
        self.assertEqual(default_content_before, default_content_after)  # 默认报告没被覆盖

        with open(path_14, encoding="utf-8") as f:
            self.assertIn("最近 14 天", f.read())
        with open(path_30, encoding="utf-8") as f:
            self.assertIn("最近 30 天", f.read())


class TestSkillDocExists(unittest.TestCase):
    """SKILL.md 必须存在,并覆盖运行命令、数据文件、运行边界、V1 后置项。"""

    def setUp(self):
        base = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(base, "SKILL.md"), encoding="utf-8") as f:
            self.content = f.read()

    def test_skill_md_documents_all_commands(self):
        for cmd in [
            "python main.py",
            "python main.py --once",
            "--migrate-feedback",
            "--weekly-review",
            "--days 30",
            "--refresh-signals",
            "--status",
        ]:
            self.assertIn(cmd, self.content)

    def test_skill_md_documents_data_files(self):
        for name in ["kendama.db", "daily_pool.json", "feedback.db", "reports/", "personalized_signals.md"]:
            self.assertIn(name, self.content)

    def test_skill_md_documents_boundaries_and_backlog(self):
        self.assertIn("不是故障", self.content)
        self.assertIn("模型准确率", self.content)
        for backlog in ["关注卖家", "item_id", "embedding", "Agent"]:
            self.assertIn(backlog, self.content)


class TestReadmePointsToSkillDoc(unittest.TestCase):
    def test_readme_references_skill_md(self):
        base = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(base, "README.md"), encoding="utf-8") as f:
            content = f.read()
        self.assertIn("SKILL.md", content)


if __name__ == "__main__":
    unittest.main()
