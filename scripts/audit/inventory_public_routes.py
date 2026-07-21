#!/usr/bin/env python3
"""Statically inventory route declarations without importing application code."""

from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path


TS_ROUTE = re.compile(
    r"\b(?:app|server|router)\.(get|post|put|patch|delete|options|head)\s*"
    r"\(\s*([\"'`])([^\"'`]+)\2",
    re.I,
)
NGINX_LOCATION = re.compile(r"^\s*location\s+(?:(=|~\*?|\\^~)\s+)?([^\s{]+)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def python_routes(path: Path, source: str) -> list[dict]:
    tree = ast.parse(source, filename=str(path))
    routes: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(
                decorator.func, ast.Attribute
            ):
                continue
            if decorator.func.attr not in {"route", "get", "post", "put", "patch", "delete"}:
                continue
            if not decorator.args or not isinstance(decorator.args[0], ast.Constant):
                continue
            route_path = decorator.args[0].value
            if not isinstance(route_path, str):
                continue
            methods = [decorator.func.attr.upper()]
            if decorator.func.attr == "route":
                methods = ["ANY"]
                for keyword in decorator.keywords:
                    if keyword.arg == "methods" and isinstance(
                        keyword.value, (ast.List, ast.Tuple)
                    ):
                        values = [
                            item.value
                            for item in keyword.value.elts
                            if isinstance(item, ast.Constant)
                            and isinstance(item.value, str)
                        ]
                        if values:
                            methods = [value.upper() for value in values]
            routes.append(
                {
                    "file": str(path),
                    "line": node.lineno,
                    "methods": methods,
                    "path": route_path,
                    "handler": node.name,
                    "kind": "python",
                }
            )
    return routes


def typescript_routes(path: Path, source: str) -> list[dict]:
    return [
        {
            "file": str(path),
            "line": source.count("\n", 0, match.start()) + 1,
            "methods": [match.group(1).upper()],
            "path": match.group(3),
            "handler": None,
            "kind": "typescript",
        }
        for match in TS_ROUTE.finditer(source)
    ]


def nginx_locations(path: Path, source: str) -> list[dict]:
    routes = []
    for line_number, line in enumerate(source.splitlines(), 1):
        match = NGINX_LOCATION.match(line)
        if match:
            routes.append(
                {
                    "file": str(path),
                    "line": line_number,
                    "methods": ["ANY"],
                    "path": match.group(2),
                    "handler": match.group(1) or "prefix",
                    "kind": "nginx",
                }
            )
    return routes


def inspect(path: Path) -> list[dict]:
    if not path.is_file():
        raise SystemExit(f"file not found: {path}")
    source = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".py":
        return python_routes(path, source)
    if path.suffix in {".ts", ".tsx", ".js", ".mjs", ".cjs"}:
        return typescript_routes(path, source)
    return nginx_locations(path, source)


def main() -> int:
    args = parse_args()
    routes = [route for path in args.files for route in inspect(path.resolve())]
    payload = {
        "read_only": True,
        "files_inspected": len(args.files),
        "route_declarations": len(routes),
        "routes": routes,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"files={payload['files_inspected']} "
            f"route_declarations={payload['route_declarations']}"
        )
        for route in routes:
            print(
                f"{','.join(route['methods'])}\t{route['path']}\t"
                f"{route['file']}:{route['line']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
