# Postdeployment Observation

Phase 1.1 release `e2f11a0` / `20260722T192010Z-e2f11a0b1c5d` observed T+0..T+30 with stories=8 and zero publishes.

Before T+60, concurrent workstream deployed `c862d9f` and ran authorized account-106 controlled canary (full flag stack), publishing story DB id=30 at 20:19:37Z.

T+60: stories=9, publish_hits=1, runtime on c862d9f.

Post-cleanup denial smoke: all entry points deny with `story_mutations_disabled` / p3 lock.
