"""Alembic 迁移环境。

- 连接串来自应用 Settings（DATABASE_URL），与 ORM 共用同一配置来源；
- SQLite：迁移期间临时关闭 foreign_keys（batch ALTER 重建父表会级联删除子表行），
  迁移结束后立即重新开启；
- render_as_batch=True 以支持 SQLite 的 ALTER TABLE。
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, event, pool

from backend.app.config import settings
from backend.app.db import Base
from backend.app import models  # noqa: F401  # 确保 ORM 元数据已注册

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=_is_sqlite(settings.database_url),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        {"sqlalchemy.url": settings.database_url},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    if _is_sqlite(settings.database_url):
        # 迁移期间关闭外键约束，避免 batch ALTER 重建 data_models 时
        # 触发 model_fields 等子表级联删除；迁移结束后立即恢复。
        @event.listens_for(connectable, "connect")
        def _fk_off(dbapi_conn, _record):  # noqa: ANN001
            dbapi_conn.execute("PRAGMA foreign_keys = OFF")

        @event.listens_for(connectable, "close")
        def _fk_on(dbapi_conn, _record):  # noqa: ANN001
            dbapi_conn.execute("PRAGMA foreign_keys = ON")

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=_is_sqlite(settings.database_url),
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
