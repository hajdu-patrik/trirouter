"""Old MCP shims run `jev_router.mcp_server`: this forwards to trirouter.mcp_server."""
from trirouter.mcp_server import main

if __name__ == "__main__":
    main()
