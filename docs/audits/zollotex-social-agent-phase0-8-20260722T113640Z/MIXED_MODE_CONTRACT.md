# Mixed Mode Contract

| Mode | Reads plaintext | Reads enc:v1 | New writes | Notes |
|---|---|---|---|---|
| disabled | yes | yes if keys present | plaintext | **production current** |
| transition | yes | yes | encrypted | migration mode |
| encrypted-only | no | yes | encrypted | post full migration gate |

Unsupported mode values fail at startup (`SessionMaterialConfigurationError`).

Production must remain `disabled` until separately authorized migration phase.
