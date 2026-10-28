"""lineage scope governance v09.1: scope_key / scan_mode / failure counts

Revision ID: a4d7c92f5e10
Revises: 9bf3e67a1d52
Create Date: 2026-10-22 10:00:00.000000

V0.9.1 导入运维治理（scope 身份引入的第一步；唯一键纳入 scope 在
V0.9.2 完成）：
- external_lineage_edges.scope_key（既有行回填 "default"）
- lineage_import_runs.scope_key / scan_mode / complete_scan /
  attempt_count / failure_count / deactivated_count / error_summary_json
  （既有行回填 incremental 语义与默认计数）
- scope 查询索引（project_id, source_system, scope_key, active/status）

注意：本迁移不改变两项业务唯一键；跨 scope 身份冲突由 V0.9.2 修复。
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a4d7c92f5e10"
down_revision: Union[str, None] = "9bf3e67a1d52"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- external_lineage_edges：扫描 scope 身份 ---
    op.add_column(
        "external_lineage_edges",
        sa.Column("scope_key", sa.String(length=128), nullable=False,
                  server_default="default"),
    )
    op.create_index(
        "ix_external_lineage_scope",
        "external_lineage_edges",
        ["project_id", "source_system", "scope_key", "active"],
        unique=False,
    )
    # --- lineage_import_runs：批次治理字段 ---
    op.add_column(
        "lineage_import_runs",
        sa.Column("scope_key", sa.String(length=128), nullable=False,
                  server_default="default"),
    )
    op.add_column(
        "lineage_import_runs",
        sa.Column("scan_mode", sa.String(length=24), nullable=False,
                  server_default="incremental"),
    )
    op.add_column(
        "lineage_import_runs",
        sa.Column("complete_scan", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )
    op.add_column(
        "lineage_import_runs",
        sa.Column("deactivated_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "lineage_import_runs",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "lineage_import_runs",
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "lineage_import_runs",
        sa.Column("error_summary_json", sa.Text(), nullable=False, server_default="[]"),
    )
    op.create_index(
        "ix_lineage_run_scope_status",
        "lineage_import_runs",
        ["project_id", "source_system", "scope_key", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_lineage_run_scope_status", table_name="lineage_import_runs")
    op.drop_column("lineage_import_runs", "error_summary_json")
    op.drop_column("lineage_import_runs", "failure_count")
    op.drop_column("lineage_import_runs", "attempt_count")
    op.drop_column("lineage_import_runs", "deactivated_count")
    op.drop_column("lineage_import_runs", "complete_scan")
    op.drop_column("lineage_import_runs", "scan_mode")
    op.drop_column("lineage_import_runs", "scope_key")
    op.drop_index("ix_external_lineage_scope", table_name="external_lineage_edges")
    op.drop_column("external_lineage_edges", "scope_key")
