"""MCP stdio server for modes that run no hooks (Claude desktop Chat and Cowork); the tools live in mcp_tools and reload
on the next call after a package source or config change."""
import json
import sys

from . import __version__, platforms as P
from .hotreload import HotReloader

PROTOCOL = "2025-06-18"
RELOADER = HotReloader("trirouter", P.PKG, "mcp_tools", keep=("hotreload", "mcp_server"))


def handle(msg):
    """Answers one message with the freshest tools; a failed reload warns in the tool result and keeps the old code."""
    try:
        tools = RELOADER.current()
    except Exception as exc:  # noqa: BLE001 - the very first load failed (a half-edited module): the server stays up
        mid = msg.get("id")
        return None if mid is None else {"jsonrpc": "2.0", "id": mid,
                                         "error": {"code": -32603, "message": f"trirouter tools failed to load: {type(exc).__name__}: {exc}"[:300]}}
    reply = _handle(msg, tools)
    if RELOADER.warning and isinstance(reply, dict) and "result" in reply and reply["result"].get("content"):
        reply["result"]["content"][0]["text"] += f"\n\n(warning: {RELOADER.warning})"
    return reply


def _handle(msg, tools):
    method, mid = msg.get("method"), msg.get("id")
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    if mid is None:
        return None  # notification
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "trirouter", "version": __version__},
                  "instructions": "Call route_prompt with the user's request before answering it, then follow its instructions."}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tools.TOOLS}
    elif method == "tools/call":
        fn = tools.HANDLERS.get(params.get("name"))
        if not fn:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool {params.get('name')}"}}
        try:
            text, is_error = fn(params.get("arguments") or {})
        except Exception as exc:  # noqa: BLE001 - reported to the client, the server never crashes
            text, is_error = f"{type(exc).__name__}: {exc}", True
        result = {"content": [{"type": "text", "text": text}], "isError": is_error}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def respond(line):
    try:
        msg = json.loads(line)
    except ValueError:
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
    if isinstance(msg, list):
        return [r for r in (handle(m) for m in msg if isinstance(m, dict)) if r]
    return handle(msg) if isinstance(msg, dict) else None


def main():
    stdin = open(sys.stdin.fileno(), "r", encoding="utf-8", errors="replace", newline="\n", closefd=False)
    stdout = open(sys.stdout.fileno(), "w", encoding="utf-8", newline="\n", closefd=False)
    for line in stdin:
        resp = respond(line) if line.strip() else None
        if resp:
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            stdout.flush()


if __name__ == "__main__":
    main()
