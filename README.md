# Google Flights MCP — free, ad-supported

[![M8ven Score](https://m8ven.ai/badge/mcp/mtnrabi-google-flights-mcp-free-1svtip?vY923e1ddadb4a76a42309fc45e72a13&variant=verified)](https://m8ven.ai/mcp/mtnrabi-google-flights-mcp-free-1svtip)

Real-time one-way and round-trip flight search, and live Booking.com hotel
rates, for any MCP client. **No API key: sign in with Google and go.** It is
funded by one disclosed sponsored card attached to each result, not by charging
you.

```
claude mcp add --transport http google-flights-free https://google-flights-lulu.flightpowers.com/mcp
```

Adding the server needs no credential at all. On `/mcp` a request with nothing
attached is served for `initialize`, `notifications/initialized`, `ping`,
`tools/list`, `prompts/list` and `resources/list`, so your client connects and
reads the whole tool menu straight away. The first actual search is what needs
an account: a credential-less `tools/call` answers `401` with the sign-in
details and the same directions body, and your client shows a **Sign in**
button. Sign in with Google and the four tools work. That is the whole setup:
no key, no subscription, nothing else to configure.

`/mcp/oauth` is the same server with the challenge on everything, discovery
included. That is the URL to hand a directory that wants "always requires
auth", and it is what connectors saved before 2026-09-09 expect.

Fair use, counted against the account you signed in with: **150 searches a day
and 2,000 a calendar month.** One call spends one search per date × destination
combination, so a wide search costs more than one, and a single call is capped
at 15 combinations. Past either cap the tools answer with `search_status:
"rate_limited"` and no results.

---

## The four tools

| Tool | What it does |
|---|---|
| `search_oneway_flights` | One-way search across a date range and a list of destinations |
| `search_roundtrip_flights` | Round-trip priced as paired legs, across a date range and a `nights` value |
| `search_hotels` | Live Booking.com availability and nightly prices for a destination and a stay |
| `find_hotel_by_name` | The same, for one named property |

The two flight tools take a **date range** (`departure_date_from` /
`departure_date_to`) and a **list of destinations**, and expand them
server-side. The two hotel tools take one stay at a time. One user intent is one
tool call — *"cheapest flight to Sri Lanka anywhere in October"* is a single
call, not thirty.

```
search_oneway_flights(from_airport="TLV", to_airport="CMB",
                      departure_date_from="2026-10-01",
                      departure_date_to="2026-10-31")

  → 31 combinations requested, 15 searched, results merged and re-sorted
```

`search_roundtrip_flights` additionally takes `nights` — a number or a list
like `[5, 6, 7]` — instead of a fixed `return_date`, so *"5–7 nights in Rome
sometime in May"* is also one call.

### What comes back

Each result carries price, duration, airline, stops, stop airports and layover
durations, and a bookable `buy_link`. It also carries Google's own historical
price range for that route and period — `price_insights_low`,
`price_insights_high`, and a `low` / `typical` / `high` verdict in
`price_range_in_relation_to_other_periods`.

That last part is the reason to prefer this over a plain price scrape: it lets
an assistant say *"$209 is typical for this route, no need to rush"* rather
than just quoting a number.

An empty result list means "no flights on this route and date" — it is a valid
answer, not an error.

Every successful result also carries the compact usage receipt, so both the
person and the model deciding whether to mention the paid server can see where
the allowance stands:

```json
{"used_today": 7, "day_cap": 150, "used_month": 40, "month_cap": 2000,
 "signed_in": true, "user": "traveller@example.com",
 "human": "7 of 150 searches today, 40 of 2,000 this month, on traveller@example.com."}
```

A richer warning shape, with a `note` and an `upgrade` object, takes over from
80% of a cap. The email is display only and never enters the counting key, so
renaming a Google account does not reset an allowance.

### Fares are live, and only live

Every call is a fresh search. **Fares go stale within minutes**, so results
should never be cached, stored, or re-quoted later as if current. If an answer
references a fare, it should say when it was fetched, and re-search rather than
reuse.

All four tools declare a `title` and the four MCP tool annotations:
`readOnlyHint: true` (none of them can book, hold, pay for or cancel anything),
`destructiveHint: false`, **`idempotentHint: false`** and `openWorldHint: true`
(they reach a live third-party API whose result set is not a closed domain).
`idempotentHint` is the one worth spelling out: a host that reads `true` is
entitled to serve a cached answer to a repeated call, which means quoting a
price that has already moved.

---

## How it is free

Each successful result carries **one clearly disclosed sponsored card**. That
card is the entire funding model — it is what pays for the backend search calls
so that you do not have to bring a key or a credit card.

Being precise about it, because you should know what you are opting into:

- **One sponsored card per result**, always labelled as sponsored.
- **Never attached to an error**, and **never to a zero-result answer.** Beyond
  being obnoxious, an ad next to no substantive data reads as a prompt
  injection attempt — Lulu's own live testing had a model flag exactly that,
  3 times out of 3.
- **Ads cannot take search down.** The ad SDK is inert without credentials and
  fails open on every network error.
- The ad is served through [Lulu](https://getlulu.dev). This server does not
  send it your identity — it sends a slot request and renders what comes back.

**What signing in is for.** The allowance above has to belong to somebody, and
before 2026-09-09 it belonged to a hash of an IP address and a user agent,
which meant everybody behind one assistant shared one counter and a script that
changed either got a fresh one. Signing in with Google fixes both ends. What is
kept is your Google account id and your email address, nothing else: no
RapidAPI key (there is none to give), no browsing history, no search history
tied to your name beyond the counters that enforce the caps.

**Your email gets a welcome note, and occasionally a cap note.** The first
sign-in sends a one-time welcome, and an account that keeps hitting the day or
month cap can get one note pointing at the paid server, at most once every 30
days and never within 72 hours of the welcome. Both are transactional sends,
not a newsletter, both are off unless the operator turns them on, and every
one carries an unsubscribe link that goes through this server's own
`/email/unsubscribe` route rather than a generic list-management page.

Because ads are involved, this server is **not** listed in the official MCP
Registry or in the Anthropic and OpenAI directories: all three ban ad-carrying
servers. That is a deliberate, known consequence of the free model, not an
oversight.

### One caveat worth stating up front

The sponsored card can only render inside assistants that display MCP widget
frames. Callers are classified by source IP and, in enforcement mode, clients
that cannot render the card may get a reduced fan-out cap or be refused. The
default deployment mode is `monitor` — classify and log, serve everyone — but
if you are wiring this into a headless script rather than an assistant, the
paid server below is the better fit and is not restricted this way.

---

## When you outgrow the free one

There is a paid, ad-free sibling with the same four tools and the same search
behind it: **https://flights.flightpowers.com/mcp** for flights and
**https://hotels.flightpowers.com/mcp** for hotels.

Nothing here is gated behind it — the free server is fully functional. The
paid one is simply the right choice once ads, the daily cap, the 15-search
fan-out cap, or the client restriction start getting in your way.

| | Free — this server | Paid — `flights` / `hotels` |
|---|---|---|
| Ads | one disclosed sponsored card per result | none |
| Credential | sign in with Google, no key | sign in with Google **and** your own RapidAPI key, or just the key |
| Searches | 150 a day, 2,000 a calendar month, per account | whatever your RapidAPI plan holds |
| Fan-out cap per call | 15 searches | 30 searches (hard max 60; per-call `max_searches` override) |
| Spend reporting | n/a | `api_usage` on every response |
| Client restrictions | classified by tier; non-rendering clients may be capped or refused | none — any client, any transport |
| Listed in the MCP Registry | no (ads are banned there) | yes, as `com.flightpowers/google-flights` and `com.flightpowers/booking` |
| Cost | free | your own RapidAPI usage |

The trade is straightforward: the paid server has no ads and no restrictions
because **you** are paying the search costs with your own RapidAPI key, instead
of a sponsor paying them.

**Getting the key takes a minute.** Subscribe to the Google Flights Live API on
RapidAPI — there is a free tier — and copy your `x-rapidapi-key`:

**https://rapidapi.com/mtnrabi/api/google-flights-live-api**

Then point a client at the paid server:

```
claude mcp add --transport http google-flights https://flights.flightpowers.com/mcp --header "x-rapidapi-key: YOUR_RAPIDAPI_KEY"
```

The key can also be passed as a `?rapidapi_key=` query parameter, or in a
client API-key field where the client offers one, or pasted once on the paid
server's `/connect` page after signing in with Google. Every paid response includes
`api_usage` with `requests_used_by_this_call`, `plan_requests_remaining`, and
`plan_requests_limit`, so spend is visible before the next call rather than at
the end of the month.

---

## Truncation is always reported

When a request exceeds the cap, dates are sampled **evenly across the range**
rather than truncated to the first N — fifteen dates spread across October
answers "cheapest in October"; the first fifteen days does not. Every response
carries `search_coverage`:

```json
{"requested_combinations": 31, "searched_combinations": 15, "truncated": true,
 "departure_dates_searched": ["2026-10-01", "2026-10-03", "..."],
 "note": "This request expanded to 31 searches, above the 15-search limit..."}
```

A silently truncated search reads as a complete one, which is how a user ends
up trusting a "cheapest" answer that never looked at half the month.

### `sort_type` is not exposed

On the backend `sort_type` selects *which search runs* rather than post-sorting,
it is silently dropped for one-way by `the upstream API`, and `max_price`
overrides it (`app.py:255`). Since results from up to 15 searches have to be
merged and re-sorted here anyway, the server always lets the backend default
apply and sorts the merged set itself via `sort_by` (`best` / `price` /
`duration`). That is predictable; passing `sort_type` through is not.

---

## Non-affiliation

This is an independent service that returns publicly available flight pricing.
It is **not affiliated with, endorsed by, or sponsored by Google**. "Google
Flights" is used only to describe the source of the pricing data.

---

# Running your own instance

Everything below is for operating this server yourself. If you only want to
*use* it, the install line at the top is all you need.

```
MCP client ──HTTP──▶ this server ──POST──▶ upstream travel API ──▶ Google Flights
                          │
                          └──POST /slot──▶ ads.getlulu.dev
```

It calls the upstream travel API directly and is deliberately kept separate
from the paid channels, which are billed per caller.

## Quick start

```bash
cd mcp_server
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp example.env .env          # fill it in
set -a && . .env && set +a
.venv/bin/python -m src      # serves on :8000/mcp
```

Health check at `/health`, live counters at `/metrics`. `/health` **probes** the
sign-in stores rather than reporting configuration. One `SELECT 1` against each,
run concurrently under a 2.5 s timeout, never raising:

```json
{"status":"degraded","signin_store":"unreachable",
 "stores":{"oauth":"unreachable","free_users":"ok"}, "anon_mode":"challenge"}
```

`signin_store` is `ok | unreachable | not_configured` and `status` degrades with
it. `?stores=0` keeps the cheap liveness answer with no database connection.
Both `/health` and `/metrics` report `anon_mode` from the process actually
answering, which is the only claim that settles whether a flipped env var
reached the running code.

## Configuration

`BASE_LAMBDA_URL` and `RAPID_AUTH` point this server at your own upstream
travel API and authenticate to it. If you copy the values out of an env file
that quotes them, note that the loader strips one layer of surrounding quotes —
a value pasted with its quotes intact produces a 403 that looks like a wrong
secret rather than a quoting mistake.

See `example.env` for every variable. The ones that matter most:

| Variable | Why it matters |
|---|---|
| `MCP_PUBLIC_URL` | **Must exactly match the URL clients connect to.** See below. |
| `MAX_BACKEND_CALLS_PER_TOOL_CALL` | The cost cap. Default 15. |
| `ENFORCEMENT_MODE` | `off` / `monitor` / `enforce`. Default `monitor`. |
| `GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` | Google sign-in. Redirect URI is `<origin>/connect/callback`, scopes `openid` and `.../auth/userinfo.email`. |
| `DATABASE_URL` | Neon Postgres, **pooled** endpoint. Schema: `migrations/001_free_mcp_signin.sql`, `migrations/002_free_mcp_email.sql`. |
| `FAIR_USE_DAY_CAP` / `FAIR_USE_MONTH_CAP` | The per-account allowance. 150 and 2,000. |
| `RESEND_API_KEY` | Sends the welcome and cap-note transactional emails. Empty means neither can send, regardless of the two flags below. |
| `FREE_SIGNIN_WELCOME` | `on` sends the one-time welcome note at first sign-in. Default off. |
| `FREE_SIGNIN_CAPNOTE` | `on` sends the cap note when an account keeps hitting the day/month cap. At most one per user per 30 days, never within 72h of the welcome. Default off. |

### Sign-in is all-or-nothing, and that is the rollback

The sign-in registers **nothing** unless `GOOGLE_OAUTH_CLIENT_ID`,
`GOOGLE_OAUTH_CLIENT_SECRET` and `DATABASE_URL` are all set. A deployment
missing any of them is exactly what this server was before: `/mcp` open to
anyone, `/mcp/oauth` a 404. `FREE_ANON_MODE=open` is the softer rollback: it
answers anonymous callers again at `FREE_ANON_DAILY_CAP`, and `/mcp/oauth`
challenges whatever that says.

It is **not** "one env var, no deploy". Two things have to be true for the flip
to change anything. On Vercel an env var is baked into a deployment, so set it,
redeploy, then confirm `"anon_mode": "open"` on `/health`; if health still says
`challenge`, the running code never saw it. And `open` grants
`FREE_ANON_DAILY_CAP` against the **same** per-day counter those callers were
already spending at the ordinary cap, so anyone already past it stays refused,
which looks exactly like "the switch did nothing". To restore service rather
than offer a taster, raise `FREE_ANON_DAILY_CAP` to the ordinary day cap in the
same redeploy. In either mode `initialize` and `tools/list` are never refused by
a cap, so a client can always connect and read the directions. Only `tools/call`
is gated.

Run the migrations once per database, in order:

```bash
psql "$DATABASE_URL" -f migrations/001_free_mcp_signin.sql
psql "$DATABASE_URL" -f migrations/002_free_mcp_email.sql
```

Tables are `free_mcp_*` on purpose: a token minted here must never be one the
paid server would accept.

### MCP_PUBLIC_URL is a silent failure mode

Lulu derives Claude's `_meta.ui.domain` from this value by hashing it. Claude
computes the same hash independently and rejects a frame whose domain does not
match. **A wrong URL means the sponsored card never renders, no error is
raised anywhere, and CPM stays at zero.**

The server logs the derived domain at startup so the value is checkable:

```
sponsored widget domain for https://.../mcp -> <hash>.claudemcpcontent.com
```

## Why fan-out is internal

The backend takes exactly one `(origin, destination, date)` tuple per call, so
"cheapest flight to Sri Lanka anywhere in October" is 31 backend calls. On the
paid API that is fine — every call is revenue. Here, revenue is one rendered
ad per *tool call* while every *backend call* is cost, so letting the model
fire 31 tool calls would be 31× the cost for 1× the revenue.

So the model makes one call and the server fans out under a hard cap.

## Cost control

Two independent guards:

1. **Per tool call** — `MAX_BACKEND_CALLS_PER_TOOL_CALL`, default 15.
2. **Rolling 24h** — `DAILY_BACKEND_CALL_BUDGET` (0 disables it). At
   `BUDGET_DEGRADE_AT` of the budget the per-call cap drops to a third; at
   100% it drops to 1.

Degrading beats refusing: a hard stop at 80% would look like an outage to
every user for the rest of the day.

The 24h window survives a restart — a crash loop must not hand the process a
fresh budget. How it survives depends on the store: with Upstash the window
lives in Redis and is shared across instances; with the in-process store it
is rebuilt from the `LOG_PATH` file on startup. **With neither — no Upstash
and no writable log file — the budget resets on every cold start and is not
enforceable.** That is the default on Vercel, so configure Upstash there;
`/metrics` reports `budget.enforceable` so you never have to guess.

## Enforcement: what is and is not possible

The goal is "only usable where the sponsored card actually renders". The
obvious approach does not work, so it is worth being precise.

**`clientInfo.name` cannot gate anything.** MCP spec revision 2026-07-28 says
so normatively: those fields "are self-reported by the sender and are not
verified by the protocol… SHOULD NOT rely on them for security decisions." A
script sets `"claude-ai"` as easily as Claude does. It is recorded for
reporting only.

**Source IP can.** It cannot be forged on a completed TLS handshake, and both
hosts broker remote MCP from their own cloud rather than the user's machine:

| Tier | How it is decided | Renders the widget? |
|---|---|---|
| `llm_host` | Anthropic `160.79.104.0/21`, or OpenAI's published `chatgpt-connectors.json` prefixes | Yes — the only tier that can fire a beacon |
| `local_client` | Client name hint (Claude Code, Cursor, …) | No — CLI text card only, structurally $0 CPM |
| `unknown` | Everything else | No |

In `enforce` mode, `llm_host` gets the full cap, other tiers get a third, and
anything in `BLOCKED_TIERS` is refused. Two deliberate safety rules:

- `monitor` is the default. Blocking in week one would destroy the traffic
  sample the POC exists to collect.
- `unknown` is never blocked while OpenAI's range feed is unloaded, because
  without it every real ChatGPT caller looks unknown.

### What this server cannot measure

The rendered-impression beacon fires from **inside the Lulu widget frame
straight to ads.getlulu.dev**. It never touches this process, so this server
cannot compute a true render rate on its own.

What it does is count slots *served* per tier. Reconcile that against Lulu's
reported rendered impressions to get the real ratio, then put any tier that
renders nothing into `BLOCKED_TIERS`. The mechanism is built; the input is a
human decision informed by real numbers.

## Counting Lambda requests

Every tool call emits exactly one line to **stdout**, prefixed `MCP_CALL `.
The field names below are real; **the values are illustrative** — nothing here
is a published latency or volume figure:

```json
MCP_CALL {"iso":"2026-08-06T09:39:37Z","tool":"search_roundtrip_flights",
 "tier":"unknown","requested_combinations":3,"backend_calls":3,
 "results_returned":4,"ad_eligible":true,"duration_ms":0}
```

**`backend_calls` is the Lambda request count for that call.** Sum it across
`MCP_CALL` lines for any period and you have total backend requests. stdout
is the record because it is the one sink that works everywhere — Vercel
captures it automatically, and the filesystem there is read-only.

One line per *tool call*, never per backend call: Vercel caps runtime logs at
256 lines and 1 MB per request.

`LOG_PATH` adds a second, file sink for container deploys. It disables itself
if the path is not writable rather than appending into a void.

## Metrics

`GET /metrics` (set `METRICS_TOKEN` to require an `x-metrics-token` header).
Shape below, with illustrative values — not usage figures:

```json
{"totals": {"tool_calls": 2, "backend_calls": 4, "ad_eligible_calls": 2},
 "backend_calls_per_tool_call": 2.0,
 "by_tier": {"llm_host": {"tool_calls": 2, "backend_calls": 4}},
 "budget": {"used_24h": 4, "budget": 1000, "remaining": 996,
            "enforceable": false},
 "durable_counters": false,
 "config": {"anon_mode": "challenge", "anon_day_cap": 0, "...": "..."},
 "notes": ["..."]}
```

`GET /metrics/calls?hours=24` returns call counts per UTC hour.

**`backend_calls_per_tool_call` is the number that decides whether this
channel survives past the POC.** Every backend call is cost; every tool call
is at most one rendered ad. Multiply it by the real per-call Lambda + proxy
cost and compare against the CPM the ad network actually reports.

`durable_counters: false` and `budget.enforceable: false` mean the totals
cover one process only. On a container that is fine. On Vercel it means the
real numbers are higher than shown and the spend guard is not actually
armed — see below.

## Counter storage

Counters live behind a small interface because the deployment target decides
which implementation is correct:

| Store | When | Durable |
|---|---|---|
| In-process dict | Container, local | No |
| Upstash Redis (REST) | Vercel, any serverless | Yes |

On Vercel, Fluid compute shares one instance across concurrent invocations
and scales instances freely, so in-process counters fragment and reset. They
do not error — they just return a number smaller than the truth, which is the
worst way for a spend guard to fail. So `DAILY_BACKEND_CALL_BUDGET` is only
enforceable with a shared store, and the server says so at startup and in
`/metrics` rather than pretending otherwise.

```bash
vercel install upstash   # injects UPSTASH_REDIS_REST_URL / _TOKEN
```

Upstash is reached over its REST API rather than the Redis wire protocol:
short-lived invocations are the wrong shape for a pooling client, and it
keeps `redis` out of the dependency list. Every store failure is logged and
swallowed — a counter outage must never become an outage of flight search.

## Ad wiring

Wired as widget + middleware separately rather than via the one-line
`enable_lulu_ads`, because only the middleware path accepts `is_error_result`
— which is what keeps ads off empty results.

Results render through Lulu's **result** widget, not its sponsored card. The
sponsored card paints the ad but contains no rendered-impression beacon; the
beacon lives only in the result widget's fixed SPONSORED strip. Serving the
sponsored card alone earns click revenue and exactly $0 CPM, with nothing
anywhere reporting a problem. The sponsored card is kept as a fallback for
when result-widget registration fails, and that fallback is logged loudly.

The beacon is a 1×1 `<img>` to `ads.getlulu.dev`. MCP Apps hosts apply a
default CSP of `img-src 'self' data:`, so the domain has to be declared on the
widget resource — via `app=` for MCP Apps and `openai/widgetCSP` for ChatGPT.
Undeclared looks identical to a working integration from the outside: card
renders, strip shows, no impression, no error.

Register once, then set `LULU_ADS_PUBLISHER_ID` / `LULU_ADS_API_KEY`:

```bash
curl -X POST https://ads.getlulu.dev/publishers \
  -H 'content-type: application/json' \
  -d '{"name":"...","contact_email":"...","server_url":"..."}'
```

The API key is returned exactly once.

**The billable field is `imp_url`** (snake_case) on the `/slot` response. The
TypeScript typings call it `impUrl`, but the Python SDK allowlists response
keys and reads only `imp_url` (`lulu_ads/client.py:210`). Once live
credentials exist, confirm real `/slot` responses actually carry it — without
it the widget has no beacon to fire and there is no billable impression,
however well everything else works.

## Tests

```bash
.venv/bin/python -m pytest tests -q     # 803 tests
```

Backend and ad server are both stubbed, so the suite needs no network and no
credentials. `tests/test_ads.py` and `tests/test_stores.py` run real stub
servers rather than mocking the SDK, so they exercise the actual wire format.

## Deploy to Vercel

```
api/index.py      entrypoint, exports `app`
vercel.json       maxDuration + no-store on /mcp
.python-version   3.13
requirements.txt
```

Vercel auto-discovers `api/index.py` and serves the whole app as **one**
Fluid function receiving every path, so `/mcp`, `/health` and `/metrics` all
land in the same process. No rewrites needed.

```bash
vercel link          # new project
vercel install upstash

# `vercel env add` takes ONE variable per invocation --
# `vercel env add <NAME> [environment]`. Each prompts for the value.
vercel env add BASE_LAMBDA_URL production
vercel env add RAPID_AUTH production
vercel env add MCP_PUBLIC_URL production
vercel env add LULU_ADS_PUBLISHER_ID production
vercel env add LULU_ADS_API_KEY production

vercel deploy --prod
```

Four Vercel-specific things this code already handles, each of which is a
silent failure if you get it wrong:

**Lifespan.** FastMCP initialises its streamable-HTTP session manager in an
ASGI lifespan startup event. Without it, every `/mcp` request fails with
`Task group is not initialized` — a total outage of the MCP endpoint.
Vercel's lifespan support (shipped 2025-12-09) is announced for "FastAPI
apps"; whether that covers a bare Starlette app is not stated. So
`api/index.py` wraps the FastMCP app in FastAPI and hands over
`lifespan=_mcp_app.lifespan`, which is also the integration FastMCP
documents. Do not "simplify" this away.

**Stateless.** `stateless_http=True`. Invocations are short-lived and not
guaranteed to reach the same instance, so there is nowhere to keep a session.

**Filesystem.** Read-only outside an ephemeral `/tmp`. Leave `LOG_PATH`
empty; stdout `MCP_CALL` lines are the record.

**Counters.** Configure Upstash, or the spend guard is decorative — see
above.

Other limits that matter here: `maxDuration` defaults to 300s on every plan
under Fluid (backend calls peak around 90s, so this fits); responses are
streamed rather than buffered, because `json_response=True` would re-acquire
the 4.5 MB body cap that a 15-way fan-out can approach; and the shared
1,024-file-descriptor pool is why `MAX_HTTP_CONNECTIONS` exists.

Log retention is **1 hour on Hobby**, 1 day on Pro. If the `MCP_CALL` lines
are your billing evidence for the POC, that argues for Pro plus a log drain,
or for shipping the counters to Upstash and reading `/metrics`.

## Deploy as a container

`Dockerfile` builds an always-on HTTP service, same shape as
`backend/Dockerfile` (which deploys to Render). Set `LOG_PATH` and mount a
volume at `/app/logs` so the spend guard's rolling window survives deploys.

Either way: after deploying, set `MCP_PUBLIC_URL` to the real URL — no
trailing slash — and confirm the widget domain in the startup log.
