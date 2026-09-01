"""Behavioral checks for each tool: it calls the correct underlying stub
services.* function with the right arguments, returns that function's
result unchanged, and (for tenant-scoped tools) sets the ambient tenant_id
via tenant_scope for the duration of the call.

Each tool's underlying function is fetched via the tool manager rather than
mcp.call_tool(), because MCP's structured-content wrapping differs by
declared return type (list[dict] vs dict) and isn't what these tests care
about — see tests/test_tool_registration.py for the MCP-facing schema
checks.
"""


def _tool_fn(mcp, name):
    return mcp._tool_manager.get_tool(name).fn


async def test_list_workspaces_scopes_tenant_and_returns_result(
    mcp, mock_service_fn, get_ambient_tenant_id
):
    seen_tenant = {}

    async def fake_list_workspaces():
        seen_tenant["tenant_id"] = get_ambient_tenant_id()
        return [{"slug": "revenue-leakage"}]

    mock_service_fn("workspace_service", "list_workspaces", side_effect=fake_list_workspaces)

    result = await _tool_fn(mcp, "list_workspaces")(tenant_id="tenant-a")

    assert result == [{"slug": "revenue-leakage"}]
    assert seen_tenant["tenant_id"] == "tenant-a"
    assert get_ambient_tenant_id() is None  # reset after the call


async def test_list_use_cases_passes_flags_through_with_no_tenant(mcp, mock_service_fn):
    mock = mock_service_fn("use_case_service", "list_use_cases", return_value=[{"slug": "csat"}])

    result = await _tool_fn(mcp, "list_use_cases")(
        starter_only=True, include_workflows=True, include_archived=False,
    )

    assert result == [{"slug": "csat"}]
    mock.assert_awaited_once_with(
        starter_only=True, include_workflows=True, include_archived=False,
    )


async def test_list_agents_scopes_tenant_and_passes_filters(
    mcp, mock_service_fn, get_ambient_tenant_id
):
    seen_tenant = {}

    async def fake_list_definitions(**kwargs):
        seen_tenant["tenant_id"] = get_ambient_tenant_id()
        seen_tenant["kwargs"] = kwargs
        return [{"id": "agent-1"}]

    mock_service_fn(
        "agent_definition_service", "list_definitions", side_effect=fake_list_definitions,
    )

    result = await _tool_fn(mcp, "list_agents")(tenant_id="tenant-b", top_level_only=True)

    assert result == [{"id": "agent-1"}]
    assert seen_tenant["tenant_id"] == "tenant-b"
    assert seen_tenant["kwargs"]["top_level_only"] is True


async def test_get_shared_memory_scopes_tenant_and_returns_result(
    mcp, mock_service_fn, get_ambient_tenant_id
):
    seen_tenant = {}

    async def fake_list_shared_memory(**kwargs):
        seen_tenant["tenant_id"] = get_ambient_tenant_id()
        return [{"content": "note"}]

    mock_service_fn(
        "agent_memory_service", "list_shared_memory", side_effect=fake_list_shared_memory,
    )

    result = await _tool_fn(mcp, "get_shared_memory")(tenant_id="tenant-c", limit=5)

    assert result == [{"content": "note"}]
    assert seen_tenant["tenant_id"] == "tenant-c"


async def test_get_temporal_knowledge_combines_effective_and_history(
    mcp, mock_service_fn, get_ambient_tenant_id
):
    seen_tenant = {}

    async def fake_compose(entity_type, entity_ref, as_of=None):
        seen_tenant["tenant_id"] = get_ambient_tenant_id()
        seen_tenant["as_of"] = as_of
        return {"entity_type": entity_type, "entity_ref": entity_ref}

    mock_service_fn(
        "knowledge_temporal_service", "compose_temporal_context", side_effect=fake_compose,
    )
    mock_service_fn(
        "knowledge_temporal_service",
        "get_version_history",
        return_value=[{"version": 1}, {"version": 2}],
    )

    result = await _tool_fn(mcp, "get_temporal_knowledge")(
        tenant_id="tenant-d",
        entity_type="policy",
        entity_ref="policy-123",
        as_of="2026-01-01T00:00:00",
    )

    assert result == {
        "effective": {"entity_type": "policy", "entity_ref": "policy-123"},
        "version_history": [{"version": 1}, {"version": 2}],
    }
    assert seen_tenant["tenant_id"] == "tenant-d"
    assert seen_tenant["as_of"].isoformat() == "2026-01-01T00:00:00"


async def test_get_temporal_knowledge_defaults_as_of_to_none(mcp, mock_service_fn):
    compose_mock = mock_service_fn(
        "knowledge_temporal_service", "compose_temporal_context", return_value=None,
    )
    mock_service_fn("knowledge_temporal_service", "get_version_history", return_value=[])

    await _tool_fn(mcp, "get_temporal_knowledge")(
        tenant_id="tenant-e", entity_type="policy", entity_ref="policy-999",
    )

    compose_mock.assert_awaited_once_with("policy", "policy-999", as_of=None)
