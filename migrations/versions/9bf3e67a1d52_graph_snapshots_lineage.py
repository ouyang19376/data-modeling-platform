"""graph snapshots & external lineage v09: three new tables

Revision ID: 9bf3e67a1d52
Revises: 4a8fc21d0e63
Create Date: 2026-10-22 09:00:00.000000

V0.9 项目图谱快照与外部血缘：
- project_graph_snapshots：发布事务同写的不可变项目图快照
  （snapshot_no 项目内唯一，graph_hash 为规范化 JSON 的 SHA-256）
- external_lineage_edges：外部来源系统观测血缘边（upsert 身份 =
  project_id + source_system + source_ref；active 显式停用）
- lineage_import_runs：导入批次幂等记录（同 run_id 同 payload 重放，
  不同 payload 冲突）

非破坏性升级：既有业务表不改列、不重建。downgrade 会删除三张新表
及其数据，仅限隔离测试库演练。
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "9bf3e67a1d52"
down_revision: Union[str, None] = "4a8fc21d0e63"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "project_graph_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("snapshot_no", sa.Integer(), nullable=False),
        sa.Column("trigger_model_id", sa.Integer(), nullable=True),
        sa.Column("trigger_model_version_no", sa.Integer(), nullable=True),
        sa.Column("snapshot_type", sa.String(length=32), nullable=False),
        sa.Column("graph_json", sa.Text(), nullable=False),
        sa.Column("graph_hash", sa.String(length=64), nullable=False),
        sa.Column("node_count", sa.Integer(), nullable=False),
        sa.Column("edge_count", sa.Integer(), nullable=False),
        sa.Column("omitted_edge_count", sa.Integer(), nullable=False),
        sa.Column("change_note", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trigger_model_id"], ["data_models.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("snapshot_no > 0", name="ck_graph_snapshot_no_positive"),
        sa.UniqueConstraint("project_id", "snapshot_no", name="uq_graph_snapshot_project_no"),
    )
    op.create_index(
        "ix_graph_snapshot_project_created",
        "project_graph_snapshots",
        ["project_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "external_lineage_edges",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("source_system", sa.String(length=64), nullable=False),
        sa.Column("source_ref", sa.String(length=256), nullable=False),
        sa.Column("source_model_id", sa.Integer(), nullable=False),
        sa.Column("target_model_id", sa.Integer(), nullable=False),
        sa.Column("job_key", sa.String(length=160), nullable=False),
        sa.Column("lineage_type", sa.String(length=32), nullable=False),
        sa.Column("transformation_type", sa.String(length=24), nullable=False),
        sa.Column("source_fields_json", sa.Text(), nullable=False),
        sa.Column("target_fields_json", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_model_id"], ["data_models.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_model_id"], ["data_models.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "source_model_id <> target_model_id",
            name="ck_external_lineage_not_self",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 100",
            name="ck_external_lineage_confidence",
        ),
        sa.UniqueConstraint(
            "project_id", "source_system", "source_ref",
            name="uq_external_lineage_ref",
        ),
    )
    op.create_index(
        "ix_external_lineage_source", "external_lineage_edges", ["source_model_id"],
        unique=False,
    )
    op.create_index(
        "ix_external_lineage_target", "external_lineage_edges", ["target_model_id"],
        unique=False,
    )
    op.create_index(
        "ix_external_lineage_observed", "external_lineage_edges", ["observed_at"],
        unique=False,
    )
    op.create_table(
        "lineage_import_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("source_system", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("received_count", sa.Integer(), nullable=False),
        sa.Column("inserted_count", sa.Integer(), nullable=False),
        sa.Column("updated_count", sa.Integer(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(length=80), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "source_system", "run_id",
            name="uq_lineage_run_ref",
        ),
    )
    op.create_index(
        "ix_lineage_run_project_created",
        "lineage_import_runs",
        ["project_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_lineage_run_project_created", table_name="lineage_import_runs")
    op.drop_table("lineage_import_runs")
    op.drop_index("ix_external_lineage_observed", table_name="external_lineage_edges")
    op.drop_index("ix_external_lineage_target", table_name="external_lineage_edges")
    op.drop_index("ix_external_lineage_source", table_name="external_lineage_edges")
    op.drop_table("external_lineage_edges")
    op.drop_index(
        "ix_graph_snapshot_project_created", table_name="project_graph_snapshots"
    )
    op.drop_table("project_graph_snapshots")
