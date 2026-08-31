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
- `TELEGRAM_CHAT_ID` — one id, or several separated by commas. Message your bot,
  then read `result[].message.chat.id` from
  `https://api.telegram.org/bot<TOKEN>/getUpdates`
- `IM_AREA` — a pincode, an area name, or `"Area, City"`. This picks the dark
  store, which decides both the catalogue and the prices.

A bot cannot open a chat first, so press Start on your bot before anything will
send — otherwise Telegram answers `400 chat not found` even with a correct id.
`uv run im chat-id` prints the ids of every chat that has messaged the bot.

### Sending to more than one person

`TELEGRAM_CHAT_ID` takes a list. Every alert goes to every id on it:

```
TELEGRAM_CHAT_ID=958113963, 123456789
```

Commas, spaces and newlines all separate; duplicates are dropped. The same field
in the control panel is a chip input, so you can add people by clicking them out
of **find**.

Three kinds of id work:

| Id | What it is | How to get it |
| --- | --- | --- |
| `958113963` | a person | they press Start on the bot, then `im chat-id` |
| `-1001234567890` | a group or private channel | add the bot to it, then `im chat-id` |
| `@my_alerts` | a public channel | add the bot as an administrator |

Everyone on the list must have started the bot (or, for a channel, have the bot
added) before they can be sent to. A send counts as successful when **at least
one** recipient got it: one wrong id would otherwise roll the alert back and
re-send it to the healthy chats on every pass afterwards. Whoever missed out is
named in the log, and `im test-telegram` reports them one by one.

Everyone on the list can also open the Mini App — `initData` is narrowed to the
whole list, not just the first id.

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
uv run im test-telegram    # send a test message to every recipient
uv run im web              # standalone control panel in a browser
uv run im serve            # Mini App web server
uv run im bot --url https://…   # /start button that opens the Mini App
```

## Control panel

A standalone web app — an ordinary browser page, nothing to do with Telegram.
It is the easiest way to set the thing up, and the only place you can watch it
work.

```sh
uv run im web            # http://127.0.0.1:8090
```

From it you can:

- **paste the bot token and the recipients** and save them without touching
  `.env`. `find` lists every chat that has messaged the bot, so you add people by
  clicking them instead of digging through `getUpdates`. Add as many as you like
  — every alert goes to all of them.
- **send a test alert** and see, per recipient, who got it and exactly why
  Telegram refused anyone who didn't.
- **edit search terms** — threshold, categories, include/exclude filters, max
  price, in-stock-only — and hit `prices` on any watch to pull live Instamart
  results and see which ones the filters actually keep.
- **run a check or a dry run** on the spot, or flip the poller on and leave the
  panel running as the scheduler.
- **set the proxy and the Swiggy build version**, the two things you reach for
  when Instamart starts refusing the calls.
- **read every log line the process emits**, live, filtered by level or text.
  History survives a restart (`data/console.log`), and finished runs are pinned
  into the stream as result blocks.

Anything saved here lands in `data/settings.json`, which is layered over `.env`
and read by the CLI too — so the panel and `uv run im check` never disagree.

### Getting in

The panel can write your bot token, so it does not answer strangers:

| | |
| --- | --- |
| `IM_WEB_PASSWORD` unset | only loopback clients are served; anything else gets a 403 |
| `IM_WEB_PASSWORD=…` set | a login page, then a signed cookie good for 30 days |

`im web` refuses to bind anything but `127.0.0.1` until a password is set. To
reach it from your phone, either set one, or leave it on loopback and forward
the port:

```sh
ssh -L 8090:127.0.0.1:8090 you@yourbox     # then open http://127.0.0.1:8090
```

There is no TLS here. On a public address, put it behind a reverse proxy that
terminates HTTPS — a session cookie over plain HTTP is a session cookie anyone
on the path can lift.

## Telegram Mini App

The same settings again, but opened from inside Telegram rather than a browser.
It exists because it is convenient on a phone; `im web` above is the fuller
tool.

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
then narrowed to the ids in `TELEGRAM_CHAT_ID`. Requests without it get a 401. A
valid signature alone is not enough — it only proves the request came through
Telegram, not that it came from someone on your list.

`IM_WEB_DEV=1` skips that check so the UI can be opened in a normal browser.
Only use it bound to localhost.

## Deploying

The panel is one process: it serves the page, the JSON API that page calls, and
the polling loop. The page fetches `/api/…` on its own origin, so there is no
second service to route and nothing to proxy between. **One domain, pointed at
`panel` on port 8090, is the whole deployment.**

```sh
IM_WEB_PASSWORD=… docker compose up -d      # starts `panel` and nothing else
```

`docker-compose.yml` also carries the Mini App, its bot, and the standalone
`im watch` poller, but they sit behind profiles (`--profile miniapp`,
`--profile cli-poller`) so a plain `up` can never start a second poller racing
the panel's own over the same WAF session.

### On Dokploy

1. Create a **Compose** application from this repo.
2. Under **Environment**, set at least `IM_WEB_PASSWORD`. Nothing else is
   required — the bot token, chat ids and area are all set from the panel
   afterwards and stored in the data volume. If you would rather bake them in,
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` and `IM_AREA` still work.
3. Under **Domains**, add one domain → service `panel` → port `8090`, HTTPS on.
4. Deploy, open the domain, and sign in with the password from step 2.

`.dockerignore` keeps `.env` and `data/` out of the image, so a pushed image
never carries your token and a fresh volume is never seeded from one. Whatever
you set in the platform's environment still seeds the install; everything you
save in the panel afterwards lives on the volume. Starting with only
`IM_WEB_PASSWORD` set and filling in the rest from the panel works fine.

Things that bite on a first deploy:

- **The container exits immediately.** `im web` refuses to bind `0.0.0.0`
  without `IM_WEB_PASSWORD`, because the panel can write your bot token. The
  logs say so. Set it and redeploy.
- **`shm_size: 1gb`** is already in the compose file and needs to stay.
  Chromium dies during the WAF bootstrap on the default 64 MB `/dev/shm`, and
  the failure looks like an unrelated browser crash.
- **Keep the `data-volume`.** It holds the WAF session, the alert history, the
  watchlist and everything the panel saves. Losing it means re-entering the
  token and re-alerting every live deal once.
- The session cookie is marked `Secure` automatically when the request arrives
  with `X-Forwarded-Proto: https`, which Dokploy's proxy sets. The client IP is
  deliberately *not* read from headers, so a forwarded header can never
  impersonate a localhost client.

`GET /api/health` is unauthenticated and answers `{"ok": true, "poller": …}` —
the compose healthcheck uses it, and it works for an uptime monitor too.

## What lives where

Everything on the left is set in the panel and stored in
`data/settings.json`; everything on the right has to come from the environment,
because it is needed before there is a panel to log into.

| Set from the panel | Set in the environment |
| --- | --- |
| bot token | `IM_WEB_PASSWORD` — the panel's own password |
| recipients (chat ids) | `IM_DATA_DIR` — where state is written |
| delivery area | `IM_WATCHLIST` — path to the watchlist |
| watches: query, threshold, categories, include/exclude, max price, in-stock | `IM_HEADLESS=0` — watch the bootstrap browser |
| poll interval, re-alert cooldown | `IM_WEB_DEV=1` — Mini App only |
| poller on/off | `IM_WEBAPP_URL` — Mini App only |
| proxy URL | |
| Swiggy build version | |

`.env` still works for everything on the left, and is read as the floor —
anything saved in the panel wins over it.

## Watchlist

`watchlist.json` holds one entry per thing you track, and is what the control
panel edits:

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

## Fetch mode

The cheap path hands the browser's `aws-waf-token` to `httpx` and every poll
after the bootstrap costs about a second. That works until the WAF starts
checking more than the cookie — the TLS handshake and header order are the other
half of what it fingerprints, and `httpx`'s do not look like Chromium's. On a
home connection the mismatch is tolerated. On a datacenter IP it often is not,
and the symptom is unmistakable: a token minted seconds ago answered `202` on
its very first use.

**Browser mode** removes the mismatch. Calls go out as `fetch()` from inside the
page that solved the challenge, so the fingerprint, the cookies and the JS
environment are all the ones the token was issued to.

| Mode | What it does | Cost |
| --- | --- | --- |
| `auto` (default) | httpx first, browser on the last retry | 1s normally |
| `http` | never opens a browser to fetch | 1s |
| `browser` | every call from inside the page | ~5s a pass |

Set it under **Connection** in the panel, or `IM_TRANSPORT=browser` in the
environment. `auto` means a healthy install pays nothing for the fallback and a
blocked one still gets its results.

## When the bootstrap fails

`no aws-waf-token: …` means headless Chromium loaded the page but the WAF never
handed over a cookie. The message names which of four things happened, and the
panel keeps a screenshot of the page Chromium was actually looking at, under
**Connection**. Read that before changing anything:

| What the log says | What it means | What fixes it |
| --- | --- | --- |
| served an interactive CAPTCHA | the WAF wants a human. Datacenter IPs get this almost every time | a residential `PROXY_URL` |
| refused the page outright | the IP is blocked, not challenged | a different egress |
| challenge script never finished | it was still working when time ran out | raise the bootstrap wait |
| set no cookies at all | the page was never reached | egress, DNS, proxy config |
| handed over a token but never let the real page load | the token was issued and then not honoured — on a rotating proxy, the exit IP changed underneath it | a sticky proxy session |

A bootstrap only counts as successful once the challenge has *cleared*: the WAF
sets `aws-waf-token`, then reloads the page, and only the reload proves the
token was accepted. A session holding nothing but the token is the interstitial,
not the site — it will be answered `202` on its first use. The console prints
the cookie count for exactly this reason: one cookie is a failure wearing a
success's clothes, a cleared session carries fifteen or so.

A second symptom is worth knowing: the bootstrap can *succeed* and the very next
call still get `HTTP 202`. That is the WAF issuing a token it has already
decided to re-challenge — the same IP-reputation problem wearing a different
hat, not a bug in the retry ladder. A home connection clears it routinely; a
cloud VM often does not, which is why `PROXY_URL` and the bootstrap wait are
both settable from the panel without a redeploy.

## Proxy

Set it in the panel under **Connection**, or seed it from `.env`:

```
PROXY_URL=socks5://user:pass@host:1080
```

It routes both the browser bootstrap and the polling calls.

**Use a sticky session.** The WAF ties the token to the IP that solved the
challenge, so a rotating proxy that hands out a new exit between the challenge
and the reload invalidates the token before it is ever used — every time, which
reads as a permanent block rather than a configuration problem. Most providers
offer stickiness as a username suffix or a dedicated port; check yours. The
symptom to watch for in the console is `challenge not cleared` alongside a
cookie count of one.

## Tests

```sh
uv run pytest
```

Covers price parsing against a captured response shape, the watch filters, the
de-duplication rules, `initData` verification (tampering, wrong token, replay,
wrong user), and both web APIs' auth gating — including that the panel never
echoes the bot token back and never overwrites it with its own mask. Fan-out
delivery is covered against a stubbed Telegram: every recipient is tried, a
rejection does not stop the ones after it, and a partial failure still counts as
delivered. Nothing in the suite hits the network.

## Notes

- Discounts vary a lot between dark stores for the same product on the same day
  — eggs topped out at 36% in Bengaluru and 39% in Delhi while the Vasai-Virar
  store ran the same NOICE pack at 75% off. Set thresholds against your own
  store; `uv run im list eggs` shows its live spread.
- `data/` holds the session token, alert history, panel settings and the console
  log, and is gitignored. `settings.json` is written owner-only — it can hold a
  bot token.
- If Swiggy starts rejecting the calls, bump the build version under
  **Connection** in the panel — it mirrors their deployed web build
  (`x-build-version`), and saving it drops the pinned session so the next call
  goes out with the new header. `BUILD_VERSION` in `config.py` is only the
  fallback, so a deployed install can be fixed without a redeploy.
