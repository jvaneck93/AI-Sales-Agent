"""unique license plate

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vehicle", schema=None) as batch_op:
        batch_op.create_unique_constraint("uq_vehicle_license_plate", ["license_plate"])


def downgrade() -> None:
    with op.batch_alter_table("vehicle", schema=None) as batch_op:
        batch_op.drop_constraint("uq_vehicle_license_plate", type_="unique")
