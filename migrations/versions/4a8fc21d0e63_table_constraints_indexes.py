"""table constraints & indexes v08: model_constraints / model_indexes

Revision ID: 4a8fc21d0e63
Revises: 1b30fa87c419
Create Date: 2026-10-21 20:30:00.000000

V0.8 表级约束与索引：新增 model_constraints（primary_key/unique/check/
foreign_key 四类表级约束）与 model_indexes（btree/hash/gin 索引元数据）。
非破坏性升级：既有业务表不改列、不重建；约束/索引随模型删除级联，
外键目标模型删除 RESTRICT。downgrade 会删除两张新表及其数据，
仅限隔离测试库演练。
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "4a8fc21d0e63"
down_revision: Union[str, None] = "1b30fa87c419"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "model_constraints",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("model_id", sa.Integer(), nullable=False),
        sa.Column("constraint_name", sa.String(length=128), nullable=False),
        sa.Column("constraint_type", sa.String(length=24), nullable=False),
        sa.Column("field_codes_json", sa.Text(), nullable=False),
        sa.Column("expression", sa.Text(), nullable=False),
        sa.Column("referenced_model_id", sa.Integer(), nullable=True),
        sa.Column("referenced_field_codes_json", sa.Text(), nullable=False),
        sa.Column("on_delete", sa.String(length=24), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["model_id"], ["data_models.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["referenced_model_id"], ["data_models.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("model_id", "constraint_name", name="uq_model_constraint_name"),
    )
    op.create_index("ix_model_constraint_type", "model_constraints", ["model_id", "constraint_type"], unique=False)
    op.create_table(
        "model_indexes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("model_id", sa.Integer(), nullable=False),
        sa.Column("index_name", sa.String(length=128), nullable=False),
        sa.Column("index_type", sa.String(length=16), nullable=False),
        sa.Column("field_codes_json", sa.Text(), nullable=False),
        sa.Column("is_unique", sa.Boolean(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["model_id"], ["data_models.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("model_id", "index_name", name="uq_model_index_name"),
    )
    op.create_index("ix_model_index_model", "model_indexes", ["model_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_model_index_model", table_name="model_indexes")
    op.drop_table("model_indexes")
    op.drop_index("ix_model_constraint_type", table_name="model_constraints")
    op.drop_table("model_constraints")
