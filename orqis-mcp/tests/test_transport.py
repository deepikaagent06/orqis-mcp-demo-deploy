"""Transport-related checks: STDIO stays the default, streamable-http is
env-controlled, /health is a plain liveness check with no backend/tenant
data, and the MCP endpoint is mounted at the SDK's standard /mcp path.

None of this touches a real database: streamable_http_app() only wires up
the MCP protocol server and Starlette routes, it doesn't open a Postgres
connection (see backend/database/__init__.py's lazy get_pool()).
"""
from unittest.mock import MagicMock

from starlette.testclient import TestClient

from orqis_mcp import server as orqis_mcp_server


def test_default_transport_is_stdio(monkeypatch):
    monkeypatch.delenv("MCP_TRANSPORT", raising=False)
    run_mock = MagicMock()
    monkeypatch.setattr(orqis_mcp_server.mcp, "run", run_mock)

    orqis_mcp_server.main()

    run_mock.assert_called_once_with(transport="stdio")


def test_streamable_http_transport_is_env_controlled(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")
    run_mock = MagicMock()
    monkeypatch.setattr(orqis_mcp_server.mcp, "run", run_mock)

    orqis_mcp_server.main()

    run_mock.assert_called_once_with(transport="streamable-http")


def test_default_host_and_port():
    # The module-level `mcp` was constructed at import time with no
    # MCP_HOST/MCP_PORT set, so it reflects the documented defaults.
    assert orqis_mcp_server.mcp.settings.host == "127.0.0.1"
    assert orqis_mcp_server.mcp.settings.port == 8001


def test_health_endpoint_returns_ok():
    app = orqis_mcp_server.mcp.streamable_http_app()

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_mcp_endpoint_mounted_at_standard_path():
    app = orqis_mcp_server.mcp.streamable_http_app()

    paths = {getattr(route, "path", None) for route in app.routes}

    assert "/mcp" in paths
    assert "/health" in paths
