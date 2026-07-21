# Social Provider Capability Matrix

Status values below reflect this host's certified implementation, not theoretical provider marketing claims.

| Provider | Existing adapter | Connect | Publish | Schedule | Analytics | Comments | Messages | Current status |
|---|---|---|---|---|---|---|---|---|
| Telegram | Telethon user-account implementation, not target canonical adapter | session import/login | stories and chat messages exist | persisted scheduler exists | no target social analytics module | not unified | negotiation DM flow exists | implemented legacy; live locked; needs adapter consolidation |
| Facebook | none found | not started | not started | not started | not started | not started | not started | not started |
| Instagram | none found | not started | not started | not started | not started | not started | not started | not started |
| X | none found | not started | not started | not started | not started | not started | not started | not started |
| LinkedIn | none found | not started | not started | not started | not started | not started | not started | not started |
| Discord | none found | not started | not started | not started | not started | not started | not started | not started |
| YouTube | none found | not started | not started | not started | not started | not started | not started | not started |
| TikTok | none found | not started | not started | not started | not started | not started | not started | not started |

## Capability policy

Every provider adapter must return explicit support metadata for:

- account connection and refresh;
- destination discovery;
- text, image, carousel, story, short-video, and long-video publishing;
- native scheduling;
- post-status reconciliation;
- metric names and reporting windows;
- comments and replies;
- messages and replies;
- deletion/revocation;
- sandbox/test mode;
- idempotency support.

Unsupported operations return a typed `UNSUPPORTED_CAPABILITY`; they must not be simulated.

## Provider order

Discovery supports Telegram first because substantial reusable implementation exists. Before certification:

1. consolidate Telegram's scheduler-executor and gateway rails behind one canonical service;
2. retain all production kill switches;
3. implement dry-run payload rendering and destination resolution;
4. add an official bot/API adapter where business posting requirements permit, rather than assuming user-account automation is appropriate;
5. certify connection and dry run only;
6. require separate authorization for a real canary.

Meta should follow Telegram only after app ownership, review mode, required permissions, webhook verification, and test assets are available. Other providers remain phase-gated.
