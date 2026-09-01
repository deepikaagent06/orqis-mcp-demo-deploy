"""Test fixtures for orqis-mcp.

orqis-mcp is not standalone (see README's "Dependency on ORQIS" section):
orqis_mcp.server refuses to start without ORQIS_BACKEND_PATH pointing at a
real directory, and orqis_mcp.tools.* import that directory's `services`
and `middleware` packages in-process. These tests exercise MCP tool
registration and request/response wiring without a real ORQIS backend
checkout or database, by installing lightweight stand-in `services` and
`middleware` packages into sys.modules *before* orqis_mcp.server is
imported — Python's import system finds them there instead of touching
sys.path or disk. This tests orqis-mcp's own wiring only; it does not
re-test the real ORQIS service-layer functions themselves (that belongs to
ORQIS's own test suite).
"""
import os
import sys
import types
from contextvars import ContextVar
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

_tenant_id: ContextVar[str | None] = ContextVar("tenant_id", default=None)


def _install_stub_backend() -> None:
    os.environ.setdefault("ORQIS_BACKEND_PATH", str(Path(__file__).resolve().parent))

    middleware_pkg = types.ModuleType("middleware")
    tenant_context_mod = types.ModuleType("middleware.tenant_context")
    tenant_context_mod.set_tenant_id = _tenant_id.set
    tenant_context_mod.reset_tenant_id = _tenant_id.reset
    tenant_context_mod.get_tenant_id = _tenant_id.get
    middleware_pkg.tenant_context = tenant_context_mod
    sys.modules["middleware"] = middleware_pkg
    sys.modules["middleware.tenant_context"] = tenant_context_mod

    services_pkg = types.ModuleType("services")
    for name in (
        "workspace_service",
        "use_case_service",
        "agent_definition_service",
        "agent_memory_service",
        "knowledge_temporal_service",
    ):
        mod = types.ModuleType(f"services.{name}")
        sys.modules[f"services.{name}"] = mod
        setattr(services_pkg, name, mod)
    sys.modules["services"] = services_pkg


_install_stub_backend()

from orqis_mcp import server as orqis_mcp_server  # noqa: E402
from orqis_mcp.tools import agents as agents_mod  # noqa: E402
from orqis_mcp.tools import memory as memory_mod  # noqa: E402
from orqis_mcp.tools import temporal as temporal_mod  # noqa: E402
from orqis_mcp.tools import use_cases as use_cases_mod  # noqa: E402
from orqis_mcp.tools import workspaces as workspaces_mod  # noqa: E402


@pytest.fixture
def mcp():
    return orqis_mcp_server.mcp


@pytest.fixture
def stub_services():
    """The stand-in services.* submodules, keyed by attribute name used in
    orqis_mcp.tools (e.g. stub_services["workspace_service"].list_workspaces)."""
    return {
        "workspace_service": workspaces_mod.workspace_service,
        "use_case_service": use_cases_mod.use_case_service,
        "agent_definition_service": agents_mod.agent_definition_service,
        "agent_memory_service": memory_mod.agent_memory_service,
        "knowledge_temporal_service": temporal_mod.knowledge_temporal_service,
    }


@pytest.fixture
def mock_service_fn(monkeypatch, stub_services):
    """Patch one function on a stand-in service module and return the
    AsyncMock, e.g. mock_service_fn("workspace_service", "list_workspaces",
    return_value=[...])."""

    def _patch(service_name: str, fn_name: str, **mock_kwargs) -> AsyncMock:
        mock = AsyncMock(**mock_kwargs)
        monkeypatch.setattr(stub_services[service_name], fn_name, mock, raising=False)
        return mock

    return _patch


@pytest.fixture
def get_ambient_tenant_id():
    """Read the tenant_id orqis_mcp.tools.tenant_scope has set on the stub
    middleware.tenant_context contextvar right now (for asserting a tool
    call scoped it before calling the underlying service function)."""
    return _tenant_id.get
