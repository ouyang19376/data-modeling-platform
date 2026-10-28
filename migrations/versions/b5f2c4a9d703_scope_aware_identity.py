"""scope-aware identity v09.2: include scope_key in unique constraints

Revision ID: b5f2c4a9d703
Revises: a4d7c92f5e10
Create Date: 2026-10-22 11:00:00.000000

V0.9.2 Scope 身份治理：连接器常以 DAG/job group/catalog namespace 作为
扫描 scope，某些系统的边标识只在各自 scope 内唯一。将两项业务唯一键
纳入 scope_key：
- external_lineage_edges：UNIQUE(project_id, source_system, source_ref)
  → UNIQUE(project_id, source_system, scope_key, source_ref)
- lineage_import_runs：UNIQUE(project_id, source_system, run_id)
  → UNIQUE(project_id, source_system, scope_key, run_id)

不新增业务表。downgrade 保护：V0.9.2 允许跨 scope 复用 source_ref/
run_id，旧版全局唯一键无法无损表达；降级前检查跨 scope 重复项，
存在则中止并给出冲突样例，绝不删约束或 stamp 绕过。
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "b5f2c4a9d703"
down_revision: Union[str, None] = "a4d7c92f5e10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite 无法直接 drop 约束：batch 模式重建表；PostgreSQL 走普通 ALTER。
    with op.batch_alter_table("external_lineage_edges") as batch:
        batch.drop_constraint("uq_external_lineage_ref", type_="unique")
        batch.create_unique_constraint(
            "uq_external_lineage_identity",
            ["project_id", "source_system", "scope_key", "source_ref"],
        )
    with op.batch_alter_table("lineage_import_runs") as batch:
        batch.drop_constraint("uq_lineage_run_ref", type_="unique")
        batch.create_unique_constraint(
            "uq_lineage_run_identity",
            ["project_id", "source_system", "scope_key", "run_id"],
        )


def downgrade() -> None:
    conn = op.get_bind()
    edge_dups = conn.execute(
        sa.text(
            "SELECT source_system, scope_key, source_ref, COUNT(*) AS n "
            "FROM external_lineage_edges "
            "GROUP BY project_id, source_system, source_ref "
            "HAVING COUNT(*) > 1 LIMIT 5"
        )
    ).fetchall()
    run_dups = conn.execute(
        sa.text(
            "SELECT source_system, scope_key, run_id, COUNT(*) AS n "
            "FROM lineage_import_runs "
            "GROUP BY project_id, source_system, run_id "
            "HAVING COUNT(*) > 1 LIMIT 5"
        )
    ).fetchall()
    if edge_dups or run_dups:
        samples = []
        for row in edge_dups:
            samples.append(
                f"edge source_system={row[0]} scope_key={row[1]} "
                f"source_ref={row[2]} x{row[3]}"
            )
        for row in run_dups:
            samples.append(
                f"run source_system={row[0]} scope_key={row[1]} "
                f"run_id={row[2]} x{row[3]}"
            )
        raise RuntimeError(
            "中止降级：存在跨 scope 复用的 source_ref/run_id，"
            "旧版全局唯一键无法无损表达。冲突样例："
            + "; ".join(samples)
        )

    with op.batch_alter_table("lineage_import_runs") as batch:
        batch.drop_constraint("uq_lineage_run_identity", type_="unique")
        batch.create_unique_constraint(
            "uq_lineage_run_ref",
            ["project_id", "source_system", "run_id"],
        )
    with op.batch_alter_table("external_lineage_edges") as batch:
        batch.drop_constraint("uq_external_lineage_identity", type_="unique")
        batch.create_unique_constraint(
            "uq_external_lineage_ref",
            ["project_id", "source_system", "source_ref"],
        )
