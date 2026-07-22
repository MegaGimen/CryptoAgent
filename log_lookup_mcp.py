#!/usr/bin/env python3
"""MCP server for querying nearest log artifacts by timestamp."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List

from fastmcp import FastMCP

TIME_FMT = "%Y%m%d_%H%M%S"
TIME_RE = re.compile(r"^\d{8}_\d{6}$")

PROJECT_ROOT = Path(__file__).resolve().parent
LOG_ROOTS = [PROJECT_ROOT / "logs", PROJECT_ROOT / "logs" / "stupid"]

mcp = FastMCP("LogLookupMCP")


@dataclass(frozen=True)
class LogDir:
    name: str
    dt: datetime
    path: Path


def _parse_time_str(value: str) -> datetime:
    if not TIME_RE.match(value or ""):
        raise ValueError(
            f"Invalid time format: {value!r}. Expected format: YYYYMMDD_HHMMSS"
        )
    try:
        return datetime.strptime(value, TIME_FMT)
    except ValueError as exc:
        raise ValueError(
            f"Invalid calendar time: {value!r}. Expected format: YYYYMMDD_HHMMSS"
        ) from exc


def _iter_log_dirs() -> List[LogDir]:
    result: List[LogDir] = []
    for root in LOG_ROOTS:
        if not root.exists():
            continue
        for child in root.iterdir():
            if not child.is_dir():
                continue
            name = child.name
            if not TIME_RE.match(name):
                continue
            try:
                dt = datetime.strptime(name, TIME_FMT)
            except ValueError:
                continue
            result.append(LogDir(name=name, dt=dt, path=child))
    result.sort(key=lambda x: x.dt)
    return result


def _nearest_log_dir(target_time: str, filename: str) -> LogDir:
    target_dt = _parse_time_str(target_time)
    dirs = _iter_log_dirs()
    candidates = [d for d in dirs if (d.path / filename).is_file()]
    if not candidates:
        raise FileNotFoundError(f"No {filename} found in logs/ or logs/stupid/")

    # Tie-breaker: earlier timestamp wins for deterministic behavior.
    return min(candidates, key=lambda d: (abs((d.dt - target_dt).total_seconds()), d.dt))


@mcp.tool()
def list_log_times() -> dict:
    """Return all timestamp folder names from logs/ and logs/stupid/."""
    dirs = _iter_log_dirs()
    return {
        "count": len(dirs),
        "times": [d.name for d in dirs],
    }


@mcp.tool()
def get_nearest_output_json(time_str: str) -> dict:
    """Return nearest output.json by input time string (YYYYMMDD_HHMMSS)."""
    nearest = _nearest_log_dir(time_str, "output.json")
    file_path = nearest.path / "output.json"
    raw = file_path.read_text(encoding="utf-8")

    # Keep response robust even if output.json is malformed in edge logs.
    parsed = None
    parse_error = None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        parse_error = str(exc)

    return {
        "query_time": time_str,
        "nearest_time": nearest.name,
        "path": str(file_path),
        "json": parsed,
        "raw": raw if parsed is None else None,
        "parse_error": parse_error,
    }


@mcp.tool()
def get_nearest_input_md(time_str: str) -> dict:
    """Return nearest input.md by input time string (YYYYMMDD_HHMMSS)."""
    nearest = _nearest_log_dir(time_str, "input.md")
    file_path = nearest.path / "input.md"
    return {
        "query_time": time_str,
        "nearest_time": nearest.name,
        "path": str(file_path),
        "content": file_path.read_text(encoding="utf-8"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="LogLookup MCP server")
    parser.add_argument("--transport", choices=["stdio", "sse"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18123)
    args = parser.parse_args()

    if args.transport == "sse":
        mcp.run(transport="sse", host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
