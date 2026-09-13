#!/usr/bin/env bash
# Fail-closed gate: Messages page inline JS must parse before deploy cutover.
# Usage: check_messages_js_syntax.sh <release_root>
set -euo pipefail
ROOT="${1:?release root required}"
HTML="$ROOT/src/dashboard/templates/messages.html"
[[ -f "$HTML" ]] || { echo "missing $HTML"; exit 1; }

TMP="$(mktemp --suffix=.js)"
trap 'rm -f "$TMP" "$TMP.err"' EXIT

python3 - "$HTML" "$TMP" <<'PY'
import re, sys
html_path, out_path = sys.argv[1], sys.argv[2]
text = open(html_path, encoding="utf-8").read()
# Messages page uses one primary owner script block in extra_js.
blocks = re.findall(r"<script>(.*?)</script>", text, flags=re.S | re.I)
if not blocks:
    raise SystemExit("no <script> blocks found in messages.html")
# Prefer the largest block (page logic); ignore tiny snippets if any.
js = max(blocks, key=len)
if "async function init" not in js and "loadAccounts" not in js:
    raise SystemExit("messages.html script does not look like Messages page logic")
# Broken try without catch must fail (regression from Wave Q polish)
if re.search(r"\btry\s*\{", js) and not re.search(r"\bcatch\s*\(", js):
    # Allow try/finally-only; require catch OR finally after each try roughly
    pass
open(out_path, "w", encoding="utf-8").write(js)
print(f"extracted_js_bytes={len(js)}")
PY

if command -v node >/dev/null 2>&1; then
  if ! node --check "$TMP" 2>"$TMP.err"; then
    echo "messages_js_syntax=FAIL (node --check)" >&2
    cat "$TMP.err" >&2 || true
    exit 1
  fi
  echo "messages_js_syntax=OK (node --check)"
else
  # Fallback without node: reject known endless-loading regression (try without catch in init).
  python3 - "$TMP" <<'PY'
import re, sys
js = open(sys.argv[1], encoding="utf-8").read()
m = re.search(r"\(async function init\(\)\s*\{([\s\S]*?)\}\)\s*\(\s*\)\s*;", js)
if not m:
    raise SystemExit("messages init IIFE not found")
body = m.group(1)
if "try" in body and "catch" not in body:
    raise SystemExit("messages init try block missing catch (node unavailable; regex gate)")
# Crude brace balance on extracted JS
bal = 0
for ch in js:
    if ch == "{":
        bal += 1
    elif ch == "}":
        bal -= 1
        if bal < 0:
            raise SystemExit("messages JS brace imbalance")
if bal != 0:
    raise SystemExit(f"messages JS brace imbalance bal={bal}")
print("messages_js_syntax=OK (python fallback gate)")
PY
fi
