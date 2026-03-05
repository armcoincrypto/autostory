#!/bin/bash
# Run ON THE SERVER to check who answers on port 8000 and optionally stop uvicorn.
# Usage: ssh root@207.180.212.142 'bash -s' < deploy/check-port-8000.sh
# Or on server: bash /opt/autostory/deploy/check-port-8000.sh

set -e
echo "=== Who is listening on 8000? ==="
lsof -i :8000 2>/dev/null || ss -tlnp | grep 8000 || true
echo ""
echo "=== Response from localhost:8000 ==="
curl -sS -o /dev/null -w "HTTP %{http_code} Server: %{header{server}}\n" http://127.0.0.1:8000/ 2>/dev/null || echo "curl failed"
echo ""
echo "=== First 3 lines of response body from 127.0.0.1:8000 ==="
curl -sS http://127.0.0.1:8000/ 2>/dev/null | head -3 || echo "curl failed"
echo ""
echo "If you see 'Server: uvicorn' above but lsof shows gunicorn, external traffic may be going to a proxy."
echo "If you see HTML above, our app is fine; open http://YOUR_SERVER_IP:8000/ from browser."
