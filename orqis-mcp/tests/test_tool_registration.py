"""MCP-level checks: exactly the five documented tools are registered, with
the expected read-only shape (no write/execute tools, correct required
tenant_id args)."""

EXPECTED_TOOL_NAMES = {
    "list_workspaces",
    "list_use_cases",
    "list_agents",
    "get_shared_memory",
    "get_temporal_knowledge",
}

TENANT_SCOPED_TOOLS = {
    "list_workspaces",
    "list_agents",
    "get_shared_memory",
    "get_temporal_knowledge",
}


async def test_exactly_five_tools_registered(mcp):
    tools = await mcp.list_tools()
    assert {t.name for t in tools} == EXPECTED_TOOL_NAMES
    assert len(tools) == 5


async def test_tenant_scoped_tools_require_tenant_id(mcp):
    tools = {t.name: t for t in await mcp.list_tools()}
    for name in TENANT_SCOPED_TOOLS:
        assert "tenant_id" in tools[name].inputSchema.get("required", []), name


async def test_list_use_cases_takes_no_tenant_id(mcp):
    tools = {t.name: t for t in await mcp.list_tools()}
    assert "tenant_id" not in tools["list_use_cases"].inputSchema.get("properties", {})


async def test_all_tools_are_read_only_by_name(mcp):
    # Every tool name must read as list_*/get_* — a guard against silently
    # growing a write/execute tool in this package (explicitly out of scope).
    tools = await mcp.list_tools()
    for tool in tools:
        assert tool.name.startswith(("list_", "get_")), tool.name
