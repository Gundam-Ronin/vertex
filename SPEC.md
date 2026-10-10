# Vertex — Overnight Card Buy/Sell Engine — Build Spec v1

Oct 9, 2026 · @Anthony

**Purpose:** Drop this into Claude Code as the build brief. It defines what to build, the exact tools, the buy rule, and the kill-number that tells you to scale or shut down.

## 1. What this is

An overnight, human-in-the-loop engine that:

1. Watches a fixed watchlist of cards ending at auction tonight.
2. Pulls real sold comps and computes a max bid per card.
3. Produces a bid list a human approves.
4. Executes the approved bids via a cloud sniper at auction close.
5. Lists and reprices the cards that win.
6. Measures true profit-per-bid so you know whether to scale or kill.

The edge is buying below comp at auction close and reselling at or near comp. That edge compresses as more people automate — the engine's own measurement loop is what tells you, in real numbers, when it has compressed past the point of being worth your time.

## 2. Hard guardrails (legal — non-negotiable)

- eBay's User Agreement (effective Feb 20, 2026) bans end-to-end buy-for-me agents that place orders without human review.
- Bid sniping — software that places a pre-set bid for you at close — is explicitly allowed by eBay.
- Seller-side automation (listing, relisting, repricing, cross-listing) is permitted.
- Therefore the AI never buys autonomously. It produces a bid list, a human approves the list, and the sniper executes only the approved list. That human approval is the required human-review step, and the whole reason the model is legal. Do not design it out.

## 3. Buy side

**Universe:** a fixed watchlist of roughly 30 players or sets to start, the way Sports Card Radio does it. Not everything.

**Value band:** target cards that resell in the $20–$80 range. Avoid the $1–$10 band — the flat per-order fee plus postage is a brutal percentage drag there, and packing hundreds of cards a day is a full-time job for pennies.

**Comp logic:** for each candidate, pull real sold comps, not asking prices. Use the median of the last 30 days and trim outliers. Data-confidence gate: require at least 5 real sold comps in the last 30 days. No comps, no bid — this stops the engine guessing.

**The buy rule (the real safety engine):** max bid = median comp × (1 − fee rate − shipping rate − target margin), where fees run about 14% (13.25% final value fee plus the $0.40 order fee), shipping is roughly 10–12% on a mid-value card, and target margin is about 10%. That pencils out to a ceiling of about 65% of median comp for $20–$80 cards, flexing up toward 70% on higher-value cards where shipping is a smaller share. Never bid above the ceiling.

**Volume:** place 50–100 quality bids a night, not 500. You will win roughly 10–20 — a number one person can actually pack.

**Output:** a reviewable bid list (CSV) with card title, item URL, auction end time, computed max bid, comp count, and median comp. This is what the human approves and what the sniper imports.

## 4. Sell side

- Listing: SpeedyCardLister or FlowLister (AI listing from a photo).
- Repricing: TCG Automate or 3Dsellers (rule-based).
- Cross-listing (optional): Vendoo or List Perfectly.

## 5. Measurement and the kill-number

The one metric you watch is net profit per bid placed — not per card won. The losing bids and the cards that never sell are where these engines quietly bleed, and only per-bid-placed catches the full funnel. Pull the fees from eBay's Finances API, never the Fulfillment API — Fulfillment omits promoted-listing ad fees and shipping-label costs and flatters your P&L by 5–10%. Read the number over a rolling 200-bid window (or 30 days) so it is signal, not one lucky night.

Three thresholds, all set before you start:

| Zone | Net per bid placed | Action |
| --- | --- | --- |
| Green | $0.30 or more | Working — let it run |
| Yellow | $0.00 to $0.15 | Tighten: lower the max-bid %, raise the comp-count gate, get pickier |
| Red | Below $0.00 over any rolling 200 bids, OR total net ÷ your hours under $40/hr | Kill or re-tool |

The hourly condition is the one that matters most for you: your real constraint is attention across your Doxen ventures, not cash. An engine that nets $300 a month but eats 25 hours is a $12/hr loss of your time — kill it or automate the labor away before scaling.

## 6. Tooling stack

| Role | Tool | Key detail |
| --- | --- | --- |
| Build / orchestration | Claude Code | where it is assembled and run |
| Comp agent | Claude | reads the watchlist, computes max bids, drafts the bid list |
| Live listings | eBay Browse API | what is ending tonight |
| Real sold comps | The Card API | MCP server, free tier 5,000 sales/day, captures true Best-Offer prices; CardHedger is the paid upgrade |
| Fees / accounting | eBay Finances API | true fees including promoted listings and labels |
| Sniper execution | Gixen | cloud, CSV import, group bidding (first win cancels the rest) |
| Scheduling / glue | n8n | nightly jobs and alerts |
| Dashboard (optional) | Replit | only if you want a hosted view |

Hyperagent stays on outreach only — too slow and costly for nightly listing volume, and it collides with eBay's agent ban.

## 7. Rollout phases

- **Phase 0 — paper trade (week 1):** the engine produces the bid list and predicted P&L, but you do not bid. Compare predictions to actual closing prices. Validates comp accuracy at zero cash risk.
- **Phase 1 — live small (weeks 2–4):** 50–100 bids a night, $20–$80 targets, 65% comp ceiling, human approves every list. Measure net-per-bid.
- **Phase 2 — decide:** green over 200 bids → scale volume or widen the watchlist; yellow → tighten; red → kill or re-tool.

## 8. Build order in Claude Code

1. Connect The Card API MCP and the eBay Browse API; pull tonight's ending listings for the watchlist.
2. Comp module: per card, fetch sold comps, take the median, trim outliers, apply the comp-count gate.
3. Buy rule: compute max bid and emit the reviewable bid-list CSV.
4. Human-approval gate: you edit and approve the CSV.
5. Gixen import of the approved CSV.
6. Post-win: pull won items and push them to the listing tool.
7. Measurement job: nightly pull from the Finances API, compute net-per-bid, log it against the three thresholds.

## 9. Deployment — how you actually run it

Claude Code is the build shop, not the runtime. Once the engine is built and tested there, the code runs on a schedule somewhere else — there is no Claude dashboard that hosts a live app for you. The insight that keeps this cheap: because Gixen (the cloud sniper) is the only piece that has to be awake at the exact second an auction closes, the engine itself does not need 24/7 hosting. It only runs a short job each evening — pull listings, build comps, produce the approved bid list — and Gixen handles the close-time bidding while you sleep.

**Where the nightly job runs (cheapest first):**

- Your own machine plus a scheduled task (cron or Windows Task Scheduler): free; the machine only needs to be on when the evening job fires.
- A Replit Scheduled Deployment, or a cloud function on a timer (AWS Lambda with EventBridge, or Google Cloud Scheduler): a few dollars a month, always-on for the job without depending on your laptop. Note: Replit retired its free always-on tier, so truly-free 24/7 hosting is mostly gone — and you do not need it here.
- n8n (cloud or self-hosted) can handle the scheduling, the API glue, and sending you the bid list for approval.

**Approval and dashboard:**

- A human approves the bid list every night (legal requirement plus quality control). Flow: the nightly job sends you the list → you approve → it exports the CSV to Gixen.
- Optional dashboard: a small web app (Replit is the easy host) to see tonight's picks, approve with a tap, and watch profit-per-bid — open it from your phone.
- Cheapest start: skip the dashboard. The engine emails or texts you the list, you approve in a spreadsheet, and it uploads to Gixen.

**Recommendation:** start on your own machine (or a cheap Replit scheduled deployment) with email or text approval, and add a hosted dashboard only when you want the nicer approval experience and profit tracking in one place. Keeps the build cheap, per CardDrop lesson #1.

## 10. Two CardDrop lessons that carry over

1. Keep the build cheap — rented APIs and thin glue, not a heavy custom pipeline.
2. The engine must report clean profit-per-bid, net of fees, postage and your time, with the kill-number defined before you start (done — Section 5).

This is a secondary-market eBay play (comp → snipe → relist, running on APIs), not a retail scraping and checkout-bot play. That difference is why it is buildable where CardDrop was not.

## 11. Edges beyond the base engine — the arbitrage others aren't running

The sniper engine is table stakes and will compress as more people copy Sports Card Radio. These layers are the durable advantage, because they come from assets a bot-only operator doesn't have: distribution, a demand signal, and sport-specific timing.

### 11.1 Sell into the group, not just back onto eBay

Jake's group is roughly 7,000 members with about 30 active buyers. Reselling a won card back on eBay gives back around 13% in sell-side fees. Offer each night's wins to the group first, at a markup that still sits below public comp; only relist on eBay what the group doesn't absorb. Two gains: you keep the ~13%, and the group is a demand signal — you see what's hot before the broader eBay market reprices it. Build: a simple nightly "here's tonight's inventory" post or DM flow before anything hits eBay.

### 11.2 Sell the signal — a "sharp picks" subscription

The nightly undervalued-auction list Vertex produces is a product on its own. A paid feed to the group is recurring revenue at near-zero marginal cost — the Sports Card Investor / Market Movers model, but powered by The Card API's true accepted-offer prices, not guide estimates. Honest caveat: if subscribers bid on the same auctions you target, you compress your own edge. Resolve it by separating lanes — publish picks in categories or price bands you are not flipping yourself, or point the feed at cards you've already bought and are reselling ("what's undervalued right now" = your own inventory). Never sell the exact bids your sniper is about to place.

### 11.3 Catalyst buying — the event-driven layer

The finance-meets-sports edge: card values move on public, scheduled catalysts — nationally televised games, playoff runs, rookie debuts. Pre-position cheap on a slate of likely performers before the catalyst, sell into the post-performance spike. Treat it as a small, higher-variance satellite on top of the core close-discount flipping: size positions small, spread across a slate so winners cover misses, and measure its profit separately from the base engine so it can be killed on its own.

### 11.4 Capital and liquidity guardrail (fixes a hole in the kill-number)

Profit-per-bid doesn't see capital stuck in cards that won't sell. Add two hard limits: (a) a liquidity filter — only bid on cards with genuine velocity, for example at least 8 real sales in the last 30 days rather than just 5; (b) an open-inventory cap — a ceiling on dollars tied up in unsold cards at once, and pause buying when it's hit. Track median days-from-win-to-sold as a cash-conversion metric alongside profit-per-bid.

### Also worth testing later

- Cross-platform routing: The Card API covers 5 marketplaces — buy on eBay at the close discount, resell wherever that card sells highest (MySlabs, COMC, Whatnot), not reflexively on eBay.
- Auction-end timing: buy at dead-hour closes when bidding is thin, relist to end at high-traffic windows such as Sunday evening, capturing the liquidity premium.
