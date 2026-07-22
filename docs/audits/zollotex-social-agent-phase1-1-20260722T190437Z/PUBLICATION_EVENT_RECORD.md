# Canonical Publication Event

| Field | Value |
|---|---|
| TIMESTAMP_UTC | 2026-07-22T14:04:35.513410Z (DB `stories.published_at`; Telegram accept ≈ same second) |
| ACCOUNT_ID | 140 |
| STORY_ID (DB) | 29 |
| EXTERNAL_TELEGRAM_STORY_ID | 2 |
| STORY_RUN_ID | 9 |
| STORY_RUN_STEP_ID | 3 |
| CANDIDATE_ID | n/a (zero mentions) |
| MEDIA | `data/media/canary_140_story_1080x1920.jpg` |
| TRIGGER_TYPE | controlled_live canary |
| TRIGGER_SOURCE | Phase 0.6 `POST /api/stories/runs` (localhost/direct to gunicorn; not nginx-logged) |
| SERVICE | autostory-web (gunicorn worker) |
| PID | 684742 |
| RELEASE | `/opt/autostory-releases/20260722T140147Z-a28b54ab2c2f` |
| SHA | `a28b54ab2c2f3f9caed29614f14add4b26a19337` |
| ENTRY_POINT | `POST /api/stories/runs` → `controlled_live_run_http_response` → `_execute_controlled_live_story_run` |
| FUNCTION | `src.stories.publisher.StoryPublisher.publish_story` → `SendStoryRequest` |
| REASON_CODE | `controlled_story_publish_ok` |
| STATUS | run 9 completed; stories_ok=1 |

### Timestamp meaning

`14:04:35Z` is the DB `published_at` written immediately after Telegram acceptance in the
publisher; journal shows upload at 14:04:33Z and success at 14:04:35Z (host CEST = UTC+2).
