# Vertex — Phase 0 (paper trade)

The nightly comp engine from `SPEC.md`, Phase 0 only (Section 7, build-order steps 1–3):

1. read `watchlist.csv`
2. pull tonight's ending auctions for those players from the **eBay Browse API**
3. comp each one against **The Card API** (real sold sales, last 30 days, outliers trimmed, ≥ 8 comps)
4. apply the buy rule and write **`bid_list.csv`**
5. log every prediction so it can be scored against the actual closing price

**Paper trade only.** Nothing here bids, and there is no Gixen integration yet. The eBay token
is an application token (client-credentials grant), which can read listings but cannot bid or buy.

## Run it

```bash
python -m pip install -r requirements.txt
export EBAY_CLIENT_ID=...      # eBay production keyset
export EBAY_CLIENT_SECRET=...
export CARD_API_KEY=...        # tca_...
python -m vertex                              # auctions ending in the next 12 hours
python -m vertex --hours 8 --tz America/New_York
```

The next day, score the predictions against what the auctions actually sold for:

```bash
python -m vertex.reconcile
```

Options: `python -m vertex --help`. Tests: `python -m pip install -r requirements-dev.txt && python -m pytest`.

### Schedule it (cron example, 6:45pm local every evening)

```cron
45 18 * * * cd /path/to/vertex && . ./.env && python -m vertex --tz America/New_York >> logs/cron.log 2>&1
15 12 * * * cd /path/to/vertex && . ./.env && python -m vertex.reconcile >> logs/cron.log 2>&1
```

## Your Card API plan needs 30 days of history

The Card API's free plan only returns the **last 3 days** of sales (Starter 14 days; **Builder, $49/mo,
30 days**; Pro 90 days). Asking for older sales is silently clamped, not refused. Vertex reads the
coverage the API reports and **stops with an error** if it is shorter than the buy rule's 30 days,
rather than quietly computing a 3-day median. To paper-trade the plumbing on a smaller plan
anyway, add `--allow-short-comp-window`. The window actually used is recorded in
`logs/runs.csv` (`comp_coverage_from`).

Every row the API returns counts against your daily allowance (5,000 rows on Free, 50,000 on
Builder). Vertex comps each distinct card once per run, caps rows per card
(`--max-rows-per-card`, default 300), and stops comping cleanly if the allowance runs out.
Where a card has more sales than the cap, its median is taken over the most recent 300.

## eBay call budget

The Browse API allows 5,000 calls a day per app. Searching is cheap (200 auctions per call),
but reading a listing's item specifics (Set, Card Number, grade) is one call per auction. eBay's
bulk lookup is partner-only. So Vertex filters on title and price first, and only looks up
auctions that could still produce a bid. It caps lookups at `--max-item-lookups` (default
2,500) per run, soonest-ending first. Reconciling uses up to another 1,500 (`--max-lookups`).

## watchlist.csv

| column | meaning |
|---|---|
| `player` | Player name. Every word must appear in the listing title and in each comp. Prefix with `#` to disable a row. Leave blank for a set-level row. |
| `include` | Optional extra search words, e.g. `Prizm`, `2023 Bowman Chrome`. |
| `exclude` | Optional words to skip, separated by `;`, e.g. `auto;patch`. |
| `category_id` | Optional eBay category. Blank = Sports Trading Card Singles (`261328`). Pokémon/TCG singles are `183454`. |

The starter rows are placeholders; replace them with your own ~30.

## The buy rule

Set in `vertex/config.py`, decided with you on 2026-10-10:

```
sell-side net  = median − (13.25% × median + $0.40) − $4.00 outbound postage
max all-in     = min(sell-side net − 10% × median,  70% × median)
max bid        = max all-in − the auction's own shipping charge to you
```

* **Median comp** is the median of real sold sales from the last 30 calendar days (today included), after dropping prices
  outside Tukey fences (Q1 − 1.5×IQR, Q3 + 1.5×IQR).
* **Comp gate:** at least **8** sales must remain *after* trimming (SPEC §11.4), or no bid.
* **Value band:** median comp must be **$20–$80**, or no bid.
* **Shipping:** the cheapest fixed shipping option is subtracted. Calculated or unknown shipping means no bid.
* Because postage is a fixed $4, the ceiling flexes with value: about 55% of median at $20,
  66% at $40, capped at 70% from about $65 up.
* Max bids are rounded **down** to the cent.
* **Predicted profit** = profit if you win at exactly your max bid (the worst case): 10% of median
  where the cap doesn't bind, more where it does.

A listing that passes the rule is still left off the bid list if its current price plus eBay's
bid increment is already above the max bid (the bid could not be placed). It stays in the log.

## Which comps count

A watchlist row names a player, but a player has hundreds of different cards, so each auction is
identified individually from its title plus eBay item specifics (Set, Card Number, Season,
Parallel/Variety, grader and grade). A sold comp counts only if it is the same card:

* same player, year, set, and card number
* same grading company **and** grade, or both raw. "PSA 10 candidate?" style raw listings count as raw.
* the same parallel markers: color, refractor, holo, auto, patch, `/99` print runs, 1/1, SP/SSP, and so on
* not a lot, reprint, custom, damaged card, or sealed product
* a confirmed sale price in USD on eBay (`--comp-platform all` to include the other marketplaces)

Matching fails closed: if the listing's identity is unclear (no Set item specific, conflicting
grade information, or a grader without a grade), it gets no bid. Every rejected comp is counted
by reason in the audit file, so you can see when matching is too strict.

## Files

| file | what |
|---|---|
| `bid_list.csv` | Overwritten each run. Columns: `card_title, item_url, auction_end_time, max_bid, comp_count, median_comp, predicted_profit`, sorted by end time. This is what you review. |
| `logs/predictions.csv` | Appended each run. One row per auction evaluated, bid or not, with the reason for every no-bid, plus `actual_close_price, actual_bid_count, would_have_won, profit_at_close, close_source` for scoring later. |
| `logs/runs.csv` | Appended each run: counts, total max-bid exposure, predicted profit, Card API rows used, comp window. |
| `logs/comps/<run_id>.jsonl` | The exact comps behind each median, with rejection counts. Use it to audit matches. |

### Scoring paper trades (`python -m vertex.reconcile`)

For every logged auction that has ended and has a median comp, reconcile asks eBay how it
ended; eBay keeps ended listings readable for a few days. If eBay no longer has it, reconcile
falls back to The Card API and matches the item number (once the sale is indexed, about 36
hours later). It fills these columns:

* **`actual_close_price`, `actual_bid_count`, `close_source`** (`ebay`, `ebay_unsold`, `card_api`,
  or `not_found` after 7 days).
* **`would_have_won`:**
  * **`no`** if the auction closed at or above your max bid. A tie goes to the earlier bidder.
  * **`possibly`** if it closed below your max bid, or ended with no bids at an opening price
    your max covered. It is only "possibly" because a hidden max above yours could still have won.
* **`profit_at_close`:** for `possibly` rows, the best-case profit if you had won. To beat the winner
  a sniper pays at least one bid increment over the close, capped at your max; for a no-bid
  auction it pays the opening price.

What to look at after a week: the share of `BID` rows that are `possibly`, and the sum of
`profit_at_close` over them, against the predicted profit.

## Not built yet (later phases)

Human-approval flow, Gixen import, post-win listing, Finances-API measurement, and the
open-inventory cap (SPEC §8 steps 4–7, §11.4b).
