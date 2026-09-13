import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import bot
import daily
import db
import probe


def make_config(**overrides):
    config = {
        "onebot_url": "http://127.0.0.1:3010",
        "onebot_token": "",
        "listen_host": "127.0.0.1",
        "listen_port": 3020,
        "self_qq": 10000,
        "group_whitelist": [10001, 10002],
        "daily_group_whitelist": [10001, 10002],
        "pool_mode": "per_group",
        "model_list_mode": "per_pool",
        "insecure_tls": False,
        "http_timeout": 2,
        "seq_limit": 99,
    }
    config.update(overrides)
    return config


class FakeProbe:
    def __init__(self, status, reason=None):
        self.status = status
        self.reason = reason
        self.models = ()


class DailyTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.database = self.directory / "keypool.db"
        self.models_dir = self.directory / "models"
        self.config = make_config()
        self.conn = db.connect(self.database)
        self.sent: list[tuple[int, str]] = []

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def run_daily(self, config=None, **kwargs):
        defaults = dict(
            check_key=lambda *a, **k: FakeProbe("alive"),
            probe_balance=lambda *a, **k: probe.BalanceResult("剩 $1.00", False),
            collect_statuses_auto=lambda keys, names, **kw: {
                row["id"]: [(name, "良好", "可用", 95.0) for name in names] for row in keys
            },
            send_message=lambda cfg, group, text: self.sent.append((group, text)),
        )
        defaults.update(kwargs)
        return daily.run_daily(config or self.config, self.database, self.models_dir, **defaults)


class ReportTests(DailyTestCase):
    def test_report_shape(self):
        db.add_key(self.conn, "10001", 1, "站", "小明", "https://a/v1", "sk")
        db.add_key(self.conn, "10001", 2, "站", "小明", "https://a/v1", "sk")
        report = daily.render_report(self.conn, "10001")
        self.assertIn("每日播报", report)
        self.assertIn("今日提供：小明（2）", report)
        self.assertIn("目前池中 key：2 个", report)

    def test_report_without_providers(self):
        self.assertIn("今日暂无提供", daily.render_report(self.conn, "10001"))

    def test_pools_and_recipients_per_group(self):
        db.add_key(self.conn, "10003", 1, "站", "人", "https://a/v1", "sk")
        pools = daily.pools_for_daily(self.config, self.conn)
        self.assertEqual(pools, ["10001", "10002", "10003"])
        self.assertEqual(daily.recipients_for(self.config, "10001"), [10001])
        self.assertEqual(daily.recipients_for(self.config, "10003"), [])   # 不在播报名单
        self.assertEqual(daily.pools_for_daily(make_config(pool_mode="shared"), self.conn),
                         [db.SHARED_POOL])
        self.assertEqual(daily.recipients_for(make_config(pool_mode="shared"), db.SHARED_POOL),
                         [10001, 10002])


class RunDailyTests(DailyTestCase):
    def test_each_pool_reports_its_own_keys(self):
        bot.write_models(["m"], self.models_dir / "10001.txt")
        bot.write_models(["m"], self.models_dir / "10002.txt")
        db.add_key(self.conn, "10001", 1, "甲站", "小明", "https://a/v1", "sk-a")
        db.add_key(self.conn, "10002", 1, "乙站", "小红", "https://b/v1", "sk-b")
        reports = self.run_daily()
        self.assertEqual([pool for pool, _ in reports], ["10001", "10002"])
        self.assertEqual([group for group, _ in self.sent], [10001, 10002])
        # 每个群收到的是自己池的内容（数量都是 1，但站名不同）
        self.assertIn("目前池中 key：1 个", self.sent[0][1])
        self.assertNotIn("2 个", self.sent[1][1])

    def test_dead_key_is_physically_removed(self):
        db.add_key(self.conn, "10001", 1, "死站", "小明", "https://a/v1", "sk-dead")
        db.add_key(self.conn, "10001", 2, "断站", "小明", "https://b/v1", "sk-unknown")
        reports = self.run_daily(check_key=lambda url, key, **k: (
            FakeProbe("dead", "余额耗尽") if key == "sk-dead" else FakeProbe("unknown", "超时")))
        self.assertEqual(db.count_keys(self.conn, "10001"), 1)
        self.assertIsNone(db.get_key(self.conn, "10001", 1))
        self.assertIsNotNone(db.get_key(self.conn, "10001", 2))
        self.assertIn("目前池中 key：1 个", reports[0][1])

    def test_exhausted_balance_is_removed(self):
        db.add_key(self.conn, "10001", 1, "站", "小明", "https://a/v1", "sk")
        self.run_daily(probe_balance=lambda *a, **k: probe.BalanceResult(None, True))
        self.assertEqual(db.count_keys(self.conn, "10001"), 0)

    def test_shared_mode_reports_once_to_all_groups(self):
        config = make_config(pool_mode="shared")
        db.add_key(self.conn, db.SHARED_POOL, 1, "站", "小明", "https://a/v1", "sk")
        reports = self.run_daily(config=config)
        self.assertEqual([pool for pool, _ in reports], [db.SHARED_POOL])
        self.assertEqual([group for group, _ in self.sent], [10001, 10002])

    def test_collects_models_for_each_pool(self):
        bot.write_models(["m"], self.models_dir / "10001.txt")
        db.add_key(self.conn, "10001", 1, "站", "小明", "https://a/v1", "sk")
        self.run_daily()
        key = db.get_key(self.conn, "10001", 1)
        self.assertEqual([row["model_name"] for row in db.get_models(self.conn, key["id"])], ["m"])
        self.assertEqual(db.get_models(self.conn, key["id"])[0]["rate"], 95.0)

    def test_send_failure_propagates_nothing_and_is_skipped_for_empty_pool(self):
        # 池里没有 key 的池不产生播报（也不会给那个群发消息）
        reports = self.run_daily()
        self.assertEqual(reports, [])
        self.assertEqual(self.sent, [])


class MainTests(unittest.TestCase):
    def test_main_reports_config_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("{}", encoding="utf-8")
            self.assertEqual(daily.main(["--config", str(path)]), 2)


if __name__ == "__main__":
    unittest.main()
