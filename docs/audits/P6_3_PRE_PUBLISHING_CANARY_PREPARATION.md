# P6.3 Pre-Publishing Readiness and Canary Preparation

## Verdict

`P6_3_PRE_PUBLISHING_READINESS_AND_CANARY_PREPARATION_PASS`

## Selected Canary Pair

| Field | Value |
|-------|-------|
| Account | **107** |
| Target | **1** (`cryptodiscussing`) |
| Binding ID | **39** |
| Verification | `VERIFIED_CAN_POST` (fresh probe `2026-07-13T12:44:01Z`) |

## Disconnected-Client Root Cause

`membership_check.check_target_membership_for_account` used `client_manager.add_account()`, which creates a Telethon client without connecting. Probes then hit `ConnectionError: Cannot send requests while disconnected`.

**Fix:** use `connect_account()` + `remove_account()` in `finally`.

## Production Refreshes (non-sending)

| Binding | Result |
|---------|--------|
| 39 (107→1) | `joined`, `can_post=True` — **VERIFIED_CAN_POST** |
| 38 (106→1) | `ChannelForbidden` — blocked |
| 45 (107→14) | `User` entity — target 14 is private Saved Messages, not promo channel |

## Remaining Blockers Before Live Send (P6.4)

- P6.2 24h soak not complete
- `NO_GO` / `PROMO_GENERATION_MODE=disabled` still block execution
- Gateway inactive; gateway path does not independently re-verify binding freshness (eligibility layer now does)
- First send requires explicit P5C/P5D-style scoped authorization
