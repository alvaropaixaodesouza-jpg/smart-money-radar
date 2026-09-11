# FOMO Robinhood Radar — the whole product, in one file

Written to be fed to an AI when producing posts, threads, landing copy or pitch material. It is
deliberately long. Take what you need. Every number in here is real, dated 2026-09-11.

Live: **https://fomoradar.app** · Bot: **@fomoradarRH_bot** · API: **https://fomoradar.app/docs**

---

## The one-sentence version

Robinhood Chain has a leaderboard of memecoin traders. It shows you a number. FOMO Robinhood Radar
shows you whether the number means anything, whose wallet it belongs to, what that wallet is buying
this minute — and verifies that it actually bought it.

## The one-paragraph version

fomo.family ranks traders by profit. A big number there can mean a person who reads launches
better than anyone on the chain, or one lucky ticket bought with beer money. The leaderboard cannot
tell you which, will not tell you which wallet they actually trade from, and shows you nothing
about what they are doing today. FOMO Robinhood Radar resolves the profile to the real on-chain
wallet, rebuilds the whole book from the chain's own logs, has Claude judge whether the record
looks repeatable, reads the chain every twenty seconds, verifies the provenance of every fill, and
publishes what the wallets that passed are buying, entering in a burst, and leaving. Every data
source it uses is public and free.

---

## Why this exists

Copy-trading on a memecoin chain has one hard problem, and it is not finding trades. Trades are
everywhere. The problem is that **a profit number tells you almost nothing about the person who
made it.**

Four separate things hide behind one figure:

**Was it skill or a ticket?** Somebody who put $2,000 into one token that ran 780x has the same
30-day PnL as somebody who took six-figure positions in four names and was right about all of
them. The leaderboard ranks them together. Only one of them can do it again.

**Whose wallet is it?** Every address fomo's own API returns for a user is an internal account with
zero on-chain history. Follow it and you watch nothing happen forever. The wallet that actually
trades is not published anywhere on fomo.

**What are they doing now?** A leaderboard is a scoreboard for last month. The token somebody
bought three weeks ago and sold two weeks ago is still holding up their rank today.

**Did they even buy it?** On this chain anyone can execute a swap and name somebody else's wallet
as the recipient, and to an ordinary tracker that reads as the famous wallet buying. Roughly one
"buy" in eleven credited to a top wallet is exactly that.

The Radar answers all four, and it answers them from the chain rather than from anybody's
dashboard.

---

## How it works, end to end

### 1. Find the traders

fomo's leaderboards over HTTP, plus the top holders of every fresh token that shows up. **912 fomo
users known**, 562 with a wallet attached, 234 of those confirmed against fomo's own verified
wallet data. This is the only step that touches fomo at all, and it costs about thirty free credits
a day.

### 2. Resolve the profile to a real wallet

fomo publishes *when* a user traded *what* — but not from where. So the wallet is inferred: take a
dozen of a trader's swaps, ask the chain who else traded that same token in that same minute, and
intersect. Quiet windows count for more than busy ones, because being one of three buyers in a
minute is evidence and being one of three hundred is not.

Graded against fomo's own verified wallets: **101 agreements, zero disagreements.** Every wallet
the resolver worked out on its own was the one fomo verifies. When two handles claim the same
wallet the resolver refuses to guess and records the conflict rather than attaching a book to the
wrong person.

### 3. Track them for free, forever

Every trade on Robinhood Chain routes through one relayer contract, which means the trader's wallet
is never the transaction's sender. Searching by sender finds nothing. That single fact is why most
tooling does not cover this chain.

What works instead: read the chain's transfer logs, filter for the leg facing the relayer, and the
direction falls out. **Two RPC calls cover the entire roster.** Not two per wallet. Two.

Current tape: **116,420 fills across 36 days**, read from the chain's public RPC for nothing.

### 4. Read it as it happens

Two readers, two jobs. A scheduled pass reads the last five hours of blocks every fifteen minutes,
so the tape never has a hole in it. A live watcher asks the chain, every twenty seconds, for only
the blocks it has not seen — four requests a tick, on its own allowance. **A fill is in the
database about twenty seconds after it lands on the chain**, and every feed reads from the same
database.

### 5. Verify whose trade it is

Every fill carries its provenance, settled from the transaction receipt the moment it lands.

- A fomo-app trade goes to fomo's entrypoint and never to the router itself. A swap sent to the
  router by an outside key with a trusted wallet named as recipient is **not that wallet's trade**
  and counts for nothing, whatever its size.
- A buy under max($5, a tenth of the wallet's own median buy) is **dust**. It is stored and shown,
  and it does not move conviction. Real traders probe small; measured over thirty days the floor
  touches about 7% of trusted buys, all of them probes.
- A token where three or more trusted wallets received dust or outside-key fills inside a day is
  **seeded**, and is quarantined from every feed while that holds. The rule scales against the
  attacker: the more wallets somebody seeds to look like a cohort, the more certainly the token
  disappears. Five tokens were quarantined on the rule's first day, with no false positive.

The token page states plainly when a token has been pushed into wallets rather than bought by
them, and marks each such fill. Nothing else indexing this chain makes the distinction.

### 6. Rebuild the book from the tape

Every buy and sell of one token folds into one position. Balances are read straight off the chain
to settle what is actually still held.

A position the wallet held before our tape begins has an unknowable entry price, so it is shown
with size and cost and **no profit figure** rather than a made-up one. Our reconstruction, checked
against fomo's own number for the same position: **$110,932 against $110,845 — a 0.08% gap, by two
completely independent routes.**

### 7. Score them

Claude reads a compact profile of each wallet — the fomo figures, the on-chain aggregates, and the
open book with cost bases — and returns a verdict: **active** (70+), **watch** (40–69) or
**dropped** (under 40), with a style, any red flags, and two or three sentences of reasoning that
cite the actual numbers.

**374 traders scored. 169 active.**

What the scoring is looking for:

- Several independent winners beat one big one.
- A real cost basis beats a large multiple on dust. `PONS at 2.1x on a 3.7M entry` is a stronger
  signal than `780x on a 2k ticket`, even though the second number is prettier.
- The losses count. Forty-eight early entries mean nothing if the same book has one position
  written off and another down two thirds.
- A six-minute median hold across 282 tokens is a bot, not a trader.

### 8. Publish what they do next

Five feeds, all reading the same database:

**Signals** — tokens several trusted wallets bought today, ranked by *conviction*: the sum of each
buyer's (score/100)². One trader at 85 outweighs a crowd at 40, because anyone can open a wallet.

**Bursts** — the fast edge. A token where conviction of 4.0 or more arrived from three or more
trusted wallets *inside thirty minutes*: four wallets scoring 80, say, all making their first buy
of the token within half an hour of each other. Caught within twenty seconds of the fill that tips
it, pushed to the bot the same tick, and the page keeps its own scorecard — every burst, and what
the price did afterwards, in the price the cohort itself paid.

**Fresh** — launches the cohort has *just started* buying, ranked by *heat*: conviction multiplied
by how early each wallet arrived, halving every hour after the pool opens. On a token that launched
this morning, when somebody bought is most of what their buy means.

**Exits** — where the cohort is *leaving*. Nobody else publishes this, and it is arguably the half
that matters: a token stays on an entry feed as long as the buy is inside the window, whether or
not the buyer is still there. One sale is not an exit, so a wallet only appears once it has sold at
least half of what the tape watched it buy.

**Leaderboard** — the roster by verdict, with the reasoning visible.

---

## The parts nobody else has

### Twenty seconds from the chain to your phone

Blocks land every tenth of a second on Robinhood Chain. The watcher reads them in near real time,
the burst rule runs on every tick, and a subscriber gets the alert the tick a burst forms — with
the wallets, their scores, the money in, the token's age, the contract to copy, and a link to the
full breakdown.

The worked example: **FLYBRAIN**, 2026-09-10. First trusted wallet in at 19:15 UTC on the bonding
curve. Eight trusted wallets and conviction 4.4 by 20:18. The pool opened at 20:51. The run began
at 23:00 and went 27x from the opening price. The burst alert fires at 20:18.

### A burst bar chosen by replay, not by feel

The rule was replayed over the whole tape — 36 days, 116,000 fills, every price the one the cohort
itself paid — against the ordinary signal feed as a baseline:

| rule | fires per day | reached 2x within a day | median best price |
|---|---|---|---|
| signal feed (2+ trusted buyers in 24h) | 52 | 36% | 1.45x |
| **burst (+4.0 conviction in 30 min)** | **5** | **39%** | **1.75x** |
| burst (+5.0 in 60 min) | 4 | 47% | 1.93x |

Ten times more selective than the plain feed and better on every measure. The scorecard on the
bursts page continues that replay live, one day at a time, so the bar is always defensible with
current numbers.

### Provenance on every fill

Every wallet tracker on every chain reads "token arrived at wallet from a swap" as "wallet bought".
The Radar checks the receipt. It knows the difference between a fomo trade and a swap somebody
else executed into a famous wallet, between a position and a fifty-cent push, and it quarantines a
token the moment it is being seeded into the cohort. This is the difference between a feed that
can be gamed for pocket change and one that cannot.

### Theses: what they said, not just what they did

Every other number here is inferred from the tape. A thesis is the trader typing it out. fomo lets
traders attach a note to a position, and the Radar collects them — 97 so far — and shows them under
the token, ranked by the writer's score.

Real examples, from wallets scoring 90+:

> **ogle (92)** — "remember that since $pons gets burnt every 15 mins, your % of the total
> outstanding pons tokens continues to go up proportionately"

> **AvgJoesCrypto (90)** — "PONS fundamentals are great. Maintaining top launchpad spot on
> Robinhood Chain and is trading at 1.8x price-to-buybacks"

That is somebody who has millions in the position explaining the mechanism. An unscored account
writing "wagmi" does not appear, because the whole product is the claim that whose money it is
decides whether the words are worth reading.

### The exit feed

Worth stating twice. Entry feeds are a commodity. Watching the same wallets leave is not, and the
first day it ran it caught **CRUMBS**: a token that had been top of the fresh feed that morning
with 22 trusted buyers, and by evening showed **19 of them out entirely**.

### Calibration: the scores are graded

Most tools that rank things never check whether the ranking predicted anything. This one does, and
publishes the method. The test is out-of-sample by construction: only positions opened *after* a
wallet was scored count, so a verdict can never be credited with the trade it was derived from.
The active band returns more per dollar than the dropped band on win rate, median and pooled
return alike, and the method that shows it is part of the product.

### It costs nothing to run

No indexer subscription. No node. No paid tape. The chain's own RPC reads 116,420 fills for
nothing, GeckoTerminal prices them on a free tier, and the fomo side runs on a free key at about
thirty credits a day against a thousand a month. **$0 a month**, by design: every time a paid
source was tried, a free one turned out to answer better on this chain.

---

## What people actually use it for

**"Is this trader worth copying?"** Send a handle to the bot. You get the verdict, the reasoning,
the open book with cost bases, what has already been sold, and which other tracked wallets are in
the same names.

**"Who is in this token?"** Send a contract address. You get every tracked wallet holding it, what
it cost them, what they said about it, the flow in and out over the window, and whether any of it
was pushed into wallets rather than bought.

**"What is bursting right now?"** `/hot`, or the tab. Several trusted wallets entering one name
inside minutes, caught within twenty seconds, with what every earlier burst went on to do.

**"What just launched that good wallets are entering?"** `/fresh`. Ranked by how early they were,
filtered so a token whose pool has already been drained cannot top the page on headcount alone.

**"Did the people I copied leave?"** `/exits`. The question nothing else answers.

**"Tell me when it happens."** Subscribe and the bot pushes bursts the tick they form, launches
and signals as they cross a threshold, and a daily digest at 18:00: what was entered, bought, left,
said, who joined the roster, and how the day's bursts turned out.

**"Feed it to my own system."** Everything the site shows is on the HTTP API at
`/docs`: signals, bursts, fresh, exits, the leaderboard, any token, any trader, with the same
provenance flags.

---

## Under the hood

- **Python 3.14, FastAPI, SQLite in WAL mode, Astro 5 server-rendered, Caddy with automatic TLS.**
  One 8 GB box, everything under systemd, health and heartbeat timers, nightly backups.
- **The tape is two `eth_getLogs` per block range** over the whole roster: outgoing transfers and
  incoming transfers with the router on the other side. Receipts are batched forty a request for
  the dollar leg and the provenance check. Decimals are learned once and cached.
- **Conviction** is Σ(score/100)². **Heat** is conviction weighted by earliness with a one-hour
  half-life. **A burst** is conviction gained inside a sliding window over each wallet's *first*
  buy of a token, so a wallet that was already in does not count as arriving twice.
- **Every feed excludes quote assets** (USDG, WETH), which raw DEX data otherwise ranks as the
  most-bought tokens on the chain.
- **Every source fails soft.** One API down never stops the loop; a token nobody can name is
  stamped and skipped rather than re-asked forever.
- **Every parser has a fixture and a test.** 187 tests, offline, run on every deploy.
- **Nothing is hardcoded.** Every threshold — trust line, dust floor, burst bar, seed window,
  cadence — is configuration.

---

## Where it goes next

**An execution engine.** The signals are meant to be acted on. Everything so far has been about
earning the right to act: resolving the real wallet, proving the book against an independent
source, grading the scores out of sample, verifying provenance. The engine reads `/api/hot` first.

**A market benchmark for the calibration.** The burst scorecard is the first piece: an entry price
and what followed, for every alert, kept forever.

**Wider coverage.** The architecture is chain-agnostic; Robinhood Chain is where the edge is
cheapest today because almost nothing else indexes it properly.

---

## Facts and figures, current (2026-09-11)

| | |
|---|---|
| Traders scored | **374** (169 active, 153 watch, 52 dropped) |
| fomo users known | **912**, 562 with a wallet, 234 confirmed against fomo's verified data |
| Wallet resolution vs fomo's verified wallets | **101 agree, 0 disagree** |
| On-chain fills | **116,420** across 36 days |
| Fill to database | **~20 seconds** |
| Tokens tracked | **5,918** |
| Theses collected | **97** |
| Highest-scored wallet | **unipcs, 96** — $19.5M over 30 days |
| Book reconstruction accuracy | **0.08%** against fomo's own figure |
| Burst rule, replayed over 36 days | ~5 fires/day, 39% reach 2x — vs 52/day and 36% for a plain feed |
| Provenance | every fill checked; seeded tokens quarantined |
| Cost to run | **$0 / month** |

---

## Talking points that land

- Two RPC calls track the entire roster. Not two per wallet.
- The address fomo gives you for a trader has never made a trade. We find the one that has —
  101 for 101 against fomo's own verified wallets.
- Twenty seconds from the fill to your phone.
- A burst is conviction with a clock on it: four 80s inside half an hour. Ten times more
  selective than a plain signal feed, and better on every measure.
- Every fill is checked for provenance. "Smart money bought" can be faked for fifty cents on this
  chain; here it cannot, and seeding a token into famous wallets makes it disappear from the feeds.
- Conviction is squared on purpose: anyone can open a wallet, so headcount is the wrong unit.
- The one number that separates skill from luck is the cost basis, and almost nobody shows it.
- A token can top the entry feed in the morning and the exit feed by evening. CRUMBS did.
- The scores are graded out of sample and the method is published.
- It runs for nothing, and every attempt to pay for better data made it worse.

---

## How to talk about it

The product's voice is flat, specific and confident. It states numbers, shows the method, names
what it cannot know, and lets the figures do the selling.

Things to avoid: calling it a signal group, promising returns, describing the scores as
predictions. A score is an opinion about whether a result looks repeatable, formed from an open
book and a trading record. A burst is a fact about timing, not a forecast.

Nothing here is financial advice.
