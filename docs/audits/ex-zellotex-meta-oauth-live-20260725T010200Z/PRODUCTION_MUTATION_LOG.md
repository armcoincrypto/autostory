# Production Mutation Log

1. Created /etc/autostory/social-agent-meta.env with redirect, publishing=false, Social credential key ring; App ID/Secret empty.
2. Backed up Social credential key to /etc/autostory/backups/social-credential-prod-v1.keyenv.
3. Added EnvironmentFile for social-agent-meta.env to autostory-web only; restarted web.
4. Built immutable release /opt/autostory-releases/20260725T010136Z-c92b93a01bde from SHA c92b93a01bdee2f7b69691c17ad721d8ff8bc921.
5. Switched web WorkingDirectory/PYTHONPATH to that release; restarted web.
6. No Meta OAuth login; no Page/IG selection; no production disconnect.

Rollback target: /opt/autostory-releases/20260724T132108Z-77bc50ef070b
