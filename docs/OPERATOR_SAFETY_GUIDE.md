# STORYFLEET Operator Safety Guide

**Production:** https://zellotex.com/  
**Server path:** `/opt/autostory`

This guide explains how to safely operate STORYFLEET and avoid Telegram bans, freezes, and rate limits.

---

## What Changed

1. **Central safety policy** – All story eligibility decisions go through `src/core/safety_policy.py`. The system prefers false-negative over risky false-positive.

2. **Warmup enforced** – Newly imported accounts must wait before first story (48–72h depending on import source). This is enforced in code, not just advice.

3. **Story cooldowns** – Every story attempt (success or fail) triggers a cooldown. Rate-limit/frozen results trigger a stronger cooldown.

4. **Daily caps** – Max story attempts, successes, and failures per account per day are enforced.

5. **Bulk protection** – Username and profile-photo bulk changes are capped per hour and per batch. Jitter is applied between accounts.

6. **Manual review** – Accounts with suspicious combinations (recent import + no successful story) may require manual review before story use.

7. **Precheck TTL** – Story precheck results expire (default 24h). Stale precheck must be refreshed before story publish.

8. **Single source of truth** – `get_story_safety_decision()` is used by `/stories/publish`, batch publish, and `get_eligible_account_ids`. No duplicate logic.

---

## What Is Now Blocked

| Action | Block |
|--------|-------|
| Story on newly imported account | Warmup period enforced (48–72h) |
| Story when precheck is stale | Must run story precheck first |
| Story when frozen/rate-limited | Blocked until Telegram unblocks |
| Story when cooldown active | Wait for cooldown |
| Story when daily cap exceeded | Try tomorrow |
| Bulk username change on many accounts | Max 5 per batch, 5 per hour (configurable) |
| Bulk profile photo on many accounts | Max 3 per batch, 3 per hour (configurable) |
| Story on account flagged for manual review | Must clear `manual_review_required` |

---

## How to Safely Use Newly Imported Accounts

1. **Import** – Add account via TDATA, QR, phone, or paste.

2. **Health check** – Run "Check if accounts are alive" to verify session and general health. Do not assume story-ready.

3. **Wait** – Do **not** post stories immediately. Default warmup: 48h (paste), 72h (tdata), 24h (QR/phone).

4. **Story precheck** – Before first story, run story precheck for the account. This tests Telegram’s actual story permission.

5. **Gradual use** – Start with 1–2 stories per account per day. Avoid bulk runs on warming accounts.

6. **Purpose** – Set Purpose to "Both" or "Autostory" if you intend to use for stories.

---

## How to Avoid Telegram Bans/Freezes

- **Alive ≠ story-ready** – Health check only verifies API connectivity. Story readiness requires warmup, precheck, cooldowns, and caps.
- **Avoid bulk patterns** – Do not change usernames or photos on many accounts at once. Use the built-in limits.
- **Respect cooldowns** – Do not retry failed stories immediately. The system enforces cooldowns.
- **Monitor story status** – Use the dashboard to see story_status (frozen, rate_limited, restricted). Do not invent unblock dates.
- **Run precheck before batch** – If using batch publish, ensure accounts have recent precheck where applicable.

---

## Labels and Risk Levels

| Label | Meaning |
|-------|---------|
| **Ready** | Can publish a story now (passes all checks) |
| **Warming** | New import; must wait before story |
| **Risky** | Has suspicious history; may need manual review |
| **Frozen by Telegram** | Story publishing blocked; unblock time unknown |
| **Rate limited until ...** | Cooldown with known expiry |
| **Manual review required** | Operator must review before story use |
| **Safe for scheduler only** | Can be used for messaging, not stories |
| **Not safe for story use** | Excluded from story publishing |

---

## Config (env / config/settings.py)

Relevant `WARMUP_*` settings:

- `min_account_age_hours` (default 48)
- `min_account_age_hours_tdata` (72)
- `story_attempt_cooldown_minutes` (60)
- `max_story_attempts_per_day` (5)
- `max_story_successes_per_day` (3)
- `max_bulk_username_batch` (5)
- `max_username_changes_per_hour` (5)
- `max_bulk_photo_batch` (3)
- `max_profile_photo_changes_per_hour` (3)
- `precheck_ttl_minutes` (1440 = 24h)

---

## Debug: Why Is Account X Not Story-Eligible?

Use: **GET** `/api/accounts/<id>/story-eligibility`

Returns the full safety decision with:
- `allowed`
- `reason_code`
- `human_reason`
- `operator_action`
- `next_allowed_at`
- `risk_level`

---

## Restart Commands

```bash
sudo systemctl restart autostory-web.service autostory-scheduler.service storyfleet-bot.service
```

---

## Verification

1. **Single publish:** `POST /api/stories/publish` with `account_id`, `media_path`. Blocked accounts receive 403 with `reason_code`, `operator_action`, `next_allowed_at`.
2. **Batch preview:** Use Stories → preview batch. Check exclusion summary in logs.
3. **Story eligibility:** `GET /api/accounts/<id>/story-eligibility` for any account.
