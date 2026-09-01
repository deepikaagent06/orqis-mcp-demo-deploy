"""orqis-mcp is not standalone (see README's "Dependency on ORQIS" section):
it refuses to start without ORQIS_BACKEND_PATH pointing at a real ORQIS
backend checkout, with a clear error rather than a cryptic ImportError once
orqis_mcp.tools tries `from services import ...`. These tests call
_bootstrap_backend_path() directly to check that guard, independent of the
module-level stub backend conftest.py installs for the other tests.
"""
import pytest

from orqis_mcp.server import _bootstrap_backend_path


def test_missing_orqis_backend_path_raises(monkeypatch):
    monkeypatch.delenv("ORQIS_BACKEND_PATH", raising=False)

    with pytest.raises(RuntimeError, match="ORQIS_BACKEND_PATH is not set"):
        _bootstrap_backend_path()


def test_nonexistent_orqis_backend_path_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("ORQIS_BACKEND_PATH", str(tmp_path / "does-not-exist"))

    with pytest.raises(RuntimeError, match="does not exist"):
        _bootstrap_backend_path()
