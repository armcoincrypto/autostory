# Canonical Mutation Boundary

Module: `src/stories/mutation_boundary.py`

- `StoryMutationService.evaluate` — authorization + audit
- `invoke_send_story` — sole provider entry for `SendStoryRequest`
- Call-time recheck of `STORY_MUTATIONS_ENABLED`
- Single-use HMAC tokens; disable invalidates pending authorizations

`DIRECT_PROVIDER_STORY_CALLS_OUTSIDE_CANONICAL_BOUNDARY=0`
