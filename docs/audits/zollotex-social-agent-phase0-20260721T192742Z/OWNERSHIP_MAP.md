# Ownership Map

| Boundary | Current owner | Reuse decision | Constraint |
|---|---|---|---|
| `ex.zellotex.com` public route | Storyfleet/AutoStory nginx + systemd runtime | Preserve | Do not redirect or replace |
| AutoStory source | `/opt/autostory`, GitHub `armcoincrypto/autostory` | Canonical product repository | Reconcile dirty/unpushed lineage first |
| Telegram sessions/accounts | AutoStory client/readiness/governance modules | Reuse through adapter/application services | Never expose session material |
| Telegram stories | AutoStory stories modules | Reuse after canonical service extraction | Existing live flow remains locked |
| Telegram channel/group messages | Scheduler executor and Telegram gateway | Consolidate before expansion | Two partially duplicate send rails exist |
| Scheduling | AutoStory scheduler/systemd worker | Reuse persisted job concepts | Do not add a second scheduler |
| Media files | `/opt/autostory/data/media` | Reuse initially with hardening | Must add canonical metadata and upload validation |
| AI negotiation | AutoStory `src/ai_agent` | Reuse OpenAI client/persistence patterns only | Not a general assistant/tool registry |
| Authentication | AutoStory Flask-Login | Migrate in place | Add organizations, memberships, roles, permissions |
| Audit records | Multiple AutoStory audit tables/files | Consolidate behind one audit service | Current audit model is fragmented |
| Exswaping/Kobbex DEX | `/root/Swaperex` and its production services | Read-only API boundary only | No direct database or financial mutation |
| Kobbex customer/financial operations | Swaperex/Kobbopay-owned services and databases | Out of scope | Never couple social app to private ledger/order/KYC data |
| DNS and TLS | Host/Cloudflare/operator ownership | Operator-controlled | Agent must stop before mutations |
| nginx | Host operations | Dedicated existing server block | Back up, validate, reload only after approval |

## Production ownership boundaries

The Social AI Agent may own:

- operator UI and application services inside the canonical AutoStory product;
- social connection metadata and encrypted provider credentials;
- content, drafts, approvals, schedules, media metadata, AI runs, tool calls, and audit records;
- read-only cached copies of explicitly public Exswaping data with source/freshness labels.

It must not own:

- exchange calculations, quote routing, orders, reserves, payments, KYC, commissions, customer balances, withdrawals, signing material, or private wallet data;
- DNS or public routing decisions without operator approval;
- legacy Telegram account session files outside a controlled adapter;
- provider permissions not granted by official APIs.

## Required organizational decisions

1. Confirm who can certify the 40 local AutoStory commits and current dirty runtime changes.
2. Confirm whether `ex.zellotex.com` remains the long-term product hostname or whether a new approved hostname is required for the Social AI Agent preview.
3. Name the identity authority and initial operator population.
4. Name the owner of a new public Exswaping business-content API.
5. Name the approver for provider credentials and any controlled social canary.
