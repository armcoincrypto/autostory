# Process and Release Timeline (UTC)

| Time | Event | Evidence |
|---|---|---|
| 13:56:52 | Phase 0.6 canary evidence opened | phase0-6 evidence dir |
| 14:01:47 | Release a28b54a built | release manifest |
| 14:02–14:04 | Web restart storm during promote | journal systemd |
| 14:03:25 | Account 140 precheck allowed | accounts.story_precheck_* |
| 14:04:30 | Web start; worker PID 684742 | journal |
| 14:04:33 | controlled_story_publish_ok; run 9 start | journal + DB |
| 14:04:35 | Story published; DB story 29 | journal + DB |
| 14:04:36 | Drop-in cleanup restart | journal + phase0-6 commands.log |
| 14:12:28 | Encrypted-only override rewrite | override birth time |
| 19:20:10 | Phase 1.1 release e2f11a0 deployed | this phase |

Responsible: SERVICE=autostory-web PID=684742 RELEASE=a28b54a ENTRY=POST /api/stories/runs FUNCTION=publish_story TRIGGER=phase0-6_canary
