import json
import unittest
from unittest.mock import patch
from urllib.error import URLError

import probe


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload

    def getcode(self):
        return self.status

    def read(self):
        if isinstance(self.payload, bytes):
            return self.payload
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ModelStatusTests(unittest.TestCase):
    def test_five_statuses(self):
        pricing = {"model"}
        self.assertEqual(probe.classify_model_status("missing", pricing, {"success": True, "data": {"groups": []}}), "不支持")
        self.assertEqual(probe.classify_model_status("model", pricing, {"success": True, "data": {"groups": []}}), "无数据")
        self.assertEqual(probe.classify_model_status("model", pricing, {"success": True, "data": {"groups": [{"success_rate": 90}]}}), "良好")
        self.assertEqual(probe.classify_model_status("model", pricing, {"success": True, "data": {"groups": [{"success_rate": 50}]}}), "不稳")
        self.assertEqual(probe.classify_model_status("model", pricing, {"success": True, "data": {"groups": [{"success_rate": 49.9}]}}), "较差")

    def test_success_rate_extraction(self):
        self.assertIsNone(probe.model_success_rate(None))
        self.assertIsNone(probe.model_success_rate({"success": True, "data": {"groups": []}}))
        self.assertIsNone(probe.model_success_rate(
            {"success": True, "data": {"groups": [{"success_rate": True}]}}))
        self.assertEqual(
            probe.model_success_rate({"success": True, "data": {"groups": [{"success_rate": 99.45}]}}),
            99.45,
        )

    @patch("probe.collect_domain_model_statuses")
    def test_collection_deduplicates_domain(self, collect):
        collect.return_value = {"m": "良好"}
        keys = [
            {"id": 1, "base_url": "https://EXAMPLE.com/v1"},
            {"id": 2, "base_url": "https://example.com/other/v1"},
        ]
        result = probe.collect_model_statuses(keys, ["m"])
        self.assertEqual(result, {1: {"m": "良好"}, 2: {"m": "良好"}})
        collect.assert_called_once()

    @patch("probe.collect_domain_model_details")
    @patch("probe.collect_domain_by_key")
    def test_statuses_auto_carries_rate(self, by_key, details):
        by_key.return_value = {"m": "可用", "n": "不支持"}
        details.return_value = {"m": ("良好", 99.45), "n": ("不支持", None)}
        keys = [{"id": 7, "base_url": "https://x/v1", "api_key": "sk"}]
        self.assertEqual(
            probe.collect_statuses_auto(keys, ["m", "n"]),
            {7: [("m", "良好", "可用", 99.45), ("n", "不支持", "不支持", None)]},
        )

    @patch("probe.collect_domain_model_details")
    @patch("probe.collect_domain_by_key")
    @patch("probe._get_or_secondary")
    def test_statuses_auto_fallback_uses_site_rate(self, request, by_key, details):
        # 检测名单一个都不命中 → 补选模型也要带上站点可用率
        by_key.return_value = {"m": "不支持"}
        details.return_value = {"b-1": ("良好", 96.0)}
        request.return_value = probe._JsonResponse(200, {"data": [{"id": "b-1"}]}, "")
        keys = [{"id": 8, "base_url": "https://x/v1", "api_key": "sk"}]
        self.assertEqual(
            probe.collect_statuses_auto(keys, ["m"], n=1),
            {8: [("b-1", "良好", "可用", 96.0)]},
        )

    def test_normalize_model_rows_accepts_legacy_triples(self):
        self.assertEqual(
            probe.normalize_model_rows([("m", "良好", "可用")]),
            [("m", "良好", "可用", None)],
        )


class SecondaryRelayTests(unittest.TestCase):
    def tearDown(self):
        probe.configure_secondary(enabled=False, ssh_target="", label="备用")

    def test_off_by_default(self):
        # 默认纯单机：没配 secondary 时保底链路不启用
        self.assertFalse(probe._secondary_available())
        self.assertEqual(probe.data_source_for("https://example.com/v1"), "1")

    def test_configure_secondary_from_config(self):
        probe.configure_secondary(enabled=True, ssh_target="relay-host",
                                  script_path="/opt/keypool-relay/relay_probe.py",
                                  label="备用机")
        self.assertTrue(probe.SECONDARY_ENABLED)
        self.assertEqual(probe.SECONDARY_SSH_TARGET, "relay-host")
        self.assertEqual(probe.secondary_suffix(), "（备用机）")

    @patch.object(probe, "_secondary_available", return_value=False)
    def test_secondary_disabled_result_stands(self, _avail):
        with patch.object(probe, "_check_key_once", return_value=probe.ProbeResult("dead", "401")):
            result = probe.check_key("https://x/v1", "sk")
        self.assertEqual(result.status, "dead")
        self.assertIsNone(result.note)
        self.assertFalse(result.via_secondary)

    @patch.object(probe, "_secondary_available", return_value=True)
    @patch.object(probe, "_check_key_secondary")
    def test_secondary_conflict_keeps_key_and_annotates(self, relay_check, _avail):
        # 本地判死、备用机说活着 → 以备用机保底，note 标注差异
        relay_check.return_value = probe.ProbeResult("alive", None, ())
        with patch.object(probe, "_check_key_once", return_value=probe.ProbeResult("dead", "401")):
            result = probe.check_key("https://x/v1", "sk")
        self.assertEqual(result.status, "alive")
        self.assertTrue(result.via_secondary)
        self.assertIn(probe.SECONDARY_LABEL, result.note)
        self.assertIn("可用", result.note)

    @patch.object(probe, "_secondary_available", return_value=True)
    @patch.object(probe, "_check_key_secondary")
    def test_secondary_agrees_no_annotation(self, relay_check, _avail):
        relay_check.return_value = probe.ProbeResult("dead", "401", ())
        with patch.object(probe, "_check_key_once", return_value=probe.ProbeResult("dead", "401")):
            result = probe.check_key("https://x/v1", "sk")
        self.assertEqual(result.status, "dead")
        self.assertIsNone(result.note)
        self.assertFalse(result.via_secondary)


class KeyProbeTests(unittest.TestCase):
    def setUp(self):
        # 备用机保底在这些打桩测试里关闭，避免真去 SSH 复测覆盖预期结论
        probe.configure_secondary(enabled=False, ssh_target="")

    @patch("probe._request_json")
    def test_models_200_is_alive_and_extracts_models(self, request):
        request.return_value = probe._JsonResponse(200, {"data": [{"id": "cheap-mini"}]}, "")
        result = probe.check_key("https://x/v1", "sk")
        self.assertEqual(result.status, "alive")
        self.assertEqual(result.models, ("cheap-mini",))

    @patch("probe._request_json")
    def test_401_is_kept(self, request):
        # 401 认证拒绝（无「令牌无效」文案）保留不删除（站点会瞬时误报）
        request.return_value = probe._JsonResponse(401, {}, "unauthorized")
        result = probe.check_key("https://x/v1", "sk")
        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.reason, "401")

    @patch("probe._request_json")
    def test_models_401_invalid_token_is_dead(self, request):
        request.return_value = probe._JsonResponse(
            401, {}, '{"error":{"message":"Invalid token (request id: abc)","type":"new_api_error"}}'
        )
        result = probe.check_key("https://x/v1", "sk")
        self.assertEqual(result.status, "dead")
        self.assertEqual(result.reason, "Invalid token")

    @patch("probe._request_json")
    def test_models_401_chinese_invalid_token_is_dead(self, request):
        request.return_value = probe._JsonResponse(
            401, {}, '{"error":{"code":"","message":"无效的令牌 (request id: xxx)","type":"new_api_error"}}'
        )
        result = probe.check_key("https://x/v1", "sk")
        self.assertEqual(result.status, "dead")
        self.assertEqual(result.reason, "Invalid token")

    @patch("probe._request_json")
    def test_chat_401_is_kept(self, request):
        request.side_effect = [
            probe._JsonResponse(500, {"data": [{"id": "m"}]}, "error"),
            probe._JsonResponse(401, {}, "unauthorized"),
        ]
        result = probe.check_key("https://x/v1", "sk")
        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.reason, "401")

    @patch("probe._request_json")
    def test_chat_401_invalid_token_is_dead(self, request):
        request.side_effect = [
            probe._JsonResponse(500, {"data": [{"id": "m"}]}, "error"),
            probe._JsonResponse(401, {}, '{"error":{"message":"Invalid token","type":"new_api_error"}}'),
        ]
        result = probe.check_key("https://x/v1", "sk")
        self.assertEqual(result.status, "dead")
        self.assertEqual(result.reason, "Invalid token")

    @patch("probe._request_json")
    def test_403_cloudflare_page_is_unknown(self, request):
        request.return_value = probe._JsonResponse(
            403, None, "<html>Attention Required! | Cloudflare</html>"
        )
        result = probe.check_key("https://x/v1", "sk")
        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.reason, "CF拦截")

    @patch("probe._request_json")
    def test_403_plain_is_kept(self, request):
        request.return_value = probe._JsonResponse(403, {}, "forbidden")
        result = probe.check_key("https://x/v1", "sk")
        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.reason, "403")

    @patch("probe._request_json")
    def test_chat_403_cloudflare_page_is_unknown(self, request):
        request.side_effect = [
            probe._JsonResponse(500, {"data": [{"id": "m"}]}, "error"),
            probe._JsonResponse(403, None, "<html>cf-error</html>"),
        ]
        result = probe.check_key("https://x/v1", "sk")
        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.reason, "CF拦截")

    @patch("probe._request_json", side_effect=URLError("offline"))
    def test_connection_failure_is_unknown(self, request):
        self.assertEqual(probe.check_key("https://x/v1", "sk").status, "unknown")

    @patch("probe._request_json")
    def test_chat_400_is_alive(self, request):
        request.side_effect = [
            probe._JsonResponse(500, {"data": [{"id": "m"}]}, "error"),
            probe._JsonResponse(400, {}, "wrong model"),
        ]
        self.assertEqual(probe.check_key("https://x/v1", "sk").status, "alive")

    @patch("probe._request_json")
    def test_quota_keyword_is_dead(self, request):
        request.side_effect = [
            probe._JsonResponse(500, {}, "error"),
            probe._JsonResponse(429, {}, "insufficient quota"),
        ]
        result = probe.check_key("https://x/v1", "sk")
        self.assertEqual(result.status, "dead")
        self.assertEqual(result.reason, "余额耗尽")

    @patch("probe._request_json")
    def test_chat_503_quota_word_not_dead(self, request):
        # 503 文案里含裸词「额度」，但那是服务器侧限流冷却，不是死号 → 保留
        request.side_effect = [
            probe._JsonResponse(500, {"data": [{"id": "m"}]}, "error"),
            probe._JsonResponse(
                503, {},
                "[服务器侧问题] 当前分组的上游账号都处于限流冷却或额度暂停中，"
                "这与你的账户余额和套餐额度无关，请稍后重试",
            ),
        ]
        result = probe.check_key("https://x/v1", "sk")
        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.reason, "503")

    @patch("probe._request_json")
    def test_chat_402_keyword_is_dead(self, request):
        request.side_effect = [
            probe._JsonResponse(500, {"data": [{"id": "m"}]}, "error"),
            probe._JsonResponse(402, {}, "insufficient balance"),
        ]
        result = probe.check_key("https://x/v1", "sk")
        self.assertEqual(result.status, "dead")
        self.assertEqual(result.reason, "余额耗尽")

    @patch("probe._request_json")
    def test_balance_unrestricted_usage_panel(self, request):
        request.side_effect = [
            probe._JsonResponse(404, None, "404 page not found"),
            probe._JsonResponse(
                200,
                {"mode": "unrestricted", "remaining": 24.95465613, "unit": "USD",
                 "subscription": {"daily_limit_usd": 25}},
                "",
            ),
        ]
        result = probe.probe_balance("http://example.com", "sk")
        self.assertEqual(result.text, "剩 $24.95 / 每日 $25.00")
        self.assertFalse(result.exhausted)


class ChineseDomainTests(unittest.TestCase):
    """2026-10-12 线上故障：群友投了中文域名，整轮采集崩在 latin-1。

    复现：库里存了 https://s.仙桥聚合.com ，update.py / daily.py 一跑就抛
    UnicodeEncodeError: 'latin-1' codec can't encode characters —— http.client
    用 latin-1 编码请求头，而 urllib 不会自动做 IDNA。事故连续两天，每小时
    重演一次，models 表停在故障时刻。
    """

    CN_URL = "https://s.仙桥聚合.com"

    def test_ascii_url_punycodes_chinese_host(self):
        # 中文域名必须落成 punycode，且整体是纯 ASCII
        out = probe.ascii_url(self.CN_URL + "/api/pricing")
        out.encode("ascii")                      # 不抛 = 修好了
        self.assertEqual(out, "https://s.xn--cnq39j20u6sw.com/api/pricing")

    def test_ascii_url_keeps_port_and_scheme(self):
        self.assertEqual(
            probe.ascii_url("https://s.仙桥聚合.com:8443/v1"),
            "https://s.xn--cnq39j20u6sw.com:8443/v1",
        )
        self.assertEqual(
            probe.ascii_url("http://s.仙桥聚合.com/v1"),
            "http://s.xn--cnq39j20u6sw.com/v1",
        )

    def test_ascii_url_percent_encodes_path_and_query(self):
        # 非 ASCII 路径/查询同样会炸请求头，必须一并编码
        self.assertEqual(
            probe.ascii_url("https://example.com/接口/pricing"),
            "https://example.com/%E6%8E%A5%E5%8F%A3/pricing",
        )
        self.assertEqual(
            probe.ascii_url("https://example.com/api/perf?model=深度求索"),
            "https://example.com/api/perf?model=%E6%B7%B1%E5%BA%A6%E6%B1%82%E7%B4%A2",
        )

    def test_ascii_url_leaves_ascii_urls_untouched(self):
        # 纯 ASCII 地址（含已 punycode、已百分号编码、带端口、localhost）不许被改动
        for url in (
            "https://example.com/api/pricing",
            "https://ai.baipiao.co/",
            "https://xn--already-punycode.com/x",
            "https://sub.domain.example.com:443/v1/models",
            "https://example.com/a%20b/c",
            "http://localhost:3020/onebot",
        ):
            self.assertEqual(probe.ascii_url(url), url)

    @patch("probe.urlopen")
    def test_request_uses_ascii_url_so_headers_encode(self, urlopen):
        # 端到端：_request_json 交给 urlopen 的必须是纯 ASCII 的 URL
        urlopen.return_value = FakeResponse(200, {"ok": True})
        probe._request_json(self.CN_URL + "/api/pricing", timeout=5)
        sent = urlopen.call_args[0][0]
        sent.full_url.encode("ascii")            # 不抛 = 请求头不会炸
        self.assertEqual(sent.host, "s.xn--cnq39j20u6sw.com")

    @patch("probe.urlopen")
    def test_chinese_domain_does_not_kill_the_whole_run(self, urlopen):
        # 坏域名只影响它自己：网络层捕获 UnicodeError 后按「连不上」处理
        urlopen.side_effect = UnicodeEncodeError(
            "latin-1", "s.仙桥聚合.com", 2, 6, "ordinal not in range(256)")
        result = probe.check_key(self.CN_URL + "/v1", "sk-x")
        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.reason, "连接失败")

    def test_probe_balance_survives_chinese_domain(self):
        # 余额探测同样要能构造出 ASCII 请求（真实网络不通也没关系，
        # 这里只要求不抛 UnicodeEncodeError）
        try:
            probe.probe_balance(self.CN_URL, "sk-x", timeout=3)
        except UnicodeError as exc:              # pragma: no cover
            self.fail(f"余额探测仍在中文域名上抛 UnicodeError: {exc}")
        except (URLError, OSError, TimeoutError):
            pass                                 # 网络不可达属于正常路径

if __name__ == "__main__":
    unittest.main()
