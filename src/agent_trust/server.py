from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("AgentTrust")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
