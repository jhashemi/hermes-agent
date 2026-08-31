"""Kernel-side shim smoke test for gateway.cluster_dispatch (ticket t_03224c92).

Guards the shim boundary: 6 required symbols must be `is`-identical to
eap.cluster_dispatch; missing EAP must ImportError (no silent-degrade).
Full behavior suite: EAP tests/cluster_dispatch/ (1,872 lines).
"""
import builtins
import importlib
import sys

import pytest

REQUIRED_SYMBOLS = (
    "spawn_on_remote", "_NODE_HOSTS", "compute_out_of_scope_boards",
    "create_cluster_node_router", "local_node_router",
    "log_out_of_scope_boards_at_startup",
)


@pytest.fixture(scope="module")
def shim_and_eap():
    return (importlib.import_module("gateway.cluster_dispatch"),
            importlib.import_module("eap.cluster_dispatch"))


@pytest.mark.parametrize("name", REQUIRED_SYMBOLS)
def test_shim_symbol_identical(shim_and_eap, name):
    shim, eap_mod = shim_and_eap
    assert getattr(shim, name) is getattr(eap_mod, name), (
        f"{name} shim vs eap.cluster_dispatch mismatch")


def test_shim_fails_loud_without_eap(monkeypatch):
    for mod in ("gateway.cluster_dispatch", "eap.cluster_dispatch", "eap"):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    real_import = builtins.__import__

    def block_eap(name, *args, **kwargs):
        if name == "eap.cluster_dispatch" or name.startswith("eap.cluster_dispatch."):
            raise ImportError("simulated missing eap")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_eap)
    with pytest.raises(ImportError, match="eap.cluster_dispatch"):
        importlib.import_module("gateway.cluster_dispatch")
