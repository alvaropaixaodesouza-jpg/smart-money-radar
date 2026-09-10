# FOMO Robinhood Radar — the whole product, in one file

Written to be fed to an AI when producing posts, threads, landing copy or pitch material. It is
deliberately long. Take what you need.

Live: **https://fomoradar.app** · Bot: **@fomoradarRH_bot** · API: **https://fomoradar.app/docs**

---

## The one-sentence version

Robinhood Chain has a leaderboard of memecoin traders. It shows you a number. This shows you
whether the number means anything, whose wallet it belongs to, and what that wallet is buying
right now.

## The one-paragraph version

fomo.family ranks traders by profit. A big number there can mean a person who reads launches
better than anyone on the chain, or one lucky ticket bought with beer money. The leaderboard cannot
tell you which, will not tell you which wallet they actually trade from, and shows you nothing
about what they are doing today. FOMO Robinhood Radar resolves the profile to the real on-chain
wallet, rebuilds the whole book from the chain's own logs, has Claude judge whether the record
looks repeatable, and then publishes what the wallets that passed are buying and leaving. Every
data source it uses is public and free.

---

## Why this exists

Copy-trading on a memecoin chain has one hard problem, and it is not finding trades. Trades are
everywhere. The problem is that **a profit number tells you almost nothing about the person who
made it.**

Three separate things hide behind one figure:

**Was it skill or a ticket?** Somebody who put $2,000 into one token that ran 780x has the same
30-day PnL as somebody who took six-figure positions in four names and was right about all of
them. The leaderboard ranks them together. Only one of them can do it again.

**Whose wallet is it?** Every address fomo's API returns for a user is an internal account with
zero on-chain history. Follow it and you watch nothing happen forever. The wallet that actually
trades is not published anywhere.

**What are they doing now?** A leaderboard is a scoreboard for last month. The token somebody
bought three weeks ago and sold two weeks ago is still holding up their rank today.

The Radar answers all three, and it answers them from the chain rather than from anybody's
dashboard.

---

## How it works, end to end

### 1. Find the traders

fomo's leaderboards, plus the top holders of every fresh token that shows up. **808 fomo users
known** so far. This is the only step that needs fomo at all.

### 2. Resolve the profile to a real wallet

The interesting part, and the thing that took longest to get right.

fomo publishes *when* a user traded *what* — but not from where. So the wallet is inferred: take a
dozen of a trader's swaps, ask the chain who else traded that same token in that same minute, and
intersect. Quiet windows count for more than busy ones, because being one of three buyers in a
minute is evidence and being one of three hundred is not.

Measured against a hand-checked set: **41 correct out of 42**. When two handles claim the same
wallet the resolver refuses to guess and records the conflict rather than attaching a book to the
wrong person.

### 3. Track them for free, forever

Every trade on Robinhood Chain routes through one relayer contract, which means the trader's wallet
is never the transaction's sender. Searching by sender finds nothing. That single fact is why most
tooling does not cover this chain.

What works instead: read the chain's transfer logs, filter for the leg facing the relayer, and the
direction falls out. **Two RPC calls cover the entire roster.** Not two per wallet. Two.

Current tape: **109,669 fills across 34 days**, and it costs nothing, because the chain's public RPC
is free and the whole roster fits in one query.

### 4. Rebuild the book from the tape

Every buy and sell of one token folds into one position. Balances are read straight off the chain
to settle what is actually still held.

This is where most of the honesty lives. A position the wallet held before our tape begins has an
unknowable entry price, so it is shown with size and cost and **no profit figure** rather than a
made-up one. When we checked our reconstruction against fomo's own number for the same position:
**$110,932 against $110,845 — a 0.08% gap, by two completely independent routes.**

### 5. Score them

Claude reads a compact profile of each wallet — the fomo figures, the on-chain aggregates, and the
open book with cost bases — and returns a verdict: **active** (70+), **watch** (40–69) or
**dropped** (under 40), with a style, any red flags, and two or three sentences of reasoning that
cite the actual numbers.

**377 traders scored. 169 active.**

What the scoring is looking for:

- Several independent winners beat one big one.
- A real cost basis beats a large multiple on dust. `PONS at 2.1x on a 3.7M entry` is a stronger
  signal than `780x on a 2k ticket`, even though the second number is prettier.
- The losses count. Forty-eight early entries mean nothing if the same book has one position
  written off and another down two thirds.
- A six-minute median hold across 282 tokens is a bot, not a trader.

### 6. Publish what they do next

Four feeds, all reading the same database:

**Signals** — tokens several trusted wallets bought today, ranked by *conviction*: the sum of each
buyer's (score/100)². One trader at 85 outweighs a crowd at 40, because anyone can open a wallet.

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

### Theses: what they said, not just what they did

Every other number here is inferred from the tape. A thesis is the trader typing it out. fomo lets
traders attach a note to a position, and the Radar collects them and shows them under the token,
ranked by the writer's score.

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

### Calibration: we grade our own scores

Most tools that rank things never check whether the ranking predicted anything. This one does, and
publishes the method.

The test is out-of-sample by construction: only positions opened *after* a wallet was scored count,
so a verdict can never be credited with the trade it was derived from.

The current reading, and it is reported honestly rather than spun: **the score separates the top
band from the bottom** — active returns more per dollar than dropped, on win rate, median and
pooled return alike — but **it does not yet cleanly separate active from watch**. That gap is
public, and closing it is the next piece of work.

### It costs nothing to run

No paid API. No indexer subscription. No node. The chain's own RPC, GeckoTerminal's free tier, and
one browser. **$0 a month**, and that is a design property rather than a temporary state: every
time a paid source was tried, a free one turned out to answer better on this chain.

---

## What people actually use it for

**"Is this trader worth copying?"** Send a handle to the bot. You get the verdict, the reasoning,
the open book with cost bases, what has already been sold, and which other tracked wallets are in
the same names.

**"Who is in this token?"** Send a contract address. You get every tracked wallet holding it, what
it cost them, what they said about it, and the flow in and out over the window.

**"What just launched that good wallets are entering?"** `/fresh`, or the tab. Ranked by how early
they were, filtered so a token whose pool has already been drained cannot top the page on
headcount alone.

**"Did the people I copied leave?"** `/exits`. The question nothing else answers.

**"Tell me when it happens."** Subscribe and the bot pushes launches and signals as they cross a
conviction threshold, with a daily digest at 18:00: what was entered, bought, left, said, and who
joined the roster.

---

## Where it goes next

**An execution engine.** The signals are meant to be acted on. Everything so far has been about
earning the right to act: resolving the real wallet, proving the book against an independent
source, grading the scores out of sample. A signal you cannot trust is not worth automating, and a
signal you can is worth automating immediately.

**A market benchmark for the calibration.** Right now the bands can be compared to each other but
not to the market. Measuring what each token did from the moment of entry turns "active returns
0.88x" into a statement with meaning.

**Wider coverage.** The architecture is chain-agnostic; Robinhood Chain is where the edge is
cheapest today because almost nothing else indexes it properly.

---

## Facts and figures, current

| | |
|---|---|
| Traders scored | **377** (169 active, 155 watch, 53 dropped) |
| fomo users known | **808** |
| On-chain fills | **109,669** across 34 days |
| Tokens tracked | **5,574** |
| Open positions watched | **$258M** unrealised |
| Highest-scored wallet | **unipcs, 96** — $19.5M over 30 days |
| Cost to run | **$0 / month** |
| Collection cadence | every 30 minutes, unattended |
| Book reconstruction accuracy | **0.08%** against fomo's own figure |
| Wallet resolution accuracy | **41 of 42** on a hand-checked set |

---

## Talking points that land

- Two RPC calls track the entire roster. Not two per wallet.
- The address fomo gives you for a trader has never made a trade.
- Conviction is squared on purpose: anyone can open a wallet, so headcount is the wrong unit.
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

Things to avoid: calling it a signal group, promising returns, describing the scores as
predictions. A score is an opinion about whether a result looks repeatable, formed from an open
book and a trading record.

Nothing here is financial advice.
