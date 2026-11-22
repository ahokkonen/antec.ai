"""Новая инсталляция из `templates/instance` ядра (ADR-0008, п. 5).

Шаблон копируется один раз; дальше инсталляция обновляется сменой версии ядра, а не повторной
генерацией. Подстановки: `__INSTANCE_ID__`, `__INSTANCE_NAME__`, `__CORE_VERSION__`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from pydantic import ValidationError

from antec_instance.core import TEMPLATE_DIR, CoreError, core_version
from antec_instance.manifest import InstanceInfo, ManifestError, load_manifest
from antec_instance.policies import check_policies


def init_instance(core_dir: Path, dest: Path, instance_id: str, name: str) -> None:
    # Проверка id и имени той же схемой, что и манифест.
    try:
        InstanceInfo.model_validate({"id": instance_id, "name": name})
    except ValidationError as exc:
        raise ManifestError([f"instance.{e['loc'][0]}: {e['msg']}" for e in exc.errors()]) from exc
    template = core_dir / TEMPLATE_DIR
    if not template.is_dir():
        raise CoreError(f"нет шаблона инсталляции {template}")
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise CoreError(f"{dest} уже существует и не пуст")
    version = core_version(core_dir)
    subst = {
        "__INSTANCE_ID__": instance_id,
        "__INSTANCE_NAME__": name,
        "__CORE_VERSION__": version,
    }
    shutil.copytree(template, dest, symlinks=False, dirs_exist_ok=True)
    for path in sorted(dest.rglob("*")):
        if path.is_symlink():
            raise CoreError(f"символическая ссылка в шаблоне: {path.relative_to(dest)}")
        if not path.is_file():
            continue
        # Шаблон только из текстовых файлов.
        text = path.read_text(encoding="utf-8")
        for key, value in subst.items():
            text = text.replace(key, value)
        path.write_text(text, encoding="utf-8")
    # Шаблон обязан давать валидную инсталляцию.
    manifest = load_manifest(dest)
    problems = check_policies(dest, manifest.instance.id)
    if problems:
        raise ManifestError(problems)
