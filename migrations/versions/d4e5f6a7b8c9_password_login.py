"""v13 password login: users.password_hash

Revision ID: d4e5f6a7b8c9
Revises: 7f0c38e854e5
Create Date: 2026-10-29 20:46:00.000000

V1.3 密码登录基座：
- users 新增 password_hash（可空）：PBKDF2-HMAC-SHA256 哈希串
  （pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>，Django 风格，
  迭代次数内嵌便于未来升级算法）；NULL 表示该主体无密码登录能力
  （服务账号/仅密钥主体不受影响）。
- 不改任何既有列、不重建既有表；既有行 password_hash 为 NULL，
  登录端点对其返回与密码错误一致的 401（不泄露主体存在性）。

downgrade 保护：存在任一非空 password_hash 时中止降级——密码哈希
是不可再生凭证材料，回滚等于注销全部密码登录能力，必须先确认。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, None] = '7f0c38e854e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('password_hash', sa.String(length=128), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    n = conn.execute(sa.text(
        "SELECT COUNT(*) FROM users WHERE password_hash IS NOT NULL"
    )).scalar()
    if n:
        raise RuntimeError(
            "中止降级：users 存在 "
            f"{n} 个已设置密码的主体。password_hash 为不可再生凭证材料，"
            "回滚将注销其密码登录能力；如确认不再需要密码登录，"
            "请先导出主体台账再手工处理。"
        )

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('password_hash')
