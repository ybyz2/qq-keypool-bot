import unittest

import db


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_schema_pragmas(self):
        self.assertEqual(self.conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
        self.assertEqual(self.conn.execute("PRAGMA journal_mode").fetchone()[0], "memory")

    def test_add_list_and_delete_key(self):
        seq = db.next_seq(self.conn, "10001")
        self.assertEqual(seq, 1)
        key_id = db.add_key(self.conn, "10001", seq, "站", "人", "https://x/v1", "sk")
        self.assertEqual(db.count_keys(self.conn, "10001"), 1)
        self.assertEqual(db.list_keys(self.conn, "10001")[0]["id"], key_id)
        self.assertTrue(db.delete_key(self.conn, "10001", seq))
        self.assertIsNone(db.get_key(self.conn, "10001", seq))
        self.assertFalse(db.delete_key(self.conn, "10001", seq))

    def test_seq_starts_at_one_per_pool_and_is_isolated(self):
        # 每个池各自从 #1 开始，互不影响
        for pool in ("10001", "10002"):
            for expected in (1, 2, 3):
                seq = db.next_seq(self.conn, pool)
                self.assertEqual(seq, expected)
                db.add_key(self.conn, pool, seq, "站", "人", "https://x/v1", "sk")
        self.assertEqual([row["seq"] for row in db.list_keys(self.conn, "10001")], [1, 2, 3])
        self.assertEqual([row["seq"] for row in db.list_keys(self.conn, "10002")], [1, 2, 3])
        self.assertIsNone(db.get_key(self.conn, "10002", 4))
        self.assertEqual(db.list_pools(self.conn), ["10001", "10002"])

    def test_seq_wraps_after_limit_and_reuses_free_numbers(self):
        # 上限 99：到顶后回绕找空号；空号会被复用
        limit = 5
        for expected in range(1, limit + 1):
            seq = db.next_seq(self.conn, "10001", limit)
            self.assertEqual(seq, expected)
            db.add_key(self.conn, "10001", seq, "站", "人", "https://x/v1", f"sk-{seq}")
        # 满了
        self.assertIsNone(db.next_seq(self.conn, "10001", limit))
        # 删掉中间的 #3 → 下一个新 key 从「最大号+1」回绕，落到 #3
        self.assertTrue(db.delete_key(self.conn, "10001", 3))
        self.assertEqual(db.next_seq(self.conn, "10001", limit), 3)
        db.add_key(self.conn, "10001", 3, "站2", "人2", "https://x/v1", "sk-新")
        self.assertIsNone(db.next_seq(self.conn, "10001", limit))

    def test_seq_wrap_prefers_next_number_after_max(self):
        limit = 4
        for seq in (1, 2, 4):
            db.add_key(self.conn, "10001", seq, "站", "人", "https://x/v1", "sk")
        # 最大号是 4 → 从 1 开始找空号（在 shared 池里也一样）
        self.assertEqual(db.next_seq(self.conn, "10001", limit), 3)
        db.add_key(self.conn, "10001", 3, "站", "人", "https://x/v1", "sk")
        self.assertIsNone(db.next_seq(self.conn, "10001", limit))

    def test_models_round_trip_with_rate(self):
        key_id = db.add_key(self.conn, "10001", 1, "站", "人", "https://x/v1", "sk")
        db.write_key_models(
            self.conn, key_id,
            [("a", "良好", "可用", 99.45), ("b", "无数据", "可用", None)],
            "2026-09-13 18:00", source="1",
        )
        rows = {row["model_name"]: row for row in db.get_models(self.conn, key_id)}
        self.assertEqual(rows["a"]["rate"], 99.45)
        self.assertEqual(rows["a"]["key_status"], "可用")
        self.assertEqual(rows["a"]["source"], "1")
        self.assertIsNone(rows["b"]["rate"])
        self.assertEqual(db.latest_collected_at(self.conn, "10001"), "2026-09-13 18:00")
        self.assertEqual(db.model_statuses(self.conn, key_id), {"a": "良好", "b": "无数据"})

    def test_delete_key_removes_models_too(self):
        key_id = db.add_key(self.conn, "10001", 1, "站", "人", "https://x/v1", "sk")
        db.replace_models(self.conn, key_id, {"a": "良好"}, "2026-09-13 18:00")
        self.assertTrue(db.delete_key(self.conn, "10001", 1))
        self.assertEqual(db.get_models(self.conn, key_id), [])
        self.assertEqual(db.count_keys(self.conn), 0)

    def test_today_providers_counts_per_pool(self):
        db.add_key(self.conn, "10001", 1, "站", "小明", "https://x/v1", "sk")
        db.add_key(self.conn, "10001", 2, "站", "小明", "https://x/v1", "sk")
        db.add_key(self.conn, "10002", 1, "站", "小红", "https://x/v1", "sk")
        rows = db.today_providers(self.conn, "10001")
        self.assertEqual([(row["nickname"], row["cnt"]) for row in rows], [("小明", 2)])
        self.assertEqual(db.count_keys(self.conn, "10001"), 2)
        self.assertEqual(db.count_keys(self.conn, "10002"), 1)


if __name__ == "__main__":
    unittest.main()
