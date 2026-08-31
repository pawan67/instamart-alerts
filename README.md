# instamart-alerts

Watches Swiggy Instamart search results and sends a Telegram message when a
product's discount crosses a threshold you set.

There is a control panel — a plain browser page, nothing to do with Telegram —
where you connect the bot, edit what you're watching, and read the log live.

![The control panel](docs/panel.png)

## Quick start

```sh
uv sync
uv run playwright install chromium     # ~115 MB, once
uv run im web                          # → http://127.0.0.1:8090
```

Open that URL and the panel walks you through the rest: paste a bot token from
[@BotFather](https://t.me/botfather), press **find** to pick who gets the
alerts, set your pincode, add a search term, and hit **Dry run**. Nothing needs
to go in a config file.

If you would rather start from `.env`:

```sh
cp .env.example .env    # TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, IM_AREA
uv run im check --dry-run
```

Either way both halves read the same files, so the panel and the CLI never
disagree.

## What it looks like

Each watch expands into its filters, and **prices** pulls live Instamart results
so you can set a threshold against real numbers instead of guessing — the header
tells you how many survive your filters:

![Editing a watch, with live prices](docs/watches.png)

The console is the whole log of the process, streamed live, filterable by level
or text, with finished runs pinned in as result blocks. Every run's summary line
opens into the full breakdown — every product the watch kept, best discount
first, with the ones over your threshold picked out and the ones actually sent
flagged. That is how you tell "nothing was on offer" from "my threshold is too
high":

![The console](docs/console.png)

`copy` puts what is on screen on the clipboard and `save` writes it to a file —
both respect the filters, and both keep the columns lined up, so a run pastes
into an issue or a chat readably. History survives a restart.

## How it works

Instamart's web API sits behind an AWS WAF JavaScript challenge, so plain HTTP
requests get a `202` and a challenge page instead of JSON. The workaround is a
bootstrap: headless Chromium loads `swiggy.com/instamart`, the WAF script runs,
the page reloads, and the resulting `aws-waf-token` cookie is cached in
`data/session.json`. Polls after that are plain `httpx` calls carrying that
cookie, about a second each. When the token goes stale, Swiggy answers
`202`/`403`, and the runner re-mints and retries — escalating to
[browser mode](#fetch-mode) if it has to.

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

## Who gets the alerts

`TELEGRAM_CHAT_ID` takes a list, and every alert goes to every id on it:

```
TELEGRAM_CHAT_ID=584219307, 123456789
```

Commas, spaces and newlines all separate; duplicates are dropped. The same field
in the panel is a chip input, so **find** lets you add people by clicking them
rather than digging through `getUpdates`.

| Id | What it is | How to get it |
| --- | --- | --- |
| `584219307` | a person | they press Start on the bot, then `im chat-id` |
| `-1001234567890` | a group or private channel | add the bot to it, then `im chat-id` |
| `@my_alerts` | a public channel | add the bot as an administrator |

A bot cannot open a chat first, so everyone on the list has to press Start
before they can be sent to — otherwise Telegram answers `400 chat not found`
even with a correct id.

A send counts as successful when **at least one** recipient got it. Anything
stricter and a single wrong id would roll the whole alert back, so the next pass
would re-send it to everyone who already had it, forever. Whoever missed out is
named in the log, and `im test-telegram` reports them one at a time:

```
  sent    584219307
  FAILED  123456789

1 of 2 failed — see the errors above. Each recipient must press Start on the bot first.
```

Everyone on the list can also open the Mini App — `initData` is narrowed to the
whole list, not just the first id.

## Watchlist

`watchlist.json` holds one entry per thing you track, and is what the panel
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

### Alert de-duplication

`data/alerts.json` records the price each SKU was last alerted at. A repeat
alert only goes out when something is genuinely new:

- the price drops below what was last alerted
- the deal lapsed below the threshold and came back
- the cooldown expires (24h by default, set in the panel)

If Telegram delivery fails to everyone, the record is rolled back so the next
pass retries.

## The control panel

```sh
uv run im web            # http://127.0.0.1:8090
```

One process serves the page, the JSON API that page calls, and the polling loop.
From it you can:

- **connect Telegram** — bot token and recipients, saved without touching
  `.env`, with a test alert that reports per recipient who got it and exactly
  why Telegram refused anyone who didn't.
- **edit search terms** — threshold, categories, include/exclude, max price,
  in-stock-only — and pull live prices for any query to see what your filters
  actually keep.
- **run a check or a dry run** on the spot, or flip the poller on and leave the
  panel running as the scheduler.
- **set the fetch mode, proxy and Swiggy build version** — the things you reach
  for when Instamart starts refusing the calls, all without a redeploy.
- **read every log line the process emits**, live, and expand any run into the
  full list of what it tracked. Copy or download what is on screen. History
  survives a restart in `data/console.log`.

Anything saved lands in `data/settings.json`, which is layered over `.env`.

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

The cookie is marked `Secure` automatically when the request arrives over HTTPS,
including via a proxy's `X-Forwarded-Proto`. The client IP is deliberately *not*
read from headers, so a forwarded header can never impersonate a loopback
client. There is no TLS of its own — on a public address, put it behind a proxy
that terminates HTTPS.

## Deploying

The page fetches `/api/…` on its own origin, so there is no second service to
route and nothing to proxy between. **One domain, pointed at `panel` on port
8090, is the whole deployment.**

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
   required — the bot token, recipients and area are all set from the panel
   afterwards and stored in the data volume. If you would rather bake them in,
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` and `IM_AREA` still work.
3. Under **Domains**, add one domain → service `panel` → port `8090`, HTTPS on.
4. Deploy, open the domain, and sign in with the password from step 2.

`.dockerignore` keeps `.env` and `data/` out of the image, so a pushed image
never carries your token and a fresh volume is never seeded from one.

Things that bite on a first deploy:

- **The container exits immediately.** `im web` refuses to bind `0.0.0.0`
  without `IM_WEB_PASSWORD`, because the panel can write your bot token. The
  logs say so. Set it and redeploy.
- **`shm_size: 1gb`** is already in the compose file and needs to stay.
  Chromium dies during the WAF bootstrap on the default 64 MB `/dev/shm`, and
  the failure looks like an unrelated browser crash.
- **Keep the `data-volume`.** It holds the WAF session, the alert history, the
  watchlist and everything the panel saves.

`GET /api/health` is unauthenticated and answers `{"ok": true, "poller": …}` —
the compose healthcheck uses it, and it works for an uptime monitor too.

## What lives where

Everything on the left is set in the panel and stored in `data/settings.json`;
everything on the right has to come from the environment, because it is needed
before there is a panel to log into.

| Set from the panel | Set in the environment |
| --- | --- |
| bot token | `IM_WEB_PASSWORD` — the panel's own password |
| recipients (chat ids) | `IM_DATA_DIR` — where state is written |
| delivery area | `IM_WATCHLIST` — path to the watchlist |
| watches: query, threshold, categories, include/exclude, max price, in-stock | `IM_HEADLESS=0` — watch the bootstrap browser |
| poll interval, re-alert cooldown | `IM_WEB_DEV=1` — Mini App only |
| poller on/off | `IM_WEBAPP_URL` — Mini App only |
| fetch mode, proxy | |
| Swiggy build version, bootstrap wait | |

`.env` still works for everything on the left, and is read as the floor —
anything saved in the panel wins over it.

## Fetch mode

The cheap path hands the browser's `aws-waf-token` to `httpx`, and every poll
after the bootstrap costs about a second. That works until the WAF starts
checking more than the cookie — the TLS handshake and header order are the other
half of what it fingerprints, and `httpx`'s do not look like Chromium's. On a
home connection the mismatch is tolerated; on a datacenter IP it often is not.

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

A bootstrap only counts as successful once the challenge has **cleared**: the
WAF sets `aws-waf-token`, then reloads the page, and only that reload proves the
token was accepted. A session holding nothing but the token is the interstitial,
not the site, and it will be answered `202` on its first use.

The console prints the cookie count for exactly this reason:

```
bootstrap succeeded (HTTP 200, 15 cookies)     ← cleared
challenge not cleared: … (HTTP 202, 1 cookie)  ← the interstitial
```

`challenge not cleared: …` names which of five things happened, and the panel
keeps a screenshot of the page Chromium was looking at, under **Connection**.
Read that before changing anything:

| What the log says | What it means | What fixes it |
| --- | --- | --- |
| handed over a token but never let the real page load | issued and then not honoured — usually the exit IP changing underneath it | a sticky proxy session |
| served an interactive CAPTCHA | the WAF wants a human; datacenter IPs get this often | a residential `PROXY_URL` |
| refused the page outright | the IP is blocked, not challenged | a different egress |
| challenge script never finished | still working when time ran out | raise the bootstrap wait |
| set no cookies at all | the page was never reached | egress, DNS, proxy config |

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
symptom is `challenge not cleared` alongside a cookie count of one.

A rotating exit also shows up as intermittent connection failures rather than
WAF refusals, because the tunnel itself is being rebuilt underneath you:

| In the log | What it is |
| --- | --- |
| `socksio…ProtocolError: Malformed reply` | the proxy answered the SOCKS handshake with something that is not a SOCKS reply — usually a concurrency or auth limit |
| `The handshake operation timed out` | the tunnel opened, the upstream TLS stalled |
| `UNEXPECTED_EOF_WHILE_READING` | the proxy dropped the connection mid-TLS |
| a search returning `0 results` after a clean bootstrap | the token was minted on one exit and used from another |

All four are retried on the same session, since the token was never the
problem — nothing reached Swiggy.

## Command line

```sh
uv run im web              # the control panel
uv run im list eggs        # everything the search returns right now, by discount
uv run im check --dry-run  # one pass, prints what it would send
uv run im check            # one pass, sends for real
uv run im watch --every 15 # poll every 15 minutes until stopped
uv run im chat-id          # ids of chats that have messaged the bot
uv run im test-telegram    # send a test message to every recipient
uv run im serve            # Mini App web server
uv run im bot --url https://…   # /start button that opens the Mini App
```

For scheduling outside the panel's own poller — systemd timer, or cron:

```
*/15 * * * * cd /path/to/instamart-alerts && uv run im check >> data/cron.log 2>&1
```

## Telegram Mini App

The same settings again, but opened from inside Telegram rather than a browser.
It exists because it is convenient on a phone; `im web` is the fuller tool.

```sh
uv run im serve                       # http://127.0.0.1:8080
cloudflared tunnel --url http://localhost:8080   # or ngrok http 8080
uv run im bot --url https://<your-tunnel>.trycloudflare.com
```

Telegram only loads Mini Apps over **HTTPS**, so a local port needs a tunnel.
`im bot` long-polls (no webhook needed), installs an **Alerts** button next to
the chat input, and replies to `/start` with a button that opens the panel. Set
`IM_WEBAPP_URL` in `.env` to skip passing `--url` each time.

Anyone who finds the tunnel URL can reach the server, so every endpoint requires
the Mini App's signed `initData`, verified by HMAC against your bot token and
then narrowed to the ids in `TELEGRAM_CHAT_ID`. A valid signature alone is not
enough — it only proves the request came through Telegram, not that it came from
someone on your list. `IM_WEB_DEV=1` skips the check for local development;
only use it bound to localhost.

## Tests

```sh
uv run pytest
```

Nothing in the suite hits the network. It covers price parsing against a
captured response shape, the watch filters, the de-duplication rules,
`initData` verification (tampering, wrong token, replay, wrong user), and both
web APIs' auth gating — including that the panel never echoes the bot token back
and never overwrites it with its own mask, and that a forwarded header cannot
fake a loopback client.

Beyond that, the three things most likely to break quietly:

- **fan-out delivery**, against a stubbed Telegram: every recipient is tried, a
  rejection does not stop the ones after it, and a partial failure still counts
  as delivered.
- **the browser transport**, against a fake page: params, JSON body and Swiggy
  headers reach the fetch, a fetch that throws surfaces as a transport error,
  and a `202` through the browser is still `Blocked`.
- **challenge clearance**, which is where the subtle bug lived: a `202` on the
  reload must keep waiting rather than report success, and a lone token is not a
  cleared session.

## Notes

- Discounts vary a lot between dark stores for the same product on the same day
  — the same pack can be 36% off in one city and 75% off in another. Set
  thresholds against your own store; `uv run im list eggs` shows its live
  spread, and so does **prices** on any watch in the panel.
- `data/` holds the session token, alert history, panel settings and the console
  log, and is gitignored. `settings.json` is written owner-only — it can hold a
  bot token.
- If Swiggy starts rejecting the calls, bump the build version under
  **Connection** in the panel — it mirrors their deployed web build
  (`x-build-version`), and saving it drops the pinned session so the next call
  goes out with the new header. `BUILD_VERSION` in `config.py` is only the
  fallback, so a deployed install can be fixed without a redeploy.
