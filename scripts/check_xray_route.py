"""Проверяет VLESS/SOCKS-маршрут без обращения к Авито."""

from __future__ import annotations

import json
import platform
import socket
import sys

from curl_cffi import requests


SOCKS_PROXY = "socks5h://127.0.0.1:10808"


def _ip(proxy: str | None = None) -> str:
    session = requests.Session(trust_env=False)
    try:
        kwargs = {"timeout": 20}
        if proxy:
            kwargs["proxies"] = {"http": proxy, "https": proxy}
        response = session.get(
            "https://api.ipify.org?format=json",
            **kwargs,
        )
        response.raise_for_status()
        return str(response.json().get("ip", ""))
    finally:
        session.close()


def main() -> int:
    report = {
        "hostname": socket.gethostname(),
        "os": platform.platform(),
        "port_10808": False,
        "direct_ip": "",
        "socks_ip": "",
        "ips_differ": False,
        "ok": False,
        "error": "",
    }
    try:
        with socket.create_connection(("127.0.0.1", 10808), timeout=3):
            report["port_10808"] = True
        report["direct_ip"] = _ip()
        report["socks_ip"] = _ip(SOCKS_PROXY)
        report["ips_differ"] = bool(
            report["direct_ip"]
            and report["socks_ip"]
            and report["direct_ip"] != report["socks_ip"]
        )
        report["ok"] = report["port_10808"] and report["ips_differ"]
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
