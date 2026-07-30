# AdsPower Avito parser-worker

The worker runs on the computer or VPS where AdsPower is installed. Railway
must never start AdsPower or expose its Local API.

## Installation

Install Python 3.12+, create a virtual environment and install dependencies:

```bash
python -m venv .venv
```

Windows:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
```

Linux:

```bash
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

The downloaded Playwright browser is not used as a replacement for the
AdsPower profile. Playwright attaches to the already running AdsPower browser
over CDP and uses its existing `BrowserContext`.

## AdsPower setup

1. Start AdsPower manually.
2. Create or select a browser profile with the required proxy and fingerprint.
3. Open Avito in that profile and sign in manually.
4. Ensure no CAPTCHA or restriction page is present.
5. Enable AdsPower Local API and copy the profile ID.
6. If AdsPower API security verification is enabled, create an API key.

AdsPower Local API must remain bound locally and must not be exposed to the
Internet.

## Worker environment

```env
ADSPOWER_API_URL=http://127.0.0.1:50325
ADSPOWER_PROFILE_ID=profile-id
ADSPOWER_API_KEY=
AVITO_REQUIRE_AUTH=true
AVITO_STOP_PROFILE_ON_EXIT=false
AVITO_MAX_PAGES=2
AVITO_MAX_ITEMS=50
AVITO_DETAIL_MODE=full
AVITO_DETAIL_CONCURRENCY=1
AVITO_SEARCH_DEADLINE=180
AVITO_WORKER_HOST=127.0.0.1
AVITO_WORKER_PORT=8081
AVITO_WORKER_TOKEN=generate-a-long-random-secret
```

Do not store real tokens, cookies, proxy credentials or CDP endpoints in Git.

## Run

Windows:

```powershell
python avito_worker.py
python scripts\debug_adspower_avito.py --city krasnodar --price-max 1000000 --max-items 10
```

Linux:

```bash
python avito_worker.py
python scripts/debug_adspower_avito.py --city krasnodar --price-max 1000000 --max-items 10
```

## Secure Railway connection

Use Tailscale, a private VPN, an HTTPS reverse proxy with access control, or a
secured tunnel. Publish only the worker HTTP API. Never publish port `50325`.

Railway variables after the local diagnostic succeeds:

```env
AVITO_PROVIDER=adspower_worker
AVITO_WORKER_URL=https://private-worker.example
AVITO_WORKER_TOKEN=same-worker-token
AVITO_WORKER_TIMEOUT_SECONDS=190
```

Until then keep:

```env
AVITO_PROVIDER=disabled
```

## Health and API

```bash
curl http://127.0.0.1:8081/health
curl -X POST http://127.0.0.1:8081/v1/avito/search \
  -H "Authorization: Bearer TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"city":"krasnodar","price_max":1000000,"max_items":10}'
```

## Troubleshooting

- `adspower_unavailable`: start AdsPower and verify `ADSPOWER_API_URL`.
- `profile_not_found`: copy the profile ID again.
- `auth_required`: sign in to Avito manually in the selected profile.
- `captcha`: stop automated searches and inspect the profile manually. The
  worker does not solve CAPTCHA.
- `browser_error`: update AdsPower, verify the returned CDP endpoint, and
  ensure the profile has an existing browser context.
- `parser_changed`: run the diagnostic and update only centralized selectors
  under `avito_adspower/selectors/`.

The integration is not production-confirmed until the diagnostic reports an
existing context, no CAPTCHA, real URLs, numeric prices and parsed listings.
