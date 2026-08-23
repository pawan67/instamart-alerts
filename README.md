# instamart-alerts

Watches Swiggy Instamart search results and sends a Telegram message when a
product's discount crosses a threshold you set.

## How it works

Instamart's web API sits behind an AWS WAF JavaScript challenge, so plain HTTP
requests get a `202` and a challenge page instead of JSON. The workaround is a
one-time bootstrap: headless Chromium loads `swiggy.com/instamart`, the WAF
script runs, and the resulting `aws-waf-token` cookie is cached in
`data/session.json`. Every poll after that is a plain `httpx` call carrying that
cookie, which takes about a second. When the token goes stale, Swiggy answers
`202`/`403`, and the runner re-mints it and retries once.

Prices are per dark store, so the session is pinned to a store before searching:

| Step | Endpoint |
| --- | --- |
| area/pincode → `place_id` | `GET /api/instamart/maps/suggestions?input=` |
| `place_id` → lat/lng | `GET /api/instamart/maps/address-widgets/v2?place_id=` |
| lat/lng → store | `POST /api/instamart/home/select-location/v2` |
| search | `POST /api/instamart/search/v2?storeId=` |

`select-location/v2` does not return the store id as a field — it is only
present inside the `swiggy://…?storeId=<n>` deeplinks in the home feed it
returns, so the code takes the most frequent one.

Discount is computed as `(mrp - offerPrice) / mrp`, both read from the search
payload's Google-style Money objects (`units` rupees + `nanos`).

## Setup

```sh
uv sync
uv run playwright install chromium     # ~115 MB, once
cp .env.example .env                   # then fill it in
```

`.env` needs three values:

- `TELEGRAM_BOT_TOKEN` — from [@BotFather](https://t.me/botfather)
- `TELEGRAM_CHAT_ID` — message your bot, then read `result[].message.chat.id`
  from `https://api.telegram.org/bot<TOKEN>/getUpdates`
- `IM_AREA` — a pincode, an area name, or `"Area, City"`. This picks the dark
  store, which decides both the catalogue and the prices.

A bot cannot open a chat first, so press Start on your bot before anything will
send — otherwise Telegram answers `400 chat not found` even with a correct id.
`uv run im chat-id` prints the ids of every chat that has messaged the bot.

Check the wiring:

```sh
uv run im chat-id
uv run im test-telegram
```

## Usage

```sh
uv run im list eggs        # everything the search returns right now, by discount
uv run im check --dry-run  # one pass, prints what it would send
uv run im check            # one pass, sends for real
uv run im watch --every 15 # poll every 15 minutes until stopped
uv run im chat-id          # ids of chats that have messaged the bot
uv run im test-telegram    # send a test message
uv run im serve            # Mini App web server
uv run im bot --url https://…   # /start button that opens the Mini App
```

## Telegram Mini App

A GUI for the same settings, opened from inside Telegram.

```sh
uv run im serve                       # http://127.0.0.1:8080
cloudflared tunnel --url http://localhost:8080   # or ngrok http 8080
uv run im bot --url https://<your-tunnel>.trycloudflare.com
```

Telegram only loads Mini Apps over **HTTPS**, so a local port needs a tunnel.
`im bot` long-polls (no webhook needed), installs an **Alerts** button next to
the chat input, and replies to `/start` with a button that opens the panel.

From the panel you can change your delivery area, add and edit watches, toggle
them, pull live prices for any query, and run a check on the spot. Edits save as
you go — `watchlist.json` and the CLI read the same files, so the two stay in
sync.

Set `IM_WEBAPP_URL` in `.env` to skip passing `--url` each time.

### Security

Anyone who finds the tunnel URL can reach the server, so every endpoint requires
the Mini App's signed `initData`, verified by HMAC against your bot token and
then narrowed to your `TELEGRAM_CHAT_ID`. Requests without it get a 401. A valid
signature alone is not enough — it only proves the request came through
Telegram, not that it came from you.

`IM_WEB_DEV=1` skips that check so the UI can be opened in a normal browser.
Only use it bound to localhost.

## Watchlist

`watchlist.json` holds one entry per thing you track, and is what the Mini App
edits:

```json
{
  "watches": [
    {
      "name": "Eggs",
      "query": "eggs",
      "min_discount_pct": 65,
      "categories": ["Eggs"],
      "exclude": ["batter", "paneer"],
      "in_stock_only": true,
      "enabled": true
    }
  ]
}
```

| Field | Meaning |
| --- | --- |
| `query` | what to type into Instamart search |
| `min_discount_pct` | alert at or above this percentage off MRP |
| `categories` | keep only these Instamart categories (substring, case-insensitive) |
| `include` / `exclude` | substring filters on the product name |
| `max_price` | ignore hits above this rupee price |
| `in_stock_only` | skip out-of-stock variants (default `true`) |
| `enabled` | set `false` to park an entry without deleting it |

`categories` matters more than it looks. A search for `eggs` returns about 50
variants, but only ~21 are eggs — Instamart pads results with sponsored and
related items (idli batter, paneer, bread), and those routinely carry deeper
discounts than eggs do. Without the filter you get alerted about batter.

## Alert de-duplication

`data/alerts.json` records the price each SKU was last alerted at. A repeat
alert only goes out when something is genuinely new:

- the price drops below what was last alerted
- the deal lapsed below the threshold and came back
- the cooldown expires (`--cooldown`, default 24h)

If Telegram delivery fails, the record is rolled back so the next pass retries.

## Scheduling

systemd timer, cron, or leave `uv run im watch` running. For cron:

```
*/15 * * * * cd /mnt/newvolume/Projects/personal/instamart-alerts && uv run im check >> data/cron.log 2>&1
```

## Proxy

Set `PROXY_URL` to route both the browser bootstrap and the polling calls:

```
PROXY_URL=socks5://user:pass@host:1080
```

This does not help with the WAF challenge (that is solved in the browser, not by
IP reputation), but it is useful if you poll often enough to attract rate limits.

## Tests

```sh
uv run pytest
```

Covers price parsing against a captured response shape, the watch filters, the
de-duplication rules, `initData` verification (tampering, wrong token, replay,
wrong user), and the web API's auth gating. Nothing in the suite hits the
network.

## Notes

- Discounts vary a lot between dark stores for the same product on the same day
  — eggs topped out at 36% in Bengaluru and 39% in Delhi while the Vasai-Virar
  store ran the same NOICE pack at 75% off. Set thresholds against your own
  store; `uv run im list eggs` shows its live spread.
- `data/` holds the session token and alert history and is gitignored.
- Bump `BUILD_VERSION` in `config.py` if Swiggy starts rejecting the calls; it
  mirrors the deployed web build (`x-build-version`).
