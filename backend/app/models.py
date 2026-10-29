"""数据建模中心 MVP — ORM 实体（15 张表）。

schema 演进由 Alembic 管理：
- 初始 revision d50979bffe74：projects / data_models / model_fields /
  model_versions / activity_events / audit_logs（V0.5 基线 6 表）
- 增量 revision 761be0cda0ff：business_requirements / subject_areas /
  model_relationships / requirement_model_links + data_models.subject_area_id
  （V0.6 业务追溯与模型关系增强）
- 增量 revision 1b30fa87c419：model_fields.is_unique（V0.7 字段设计器补全，
  单列唯一约束元数据；既有行回填 false）
- 增量 revision 4a8fc21d0e63：model_constraints / model_indexes（V0.8
  表级约束与索引元数据；非破坏性新增两张表）
- 增量 revision 9bf3e67a1d52：project_graph_snapshots /
  external_lineage_edges / lineage_import_runs（V0.9 图谱快照与外部血缘）
- 增量 revision a4d7c92f5e10：scope_key / scan_mode / complete_scan /
  attempt_count / failure_count / deactivated_count（V0.9.1 导入运维治理）
- 增量 revision b5f2c4a9d703：边与批次唯一键纳入 scope_key（V0.9.2
  Scope 身份治理；不新增业务表）
- 增量 revision 7f0c38e854e5：users / api_keys / project_members 三表 +
  lineage_import_runs.deactivation_confirmed（V1.0 认证授权与停用确认；
  users.is_admin 为设计文档 3.2 需要而 ERD 遗漏的实施补充）

时间统一存 UTC；前端按用户时区展示。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class SubjectArea(Base):
    """主题域：项目内业务分类（与数仓分层、物理引擎相互独立）。"""

    __tablename__ = "subject_areas"
    __table_args__ = (
        UniqueConstraint("project_id", "area_key", name="uq_subject_area_project_key"),
        Index("ix_subject_area_project", "project_id", "name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    area_key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    owner: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class BusinessRequirement(Base):
    """业务需求：建模工作的上游输入（为什么需要数据、谁使用、口径是什么）。"""

    __tablename__ = "business_requirements"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "requirement_key", name="uq_requirement_project_key"
        ),
        Index("ix_requirement_project_status", "project_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    requirement_key: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    business_domain: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    acceptance_criteria: Mapped[str] = mapped_column(Text, default="", nullable=False)
    priority: Mapped[str] = mapped_column(String(16), default="P2", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="draft", nullable=False)
    owner: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    created_by: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class DataModel(Base):
    """模型稳定身份：不存可变的表字段结构；结构在 model_fields，
    正式快照在 model_versions。"""

    __tablename__ = "data_models"
    __table_args__ = (
        UniqueConstraint("project_id", "model_key", name="uq_model_project_key"),
        Index("ix_model_project_status", "project_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    model_key: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    layer: Mapped[str] = mapped_column(String(16), default="DWD", nullable=False)
    model_type: Mapped[str] = mapped_column(String(16), default="TABLE", nullable=False)
    target_engine: Mapped[str] = mapped_column(
        String(32), default="postgresql", nullable=False
    )
    subject_area_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("subject_areas.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(24), default="draft", nullable=False)
    edit_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    published_version_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    fields: Mapped[list["ModelField"]] = relationship(
        back_populates="model",
        cascade="all, delete-orphan",
        order_by="ModelField.ordinal",
    )


class ModelField(Base):
    """模型字段（草稿结构）：MVP 中模型即单表，字段直接挂在模型上。"""

    __tablename__ = "model_fields"
    __table_args__ = (
        UniqueConstraint("model_id", "field_code", name="uq_field_model_code"),
        Index("ix_field_model_ordinal", "model_id", "ordinal"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("data_models.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    field_code: Mapped[str] = mapped_column(String(128), nullable=False)
    field_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    logical_type: Mapped[str] = mapped_column(String(32), default="STRING", nullable=False)
    length: Mapped[int | None] = mapped_column(Integer, nullable=True)
    precision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scale: Mapped[int | None] = mapped_column(Integer, nullable=True)
    nullable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_primary_key: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_unique: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    default_value: Mapped[str | None] = mapped_column(String(256), nullable=True)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    source_table: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    source_field: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    sensitivity: Mapped[str] = mapped_column(
        String(16), default="internal", nullable=False
    )

    model: Mapped[DataModel] = relationship(back_populates="fields")


class ModelVersion(Base):
    """正式发布版本的不可变快照。禁止通过普通更新接口修改已发布快照。"""

    __tablename__ = "model_versions"
    __table_args__ = (
        UniqueConstraint("model_id", "version_no", name="uq_version_model_no"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("data_models.id", ondelete="CASCADE"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="published", nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, default="", nullable=False)
    change_note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_by: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class RequirementModelLink(Base):
    """需求与模型的多对多关联（幂等：重复保存不产生重复行）。"""

    __tablename__ = "requirement_model_links"
    __table_args__ = (
        UniqueConstraint(
            "requirement_id", "model_id", name="uq_requirement_model_link"
        ),
        Index("ix_req_model_link_model", "model_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    requirement_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("business_requirements.id", ondelete="CASCADE"),
        nullable=False,
    )
    model_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("data_models.id", ondelete="CASCADE"), nullable=False
    )
    created_by: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ModelRelationship(Base):
    """模型间关系：平台元数据，不自动创建数据库外键，不影响 DDL。"""

    __tablename__ = "model_relationships"
    __table_args__ = (
        CheckConstraint(
            "source_model_id <> target_model_id",
            name="ck_model_relationship_not_self",
        ),
        UniqueConstraint(
            "source_model_id",
            "target_model_id",
            "relationship_type",
            name="uq_model_relationship_pair_type",
        ),
        Index("ix_model_relationship_project", "project_id", "source_model_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    source_model_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("data_models.id", ondelete="CASCADE"), nullable=False
    )
    target_model_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("data_models.id", ondelete="CASCADE"), nullable=False
    )
    relationship_type: Mapped[str] = mapped_column(String(24), nullable=False)
    source_fields_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    target_fields_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    cardinality: Mapped[str] = mapped_column(
        String(24), default="unspecified", nullable=False
    )
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_by: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ModelConstraint(Base):
    """表级约束（草稿结构）：复合主键、复合唯一、CHECK、外键。

    与字段级 is_primary_key/is_unique 互补：跨多列的结构意图在此实体化，
    允许独立校验、审计和版本比较。field_codes_json 为有序列表
    （复合键列顺序属于结构定义）；CHECK 的表达式存 expression，
    其 field_codes_json 必须为空，避免两个定义来源互相冲突。
    """

    __tablename__ = "model_constraints"
    __table_args__ = (
        UniqueConstraint(
            "model_id", "constraint_name", name="uq_model_constraint_name"
        ),
        Index("ix_model_constraint_type", "model_id", "constraint_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("data_models.id", ondelete="CASCADE"), nullable=False
    )
    constraint_name: Mapped[str] = mapped_column(String(128), nullable=False)
    constraint_type: Mapped[str] = mapped_column(String(24), nullable=False)
    field_codes_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    expression: Mapped[str] = mapped_column(Text, default="", nullable=False)
    referenced_model_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("data_models.id", ondelete="RESTRICT"),
        nullable=True,
    )
    referenced_field_codes_json: Mapped[str] = mapped_column(
        Text, default="[]", nullable=False
    )
    on_delete: Mapped[str] = mapped_column(
        String(24), default="NO ACTION", nullable=False
    )
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_by: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ModelIndex(Base):
    """索引元数据（草稿结构）：查询访问路径意图，不替代唯一性定义。

    field_codes_json 为有序列表（复合索引列顺序影响左前缀匹配）。
    引擎能力差异（MySQL 仅 btree、Hive 不生成物理索引、PG 拒绝 unique GIN）
    在服务端校验与 DDL 生成层处理。
    """

    __tablename__ = "model_indexes"
    __table_args__ = (
        UniqueConstraint("model_id", "index_name", name="uq_model_index_name"),
        Index("ix_model_index_model", "model_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("data_models.id", ondelete="CASCADE"), nullable=False
    )
    index_name: Mapped[str] = mapped_column(String(128), nullable=False)
    index_type: Mapped[str] = mapped_column(String(16), default="btree", nullable=False)
    field_codes_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    is_unique: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_by: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ProjectGraphSnapshot(Base):
    """项目级图谱快照（不可变）：每次发布在同一事务中写入。

    快照节点取每个模型 published_version_no 指向的正式版本快照，
    边从各模型已发布快照的相邻关系构建；外部观测血缘不混入本表。
    graph_hash 为规范化 JSON 的 SHA-256，用于发现序列化不一致。
    snapshot_type=publish 表示发布触发；migration_baseline 表示 V0.9
    启动时为旧库创建的一次性基线（幂等，不伪造历史发布轨迹）。
    """

    __tablename__ = "project_graph_snapshots"
    __table_args__ = (
        CheckConstraint("snapshot_no > 0", name="ck_graph_snapshot_no_positive"),
        UniqueConstraint(
            "project_id", "snapshot_no", name="uq_graph_snapshot_project_no"
        ),
        Index("ix_graph_snapshot_project_created", "project_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_no: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger_model_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("data_models.id", ondelete="SET NULL"),
        nullable=True,
    )
    trigger_model_version_no: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    snapshot_type: Mapped[str] = mapped_column(
        String(32), default="publish", nullable=False
    )
    graph_json: Mapped[str] = mapped_column(Text, default="", nullable=False)
    graph_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    node_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    edge_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    omitted_edge_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    change_note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_by: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ExternalLineageEdge(Base):
    """外部观测血缘边：Airflow/dbt/BI 等来源系统扫描结果。

    与 model_relationships 分域：本表是"某工具在某次扫描中观察到什么"，
    由导入 adapter 写入；不自动写回正式图谱快照。
    V0.9.2 身份键 = project_id + source_system + scope_key + source_ref，
    允许同一来源系统在不同扫描 scope（DAG/job group/catalog namespace）
    复用局部边 ID。active=false 为显式停用（软删除，保留可追溯）。
    """

    __tablename__ = "external_lineage_edges"
    __table_args__ = (
        CheckConstraint(
            "source_model_id <> target_model_id",
            name="ck_external_lineage_not_self",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 100",
            name="ck_external_lineage_confidence",
        ),
        UniqueConstraint(
            "project_id",
            "source_system",
            "scope_key",
            "source_ref",
            name="uq_external_lineage_identity",
        ),
        Index(
            "ix_external_lineage_scope",
            "project_id",
            "source_system",
            "scope_key",
            "active",
        ),
        Index("ix_external_lineage_source", "source_model_id"),
        Index("ix_external_lineage_target", "target_model_id"),
        Index("ix_external_lineage_observed", "observed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_key: Mapped[str] = mapped_column(
        String(128), default="default", nullable=False
    )
    source_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    source_model_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("data_models.id", ondelete="CASCADE"), nullable=False
    )
    target_model_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("data_models.id", ondelete="CASCADE"), nullable=False
    )
    job_key: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    lineage_type: Mapped[str] = mapped_column(
        String(32), default="etl_data_flow", nullable=False
    )
    transformation_type: Mapped[str] = mapped_column(
        String(24), default="unknown", nullable=False
    )
    source_fields_json: Mapped[str] = mapped_column(
        Text, default="[]", nullable=False
    )
    target_fields_json: Mapped[str] = mapped_column(
        Text, default="[]", nullable=False
    )
    confidence: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    metadata_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class LineageImportRun(Base):
    """外部血缘导入批次：幂等键 + 可观测性。

    V0.9.2 批次身份键 = project_id + source_system + scope_key + run_id；
    同身份同 payload_hash 重放返回 200 replayed=true，不同 payload 返回
    409 LINEAGE_RUN_ID_REUSED。校验失败的批次落 status=failed 行
    （记录 attempt_count/failure_count/error_summary），但不写入任何边；
    同 payload 可再次尝试。首次通过时创建 status=completed 记录。
    """

    __tablename__ = "lineage_import_runs"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "source_system",
            "scope_key",
            "run_id",
            name="uq_lineage_run_identity",
        ),
        Index("ix_lineage_run_project_created", "project_id", "created_at"),
        Index(
            "ix_lineage_run_scope_status",
            "project_id",
            "source_system",
            "scope_key",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_key: Mapped[str] = mapped_column(
        String(128), default="default", nullable=False
    )
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    scan_mode: Mapped[str] = mapped_column(
        String(24), default="incremental", nullable=False
    )
    complete_scan: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="completed", nullable=False)
    received_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    inserted_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    deactivated_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # V1.0 full_snapshot 停用人工确认标志（M5）：超阈值停用必须显式
    # confirm_deactivation=true；既有行回填 false。
    deactivation_confirmed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    error_summary_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    result_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    actor: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class User(Base):
    """认证主体（V1.0）：人与服务账号共用一张表，kind 区分。

    user_key 全局唯一，模式 `^[a-z]+:[A-Za-z0-9_.-]{1,64}$`（如 u:alice、
    sa:dbt-importer）；auth_source 为 OIDC 预留。status=disabled 后该主体
    的全部密钥即时失效（认证路径实时校验）。password_hash 为可空的
    PBKDF2 哈希串（V1.3 密码登录）；NULL 表示仅密钥/演示身份主体。
    """

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("kind IN ('human','service_account')", name="ck_user_kind"),
        CheckConstraint("status IN ('active','disabled')", name="ck_user_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    # 平台管理员旁路（设计文档 3.2 的 is_admin；ERD 遗漏列，实施补充）：
    # 绕过项目成员检查；bootstrap 创建的首个管理员为 true。
    is_admin: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    auth_source: Mapped[str] = mapped_column(
        String(24), default="local", nullable=False
    )
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False)
    # V1.3 密码登录：PBKDF2-HMAC-SHA256 哈希（格式见 auth.hash_password）。
    # 可空：服务账号与仅密钥主体无密码；种子演示账号在 demo 模式启动时
    # 幂等播种（见 auth.ensure_demo_accounts）。
    password_hash: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ApiKey(Base):
    """API Key 凭据（V1.0）：只存 SHA-256(secret) 哈希，secret 仅创建响应
    返回一次。令牌格式 Bearer dmc_<key_id>_<secret>。"""

    __tablename__ = "api_keys"
    __table_args__ = (
        CheckConstraint("status IN ('active','revoked')", name="ck_api_key_status"),
        Index("ix_api_key_user", "user_id"),
        Index("ix_api_key_hash", "key_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key_id: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ProjectMember(Base):
    """项目级角色授权（V1.0）：(project, user) 唯一，角色 owner/editor/viewer；
    平台 admin（users.is_admin 之外的旁路标记由认证主体携带）绕过检查。"""

    __tablename__ = "project_members"
    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_project_member"),
        CheckConstraint(
            "role IN ('owner','editor','viewer')", name="ck_member_role"
        ),
        Index("ix_project_member_project", "project_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(24), nullable=False)
    created_by: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ActivityEvent(Base):
    """业务活动事件：描述业务发生了什么（时间线/漏斗分析）。"""

    __tablename__ = "activity_events"
    __table_args__ = (Index("ix_event_project_created", "project_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    detail_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class AuditLog(Base):
    """安全审计：谁在何时对哪个对象做了什么关键操作，结果如何。"""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_object_created", "object_type", "object_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    object_type: Mapped[str] = mapped_column(String(64), nullable=False)
    object_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    outcome: Mapped[str] = mapped_column(String(24), default="success", nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
