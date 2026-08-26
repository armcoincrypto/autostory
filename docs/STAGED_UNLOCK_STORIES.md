# Stories staged unlock (do not flip until operator confirms)

Live Telegram Story publish remains **fail-closed** after this release.
The UI supports multi-select + Dry Run + Auto Story campaigns immediately.
Publish and scheduled waves stay locked until you unlock **selected accounts only**.

## Stage A — UI + Dry Run + Auto draft (current default)

Keep:
```bash
STORY_MUTATIONS_ENABLED=false
CONTROLLED_STORY_EXECUTION_ENABLED=false
STORY_EXECUTION_MODE=disabled
SCHEDULER_STORY_EXECUTION_ENABLED=false
STORY_ACCOUNT_MUTATION_ALLOWLIST=
```

Operator can: chat Tools 1–5, Dry Run, create Auto campaigns (draft). Live publish and scheduler ticks stay locked.
UI shows **live unlocked** only when mutations + controlled flags are on **and** the account is on the allowlist (no false unlock from legacy canary 140).

## Stage B — Selected-account live (manual first wave)

After a clean Dry Run on the accounts you chose (e.g. 10 Ready IDs):

```bash
STORY_MUTATIONS_ENABLED=true
CONTROLLED_STORY_EXECUTION_ENABLED=true
STORY_EXECUTION_MODE=controlled-canary
STORY_ACCOUNT_MUTATION_ALLOWLIST=<comma-separated selected ids only>
CONTROLLED_STORY_ACCOUNT_ID=<one of those ids>   # legacy compat
SCHEDULER_STORY_EXECUTION_ENABLED=false          # keep off for first wave
```

Restart `autostory-web`. In Stories → tool **5 Auto**: Create/Start → **Run this wave now**.

Confirmation token:
- single account: `LIVE_STORY_ACCOUNT_<id>`
- multi: `I_CONFIRM_STORY_PUBLISH`

## Stage C — Continue scheduled waves

Only after the manual first wave succeeds:

```bash
SCHEDULER_STORY_EXECUTION_ENABLED=true
```

Restart `autostory-web` **and** `autostory-scheduler` (scheduler WD/PYTHONPATH must point at the same release).

Auto Story then fires remaining slots (times spread 10:00–20:00, `posts_per_day`, until `duration_days` ends or you hit **Off / Pause**).

Each wave: **same** media + caption for all selected accounts; **different** never-mentioned users per account (no reuse within the wave).

## Stage D — Expand allowlist carefully

Grow `STORY_ACCOUNT_MUTATION_ALLOWLIST` only with Ready ids you intend to publish. Do not unlock the whole fleet.

## Off / emergency

- UI **Off / Pause** → campaign `paused` (no more waves)
- Emergency kill: `STORY_MUTATIONS_ENABLED=false` (and/or empty allowlist)

## Never reopen

- `/api/stories/publish`
- `/api/stories/batch`
