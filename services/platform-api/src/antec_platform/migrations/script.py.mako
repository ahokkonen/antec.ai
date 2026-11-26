"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | None = None
depends_on: str | None = None
# True, если downgrade невозможен или теряет данные (ADR-0014, п. 3).
IRREVERSIBLE = True


def upgrade(*, app_role: str) -> None:
    ${upgrades if upgrades else "pass"}


def downgrade(*, app_role: str) -> None:
    ${downgrades if downgrades else "pass"}
