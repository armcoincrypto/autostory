# Mutation Gate Matrix

| PATH | GLOBAL_KILL | MODE | ACCOUNT | PURPOSE/CTRL | SCHEDULER_LOCK | OPERATOR | PROVIDER_TOKEN | DRY_RUN |
|---|---|---|---|---|---|---|---|---|
| P1 controlled live | Y | Y | Y | Y | n/a | Y (token) | Y | reject live |
| P2/P3 legacy HTTP | p3 hardblock | — | — | — | — | — | — | — |
| P4/P5 celery | Y | Y | Y | purpose req | — | — | Y | — |
| P7 scheduler tick | — | — | — | never cert | Y (mutations off) | — | — | — |
| P8 bot | Y | Y | Y | purpose req | — | — | Y | — |

Missing before 1.1: GLOBAL_KILL, MODE, ACCOUNT allowlist, PROVIDER_TOKEN.
After 1.1: all live paths require all layers; missing/malformed global → deny.
