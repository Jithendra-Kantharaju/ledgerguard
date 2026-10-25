from pathlib import Path

import pytest

from app.config import load_config
from app.settings import ROOT
from data.generate import build_split


@pytest.fixture(scope="session")
def cfg():
    return load_config(ROOT / "app" / "config" / "customer_a.yaml")


@pytest.fixture(scope="session")
def split_dir(tmp_path_factory, cfg) -> Path:
    out = tmp_path_factory.mktemp("data")
    build_split(out, "t", seed=3, n_clean=6, n_scanned=2, n_per_error=1, config=cfg)
    return out / "t"
