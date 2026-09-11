# FOMO Robinhood Radar — the whole product, in one file

Written to be fed to an AI when producing posts, threads, landing copy or pitch material. It is
deliberately long. Take what you need. Every number in here is real and dated 2026-09-11.

Live: **https://fomoradar.app** · Bot: **@fomoradarRH_bot** · API: **https://fomoradar.app/docs**

---

## The one-sentence version

Robinhood Chain has a leaderboard of memecoin traders. It shows you a number. This shows you
whether the number means anything, whose wallet it belongs to, what that wallet is buying right
now — and, since today, whether it actually bought it.

## The one-paragraph version

fomo.family ranks traders by profit. A big number there can mean a person who reads launches
better than anyone on the chain, or one lucky ticket bought with beer money. The leaderboard cannot
tell you which, will not tell you which wallet they actually trade from, and shows you nothing
about what they are doing today. FOMO Robinhood Radar resolves the profile to the real on-chain
wallet, rebuilds the whole book from the chain's own logs, has Claude judge whether the record
looks repeatable, reads the chain every twenty seconds, and publishes what the wallets that passed
are buying, entering in a burst, and leaving. Every data source it uses is public and free, and the
feeds now know the difference between a wallet buying a token and a token being pushed into a
wallet — which, on this chain, turns out to be one buy in eleven.

---

## Why this exists

Copy-trading on a memecoin chain has one hard problem, and it is not finding trades. Trades are
everywhere. The problem is that **a profit number tells you almost nothing about the person who
made it.**

Three separate things hide behind one figure:

**Was it skill or a ticket?** Somebody who put $2,000 into one token that ran 780x has the same
30-day PnL as somebody who took six-figure positions in four names and was right about all of
them. The leaderboard ranks them together. Only one of them can do it again.

**Whose wallet is it?** Every address fomo's own API returns for a user is an internal account with
zero on-chain history. Follow it and you watch nothing happen forever. The wallet that actually
trades is not published anywhere on fomo.

**What are they doing now?** A leaderboard is a scoreboard for last month. The token somebody
bought three weeks ago and sold two weeks ago is still holding up their rank today.

And a fourth, which nobody talks about because nobody had measured it: **did they even buy it?**
On Robinhood Chain anyone can execute a swap and name somebody else's wallet as the recipient.
To every tracker, that reads as the famous wallet buying. It costs a few dollars.

The Radar answers all four, and it answers them from the chain rather than from anybody's
dashboard.

---

## How it works, end to end

### 1. Find the traders

fomo's leaderboards, read over HTTP from fomoapi.io on a free key, plus the top holders of every
fresh token that shows up. **912 fomo users known**, 562 with a wallet attached. This is the only
step that needs fomo at all, and it costs about thirty credits a day against a thousand a month.

### 2. Resolve the profile to a real wallet

The part that took longest to get right, and the part that has since been checked by an outside
source.

fomo publishes *when* a user traded *what* — but not from where. So the wallet is inferred: take a
dozen of a trader's swaps, ask the chain who else traded that same token in that same minute, and
intersect. Quiet windows count for more than busy ones, because being one of three buyers in a
minute is evidence and being one of three hundred is not.

Then the leaderboard feed started carrying each trader's verified wallet, and the inference could
be graded: **101 agreements, zero disagreements.** Every wallet the resolver had worked out on its
own was the one fomo verifies. When two handles claim the same wallet the resolver still refuses to
guess and records the conflict rather than attaching a book to the wrong person.

### 3. Track them for free, forever

Every trade on Robinhood Chain routes through one relayer contract, which means the trader's wallet
is never the transaction's sender. Searching by sender finds nothing. That single fact is why most
tooling does not cover this chain.

What works instead: read the chain's transfer logs, filter for the leg facing the relayer, and the
direction falls out. **Two RPC calls cover the entire roster.** Not two per wallet. Two.

Current tape: **116,420 fills across 36 days**, read from the chain's public RPC for nothing.

### 4. Read it as it happens

The scheduled pass reads the last five hours every fifteen minutes, which is the right shape for a
tape that must not have holes in it and the wrong shape for an alert. So a second process asks the
chain, every twenty seconds, for only the blocks it has not seen: four requests a tick, on an
allowance of its own. **A fill is in the database about twenty seconds after it lands on the
chain.** Nothing about the fifteen-minute pass changed; it fills any gap the watcher leaves.

### 5. Know whose trade it is

New today, and the reason it exists is worth telling in full.

The first live burst alert fired on a token seventeen trusted wallets had bought inside minutes.
None of them had. Four outside keys had called the router directly, paid with their own ETH, and
named a trusted wallet as the recipient of each swap. The same hour, a different token sat at the
top of the signal feed on twenty-seven "buys" of fifty cents each, delivered to eighteen trusted
wallets through fomo's own flow. **Thirty-five dollars for the top of the page.**

Who signed the transaction is no help — every fill on this chain is signed by one of fomo's
relayers, 142 of them in a day. What is:

- A fomo-app trade goes to fomo's entrypoint and never to the router itself. A receipt whose
  destination is the router is an outside key's swap and **nobody's trade**. It counts for nothing.
- A buy under max($5, a tenth of the wallet's own median buy) is **dust** — stored, shown, and left
  out of conviction. Real traders do probe small; measured on thirty days the floor drops about 7%
  of trusted buys, all probes.
- A token where three or more trusted wallets received dust or outside-key fills inside a day is
  **seeded**, and leaves every feed while that holds. This is the rule that turns the attack on
  itself: the more wallets a seeder touches to look like a cohort, the more certainly the token
  disappears. It caught five tokens in its first day and no real one.

Every such fill is marked on the token page, and the page says plainly when a token has been pushed
into wallets rather than bought by them. On the first 3,600 fills re-checked against their receipts,
**one buy in eleven credited to a trusted wallet was not that wallet's trade.**

### 6. Rebuild the book from the tape

Every buy and sell of one token folds into one position. Balances are read straight off the chain
to settle what is actually still held.

This is where most of the honesty lives. A position the wallet held before our tape begins has an
unknowable entry price, so it is shown with size and cost and **no profit figure** rather than a
made-up one. When we checked our reconstruction against fomo's own number for the same position:
**$110,932 against $110,845 — a 0.08% gap, by two completely independent routes.**

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
of the token within half an hour of each other. Caught within twenty seconds of the fill that
tips it, pushed to the bot the same tick, and the page carries its own scorecard: every burst,
and what the price did afterwards.

**Fresh** — launches the cohort has *just started* buying, ranked by *heat*: conviction multiplied
by how early each wallet arrived, halving every hour after the pool opens.

**Exits** — where the cohort is *leaving*. Nobody else publishes this, and it is arguably the half
that matters: a token stays on an entry feed as long as the buy is inside the window, whether or
not the buyer is still there. One sale is not an exit, so a wallet only appears once it has sold at
least half of what the tape watched it buy.

**Leaderboard** — the roster by verdict, with the reasoning visible.

---

## The parts nobody else has

### The burst feed, and how its bar was chosen

The bar was not chosen by feel. The rule was replayed over the whole tape — 36 days, 116,000
fills, every price the one the cohort itself paid — against the ordinary signal feed as a baseline:

| rule | fires per day | reached 2x in a day | ended the day below half |
|---|---|---|---|
| signal feed (2+ trusted buyers in 24h) | 52 | 36% | 43% |
| **burst (+4.0 conviction in 30 min)** | **5** | **39%** | **42%** |
| burst (+5.0 in 60 min) | 4 | 47% | 40% |

Better on every measure and ten times quieter. And still a coin with a long tail — about 40%
reach 2x within a day, about 40% end the day below half — which is stated on the page rather
than hidden. The value is in the best price, median 1.75x, not in holding to the close. Nine in ten
bursts land in a token's first hour; the few that came in hours two to six did best, on a sample
too small to build on, so there is no age cap and the age is printed instead.

The worked example: **FLYBRAIN**, 2026-09-10. First trusted wallet in at 19:15 UTC on the
bonding curve, eight of them and conviction 4.4 by 20:18, pool opened at 20:51, run began at
23:00 and went 27x from the opening price. A burst alert would have gone out at 20:18. Under the
old fifteen-minute pass it went into the database at 20:35.

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

### Provenance

Stated once more because it is the most consequential thing found so far. Every wallet tracker on
every chain reads "token arrived at wallet from a swap" as "wallet bought". On this chain that is
gameable for pocket change, and the Radar has the receipts to prove that it was being gamed, at
scale, today. The three-layer rule above is the only defence anybody has published, and its third
layer — the seeded-token rule — makes the attack cost more the harder it is tried.

### Calibration: we grade our own scores

Most tools that rank things never check whether the ranking predicted anything. This one does, and
publishes the method.

The test is out-of-sample by construction: only positions opened *after* a wallet was scored count,
so a verdict can never be credited with the trade it was derived from.

The current reading, reported honestly rather than spun: **the score separates the top band from
the bottom** — active returns more per dollar than dropped, on win rate, median and pooled return
alike — but **it does not yet cleanly separate active from watch**. That gap is public, and closing
it is the next piece of work. So is re-scoring the wallets whose histories turn out to contain
fills that were never theirs.

### It costs nothing to run

No indexer subscription. No node. No paid tape. The chain's own RPC reads 116,420 fills for
nothing, GeckoTerminal prices them on a free tier, and the fomo side runs on a free fomoapi.io key
— 1,000 credits a month against a schedule that spends about 30 a day. **$0 a month**, and that is
a design property rather than a temporary state: every time a paid source was tried, a free one
turned out to answer better on this chain.

---

## What people actually use it for

**"Is this trader worth copying?"** Send a handle to the bot. You get the verdict, the reasoning,
the open book with cost bases, what has already been sold, and which other tracked wallets are in
the same names.

**"Who is in this token?"** Send a contract address. You get every tracked wallet holding it, what
it cost them, what they said about it, the flow in and out over the window — and, if the token has
been pushed into wallets rather than bought, a plain statement of that with each such fill marked.

**"What is bursting right now?"** `/hot`, or the tab. Several trusted wallets entering one name
inside minutes, caught within twenty seconds, with what every earlier burst went on to do.

**"What just launched that good wallets are entering?"** `/fresh`. Ranked by how early they were,
filtered so a token whose pool has already been drained cannot top the page on headcount alone.

**"Did the people I copied leave?"** `/exits`. The question nothing else answers.

**"Tell me when it happens."** Subscribe and the bot pushes bursts the tick they form, launches
and signals as they cross a threshold, and a daily digest at 18:00: what was entered, bought, left,
said, who joined the roster — and how the day's bursts turned out.

---

## Where it goes next

**An execution engine.** The signals are meant to be acted on. Everything so far has been about
earning the right to act: resolving the real wallet, proving the book against an independent
source, grading the scores out of sample, and — as of today — knowing which fills were never the
wallet's. The engine reads `/api/hot` before it reads anything else.

**Re-scoring on clean tape.** Verdicts were formed on histories that included gifted fills. Once
every fill of the last month has been checked against its receipt, the wallets with a visible
share of fills that were not theirs get judged again on what they actually did.

**A market benchmark for the calibration.** Right now the bands can be compared to each other but
not to the market. The burst scorecard is the first piece of that: an entry price and what
followed, for every alert, kept forever.

**Wider coverage.** The architecture is chain-agnostic; Robinhood Chain is where the edge is
cheapest today because almost nothing else indexes it properly.

---

## Facts and figures, current (2026-09-11)

| | |
|---|---|
| Traders scored | **374** (169 active, 153 watch, 52 dropped) |
| fomo users known | **912**, 562 with a wallet, 234 of them verified by fomo's own data |
| Wallet resolution vs fomo's verified wallets | **101 agree, 0 disagree** |
| On-chain fills | **116,420** across 36 days |
| Fill to database | **~20 seconds** (watcher); 15-minute pass behind it |
| Tokens tracked | **5,918** |
| Theses collected | **97** |
| Highest-scored wallet | **unipcs, 96** — $19.5M over 30 days |
| Book reconstruction accuracy | **0.08%** against fomo's own figure |
| Burst rule, replayed over 36 days | fires ~5/day, 39% reach 2x, vs 52/day and 36% for the plain feed |
| Fills found not to be the wallet's own | **~1 in 11** on the first 3,600 re-checked |
| Seeded tokens caught in the first day | **5**, no false positive |
| fomo collection cost | ~30 credits/day of a free 1,000/month |
| Cost to run | **$0 / month** |

---

## Talking points that land

- Two RPC calls track the entire roster. Not two per wallet.
- The address fomo gives you for a trader has never made a trade.
- Our wallet inference was checked against fomo's own verified wallets: 101 for 101.
- On this chain, "smart money bought" can be faked for fifty cents a wallet. One buy in eleven
  credited to a trusted wallet in the last day was not that wallet's trade. We have the receipts.
- Seeding a token into famous wallets now makes it *disappear* from the feeds. The attack pays for
  its own detection.
- Conviction is squared on purpose: anyone can open a wallet, so headcount is the wrong unit.
- A burst is conviction with a clock on it: four 80s inside half an hour. Twenty seconds from the
  fill to the alert.
- The burst bar came from a replay of 116,000 fills, and the replay says it is a coin with a long
  tail. That number is on the page.
- The one number that separates skill from luck is the cost basis, and almost nobody shows it.
- A token can top the entry feed in the morning and the exit feed by evening. CRUMBS did.
- We grade our own scores, publish the result, and it is currently only half good. That is the
  part that makes the other half believable.
- It runs for nothing, and every attempt to pay for better data made it worse.

---

## How to talk about it

The product's voice is flat, specific and slightly stubborn. It states numbers, names what it
cannot know, and does not oversell. That restraint *is* the pitch in a market full of people
promising signals: the credible move is showing the method and admitting the gaps.

The provenance story is the strongest single piece of content the project has, and it should be
told with the receipts: the transaction hash, the four outside keys, the seventeen wallets that
did nothing, the thirty-five dollars. It is a story about every tracker on every chain, told
from the one place that measured it.

Things to avoid: calling it a signal group, promising returns, describing the scores as
predictions. A score is an opinion about whether a result looks repeatable, formed from an open
book and a trading record. A burst is a fact about timing, not a forecast.

Nothing here is financial advice.
