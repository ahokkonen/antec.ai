from __future__ import annotations

import shutil
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[3]


@pytest.fixture
def core_dir() -> Path:
    return CORE


@pytest.fixture
def dev_instance(tmp_path: Path) -> Path:
    dest = tmp_path / "dev"
    shutil.copytree(CORE / "instances" / "dev", dest)
    return dest
