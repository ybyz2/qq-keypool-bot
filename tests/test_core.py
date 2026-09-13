"""端到端（不联网）串联测试：db + 指令服务 + 每小时采集脚本。"""
import tempfile
import unittest
from pathlib import Path

import bot
import db
import probe
import update


def make_config(**overrides):
    config = {
        "onebot_url": "http://127.0.0.1:3010",
        "onebot_token": "",
        "listen_host": "127.0.0.1",
        "listen_port": 3020,
        "self_qq": 10000,
        "group_whitelist": [10001, 10002],
        "daily_group_whitelist": [10001],
        "pool_mode": "per_group",
        "model_list_mode": "per_pool",
        "insecure_tls": False,
        "http_timeout": 2,
        "seq_limit": 99,
    }
    config.update(overrides)
    return config


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.database = self.directory / "keypool.db"
        self.models_dir = self.directory / "models"
        self.config = make_config()
        self.conn = db.connect(self.database)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def service(self, **kwargs) -> bot.CommandService:
        defaults = dict(
            check_key=lambda *a, **k: probe.ProbeResult("alive", "200", ("m",)),
            probe_balance=lambda *a, **k: probe.BalanceResult("剩 $3.00 / $5.00", False),
            collect_statuses_auto=lambda keys, names, **kw: {
                row["id"]: [(name, "良好", "可用", 96.0) for name in names] for row in keys
            },
        )
        defaults.update(kwargs)
        return bot.CommandService(self.conn, self.config, self.models_dir, **defaults)

    def test_full_pool_lifecycle_snapshot(self):
        service = self.service()
        # 1) 两个群各自投 key，序号各自从 1 开始
        service.execute("提供", ["甲中转", "小明", "https://a.example/v1", "sk-a"], "10001")
        service.execute("提供", ["乙中转", "小红", "https://b.example/v1", "sk-b"], "10001")
        service.execute("提供", ["丙中转", "小刚", "https://c.example/v1", "sk-c"], "10002")

        # 2) 10001 群设置检测名单并采集（手动 /更新）
        service.execute("检测列表更新", ["alpha", "beta"], "10001")
        service.execute("更新", [], "10001")

        # 3) 采集结果落库：三列（status/key_status/rate）都在
        rows = db.get_models(self.conn, db.get_key(self.conn, "10001", 1)["id"])
        self.assertEqual({row["model_name"] for row in rows}, {"alpha", "beta"})
        self.assertEqual(db.get_key(self.conn, "10001", 1)["site"], "甲中转")

        # 4) /列表 只显示本群池、带勾叉、带页码
        listing = service.execute("列表", [], "10001")
        self.assertIn("#1 甲中转", listing)
        self.assertIn("#2 乙中转", listing)
        self.assertNotIn("丙中转", listing)
        self.assertIn("第 1/1 页", listing)

        # 5) 另一个群看不到 10001 的序号（10002 池里只有它自己的 #1）
        self.assertIsNone(service.execute("查询", ["9"], "10002"))
        self.assertEqual(service.execute("删除", ["2"], "10002"), "没有 #2")

        # 6) 删除不立刻回收序号：计数器继续往上走（到上限 99 才回绕），新 key 拿 #3
        self.assertIn("已删除 #1", service.execute("删除", ["1"], "10001"))
        service.execute("提供", ["新站", "阿强", "https://d.example/v1", "sk-d"], "10001")
        self.assertEqual(db.get_key(self.conn, "10001", 3)["site"], "新站")
        self.assertIsNone(db.get_key(self.conn, "10001", 1))

        # 7) /查询 正常出余额
        text = service.execute("查询", ["2"], "10001")
        self.assertIn("余额：剩 $3.00 / $5.00", text)
        self.assertIn("https://b.example/v1", text)

    def test_hourly_update_collects_every_pool(self):
        bot.write_models(["m1", "m2"], self.models_dir / "10001.txt")
        bot.write_models(["m9"], self.models_dir / "10002.txt")
        db.add_key(self.conn, "10001", 1, "甲", "小明", "https://a.example/v1", "sk-a")
        db.add_key(self.conn, "10002", 1, "乙", "小红", "https://b.example/v1", "sk-b")
        seen: list[tuple[str, tuple[str, ...]]] = []

        def fake_collect(keys, names, **kwargs):
            seen.append((keys[0]["base_url"], tuple(names)))
            return {row["id"]: [(name, "良好", "可用", 88.0) for name in names] for row in keys}

        update.run_update(self.config, self.database, self.models_dir,
                          collect_statuses_auto=fake_collect)
        self.assertEqual([names for _, names in seen], [("m1", "m2"), ("m9",)])
        key = db.get_key(self.conn, "10002", 1)
        self.assertEqual([row["model_name"] for row in db.get_models(self.conn, key["id"])], ["m9"])

    def test_update_skips_pool_without_model_list(self):
        db.add_key(self.conn, "10001", 1, "甲", "小明", "https://a.example/v1", "sk-a")
        seen = []
        update.run_update(self.config, self.database, self.models_dir,
                          collect_statuses_auto=lambda keys, names, **kw: seen.append(names) or {})
        self.assertEqual(seen, [])

    def test_render_pool_uses_pool_models_and_label(self):
        db.add_key(self.conn, "10001", 1, "甲", "小明", "https://a.example/v1", "sk-a")
        db.add_key(self.conn, "10002", 2, "乙", "小红", "https://b.example/v1", "sk-b")
        probe.configure_secondary(enabled=True, ssh_target="relay", label="备用")
        try:
            text = bot.render_pool(self.conn, ["m"], "10001")
        finally:
            probe.configure_secondary(enabled=False, ssh_target="", label="备用")
        self.assertIn("#1 甲", text)
        self.assertNotIn("乙", text)


if __name__ == "__main__":
    unittest.main()
