import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import bot
import db
import probe


def make_config(**overrides):
    config = {
        "onebot_url": "http://127.0.0.1:3010",
        "onebot_token": "token",
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


class ConfigTests(unittest.TestCase):
    def write(self, payload) -> Path:
        directory = Path(tempfile.mkdtemp())
        path = directory / "config.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_loads_and_defaults(self):
        config = bot.load_config(self.write(make_config()))
        self.assertEqual(config["pool_mode"], "per_group")
        self.assertEqual(bot.seq_limit_of(config), 99)

    def test_missing_field(self):
        payload = make_config()
        payload.pop("pool_mode")
        with self.assertRaises(bot.ConfigError):
            bot.load_config(self.write(payload))

    def test_bad_modes(self):
        with self.assertRaises(bot.ConfigError):
            bot.load_config(self.write(make_config(pool_mode="每群一个池")))
        with self.assertRaises(bot.ConfigError):
            bot.load_config(self.write(make_config(model_list_mode="whatever")))

    def test_group_list_must_be_ints(self):
        with self.assertRaises(bot.ConfigError):
            bot.load_config(self.write(make_config(group_whitelist=["10001"])))

    def test_seq_limit_must_be_positive(self):
        with self.assertRaises(bot.ConfigError):
            bot.load_config(self.write(make_config(seq_limit=0)))


class PoolMappingTests(unittest.TestCase):
    def test_per_group_mode(self):
        config = make_config()
        self.assertEqual(bot.pool_of(config, 10001), "10001")
        self.assertEqual(bot.pool_of(config, 10002), "10002")

    def test_shared_mode(self):
        config = make_config(pool_mode="shared")
        self.assertEqual(bot.pool_of(config, 10001), db.SHARED_POOL)
        self.assertEqual(bot.pool_of(config, 10002), db.SHARED_POOL)

    def test_models_path_per_pool_and_shared(self):
        config = make_config()
        self.assertEqual(bot.models_path_for(config, "models", "10001").name, "10001.txt")
        shared = make_config(model_list_mode="shared")
        self.assertEqual(bot.models_path_for(shared, "models", "10001").name, "shared.txt")


class EventTests(unittest.TestCase):
    def test_only_whitelist_group_and_slash_commands(self):
        config = make_config()
        event = {
            "post_type": "message", "message_type": "group", "group_id": 10001,
            "message": [{"type": "text", "data": {"text": "/列表"}}],
        }
        self.assertEqual(bot.event_command(event, config), ("列表", []))
        self.assertIsNone(bot.event_command({**event, "group_id": 99999}, config))
        self.assertIsNone(bot.event_command(
            {**event, "message": [{"type": "text", "data": {"text": "闲聊"}}]}, config))
        self.assertIsNone(bot.event_command(
            {**event, "message_type": "private"}, config))

    def test_send_group_message_payload(self):
        config = make_config()
        response = Mock()
        response.read.return_value = b"{}"
        with patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__ = lambda *_: response
            urlopen.return_value.__exit__ = lambda *_: False
            bot.send_group_message(config, 10001, "hi")
        request = urlopen.call_args[0][0]
        self.assertEqual(request.full_url, "http://127.0.0.1:3010/send_group_msg")
        self.assertEqual(request.get_header("Authorization"), "Bearer token")
        self.assertEqual(json.loads(request.data.decode("utf-8")),
                         {"group_id": 10001, "message": "hi"})


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.conn = db.connect(self.directory / "keypool.db")
        self.models_dir = self.directory / "models"
        self.config = make_config()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def service(self, **kwargs) -> bot.CommandService:
        defaults = dict(
            check_key=lambda *a, **k: probe.ProbeResult("alive", "200", ("m",)),
            probe_balance=lambda *a, **k: probe.BalanceResult("剩 $1.00 / $2.00", False),
            collect_statuses_auto=lambda rows, names, **kw: {
                row["id"]: [(name, "良好", "可用", 97.5) for name in names] for row in rows
            },
        )
        defaults.update(kwargs)
        return bot.CommandService(self.conn, self.config, self.models_dir, **defaults)


class ProvideAndListTests(ServiceTestCase):
    def test_provide_assigns_seq_per_pool(self):
        service = self.service()
        self.assertIn("已加入 #1 甲站", service.execute(
            "提供", ["甲站", "小明", "https://a/v1", "sk-a"], "10001"))
        self.assertIn("已加入 #1 乙站", service.execute(
            "提供", ["乙站", "小红", "https://b/v1", "sk-b"], "10002"))
        self.assertIn("已加入 #2 甲站2", service.execute(
            "提供", ["甲站2", "小明", "https://a2/v1", "sk-a2"], "10001"))

    def test_provide_validates_arguments_and_url(self):
        service = self.service()
        self.assertIn("用法", service.execute("提供", ["甲站"], "10001"))
        self.assertIn("http://", service.execute(
            "提供", ["甲站", "小明", "ftp://a", "sk"], "10001"))

    def test_pool_full_refuses_new_key(self):
        self.config["seq_limit"] = 2
        service = self.service()
        service.execute("提供", ["站", "人", "https://a/v1", "sk-1"], "10001")
        service.execute("提供", ["站", "人", "https://a/v1", "sk-2"], "10001")
        text = service.execute("提供", ["站", "人", "https://a/v1", "sk-3"], "10001")
        self.assertIn("池子满了", text)
        self.assertEqual(db.count_keys(self.conn, "10001"), 2)

    def test_list_only_shows_own_pool(self):
        # 检测名单用本池的 10001.txt
        bot.write_models(["m"], self.models_dir / "10001.txt")
        service = self.service()
        service.execute("提供", ["甲", "小明", "https://a/v1", "sk-a"], "10001")
        service.execute("提供", ["乙", "小红", "https://b/v1", "sk-b"], "10002")
        service.execute("更新", [], "10001")
        listing = service.execute("列表", [], "10001")
        self.assertIn("#1 甲", listing)
        self.assertNotIn("乙", listing)
        other = service.execute("列表", [], "10002")
        self.assertIn("#1 乙", other)
        self.assertNotIn("甲", other)

    def test_list_skips_empty_pool_and_marks(self):
        bot.write_models(["m"], self.models_dir / "10001.txt")
        service = self.service()
        service.execute("提供", ["甲", "小明", "https://a/v1", "sk-a"], "10001")
        self.assertIn("待检测", service.execute("列表", [], "10001"))   # 还没采集过
        service.execute("更新", [], "10001")
        self.assertIn("m【✅】可用率97%", service.execute("列表", [], "10001"))
        self.assertIn("池子是空的", service.execute("列表", [], "10002"))


class QueryAndDeleteTests(ServiceTestCase):
    def test_query_alive_shows_balance(self):
        service = self.service()
        service.execute("提供", ["甲", "小明", "https://a/v1", "sk-a"], "10001")
        text = service.execute("查询", ["1"], "10001")
        self.assertIn("#1 甲", text)
        self.assertIn("余额：剩 $1.00 / $2.00", text)
        self.assertIn("https://a/v1", text)

    def test_query_missing_seq_is_silent(self):
        service = self.service()
        self.assertIsNone(service.execute("查询", ["1"], "10001"))

    def test_query_dead_key_is_deleted(self):
        service = self.service(check_key=lambda *a, **k: probe.ProbeResult("dead", "余额耗尽"))
        service.execute("提供", ["甲", "小明", "https://a/v1", "sk-a"], "10001")
        text = service.execute("查询", ["1"], "10001")
        self.assertIn("已自动删除", text)
        self.assertIsNone(db.get_key(self.conn, "10001", 1))
        # 删掉后再查 → 静默
        self.assertIsNone(service.execute("查询", ["1"], "10001"))

    def test_query_unknown_reason_keeps_key(self):
        cases = [("CF拦截", "被CF拦截无法读取"), ("401", "拒绝访问"), ("超时", "暂时连不上")]
        for reason, expected in cases:
            with self.subTest(reason=reason):
                service = self.service(
                    check_key=lambda *a, _r=reason, **k: probe.ProbeResult("unknown", _r))
                service.execute("提供", ["甲", "小明", "https://a/v1", "sk-a"], "10001")
                text = service.execute("查询", ["1"], "10001")
                self.assertIn(expected, text)
                self.assertIsNotNone(db.get_key(self.conn, "10001", 1))
                db.delete_key(self.conn, "10001", 1)

    def test_query_exhausted_balance_is_deleted(self):
        service = self.service(
            probe_balance=lambda *a, **k: probe.BalanceResult(None, True))
        service.execute("提供", ["甲", "小明", "https://a/v1", "sk-a"], "10001")
        self.assertIn("已自动删除", service.execute("查询", ["1"], "10001"))

    def test_cross_pool_seq_is_invisible(self):
        # 每个池各自编号：101 群的 #1 在 102 群里查不到
        service = self.service()
        service.execute("提供", ["甲", "小明", "https://a/v1", "sk-a"], "10001")
        self.assertIn("#1 甲", service.execute("查询", ["1"], "10001"))
        self.assertIsNone(service.execute("查询", ["1"], "10002"))
        # 也删不掉别的群的 #
        self.assertEqual(service.execute("删除", ["1"], "10002"), "没有 #1")
        self.assertIsNotNone(db.get_key(self.conn, "10001", 1))

    def test_delete_then_seq_keeps_counting_up(self):
        # 序号一路往上走（到上限才回绕），所以删掉 #2 后新 key 拿 #4，而不是复用 #2
        service = self.service()
        for index in range(3):
            service.execute("提供", ["站", "人", "https://a/v1", f"sk-{index}"], "10001")
        self.assertIn("已删除 #2", service.execute("删除", ["2"], "10001"))
        self.assertEqual(service.execute("删除", ["2"], "10001"), "没有 #2")
        self.assertIn("已加入 #4 新站", service.execute(
            "提供", ["新站", "人", "https://a/v1", "sk-new"], "10001"))

    def test_seq_wraps_back_to_one_after_limit(self):
        # seq_limit 调小来模拟「到 99 之后回到 1」
        self.config["seq_limit"] = 3
        service = self.service()
        for index in range(3):
            service.execute("提供", ["站", "人", "https://a/v1", f"sk-{index}"], "10001")
        self.assertIn("池子满了", service.execute("提供", ["站", "人", "https://a/v1", "sk-4"], "10001"))
        # 删掉 #1 → 计数器回绕到 1，正好落到空出来的 #1
        service.execute("删除", ["1"], "10001")
        self.assertIn("已加入 #1 回来站", service.execute(
            "提供", ["回来站", "人", "https://a/v1", "sk-back"], "10001"))
        # 又满了：只有当空号存在时才可能分配
        self.assertIn("池子满了", service.execute("提供", ["站", "人", "https://a/v1", "sk-5"], "10001"))
        service.execute("删除", ["3"], "10001")
        self.assertIn("已加入 #3 补位站", service.execute(
            "提供", ["补位站", "人", "https://a/v1", "sk-fill"], "10001"))

    def test_query_all_is_silent_and_deletes_dead(self):
        service = self.service(check_key=lambda *a, **k: probe.ProbeResult("dead", "Invalid token"))
        service.execute("提供", ["甲", "小明", "https://a/v1", "sk-a"], "10001")
        self.assertIsNone(service.execute("查询全部", [], "10001"))
        self.assertEqual(db.count_keys(self.conn, "10001"), 0)

    def test_restore_command_is_gone(self):
        service = self.service()
        self.assertIn("未知指令", service.execute("恢复", ["1"], "10001"))


class ModelListTests(ServiceTestCase):
    def test_model_list_is_per_pool(self):
        service = self.service()
        self.assertIn("空的", service.execute("检测列表更新", [], "10001"))
        self.assertIn("已更新", service.execute("检测列表更新", ["a", "b"], "10001"))
        self.assertEqual(bot.read_models(self.models_dir / "10001.txt"), ["a", "b"])
        self.assertEqual(bot.read_models(self.models_dir / "10002.txt"), [])
        self.assertIn("a、b", service.execute("检测列表更新", [], "10001"))

    def test_model_list_can_be_shared(self):
        self.config["model_list_mode"] = "shared"
        service = self.service()
        service.execute("检测列表更新", ["a"], "10001")
        self.assertEqual(bot.read_models(self.models_dir / "shared.txt"), ["a"])
        self.assertIn("a", service.execute("检测列表更新", [], "10002"))

    def test_update_without_list_does_not_collect(self):
        called = []
        service = self.service(collect_statuses_auto=lambda *a, **k: called.append(1) or {})
        service.execute("提供", ["甲", "小明", "https://a/v1", "sk-a"], "10001")
        service.execute("更新", [], "10001")           # 名单为空 → 不采集
        self.assertEqual(called, [])
        self.assertIn("待检测", service.execute("列表", [], "10001"))


if __name__ == "__main__":
    unittest.main()
