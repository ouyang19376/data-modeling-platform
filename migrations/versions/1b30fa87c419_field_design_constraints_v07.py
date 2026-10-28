"""field design constraints v07: add model_fields.is_unique

Revision ID: 1b30fa87c419
Revises: 761be0cda0ff
Create Date: 2026-10-21 10:30:00.000000

V0.7 字段设计器补全：新增字段级唯一约束元数据 is_unique。
先以 false 作为临时数据库默认值回填既有行，再移除 server_default，
使未来写入继续由 ORM/API schema 的默认值控制。
downgrade 会删除该列，字段的唯一性设计信息将丢失，仅限隔离测试库演练。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1b30fa87c419'
down_revision: Union[str, None] = '761be0cda0ff'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 第一步：带 server_default=false 新增列，回填既有行（SQLite 亦支持）。
    with op.batch_alter_table('model_fields', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('is_unique', sa.Boolean(), nullable=False, server_default=sa.false())
        )
    # 第二步：移除 server_default，后续写入由 ORM/API schema 默认值控制。
    with op.batch_alter_table('model_fields', schema=None) as batch_op:
        batch_op.alter_column('is_unique', existing_type=sa.Boolean(), server_default=None)


def downgrade() -> None:
    with op.batch_alter_table('model_fields', schema=None) as batch_op:
        batch_op.drop_column('is_unique')
