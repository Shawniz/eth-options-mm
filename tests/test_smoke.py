import importlib

import pytest

SUBPACKAGES = [
    "data",
    "pricing",
    "surface",
    "quoting",
    "sim",
    "hedging",
    "risk",
    "pnl",
    "experiments",
]


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_subpackage_imports(name):
    importlib.import_module(f"mm.{name}")
