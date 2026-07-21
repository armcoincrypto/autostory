from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "audit" / "inventory_public_routes.py"


def test_static_route_inventory_does_not_import_or_execute_files(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    python_file = tmp_path / "routes.py"
    python_file.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('bad')\n"
        "class App:\n"
        "    def route(self, *args, **kwargs): ...\n"
        "app = App()\n"
        "@app.route('/health', methods=['GET'])\n"
        "def health(): ...\n",
        encoding="utf-8",
    )
    ts_file = tmp_path / "routes.ts"
    ts_file.write_text("app.post('/rpc/:chain', async () => {});", encoding="utf-8")
    nginx_file = tmp_path / "site.conf"
    nginx_file.write_text("location = /api/health { proxy_pass http://x; }\n")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(python_file),
            str(ts_file),
            str(nginx_file),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    payload = json.loads(result.stdout)
    assert payload["read_only"] is True
    assert {(route["methods"][0], route["path"]) for route in payload["routes"]} == {
        ("GET", "/health"),
        ("POST", "/rpc/:chain"),
        ("ANY", "/api/health"),
    }
