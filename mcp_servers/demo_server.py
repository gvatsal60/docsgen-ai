"""DocsGen demo MCP server (official MCP Python SDK).

A small, self-contained MCP server for trying out DocsGen's MCP integration and for the automated
tests. It exposes read-only lookup tools and one deliberately *destructive* tool to demonstrate the
approval workflow.

Run over stdio (configure in DocsGen as transport "stdio"):
    command: python   args: mcp_servers/demo_server.py

Run over Streamable HTTP:
    python mcp_servers/demo_server.py --http --port 8765        → URL http://127.0.0.1:8765/mcp
Optional: require a bearer token for HTTP requests:
    DEMO_MCP_TOKEN=secret python mcp_servers/demo_server.py --http
"""

from __future__ import annotations

import argparse
import os
import re
from datetime import date

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("docsgen-demo")

_COMPANIES = {
    "northwind analytics": {
        "name": "Northwind Analytics Ltd.",
        "founded": 2014,
        "headquarters": "Manchester, United Kingdom",
        "employees": 85,
        "industry": "Data platforms for retail and logistics",
        "website": "https://northwind.example",
    },
    "contoso retail group": {
        "name": "Contoso Retail Group",
        "founded": 1998,
        "headquarters": "Leeds, United Kingdom",
        "employees": 2300,
        "industry": "Retail (42 stores and online)",
        "website": "https://contoso.example",
    },
}
_RECORDS = {"R-1": "draft", "R-2": "final"}
_EXPECTED_TOKEN = os.environ.get("DEMO_MCP_TOKEN")


def _check_auth(ctx: Context | None) -> None:
    if not _EXPECTED_TOKEN or ctx is None:
        return
    headers = ctx.headers or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if auth != f"Bearer {_EXPECTED_TOKEN}":
        raise PermissionError(
            "Unauthorized: a valid bearer token is required.")


@server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def lookup_company(name: str, ctx: Context) -> dict:
    """Look up public profile information about a company in the demo directory.

    Args:
        name: Company name, for example "Northwind Analytics".
    """
    _check_auth(ctx)
    key = re.sub(
        r"\s+", " ", name.lower().replace("ltd.", "").replace("ltd", "")
    ).strip()
    for known, data in _COMPANIES.items():
        if key in known or known in key:
            return {"found": True, "source": "DocsGen demo directory", **data}
    return {"found": False, "source": "DocsGen demo directory", "query": name}


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def word_count(text: str, ctx: Context) -> dict:
    """Count words, sentences and characters in a text."""
    _check_auth(ctx)
    words = re.findall(r"\b\w+\b", text)
    sentences = [s for s in re.split(r"[.!?]+", text) if s.strip()]
    return {"words": len(words), "sentences": len(sentences), "characters": len(text)}


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def current_date(ctx: Context) -> str:
    """Return today's date in ISO format (YYYY-MM-DD)."""
    _check_auth(ctx)
    return date.today().isoformat()


@server.tool(annotations=ToolAnnotations(destructive_hint=True, read_only_hint=False))
def delete_record(record_id: str, ctx: Context) -> str:
    """Delete a record from the demo store (destructive; used to demonstrate approvals)."""
    _check_auth(ctx)
    if record_id in _RECORDS:
        _RECORDS.pop(record_id)
        return f"Record {record_id} deleted."
    return f"Record {record_id} not found."


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--http", action="store_true", help="Serve Streamable HTTP instead of stdio"
    )
    parser.add_argument(
        "--sse", action="store_true", help="Serve the legacy SSE transport"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.http:
        server.run(transport="streamable-http", host=args.host, port=args.port)
    elif args.sse:
        server.run(transport="sse", host=args.host, port=args.port)
    else:
        server.run()


if __name__ == "__main__":
    main()
