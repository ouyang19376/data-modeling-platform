"""v10 auth rbac: users / api_keys / project_members + deactivation confirm

Revision ID: 7f0c38e854e5
Revises: b5f2c4a9d703
Create Date: 2026-10-27 15:46:17.268133

V1.0 认证授权与血缘停用确认基座：
- 新增 users（认证主体：人/服务账号，kind 区分；is_admin 平台管理员旁路）
- 新增 api_keys（凭据：key_id 公开、key_hash=SHA-256(secret)，secret 仅创建
  响应返回一次；支持吊销与过期，last_used_at 节流更新）
- 新增 project_members（项目级三角色授权 owner/editor/viewer，(project,user)
  唯一，幂等 upsert）
- lineage_import_runs 新增 deactivation_confirmed（full_snapshot 停用人工
  确认标志；server_default='0' 保证既有行回填 false）

不修改任何既有列、不重建既有表。downgrade 保护：三张新表存在数据行时
中止降级（密钥哈希不可恢复，回滚前必须导出台账并重新签发，见升级指南
第 6 节）；绝不为"让降级成功"删数据。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7f0c38e854e5'
down_revision: Union[str, None] = 'b5f2c4a9d703'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_key', sa.String(length=80), nullable=False),
    sa.Column('display_name', sa.String(length=160), nullable=False),
    sa.Column('kind', sa.String(length=24), nullable=False),
    sa.Column('is_admin', sa.Boolean(), server_default='0', nullable=False),
    sa.Column('auth_source', sa.String(length=24), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("kind IN ('human','service_account')", name='ck_user_kind'),
    sa.CheckConstraint("status IN ('active','disabled')", name='ck_user_status'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_key')
    )
    op.create_table('api_keys',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('key_id', sa.String(length=40), nullable=False),
    sa.Column('key_hash', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by', sa.String(length=80), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("status IN ('active','revoked')", name='ck_api_key_status'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key_id')
    )
    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.create_index('ix_api_key_hash', ['key_hash'], unique=False)
        batch_op.create_index('ix_api_key_user', ['user_id'], unique=False)

    op.create_table('project_members',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('role', sa.String(length=24), nullable=False),
    sa.Column('created_by', sa.String(length=80), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("role IN ('owner','editor','viewer')", name='ck_member_role'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'user_id', name='uq_project_member')
    )
    with op.batch_alter_table('project_members', schema=None) as batch_op:
        batch_op.create_index('ix_project_member_project', ['project_id'], unique=False)

    with op.batch_alter_table('lineage_import_runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('deactivation_confirmed', sa.Boolean(), server_default='0', nullable=False))


def downgrade() -> None:
    conn = op.get_bind()
    users_n = conn.execute(sa.text("SELECT COUNT(*) FROM users")).scalar()
    keys_n = conn.execute(sa.text("SELECT COUNT(*) FROM api_keys")).scalar()
    members_n = conn.execute(sa.text("SELECT COUNT(*) FROM project_members")).scalar()
    if users_n or keys_n or members_n:
        raise RuntimeError(
            "中止降级：users/api_keys/project_members 存在数据"
            f"（users={users_n}, api_keys={keys_n}, project_members={members_n}）。"
            "密钥哈希不可恢复：请先导出成员与密钥台账，确认重新签发方案后再回滚；"
            "不得为让降级成功而删除数据。"
        )

    with op.batch_alter_table('lineage_import_runs', schema=None) as batch_op:
        batch_op.drop_column('deactivation_confirmed')

    with op.batch_alter_table('project_members', schema=None) as batch_op:
        batch_op.drop_index('ix_project_member_project')

    op.drop_table('project_members')
    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.drop_index('ix_api_key_user')
        batch_op.drop_index('ix_api_key_hash')

    op.drop_table('api_keys')
    op.drop_table('users')
