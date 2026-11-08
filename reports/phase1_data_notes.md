# Phase 1 data notes

My interpretation of `reports/phase1_data.md` (which is generated). Every gap and outlier is explained below.

## Coverage
- 181 of 181 days have option, perp and dated-future trades.
- DVOL and funding each have exactly 24 hourly rows per day.
- Every traded instrument is in Deribit's instrument list.
- Every expired option expiry has an official delivery price.
- The config specs match Deribit's records for all instruments alive in the period.

## Volume spikes (real market events)
| Date | Options/day | Perp/day | Event |
|---|---|---|---|
| 2025-02-03 | 31k | 550k | Weekend tariff headlines and an ETH crash; the largest day in the sample |
| 2025-04-07 to 09 | 18–19k | 230–330k | Tariff selloff; DVOL peaks at 82 |
| 2025-05-08 to 09 | 20–22k | 240k | Sharp ETH rally |
| 2025-06-13 | 14–16k | 165k | Israel–Iran escalation |

These are the natural candidates for **stress periods**. Under the CLAUDE.md time split, though, the DVOL threshold that labels calm vs stress must come from the training period only, so the labels are set in Phase 8, not here.

## Low-activity days
Of the 9 days below 40% of the median, 8 are weekends; the other is 1 Jan (a holiday). Nothing is missing.

## Exchange-wide pauses
Seven times, all streams stop together starting at **09:00 UTC**:
- 8 Jan (13 min)
- 4 Feb (12)
- 6 Mar (28)
- 1 Apr (12)
- 15 Apr (11)
- 13 May (10)
- 10 Jun (19)

That pattern is consistent with scheduled exchange maintenance (*not yet confirmed against Deribit's status history*). There is one off-schedule pause on 23 Apr at 14:10 (about 9 min), probably an incident.

**What this means for the simulator (Phase 4):** no trades means no fills during a pause, but the latest underlying, forward and surface inputs are stale when trading resumes. The simulator should treat a pause as a break: no carry-over of pre-pause quotes, and a refit before quoting again. Log in `reports/extensions.md` if not built.

## Perp-only quiet spells
9 Feb, 16 Feb and 5 Apr (weekend nights): the perp was silent for 5–6 min while options kept trading. That's thin weekend liquidity, not a pause.

## Dated-futures staleness
Gaps of 10–30 min between dated-future trades (all expiries combined) are common at weekends and in Asian hours. A forward taken from the *last future trade* can therefore be up to half an hour old. That is relevant to Phase 2's forward construction.

## Suspect prints (kept, flagged; D013)
17,407 option prints (1.0% of kept trades):
- `iv_nonpositive`: 8,418
- `iv_high` (above 300%): 7,193
- `far_from_mark`: 1,796

Phase 2/3 decide which of these feed the surface.

## Filters
- Removed: 16,082 block trades and 7,392 combo legs (1.4% of raw option trades), 0 invalid option prints.
- Futures and perp: 1 invalid print.
- The invalid rule and suspect thresholds are in decision D013.
