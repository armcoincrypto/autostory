# Story Execution Path Inventory (Phase 1.1)

## Production-reachable mutation paths

| PATH_ID | ENTRY | PROVIDER | GATE AFTER 1.1 |
|---|---|---|---|
| P1 | POST /api/stories/runs controlled live | SendStory via invoke_send_story | STORY_MUTATIONS + mode + allowlist + CONTROLLED + token |
| P2 | POST /api/stories/publish | blocked by p3 guard | hard 403 |
| P3 | POST /api/stories/batch | blocked by p3 guard | hard 403 |
| P4 | Celery publish_story_task | would hit guard+boundary | DENY without purpose + global |
| P5 | Celery batch | same | DENY |
| P6 | StoryRotationEngine | unwired | DENY |
| P7 | Scheduler maybe_tick | never publishes | skip |
| P8 | Bot publish | inactive service | DENY if started |
| P9 | Legacy publisher module | invoke_send_story | DENY without purpose |
| P10–P14 | dry-run/precheck/CanSend | no SendStory | N/A |

```
ALL_PRODUCTION_REACHABLE_STORY_MUTATION_PATHS=9
(with live-capable when flags on: P1 only)
DIRECT_PROVIDER_STORY_CALLS_OUTSIDE_CANONICAL_BOUNDARY=0
```
