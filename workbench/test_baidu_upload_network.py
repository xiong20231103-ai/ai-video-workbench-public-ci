from __future__ import annotations

import io
import json
import socket
import unittest
import urllib.error
from unittest import mock

import baidu_upload


class Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def ok(payload=None):
    return Answer(json.dumps(payload or {"errno": 0}).encode("utf-8"))


class BaiduDirectConnectionTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(baidu_upload, "_PROXY_FALLBACK", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self):
        return baidu_upload._json_request("https://d.pcs.baidu.com/rest/2.0/pcs/superfile2", data={"a": "1"})

    def test_direct_connection_is_used_and_bypasses_system_proxy(self):
        opener = baidu_upload._DIRECT_OPENER
        self.assertFalse(any(isinstance(h, urllib.request.ProxyHandler) and h.proxies for h in opener.handlers))
        with mock.patch.object(opener, "open", return_value=ok()) as direct, \
                mock.patch("urllib.request.urlopen") as proxied:
            self.assertEqual(0, self.request()["errno"])
        direct.assert_called_once()
        proxied.assert_not_called()

    def test_refused_or_unresolvable_falls_back_to_proxy_and_stays(self):
        for reason in (ConnectionRefusedError(10061, "refused"), socket.gaierror(11001, "getaddrinfo failed")):
            with self.subTest(reason=type(reason).__name__):
                baidu_upload._PROXY_FALLBACK = False
                with mock.patch.object(baidu_upload._DIRECT_OPENER, "open", side_effect=urllib.error.URLError(reason)) as direct, \
                        mock.patch("urllib.request.urlopen", side_effect=[ok(), ok()]) as proxied:
                    self.request()
                    self.request()
                self.assertEqual(1, direct.call_count)  # later requests go straight to the proxy
                self.assertEqual(2, proxied.call_count)

    def test_timeout_after_connect_is_not_resent_through_proxy(self):
        with mock.patch.object(baidu_upload._DIRECT_OPENER, "open", side_effect=urllib.error.URLError(socket.timeout("timed out"))), \
                mock.patch("urllib.request.urlopen") as proxied:
            with self.assertRaises(baidu_upload.BaiduApiError):
                self.request()
        proxied.assert_not_called()
        self.assertFalse(baidu_upload._PROXY_FALLBACK)

    def test_http_error_from_baidu_is_reported_not_retried(self):
        error = urllib.error.HTTPError("https://pan.baidu.com", 400, "Bad", {}, io.BytesIO(b'{"errno": 31064, "errmsg": "bad"}'))
        with mock.patch.object(baidu_upload._DIRECT_OPENER, "open", side_effect=error), \
                mock.patch("urllib.request.urlopen") as proxied:
            with self.assertRaises(baidu_upload.BaiduApiError) as caught:
                self.request()
        self.assertEqual(31064, caught.exception.code)
        proxied.assert_not_called()


if __name__ == "__main__":
    unittest.main()
