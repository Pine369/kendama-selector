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

不联网、不调用真实 DeepSeek / SiliconFlow / 飞书,不启动浏览器,不读取 .env 内容。
feedback_server.py 相关测试使用临时 SQLite 文件,不污染真实 feedback.db。
运行方式:
    python -m unittest test_offline_fixes.py -v
"""
import os
import json
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlparse, parse_qs

import main
import ai_filter
import scraper_health
import feedback_server


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
        result = main.filter_already_evaluated(items, seen)
        self.assertEqual(result, [])

    def test_same_url_different_price_is_kept(self):
        seen = {"https://a.example/item1": 18000}
        items = [{"title": "t1", "price": "¥16,000", "url": "https://a.example/item1"}]
        result = main.filter_already_evaluated(items, seen)
        self.assertEqual(len(result), 1)

    def test_new_url_is_kept(self):
        seen = {"https://a.example/item1": 18000}
        items = [{"title": "t2", "price": "¥9,000", "url": "https://b.example/item2"}]
        result = main.filter_already_evaluated(items, seen)
        self.assertEqual(len(result), 1)

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
    """feedback_server.py 的签名校验 + XSS 转义,使用临时 SQLite 文件,不碰真实 feedback.db。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "feedback_test.db")
        self._secret_patch = mock.patch.object(feedback_server, "SIGNING_SECRET", "test-secret")
        self._db_patch = mock.patch.object(feedback_server, "DB_FILE", self.db_path)
        self._secret_patch.start()
        self._db_patch.start()
        feedback_server.init_db()
        self.client = feedback_server.app.test_client()

    def tearDown(self):
        self._secret_patch.stop()
        self._db_patch.stop()
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


if __name__ == "__main__":
    unittest.main()
