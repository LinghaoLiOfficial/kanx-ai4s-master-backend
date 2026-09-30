"""Workspace structured documents."""
from collections.abc import Sequence
from alembic import op
from kanx_ai4s_master.modules.workspace_files.models import WorkspaceFile

revision: str = "foundation_0008_workspace_files"
down_revision: str | None = "foundation_0007_storage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    WorkspaceFile.__table__.create(op.get_bind())

def downgrade() -> None:
    WorkspaceFile.__table__.drop(op.get_bind())
