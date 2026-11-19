"""email_sent_message_id

Gmail replaces the Message-ID header of every message sent through its API, so the id we generate is not the one the
recipient (and their replies, and bounce notices) will quote. ``sent_message_id`` stores the Message-ID Gmail actually
assigned; our own ``rfc_message_id`` stays as the stable token that is also carried in the ``X-AOI-Message`` header.

Revision ID: 7e3b5a9c1f42
Revises: 1c271c0e3ce4
Create Date: 2026-11-20 04:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7e3b5a9c1f42"
down_revision: str | None = "1c271c0e3ce4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("email_messages", sa.Column("sent_message_id", sa.String(length=300), nullable=True))
    op.create_index(
        "ix_email_messages_sent_message_id",
        "email_messages",
        ["workspace_id", "sent_message_id"],
        unique=False,
        postgresql_where=sa.text("sent_message_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_email_messages_sent_message_id",
        table_name="email_messages",
        postgresql_where=sa.text("sent_message_id IS NOT NULL"),
    )
    op.drop_column("email_messages", "sent_message_id")
