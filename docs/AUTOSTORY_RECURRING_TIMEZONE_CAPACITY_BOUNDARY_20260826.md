# AutoStory Recurring: Campaign-Local Day vs Global UTC Capacity — Boundary Policy

Date: 2026-08-26
Wave: `feature/autostory-production-canary-hardening` (Wave 1B)
Status: Accepted for production certification. No code change to the global
counter's timezone basis. Fail-closed behavior formally proven in
`tests/test_recurring_daily_certification.py`
(`test_recurring_local_day_ahead_of_utc_capacity_blocks_not_overflows`,
`test_recurring_local_day_behind_utc_never_double_counts`).

## The two clocks

AutoStory recurring targets are gated by two independent counters that use
two different calendar-day definitions:

1. **Campaign-local day** — `campaign_local_today()` /
   `AutoStoryDailyProgress.local_date`
   ([src/stories/autostory_recurring.py](../src/stories/autostory_recurring.py)).
   Resolves the operator timezone (`SCHED_DEFAULT_TIMEZONE`, Asia/Yerevan in
   production) and computes the local calendar date. This is the day used
   for the 1–3 Stories/account/day target and the multi-day campaign
   calendar.

2. **Global account day** — `Account.stories_today` /
   `stories_today_on`, rolled over by `ensure_stories_today_current()`
   ([src/stories/daily_story_counter.py](../src/stories/daily_story_counter.py)).
   Explicitly UTC calendar day, matching Celery's `timezone=UTC` and
   `utc_day_bounds_naive`. This counter is shared by **every** Story
   publishing path (legacy `accounts_publish_once`, manual Stories, and
   recurring), not just AutoStory recurring — it is load-bearing
   infrastructure well outside this wave's scope.

Effective per-account daily remaining is
`effective_remaining_today = min(campaign_remaining_today, platform_remaining_today)`
([autostory_recurring.py `effective_remaining_today`](../src/stories/autostory_recurring.py)).

## Where the two clocks disagree

Asia/Yerevan is UTC+4 with no DST. Local midnight (start of a new
campaign-local day) occurs at **20:00 UTC of the preceding UTC calendar
day** — four hours *before* the global UTC-day counter rolls over at
00:00 UTC.

```
UTC:      ...18:00   20:00 -------- flip A -------- 00:00   ...
Yerevan:  ...22:00   00:00 (new local day starts)     04:00
Global (UTC day):  unchanged until -----------------> 00:00 (flip B)
```

Between flip A and flip B (a ~4h window every day, always in this order for
a positive UTC-offset operator timezone), `campaign_local_today()` already
reports the new local date while `Account.stories_today` / `stories_today_on`
still reflects the **previous** UTC day's usage.

## Effect on `effective_remaining_today`

Because the composition is a `min()` of the two counters, the two-clock
split can only ever be **fail-closed** in this window, never
**over-permissive**:

- **Campaign ahead of global (the Asia/Yerevan production case).** During
  the ~4h window, `campaign_remaining_today` resets to a fresh target for
  the new local day, but `platform_remaining_today` still reflects the
  stale, possibly-exhausted prior UTC day. `min()` picks the smaller value
  — if the account already used its full global cap the day before, it is
  blocked from publishing on the new local day until the global counter
  rolls over (up to ~4h). **A legitimate Story can be transiently delayed.
  A duplicate/over-cap Story can never be published** — the global side of
  the `min()` never grants more than the account's real remaining global
  capacity.

- **Campaign behind global (mirror case for a negative-offset operator
  timezone, not the current production config but proven for
  completeness).** The global UTC counter rolls over first, briefly
  offering a "fresh" global allowance while the campaign-local day (still
  in progress) keeps gating on its own partially-consumed target. `min()`
  again picks the smaller, already-correct campaign-side value — the
  fresher global counter cannot inflate what the campaign day allows.

In both directions the mismatch window can only **under-allow**, never
**over-allow**. There is no code path in `effective_remaining_today`,
`select_next_recurring_wave_accounts`, or `account_has_daily_capacity` that
takes the `max()` of the two clocks or ignores either one.

## Decision

Adopt the conservative pairing explicitly as the certified production
policy for this wave, per the fallback option in the Wave 1B brief:

- **Campaign day = operator timezone** (Asia/Yerevan in production) — this
  is the day the operator and the daily 1–3 Stories/account/day target are
  reasoned about.
- **Global capacity = UTC-day guardrail** (`Account.stories_today`,
  unchanged) — this stays the single, already-shared cap used by every
  Story publishing path across the platform.

**No change was made to `daily_story_counter.py` / the global counter's
timezone basis.** Harmonizing it to the operator timezone would touch
every Story publishing path (legacy `accounts_publish_once`, manual
Stories, warmup, etc.), not just AutoStory recurring, and is out of scope
for a "smallest change" hardening wave — it needs its own migration and
compatibility analysis if pursued later.

## Operational consequence to expect in production

Around 20:00–24:00 UTC daily (00:00–04:00 Yerevan), an account that already
hit its global daily cap the prior UTC day will show 0 remaining for the
*new* campaign-local day's target until the UTC counter rolls over, even
though the campaign-local calendar already considers it "tomorrow." This
is expected, is not a bug, and self-heals within the same ~4h window every
day. It does not block same-day capacity for accounts that have *not* hit
their global cap.

## Proof

- `tests/test_recurring_daily_certification.py::test_recurring_local_day_ahead_of_utc_capacity_blocks_not_overflows`
  — reproduces the Asia/Yerevan-ahead window; asserts `effective_remaining_today == 0`
  while the stale UTC counter is exhausted, and `== 3` once it rolls over.
- `tests/test_recurring_daily_certification.py::test_recurring_local_day_behind_utc_never_double_counts`
  — mirror case; asserts the fresher global counter never inflates the
  campaign-local remaining count.
- `tests/test_recurring_daily_certification.py::test_recurring_midnight_boundary`
  — pre-existing: `campaign_local_today()` flips exactly at Yerevan local
  midnight (20:00 UTC), independent of the UTC calendar day.
