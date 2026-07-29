"""Тесты для прокси-хелперов control_bot.py."""
import os
import unittest

os.environ.setdefault("BOT_TOKEN", "8923014188:AAHvNW2B5fin2XCmbVhlaLNjWhLwI3JhZ90")
os.environ.setdefault("PROXY_URL", "http://user:pass@example.com:8080")

import control_bot as cb


class ProxyHelpersTestCase(unittest.TestCase):
    def test_prepare_curl_cffi_proxy_parses_auth(self):
        px = {"http": "http://u:p@host:80", "https": "http://u:p@host:80"}
        proxy_cfg, auth = cb._prepare_curl_cffi_proxy(px)
        self.assertEqual(proxy_cfg, {"all": "http://host:80"})
        self.assertEqual(auth, ("u", "p"))

    def test_prepare_curl_cffi_proxy_no_auth(self):
        px = {"http": "http://host:80"}
        proxy_cfg, auth = cb._prepare_curl_cffi_proxy(px)
        self.assertEqual(proxy_cfg, px)
        self.assertIsNone(auth)

    def test_prepare_curl_cffi_proxy_socks5_auth(self):
        px = {"http": "socks5://u:p@host:1080", "https": "socks5://u:p@host:1080"}
        proxy_cfg, auth = cb._prepare_curl_cffi_proxy(px)
        self.assertEqual(proxy_cfg, {"all": "socks5://host:1080"})
        self.assertEqual(auth, ("u", "p"))

    def test_prepare_curl_cffi_proxy_empty(self):
        self.assertEqual(cb._prepare_curl_cffi_proxy(None), (None, None))
        self.assertEqual(cb._prepare_curl_cffi_proxy({}), (None, None))

    def test_mark_proxy_failed_keeps_mandatory_proxy_on_407(self):
        before = cb._proxy_auth_failed
        before_url = cb.PROXY_URL
        cb._proxy_auth_failed = False
        cb.PROXY_URL = "http://user:pass@example.com:8080"
        try:
            cb._mark_proxy_failed("CONNECT tunnel failed, response 407")
            self.assertFalse(cb._proxy_auth_failed)
        finally:
            cb._proxy_auth_failed = before
            cb.PROXY_URL = before_url

    def test_mark_proxy_failed_detects_407_without_mandatory_proxy(self):
        before = cb._proxy_auth_failed
        before_url = cb.PROXY_URL
        cb._proxy_auth_failed = False
        cb.PROXY_URL = ""
        try:
            cb._mark_proxy_failed("CONNECT tunnel failed, response 407")
            self.assertTrue(cb._proxy_auth_failed)
        finally:
            cb._proxy_auth_failed = before
            cb.PROXY_URL = before_url

    def test_avito_proxies_disabled_after_auth_fail(self):
        before = cb._proxy_auth_failed
        cb._proxy_auth_failed = True
        try:
            self.assertIsNone(cb._avito_proxies())
        finally:
            cb._proxy_auth_failed = before

    def _curl_cffi_installed(self):
        try:
            import curl_cffi  # noqa: F401
            return True
        except ImportError:
            return False

    def test_fetch_with_retry_accepts_none_impersonate(self):
        if not self._curl_cffi_installed():
            self.skipTest("curl_cffi не установлен")
        # Главное — функция не падает с TypeError из-за неизвестного аргумента.
        # Сетевой результат не важен (может быть таймаут в CI).
        try:
            cb._fetch_with_retry(
                "https://127.0.0.1:9/",
                timeout=1,
                retries=1,
                impersonate=None,
            )
        except TypeError as e:
            self.fail(f"_fetch_with_retry не принимает impersonate=None: {e}")
        except Exception:
            pass

    def test_curl_cffi_get_accepts_impersonate(self):
        if not self._curl_cffi_installed():
            self.skipTest("curl_cffi не установлен")
        try:
            cb._curl_cffi_get(
                "https://127.0.0.1:9/",
                timeout=1,
                retries=1,
                impersonate="chrome120",
            )
        except TypeError as e:
            self.fail(f"_curl_cffi_get не принимает impersonate: {e}")
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main()
