"""数据建模中心 MVP — API 请求/响应模型（Pydantic）。

与 docs/API 接口契约.json（OpenAPI 3.1, v0.6.0）、docs/v0.8 契约
（OpenAPI 3.1, v0.8.0，新增 ModelConstraintCreate / ModelIndexCreate）
及 docs/v0.9.2 契约（ExternalLineageImport / ExternalLineageEdgeIn，
Scope 身份治理）保持一致：请求校验与领域校验分层；错误响应可机器处理。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------- 枚举

Layer = Literal["ODS", "DWD", "DWS", "ADS", "DIM"]
ModelType = Literal["TABLE", "FACT", "DIMENSION", "VIEW"]
TargetEngine = Literal["postgresql", "mysql", "hive"]
LogicalType = Literal[
    "STRING", "VARCHAR", "INTEGER", "BIGINT", "DECIMAL", "DATE", "TIMESTAMP", "BOOLEAN"
]
Sensitivity = Literal["public", "internal", "sensitive", "restricted"]
Priority = Literal["P0", "P1", "P2", "P3"]
RequirementStatus = Literal["draft", "in_analysis", "modeled", "accepted", "rejected"]
RelationshipType = Literal["depends_on", "joins_to", "derived_from", "references"]
Cardinality = Literal[
    "unspecified", "one_to_one", "one_to_many", "many_to_one", "many_to_many"
]
ModelStatus = Literal["draft", "in_review", "approved", "published"]


# ---------------------------------------------------------------- 项目

class ProjectCreate(BaseModel):
    project_key: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=160)
    description: str = ""


# ---------------------------------------------------------------- 主题域

class SubjectAreaCreate(BaseModel):
    area_key: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    owner: str = Field(default="", max_length=80)


# ---------------------------------------------------------------- 业务需求

class BusinessRequirementCreate(BaseModel):
    requirement_key: str = Field(
        min_length=2, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$"
    )
    title: str = Field(min_length=1, max_length=200)
    business_domain: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=8000)
    acceptance_criteria: str = Field(default="", max_length=8000)
    priority: Priority = "P2"
    status: RequirementStatus = "draft"
    owner: str = Field(default="", max_length=80)


class BusinessRequirementUpdate(BaseModel):
    requirement_key: str = Field(
        min_length=2, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$"
    )
    title: str = Field(min_length=1, max_length=200)
    business_domain: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=8000)
    acceptance_criteria: str = Field(default="", max_length=8000)
    priority: Priority = "P2"
    status: RequirementStatus = "draft"
    owner: str = Field(default="", max_length=80)


# ---------------------------------------------------------------- 字段与模型

class FieldIn(BaseModel):
    field_code: str = Field(min_length=1, max_length=128)
    field_name: str = ""
    logical_type: LogicalType = "STRING"
    length: int | None = Field(default=None, ge=1, le=65535)
    precision: int | None = Field(default=None, ge=1, le=38)
    scale: int | None = Field(default=None, ge=0, le=18)
    nullable: bool = True
    is_primary_key: bool = False
    is_unique: bool = False
    default_value: str | None = None
    description: str = ""
    source_table: str = ""
    source_field: str = ""
    sensitivity: Sensitivity = "internal"


class ModelCreate(BaseModel):
    model_key: str = Field(
        min_length=2, max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9_]*$"
    )
    name: str = Field(min_length=1, max_length=160)
    description: str = ""
    layer: Layer = "DWD"
    model_type: ModelType = "TABLE"
    target_engine: TargetEngine = "postgresql"
    subject_area_id: int | None = Field(default=None, ge=1)
    requirement_ids: list[int] = Field(default_factory=list, max_length=100)
    fields: list[FieldIn] = Field(default_factory=list, max_length=500)


class ModelUpdate(BaseModel):
    model_key: str = Field(
        min_length=2, max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9_]*$"
    )
    name: str = Field(min_length=1, max_length=160)
    description: str = ""
    layer: Layer = "DWD"
    model_type: ModelType = "TABLE"
    target_engine: TargetEngine = "postgresql"
    subject_area_id: int | None = Field(default=None, ge=1)
    requirement_ids: list[int] = Field(default_factory=list, max_length=100)
    fields: list[FieldIn] = Field(default_factory=list, max_length=500)
    expected_revision: int = Field(ge=1)


# ---------------------------------------------------------------- 模型关系

class ModelRelationshipCreate(BaseModel):
    target_model_id: int = Field(ge=1)
    relationship_type: RelationshipType = "depends_on"
    source_fields: list[str] = Field(default_factory=list, max_length=100)
    target_fields: list[str] = Field(default_factory=list, max_length=100)
    cardinality: Cardinality = "unspecified"
    description: str = Field(default="", max_length=4000)


# ---------------------------------------------------------------- 表级约束与索引（V0.8）

ConstraintType = Literal["primary_key", "unique", "check", "foreign_key"]
OnDeleteAction = Literal["NO ACTION", "RESTRICT", "CASCADE", "SET NULL"]
IndexType = Literal["btree", "hash", "gin"]

_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"


class ModelConstraintCreate(BaseModel):
    constraint_name: str = Field(min_length=1, max_length=128, pattern=_NAME_PATTERN)
    constraint_type: ConstraintType
    field_codes: list[str] = Field(default_factory=list, max_length=50)
    expression: str = Field(default="", max_length=1000)
    referenced_model_id: int | None = Field(default=None, ge=1)
    referenced_field_codes: list[str] = Field(default_factory=list, max_length=50)
    on_delete: OnDeleteAction = "NO ACTION"
    description: str = Field(default="", max_length=4000)


class ModelIndexCreate(BaseModel):
    index_name: str = Field(min_length=1, max_length=128, pattern=_NAME_PATTERN)
    index_type: IndexType = "btree"
    field_codes: list[str] = Field(min_length=1, max_length=50)
    is_unique: bool = False
    description: str = Field(default="", max_length=4000)


# ---------------------------------------------------------------- 外部血缘导入（V0.9.2）

LineageType = Literal[
    "etl_data_flow", "scheduler_dependency", "bi_consumption",
    "dbt_dependency", "other",
]
TransformationType = Literal[
    "copy", "sql", "join", "aggregate", "filter", "lookup", "unknown"
]
ScanMode = Literal["incremental", "full_snapshot"]

# 血缘标识 token 模式（source_ref / run_id / scope_key）。V1.0 dbt 连接器
# 引入 `dbt:{P}=>{N}` 形态的 source_ref（含 = >）与 ISO 时间戳 run_id
# （含 + :），故字符集在 V0.9.2 基础上放宽（仅放宽接受面，既有取值不变）。
_TOKEN_PATTERN = r"^[A-Za-z0-9_.:/@=<>+-]+$"


class ExternalLineageEdgeIn(BaseModel):
    source_ref: str = Field(
        min_length=1, max_length=256, pattern=_TOKEN_PATTERN
    )
    job_key: str = Field(default="", max_length=160)
    source_model_key: str = Field(min_length=2, max_length=128)
    target_model_key: str = Field(min_length=2, max_length=128)
    lineage_type: LineageType = "etl_data_flow"
    source_fields: list[str] = Field(default_factory=list, max_length=100)
    target_fields: list[str] = Field(default_factory=list, max_length=100)
    transformation_type: TransformationType = "unknown"
    confidence: int = Field(default=100, ge=0, le=100)
    observed_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExternalLineageImport(BaseModel):
    source_system: str = Field(
        min_length=2, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$"
    )
    run_id: str = Field(min_length=1, max_length=128, pattern=_TOKEN_PATTERN)
    observed_at: datetime | None = None
    scan_mode: ScanMode = "incremental"
    scope_key: str = Field(
        default="default", min_length=1, max_length=128, pattern=_TOKEN_PATTERN
    )
    complete_scan: bool = False
    # V1.0 M5：full_snapshot 计划停用比例超阈值时必须显式确认
    confirm_deactivation: bool = False
    edges: list[ExternalLineageEdgeIn] = Field(default_factory=list, max_length=1000)


# ---------------------------------------------------------------- 评审与 DDL

class ReviewDecision(BaseModel):
    comment: str = ""


class DDLRequest(BaseModel):
    target_engine: TargetEngine
    schema_name: str | None = Field(default=None, max_length=128)


# ---------------------------------------------------------------- 认证与授权（V1.0）

MemberRole = Literal["owner", "editor", "viewer"]

# 主体键模式：小写前缀 + 冒号 + 1-64 位安全字符（u:alice / sa:dbt-importer）
_USER_KEY_PATTERN = r"^[a-z]+:[A-Za-z0-9_.-]{1,64}$"


class ApiKeyCreate(BaseModel):
    """创建 API Key：secret 仅在创建响应返回一次。

    user_key 仅平台管理员可用：为指定主体（成员/连接器服务账号）代建
    密钥——否则新成员没有获取密钥的自助路径（设计文档实施补充）。
    """

    name: str = Field(min_length=1, max_length=80)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)
    user_key: str | None = Field(default=None, max_length=80)


class BootstrapIn(BaseModel):
    """一次性引导：创建首个平台管理员（BOOTSTRAP_TOKEN 保护）。"""

    bootstrap_token: str = Field(min_length=1, max_length=256)
    user_key: str = Field(min_length=3, max_length=80, pattern=_USER_KEY_PATTERN)
    display_name: str = Field(min_length=1, max_length=160)


# ---------------------------------------------------------------- 密码登录（V1.3）


class LoginIn(BaseModel):
    """账号密码登录：login 接受 user_key 原样或裸登录名（自动补 u: 前缀）。"""

    login: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=128)


class PasswordChangeIn(BaseModel):
    """修改本人密码：需校验当前口令；新口令 ≥8 位且不得与当前相同。"""

    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class ProjectMemberIn(BaseModel):
    """项目成员 upsert：(project, user) 唯一，幂等改角色。"""

    user_key: str = Field(min_length=3, max_length=80, pattern=_USER_KEY_PATTERN)
    role: MemberRole


UserKind = Literal["human", "service_account"]
UserStatus = Literal["active", "disabled"]


class UserCreate(BaseModel):
    """创建主体（平台管理员）：创建即最低权限，无任何项目成员关系。

    与 V1.0 的"代建密钥即开通"互补：本端点管"人"，api-keys 代建管"凭据"。
    """

    user_key: str = Field(min_length=3, max_length=80, pattern=_USER_KEY_PATTERN)
    display_name: str = Field(min_length=1, max_length=160)
    kind: UserKind = "human"
    is_admin: bool = False


class UserUpdate(BaseModel):
    """更新主体（平台管理员）：display_name / is_admin / status 可改。

    防护规则见设计文档 2.4：P1 最后 admin 保护、P2 禁自降级、P3 禁自禁用、
    P6 禁用即时失效存量密钥——在端点层执行，本 schema 只约束形状。
    kind 为身份属性，创建后不可变更（不在本模型中出现）。
    """

    display_name: str | None = Field(default=None, min_length=1, max_length=160)
    is_admin: bool | None = None
    status: UserStatus | None = None


class DbtManifestImport(BaseModel):
    """dbt manifest 参考连接器导入（M3）：manifest 作为对象字段，
    不做 multipart；推导边数由服务端从 parent_map 计算。"""

    manifest: dict[str, Any]
    run_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=_TOKEN_PATTERN
    )
    scope_key: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=_TOKEN_PATTERN
    )
    source_system: str | None = Field(
        default=None, min_length=2, max_length=64,
        pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$",
    )
    skip_unmatched: bool = False
    observed_at: datetime | None = None


# ---------------------------------------------------------------- 响应辅助

def iso(dt: datetime | None) -> str | None:
    """统一 ISO-8601 输出；SQLite 读回为 naive 时按 UTC 处理。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.isoformat() + "+00:00"
    return dt.isoformat()
