from __future__ import annotations

import os
import threading
import unittest
from unittest import mock

import tikhub_bridge as bridge


OK_RAW = {"code": 200, "data": {"aweme_detail": {}}}


class FakeApi:
    def __init__(self, cleaned):
        self.cleaned = cleaned
        self.calls = 0

    def clean_one_video_data(self, raw):
        self.calls += 1
        return self.cleaned


class FakeClient:
    def __init__(self, answers, cleaned=None):
        self.answers = list(answers)
        self.calls = []
        self.douyin_api = FakeApi(cleaned if cleaned is not None else {"video_urls": ["https://v.example/1.mp4"]})
        self.tiktok_api = FakeApi({"video_urls": ["https://t.example/1.mp4"]})

    def get_data(self, url, clean_data=True):
        self.calls.append(clean_data)
        return self.answers.pop(0)


class NoWaitAbort:
    """Abort stand-in: never set, returns immediately instead of sleeping."""

    def __init__(self, stop_after=None):
        self.waits = []
        self.stop_after = stop_after

    def wait(self, seconds):
        self.waits.append(seconds)
        return self.stop_after is not None and len(self.waits) >= self.stop_after

    def is_set(self):
        return False


def douyin(url):
    return url


class ResolveVideoInfoTests(unittest.TestCase):
    def resolve(self, client, abort=None):
        retries = []
        info = bridge.resolve_video_info(
            client, "https://v.douyin.com/abc/", abort or NoWaitAbort(),
            lambda *args: retries.append(args), clean_url=douyin)
        return info, retries

    def test_first_answer_is_used_without_retry(self):
        client = FakeClient([OK_RAW])
        info, retries = self.resolve(client)
        self.assertEqual(["https://v.example/1.mp4"], info["video_urls"])
        self.assertEqual([], retries)
        self.assertEqual([False], client.calls)  # raw lookup; cleaning done here, once

    def test_tikhub_400_is_retried_then_succeeds(self):
        refused = {"detail": {"code": 400, "message": "Request failed", "message_zh": "请求失败，请重试"}}
        abort = NoWaitAbort()
        client = FakeClient([refused, OK_RAW])
        info, retries = self.resolve(client, abort)
        self.assertTrue(info["video_urls"])
        self.assertEqual([3], abort.waits)
        self.assertEqual(1, len(retries))
        attempt, attempts, delay, reason = retries[0]
        self.assertEqual((1, 3, 3), (attempt, attempts, delay))
        self.assertEqual("TikHub 返回 400：请求失败，请重试", reason)

    def test_exhausted_attempts_keep_legacy_text_and_real_reason(self):
        client = FakeClient([(None, None)] * 3)
        abort = NoWaitAbort()
        with self.assertRaises(RuntimeError) as caught:
            self.resolve(client, abort)
        message = str(caught.exception)
        self.assertIn("未获取到视频地址", message)  # callers classify on this text
        self.assertIn("接口请求异常", message)
        self.assertIn("已尝试 3 次", message)
        self.assertEqual([3, 8], abort.waits)
        self.assertEqual(3, len(client.calls))

    def test_answer_without_video_urls_is_retried(self):
        client = FakeClient([OK_RAW, OK_RAW, OK_RAW], cleaned={"video_urls": []})
        with self.assertRaises(RuntimeError) as caught:
            self.resolve(client)
        self.assertIn("接口返回中没有视频地址", str(caught.exception))

    def test_abort_stops_retrying_immediately(self):
        client = FakeClient([(None, None)] * 3)
        abort = NoWaitAbort(stop_after=1)
        with self.assertRaises(RuntimeError) as caught:
            self.resolve(client, abort)
        self.assertEqual(1, len(client.calls))
        self.assertIn("已尝试 1 次", str(caught.exception))

    def test_tiktok_links_use_tiktok_cleaner(self):
        client = FakeClient([OK_RAW])
        info = bridge.resolve_video_info(
            client, "https://www.tiktok.com/@a/video/1", NoWaitAbort(), lambda *a: None, clean_url=douyin)
        self.assertEqual(["https://t.example/1.mp4"], info["video_urls"])
        self.assertEqual(0, client.douyin_api.calls)


class DescribeResolveFailureTests(unittest.TestCase):
    def test_variants(self):
        self.assertIsNone(bridge.describe_resolve_failure(OK_RAW))
        self.assertIn("接口请求异常", bridge.describe_resolve_failure((None, None)))
        self.assertIn("接口请求异常", bridge.describe_resolve_failure(None))
        self.assertEqual("TikHub 返回 429：too many", bridge.describe_resolve_failure({"code": 429, "message": "too many"}))
        self.assertEqual("TikHub 返回 200", bridge.describe_resolve_failure({"code": 200, "data": None}))
        long = bridge.describe_resolve_failure({"code": 400, "message": "x" * 500})
        self.assertLessEqual(len(long), len("TikHub 返回 400：") + 120)


class NetworkRouteTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:10809", "NO_PROXY": "localhost,.hubstudio.vip"}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_direct_when_tikhub_answers(self):
        seen = []
        route = bridge.choose_network_route("https://api.tikhub.io", probe=lambda url: seen.append(url) or 200)
        self.assertEqual("direct", route)
        self.assertEqual(["https://api.tikhub.io/"], seen)
        self.assertEqual("*", os.environ["NO_PROXY"])
        self.assertEqual("*", os.environ["no_proxy"])

    def test_proxy_kept_when_direct_fails_or_errors(self):
        def boom(url):
            raise OSError("blocked")
        for probe in (boom, lambda url: 503):
            with self.subTest(probe=probe):
                self.assertEqual("proxy", bridge.choose_network_route("https://api.tikhub.io/", probe=probe))
                self.assertEqual("localhost,.hubstudio.vip", os.environ["NO_PROXY"])

    def test_blank_base_url_probes_default(self):
        seen = []
        bridge.choose_network_route("", probe=lambda url: seen.append(url) or 404)
        self.assertEqual([bridge.DEFAULT_API_BASE_URL + "/"], seen)

    def test_httpx_honours_wildcard_no_proxy(self):
        try:
            from httpx._utils import get_environment_proxies
        except ImportError:
            self.skipTest("httpx unavailable in this interpreter")
        with mock.patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "*"}):
            self.assertEqual({}, get_environment_proxies())


if __name__ == "__main__":
    unittest.main()
