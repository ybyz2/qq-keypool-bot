import unittest

import commands


class CommandParsingTests(unittest.TestCase):
    def test_all_commands(self):
        cases = {
            "/帮助": ("帮助", []),
            "/列表": ("列表", []),
            "/查询 3": ("查询", ["3"]),
            "/提供 站 昵称 https://x/v1 sk-x": ("提供", ["站", "昵称", "https://x/v1", "sk-x"]),
            "/删除 2": ("删除", ["2"]),
            "/检测列表更新 a b c": ("检测列表更新", ["a", "b", "c"]),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(commands.parse_command(text), expected)

    def test_number_glued_to_command(self):
        cases = {
            "/列表2": ("列表", ["2"]),
            "/查询3": ("查询", ["3"]),
            "/删除12": ("删除", ["12"]),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(commands.parse_command(text), expected)

    def test_unknown_and_non_command(self):
        self.assertEqual(commands.parse_command("/未知 x"), ("未知", ["x"]))
        self.assertIsNone(commands.parse_command("普通消息"))
        # 已下线的 /恢复 现在只是普通未知指令
        self.assertEqual(commands.parse_command("/恢复 1"), ("恢复", ["1"]))

    def test_strip_multiple_at_and_at_in_middle(self):
        message = [
            {"type": "text", "data": {"text": " /查"}},
            {"type": "at", "data": {"qq": "123"}},
            {"type": "text", "data": {"text": "询 7 "}},
            {"type": "at", "data": {"qq": "999"}},
        ]
        self.assertEqual(commands.strip_at_segments(message, 123), "/查询 7")
        self.assertEqual(commands.parse_event_command(message, 123), ("查询", ["7"]))

    def test_insufficient_arguments_are_preserved_for_dispatch(self):
        self.assertEqual(commands.parse_command("/提供 站"), ("提供", ["站"]))
        self.assertEqual(commands.parse_command("/查询"), ("查询", []))


class RenderingTests(unittest.TestCase):
    def test_help_snapshot(self):
        text = commands.render_help()
        for token in ("/列表", "/查询", "/更新", "/提供", "/删除", "/检测列表更新", "/帮助"):
            self.assertIn(token, text)
        self.assertNotIn("/恢复", text)
        self.assertIn("99", text)          # 序号上限写在帮助里
        self.assertIn("整体覆盖", text)

    def test_empty_pool_snapshot(self):
        text = commands.render_list([], ["a"], None)
        self.assertIn("池子是空的", text)
        self.assertIn("/提供", text)

    def test_list_shows_rate_marks(self):
        keys = [{
            "id": 1, "seq": 4, "site": "站B", "nickname": "阿花",
            "models": [
                {"model_name": "a", "status": "良好", "key_status": "可用", "rate": 99.45},
                {"model_name": "b", "status": "不稳", "key_status": "可用", "rate": 60.0},
                {"model_name": "c", "status": "较差", "key_status": "可用", "rate": 59.6},
                {"model_name": "d", "status": "较差", "key_status": "可用", "rate": 12.4},
                {"model_name": "e", "status": "无数据", "key_status": "可用", "rate": None},
                {"model_name": "f", "status": "不支持", "key_status": "不支持", "rate": None},
            ],
        }]
        text = commands.render_list(keys, ["a", "b", "c", "d", "e", "f"], None)
        self.assertIn("#4 站B", text)
        self.assertIn("a【✅】可用率99%", text)
        self.assertIn("b【✅】可用率60%", text)      # 60 本身算达标
        self.assertIn("c【❌】可用率59%", text)      # 59.6 截尾显示 59，不会出现「❌ 60%」
        self.assertIn("d【❌】可用率12%", text)
        self.assertIn("e【✅】", text)               # 采不到可用率也要带勾、不写百分比
        self.assertNotIn("f【", text)               # key 级「不支持」的模型不展示
        self.assertNotIn("无数据", text)

    def test_list_states_and_source_suffix(self):
        keys = [
            {"id": 1, "seq": 1, "site": "甲", "nickname": "人", "models": [], "source": "1"},
            {"id": 2, "seq": 2, "site": "乙", "nickname": "人", "source": "2",
             "models": [{"model_name": "m", "status": "CF拦截", "key_status": "CF拦截"}]},
            {"id": 3, "seq": 3, "site": "丙", "nickname": "人",
             "models": [{"model_name": "m", "status": "良好", "key_status": "不支持"}]},
        ]
        text = commands.render_list(keys, ["m"], "2026-09-13 18:00")
        self.assertIn("待检测", text)
        self.assertIn("被CF拦截无法读取", text)
        self.assertIn("乙（备用）", text)      # 数据来自备用机 → 标注来源
        self.assertNotIn("甲（备用）", text)
        self.assertIn("无可用模型", text)
        self.assertIn("更新于 09-13 18:00", text)
        self.assertIn("第 1/1 页", text)

    def test_list_pagination(self):
        keys = [{"id": i, "seq": i, "site": f"站{i}", "nickname": "人", "models": []}
                for i in range(1, 8)]
        first = commands.render_list(keys, ["m"], None, page=1)
        second = commands.render_list(keys, ["m"], None, page=2)
        self.assertIn("#1 站1", first)
        self.assertNotIn("#6 站6", first)
        self.assertIn("#6 站6", second)
        self.assertIn("第 2/2 页", second)
        # 超界自动钳到最后一页
        self.assertIn("第 2/2 页", commands.render_list(keys, ["m"], None, page=99))

    def test_query_render(self):
        key = {"seq": 3, "site": "站名A", "base_url": "https://xxx/v1", "api_key": "sk-xxxxxxxx"}
        ok = commands.render_query(key, "剩 $12.40 / $20.00")
        self.assertIn("#3 站名A", ok)
        self.assertIn("余额：剩 $12.40 / $20.00", ok)
        self.assertIn("https://xxx/v1", ok)
        self.assertIn("sk-xxxxxxxx", ok)
        self.assertIn("无接口", commands.render_query(key, None))
        dead = commands.render_query(key, None, "⛔ 检测失败（余额耗尽）— 已自动删除")
        self.assertIn("检测失败", dead)

    def test_simple_renders(self):
        self.assertIn("已加入 #7", commands.render_key_added(7, "某站"))
        self.assertIn("某站", commands.render_key_added(7, "某站"))
        self.assertIn("已删除 #7", commands.render_deleted(7))
        self.assertIn("99", commands.render_pool_full(99))
        self.assertIn("池子满了", commands.render_pool_full(99))
        self.assertIn("检测名单已更新", commands.render_model_update(["a", "b"]))
        self.assertIn("空的", commands.render_model_list([]))
        self.assertIn("当前检测名单", commands.render_model_list(["a"]))
        self.assertFalse(hasattr(commands, "render_restored"))   # 恢复功能不存在


if __name__ == "__main__":
    unittest.main()
