from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from antec_release.changelog import CHANGELOG_FILE
from antec_release.manifest import MIGRATIONS_DIR
from antec_release.versions import LOCATIONS, VERSION_FILE, rewrite_locations

CORE = Path(__file__).resolve().parents[3]


@pytest.fixture
def core_copy(tmp_path: Path) -> Path:
    """Копия только тех файлов ядра, которые читает antec-release."""
    root = tmp_path / "core"
    for rel in {VERSION_FILE, CHANGELOG_FILE, *(loc.path for loc in LOCATIONS)}:
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(CORE / rel, dest)
    shutil.copytree(CORE / MIGRATIONS_DIR, root / MIGRATIONS_DIR)
    # Тесты не зависят от текущей версии ядра: копия всегда 0.1.0 до первого релиза.
    rewrite_locations(root, "0.1.0")
    (root / CHANGELOG_FILE).write_text(
        "# Изменения\n\n## [Не выпущено]\n\n### Добавлено\n\n- Релизный процесс ядра.\n",
        encoding="utf-8",
    )
    return root
