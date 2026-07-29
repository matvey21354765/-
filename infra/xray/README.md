# Xray для Авито

Этот файл — только шаблон. Реальные UUID, Reality public key, shortId,
serverName и адрес сервера нельзя сохранять в Git.

На Linux VPS положите реальный конфиг в `/etc/xray/config.json`, выдайте доступ
отдельному пользователю `xray` и проверьте:

```bash
sudo -u xray /usr/local/bin/xray run -test -config /etc/xray/config.json
```

SOCKS inbound обязан слушать только `127.0.0.1:10808`. После запуска выполните:

```bash
python3 scripts/check_xray_route.py
```

Если бот работает в контейнере, `127.0.0.1` контейнера не является localhost
VPS. Используйте отдельный Xray-контейнер в той же private Docker network и
задайте `AVITO_SOCKS_PROXY=socks5h://xray:10808`; публичный port mapping запрещён.
