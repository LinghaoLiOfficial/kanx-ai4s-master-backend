"""Groups, invitations, and notifications."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "foundation_0009_collaboration"
down_revision: str | None = "foundation_0008_workspace_files"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("kind", sa.String(20), nullable=False, server_default="personal"))
    op.add_column("organizations", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_organizations_kind", "organizations", ["kind"])
    op.create_index("ix_organizations_deleted_at", "organizations", ["deleted_at"])
    op.create_table(
        "group_invitations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("inviter_id", sa.String(36), nullable=False),
        sa.Column("invitee_id", sa.String(36), nullable=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["inviter_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["invitee_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_group_invitations_organization_id", "group_invitations", ["organization_id"])
    op.create_index("ix_group_invitations_inviter_id", "group_invitations", ["inviter_id"])
    op.create_index("ix_group_invitations_invitee_id", "group_invitations", ["invitee_id"])
    op.create_index("ix_group_invitations_token_digest", "group_invitations", ["token_digest"], unique=True)
    op.create_index("ix_group_invitations_status", "group_invitations", ["status"])
    op.create_index("ix_group_invitations_group_status", "group_invitations", ["organization_id", "status"])
    op.create_table(
        "notifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("recipient_id", sa.String(36), nullable=False),
        sa.Column("actor_id", sa.String(36), nullable=True),
        sa.Column("organization_id", sa.String(36), nullable=True),
        sa.Column("invitation_id", sa.String(36), nullable=True),
        sa.Column("type", sa.String(50), nullable=False),
        sa.Column("title", sa.String(180), nullable=False),
        sa.Column("body", sa.String(500), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["recipient_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["invitation_id"], ["group_invitations.id"], ondelete="SET NULL"),
    )
    for name, columns in (("ix_notifications_recipient_id", ["recipient_id"]), ("ix_notifications_organization_id", ["organization_id"]), ("ix_notifications_type", ["type"]), ("ix_notifications_recipient_created", ["recipient_id", "created_at"])):
        op.create_index(name, "notifications", columns)


def downgrade() -> None:
    op.drop_table("notifications")
    op.drop_table("group_invitations")
    op.drop_index("ix_organizations_deleted_at", table_name="organizations")
    op.drop_index("ix_organizations_kind", table_name="organizations")
    op.drop_column("organizations", "deleted_at")
    op.drop_column("organizations", "kind")
