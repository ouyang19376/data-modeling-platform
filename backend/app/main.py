"""数据建模中心 MVP — FastAPI 主应用。

路由做入参验证与编排；复杂规则在 validation.py / ddl.py；
状态迁移、版本快照、发布事务在本模块的用例逻辑中保证原子性。

启动顺序：环境安全检查 → schema revision 检查/迁移 → 可选演示数据 → 接收请求。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import sys
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from fastapi import Depends, FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from sqlalchemy import func, inspect, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models
from . import auth as auth_mod
from .config import settings
from .db import engine, get_db
from .ddl import DDLGenerationError, generate_create_table
from .errors import ApiError, conflict, invalid_state, not_found
from .events import record_audit, record_event
from .logging_setup import setup_logging
from .schemas import (
    ApiKeyCreate,
    BootstrapIn,
    BusinessRequirementCreate,
    BusinessRequirementUpdate,
    DDLRequest,
    DbtManifestImport,
    ExternalLineageEdgeIn,
    ExternalLineageImport,
    FieldIn,
    LoginIn,
    ModelConstraintCreate,
    ModelCreate,
    ModelIndexCreate,
    ModelRelationshipCreate,
    ModelUpdate,
    PasswordChangeIn,
    ProjectCreate,
    ProjectMemberIn,
    ReviewDecision,
    SubjectAreaCreate,
    UserCreate,
    UserUpdate,
    iso,
)
from .seed import seed_demo_data
from .validation import (
    RULES,
    engine_supports_index,
    validate_check_expression,
    validate_model,
)

BASE_DIR = Path(__file__).resolve().parents[2]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

HEAD_REVISION = "d4e5f6a7b8c9"
INITIAL_REVISION = "d50979bffe74"

STARTED_AT = time.time()

APP_VERSION = "1.3.0"

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 错误模型
# ApiError / not_found / conflict / invalid_state 定义于 errors.py（V1.0
# 抽出，供 auth 等模块共用，避免循环导入）。

# ---------------------------------------------------------------- 请求上下文

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def resolve_request_id(request: Request) -> str:
    raw = request.headers.get("x-request-id")
    if raw and _REQUEST_ID_PATTERN.match(raw):
        return raw
    return uuid.uuid4().hex


def resolve_actor(request: Request) -> str:
    if settings.allow_demo_actor_header:
        actor = (request.headers.get("x-actor") or "").strip()
        if actor:
            return actor[:80]
    return "demo.user"


# ---------------------------------------------------------------- 序列化


def project_dict(p: models.Project) -> dict[str, Any]:
    return {
        "id": p.id,
        "project_key": p.project_key,
        "name": p.name,
        "description": p.description,
        "created_at": iso(p.created_at),
    }


def subject_area_dict(a: models.SubjectArea) -> dict[str, Any]:
    return {
        "id": a.id,
        "project_id": a.project_id,
        "area_key": a.area_key,
        "name": a.name,
        "description": a.description,
        "owner": a.owner,
        "created_at": iso(a.created_at),
    }


def requirement_dict(
    r: models.BusinessRequirement, linked_model_count: int | None = None
) -> dict[str, Any]:
    data = {
        "id": r.id,
        "project_id": r.project_id,
        "requirement_key": r.requirement_key,
        "title": r.title,
        "business_domain": r.business_domain,
        "description": r.description,
        "acceptance_criteria": r.acceptance_criteria,
        "priority": r.priority,
        "status": r.status,
        "owner": r.owner,
        "created_by": r.created_by,
        "created_at": iso(r.created_at),
        "updated_at": iso(r.updated_at),
    }
    if linked_model_count is not None:
        data["linked_model_count"] = linked_model_count
    return data


def field_dict(f: models.ModelField) -> dict[str, Any]:
    return {
        "id": f.id,
        "ordinal": f.ordinal,
        "field_code": f.field_code,
        "field_name": f.field_name,
        "logical_type": f.logical_type,
        "length": f.length,
        "precision": f.precision,
        "scale": f.scale,
        "nullable": f.nullable,
        "is_primary_key": f.is_primary_key,
        "is_unique": f.is_unique,
        "default_value": f.default_value,
        "description": f.description,
        "source_table": f.source_table,
        "source_field": f.source_field,
        "sensitivity": f.sensitivity,
    }


def relationship_dict(r: models.ModelRelationship) -> dict[str, Any]:
    return {
        "id": r.id,
        "project_id": r.project_id,
        "source_model_id": r.source_model_id,
        "target_model_id": r.target_model_id,
        "relationship_type": r.relationship_type,
        "source_fields": json.loads(r.source_fields_json or "[]"),
        "target_fields": json.loads(r.target_fields_json or "[]"),
        "cardinality": r.cardinality,
        "description": r.description,
        "created_by": r.created_by,
        "created_at": iso(r.created_at),
    }


def constraint_dict(c: models.ModelConstraint) -> dict[str, Any]:
    return {
        "id": c.id,
        "model_id": c.model_id,
        "constraint_name": c.constraint_name,
        "constraint_type": c.constraint_type,
        "field_codes": json.loads(c.field_codes_json or "[]"),
        "expression": c.expression,
        "referenced_model_id": c.referenced_model_id,
        "referenced_field_codes": json.loads(c.referenced_field_codes_json or "[]"),
        "on_delete": c.on_delete,
        "description": c.description,
        "created_by": c.created_by,
        "created_at": iso(c.created_at),
    }


def index_dict(i: models.ModelIndex) -> dict[str, Any]:
    return {
        "id": i.id,
        "model_id": i.model_id,
        "index_name": i.index_name,
        "index_type": i.index_type,
        "field_codes": json.loads(i.field_codes_json or "[]"),
        "is_unique": i.is_unique,
        "description": i.description,
        "created_by": i.created_by,
        "created_at": iso(i.created_at),
    }


def version_dict(v: models.ModelVersion) -> dict[str, Any]:
    return {
        "id": v.id,
        "model_id": v.model_id,
        "version_no": v.version_no,
        "status": v.status,
        "change_note": v.change_note,
        "created_by": v.created_by,
        "created_at": iso(v.created_at),
    }


def event_dict(e: models.ActivityEvent) -> dict[str, Any]:
    return {
        "id": e.id,
        "project_id": e.project_id,
        "event_type": e.event_type,
        "actor": e.actor,
        "detail": json.loads(e.detail_json or "{}"),
        "request_id": e.request_id,
        "created_at": iso(e.created_at),
    }


def model_dict(
    m: models.DataModel,
    fields: list[models.ModelField],
    requirement_ids: list[int],
    relationships: list[models.ModelRelationship] | None = None,
    constraints: list[models.ModelConstraint] | None = None,
    indexes: list[models.ModelIndex] | None = None,
) -> dict[str, Any]:
    data = {
        "id": m.id,
        "project_id": m.project_id,
        "model_key": m.model_key,
        "name": m.name,
        "description": m.description,
        "layer": m.layer,
        "model_type": m.model_type,
        "target_engine": m.target_engine,
        "subject_area_id": m.subject_area_id,
        "status": m.status,
        "edit_revision": m.edit_revision,
        "published_version_no": m.published_version_no,
        "created_by": m.created_by,
        "created_at": iso(m.created_at),
        "updated_at": iso(m.updated_at),
        "fields": [field_dict(f) for f in fields],
        "requirement_ids": requirement_ids,
    }
    if relationships is not None:
        data["relationships"] = [relationship_dict(r) for r in relationships]
    if constraints is not None:
        data["constraints"] = [constraint_dict(c) for c in constraints]
    if indexes is not None:
        data["indexes"] = [index_dict(i) for i in indexes]
    return data


# ---------------------------------------------------------------- 事件与审计
# record_event / record_audit 定义于 events.py（V1.0 抽出，供 auth 模块
# 在认证失败留痕时共用，避免循环导入）。

# ---------------------------------------------------------------- 查询辅助


def get_project(db: Session, project_id: int) -> models.Project:
    p = db.get(models.Project, project_id)
    if p is None:
        raise not_found("PROJECT_NOT_FOUND", f"项目 {project_id} 不存在")
    return p


def get_model(db: Session, model_id: int) -> models.DataModel:
    m = db.get(models.DataModel, model_id)
    if m is None:
        raise not_found("MODEL_NOT_FOUND", f"模型 {model_id} 不存在")
    return m


def get_requirement(db: Session, requirement_id: int) -> models.BusinessRequirement:
    r = db.get(models.BusinessRequirement, requirement_id)
    if r is None:
        raise not_found("REQUIREMENT_NOT_FOUND", f"需求 {requirement_id} 不存在")
    return r


def get_subject_area(db: Session, area_id: int) -> models.SubjectArea:
    a = db.get(models.SubjectArea, area_id)
    if a is None:
        raise not_found("SUBJECT_AREA_NOT_FOUND", f"主题域 {area_id} 不存在")
    return a


def model_fields(db: Session, model_id: int) -> list[models.ModelField]:
    return list(
        db.scalars(
            select(models.ModelField)
            .where(models.ModelField.model_id == model_id)
            .order_by(models.ModelField.ordinal)
        )
    )


def model_requirement_ids(db: Session, model_id: int) -> list[int]:
    return sorted(
        db.scalars(
            select(models.RequirementModelLink)
            .with_only_columns(models.RequirementModelLink.requirement_id)
            .where(models.RequirementModelLink.model_id == model_id)
        )
    )


def model_relationships(db: Session, model_id: int) -> list[models.ModelRelationship]:
    return list(
        db.scalars(
            select(models.ModelRelationship)
            .where(
                (models.ModelRelationship.source_model_id == model_id)
                | (models.ModelRelationship.target_model_id == model_id)
            )
            .order_by(models.ModelRelationship.id)
        )
    )


def model_constraints(db: Session, model_id: int) -> list[models.ModelConstraint]:
    return list(
        db.scalars(
            select(models.ModelConstraint)
            .where(models.ModelConstraint.model_id == model_id)
            .order_by(models.ModelConstraint.id)
        )
    )


def model_indexes(db: Session, model_id: int) -> list[models.ModelIndex]:
    return list(
        db.scalars(
            select(models.ModelIndex)
            .where(models.ModelIndex.model_id == model_id)
            .order_by(models.ModelIndex.id)
        )
    )


def build_model_dict(db: Session, m: models.DataModel) -> dict[str, Any]:
    return model_dict(
        m,
        model_fields(db, m.id),
        model_requirement_ids(db, m.id),
        model_relationships(db, m.id),
        model_constraints(db, m.id),
        model_indexes(db, m.id),
    )


def replace_fields(db: Session, model_id: int, fields: list[FieldIn]) -> None:
    # DUPLICATE_FIELD_CODE 前置检查：同一模型内字段编码不重复（大小写不敏感归一化）。
    # 在落库前拦截，避免触发数据库唯一约束返回无信息量的 RESOURCE_CONFLICT，
    # 同时与校验引擎的 DUPLICATE_FIELD_CODE 规则保持同一 issue 结构，前端可统一渲染。
    seen: set[str] = set()
    for f in fields:
        key = f.field_code.lower()
        if key in seen:
            raise ApiError(
                422,
                "MODEL_VALIDATION_FAILED",
                f"字段编码 '{f.field_code}' 在模型内重复",
                {"issues": [{
                    "rule_id": "DUPLICATE_FIELD_CODE",
                    "severity": "error",
                    "object_type": "field",
                    "object_id": f.field_code,
                    "message": f"字段编码 '{f.field_code}' 在模型内重复",
                    "suggestion": "保留一个编码，修改另一个编码",
                    "location": f"field:{f.field_code}",
                }]},
            )
        seen.add(key)

    # V0.7 字段 ID 稳定性（文档 7.3）：按精确 field_code upsert。
    # 编码不变的字段更新原行并保留 id；从列表移除的编码删除；新编码插入新记录。
    # 改名或大小写变化都按"删旧增新"处理（当前无独立 field_uid）。
    existing = {
        row.field_code: row
        for row in db.scalars(
            select(models.ModelField).where(models.ModelField.model_id == model_id)
        )
    }
    wanted_codes = {f.field_code for f in fields}
    for code, row in existing.items():
        if code not in wanted_codes:
            db.delete(row)

    for idx, f in enumerate(fields, start=1):
        row = existing.get(f.field_code)
        if row is None:
            db.add(models.ModelField(
                model_id=model_id,
                ordinal=idx,
                field_code=f.field_code,
                field_name=f.field_name,
                logical_type=f.logical_type,
                length=f.length,
                precision=f.precision,
                scale=f.scale,
                nullable=f.nullable,
                is_primary_key=f.is_primary_key,
                is_unique=f.is_unique,
                default_value=f.default_value,
                description=f.description,
                source_table=f.source_table,
                source_field=f.source_field,
                sensitivity=f.sensitivity,
            ))
        else:
            row.ordinal = idx
            row.field_name = f.field_name
            row.logical_type = f.logical_type
            row.length = f.length
            row.precision = f.precision
            row.scale = f.scale
            row.nullable = f.nullable
            row.is_primary_key = f.is_primary_key
            row.is_unique = f.is_unique
            row.default_value = f.default_value
            row.description = f.description
            row.source_table = f.source_table
            row.source_field = f.source_field
            row.sensitivity = f.sensitivity


def sync_requirement_links(
    db: Session, model: models.DataModel, requirement_ids: list[int], actor: str
) -> None:
    wanted = set(requirement_ids)
    existing = {
        link.requirement_id: link
        for link in db.scalars(
            select(models.RequirementModelLink)
            .where(models.RequirementModelLink.model_id == model.id)
        )
    }
    for rid in wanted - set(existing):
        db.add(models.RequirementModelLink(
            requirement_id=rid, model_id=model.id, created_by=actor,
        ))
    for rid in set(existing) - wanted:
        db.delete(existing[rid])


def check_subject_area(db: Session, model: models.DataModel, area_id: int | None) -> None:
    if area_id is None:
        return
    area = get_subject_area(db, area_id)
    if area.project_id != model.project_id:
        raise conflict(
            "SUBJECT_AREA_PROJECT_MISMATCH",
            f"主题域 {area_id} 不属于项目 {model.project_id}",
        )


def check_requirements(db: Session, model: models.DataModel, requirement_ids: Iterable[int]) -> None:
    for rid in requirement_ids:
        req = get_requirement(db, rid)
        if req.project_id != model.project_id:
            raise conflict(
                "REQUIREMENT_PROJECT_MISMATCH",
                f"需求 {rid} 不属于项目 {model.project_id}",
            )


def _referenced_key_is_unique(
    db: Session, target: models.DataModel, ref_codes: list[str]
) -> bool:
    """外键目标列是否被主键/唯一约束覆盖（字段级或表级）。

    目标引擎执行外键要求引用列具备唯一性；未覆盖时由校验给出警告
    （不阻断：真实引擎的 catalog 校验仍需在目标环境执行）。
    """
    ref_set = set(ref_codes)
    tfields = {f.field_code: f for f in model_fields(db, target.id)}
    if not ref_set or not ref_set <= set(tfields):
        return False
    if all(tfields[c].is_primary_key or tfields[c].is_unique for c in ref_codes):
        return True
    for c in db.scalars(
        select(models.ModelConstraint).where(
            models.ModelConstraint.model_id == target.id,
            models.ModelConstraint.constraint_type.in_(["primary_key", "unique"]),
        )
    ):
        if set(json.loads(c.field_codes_json or "[]")) == ref_set:
            return True
    return False


def _project_fk_context(
    db: Session, project_id: int
) -> tuple[dict[int, str], dict[int, list[frozenset[str]]]]:
    """项目内模型的物理表名映射 + 各模型唯一/PK 列集合。

    供 DDL 预览解析外键 REFERENCES 目标，并对目标键唯一性给出警告
    （数据库执行前置条件；规则器提醒而非替代目标环境 catalog 检查）。
    列集合包含字段级 PK/unique（单列）与表级 PK/unique（组合列）。
    """
    project_models = list(db.scalars(
        select(models.DataModel).where(models.DataModel.project_id == project_id)
    ))
    table_names: dict[int, str] = {}
    unique_keys: dict[int, list[frozenset[str]]] = {}
    for m in project_models:
        table_names[m.id] = m.model_key
        keys: list[frozenset[str]] = []
        for f in model_fields(db, m.id):
            if f.is_primary_key or f.is_unique:
                keys.append(frozenset({f.field_code}))
        for c in db.scalars(
            select(models.ModelConstraint).where(
                models.ModelConstraint.model_id == m.id,
                models.ModelConstraint.constraint_type.in_(["primary_key", "unique"]),
            )
        ):
            keys.append(frozenset(json.loads(c.field_codes_json or "[]")))
        unique_keys[m.id] = keys
    return table_names, unique_keys


def run_validation(db: Session, model: models.DataModel) -> dict[str, Any]:
    fields = model_fields(db, model.id)
    constraints = model_constraints(db, model.id)
    indexes = model_indexes(db, model.id)
    # 外键目标键唯一性/目标字段存在性：跨模型信息在此预计算
    fk_at_risk: set[str] = set()
    for c in constraints:
        if c.constraint_type != "foreign_key":
            continue
        ref_codes = json.loads(c.referenced_field_codes_json or "[]")
        target = (
            db.get(models.DataModel, c.referenced_model_id)
            if c.referenced_model_id
            else None
        )
        if target is None or not _referenced_key_is_unique(db, target, ref_codes):
            fk_at_risk.add(c.constraint_name)
    result = validate_model(
        model,
        fields,
        engine=model.target_engine,
        constraints=constraints,
        indexes=indexes,
        fk_at_risk=fk_at_risk,
    )
    return result.to_dict()


def _constraint_snapshot(c: models.ModelConstraint) -> dict[str, Any]:
    """表级约束的规范化快照（剥离 id/created_by/created_at 运行属性）。"""
    return {
        "constraint_name": c.constraint_name,
        "constraint_type": c.constraint_type,
        "field_codes": json.loads(c.field_codes_json or "[]"),
        "expression": c.expression,
        "referenced_model_id": c.referenced_model_id,
        "referenced_field_codes": json.loads(c.referenced_field_codes_json or "[]"),
        "on_delete": c.on_delete,
        "description": c.description,
    }


def _index_snapshot(i: models.ModelIndex) -> dict[str, Any]:
    """索引的规范化快照（剥离 id/created_by/created_at 运行属性）。"""
    return {
        "index_name": i.index_name,
        "index_type": i.index_type,
        "field_codes": json.loads(i.field_codes_json or "[]"),
        "is_unique": i.is_unique,
        "description": i.description,
    }


def _relationship_snapshot(r: models.ModelRelationship) -> dict[str, Any]:
    """相邻关系的规范化快照（剥离 id/created_by/created_at 运行属性）。"""
    return {
        "source_model_id": r.source_model_id,
        "target_model_id": r.target_model_id,
        "relationship_type": r.relationship_type,
        "source_fields": json.loads(r.source_fields_json or "[]"),
        "target_fields": json.loads(r.target_fields_json or "[]"),
        "cardinality": r.cardinality,
        "description": r.description,
    }


def build_snapshot(db: Session, model: models.DataModel) -> str:
    fields = model_fields(db, model.id)
    snapshot = {
        "model": {
            "model_key": model.model_key,
            "name": model.name,
            "description": model.description,
            "layer": model.layer,
            "model_type": model.model_type,
            "target_engine": model.target_engine,
            "subject_area_id": model.subject_area_id,
        },
        "fields": [
            {
                "field_code": f.field_code,
                "field_name": f.field_name,
                "logical_type": f.logical_type,
                "length": f.length,
                "precision": f.precision,
                "scale": f.scale,
                "nullable": f.nullable,
                "is_primary_key": f.is_primary_key,
                "is_unique": f.is_unique,
                "default_value": f.default_value,
                "description": f.description,
                "source_table": f.source_table,
                "source_field": f.source_field,
                "sensitivity": f.sensitivity,
            }
            for f in fields
        ],
        "constraints": [
            _constraint_snapshot(c) for c in model_constraints(db, model.id)
        ],
        "indexes": [_index_snapshot(i) for i in model_indexes(db, model.id)],
        # 相邻关系（仅与当前模型相关的入边/出边）——支撑直接关系删除后的
        # 历史下游候选影响保留；不构成全项目全版本图
        "relationships": [
            _relationship_snapshot(r) for r in model_relationships(db, model.id)
        ],
        "requirement_ids": model_requirement_ids(db, model.id),
    }
    return json.dumps(snapshot, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------- 指标（进程内）


class Metrics:
    def __init__(self) -> None:
        self.requests_total = 0
        self.by_status_class: dict[str, int] = {"2xx": 0, "4xx": 0, "5xx": 0}
        self.by_route: dict[tuple[str, str, str], int] = {}
        self.duration_sum_ms = 0.0
        self.errors_total = 0
        # V1.0 业务指标族（M6）：进程内计数，重启归零；标签不用
        # project_key/model_key 等高基数字段（_extracted_spec.md:578）。
        self.auth_denied_by_reason: dict[str, int] = {}
        self.model_published_by_layer: dict[str, int] = {}
        self.graph_snapshot_created_by_type: dict[str, int] = {}
        self.lineage_import_runs_by_status: dict[str, int] = {}
        self.lineage_import_records_by_result: dict[str, int] = {}
        self.lineage_import_conflicts_total = 0

    def record(self, method: str, route: str, status: int, duration_ms: float) -> None:
        self.requests_total += 1
        self.duration_sum_ms += duration_ms
        cls = f"{status // 100}xx"
        self.by_status_class[cls] = self.by_status_class.get(cls, 0) + 1
        key = (method, route, cls)
        self.by_route[key] = self.by_route.get(key, 0) + 1
        if status >= 500:
            self.errors_total += 1

    def record_auth_denied(self, reason: str) -> None:
        self.auth_denied_by_reason[reason] = (
            self.auth_denied_by_reason.get(reason, 0) + 1
        )

    def record_model_published(self, layer: str) -> None:
        self.model_published_by_layer[layer] = (
            self.model_published_by_layer.get(layer, 0) + 1
        )

    def record_graph_snapshot_created(self, snapshot_type: str) -> None:
        self.graph_snapshot_created_by_type[snapshot_type] = (
            self.graph_snapshot_created_by_type.get(snapshot_type, 0) + 1
        )

    def record_lineage_import_run(self, status: str) -> None:
        self.lineage_import_runs_by_status[status] = (
            self.lineage_import_runs_by_status.get(status, 0) + 1
        )

    def record_lineage_import_records(
        self, inserted: int, updated: int, deactivated: int
    ) -> None:
        for result, count in (
            ("inserted", inserted), ("updated", updated),
            ("deactivated", deactivated),
        ):
            self.lineage_import_records_by_result[result] = (
                self.lineage_import_records_by_result.get(result, 0) + count
            )

    def record_lineage_import_conflict(self) -> None:
        self.lineage_import_conflicts_total += 1


metrics = Metrics()


def route_template(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if path:
        return path
    # 兜底：把数字段替换为 {id}，避免高基数
    return re.sub(r"/\d+", "/{id}", request.url.path)


# ---------------------------------------------------------------- 应用与生命周期


def _ensure_data_dir() -> None:
    if settings.is_sqlite:
        Path(settings.data_dir).mkdir(parents=True, exist_ok=True)


def _current_revision() -> str | None:
    with engine.connect() as conn:
        if not inspect(conn).has_table("alembic_version"):
            return None
        row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
        return row[0] if row else None


def _has_business_tables() -> bool:
    with engine.connect() as conn:
        return inspect(conn).has_table("projects")


def _run_migrations() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BASE_DIR / "alembic.ini"))
    revision = _current_revision()

    if revision is None and _has_business_tables():
        if not settings.adopt_legacy_schema:
            raise RuntimeError(
                "检测到未版本化的既有业务表（缺少 alembic_version 记录）。"
                "默认拒绝自动接管：请先备份，在副本核对 schema 后，"
                "显式设置 ADOPT_LEGACY_SCHEMA=true 执行一次性接管。"
            )
        command.stamp(cfg, INITIAL_REVISION)

    command.upgrade(cfg, "head")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_logging(settings.log_level)
    settings.validate_startup()
    _ensure_data_dir()

    revision = _current_revision()
    if settings.auto_migrate:
        _run_migrations()
    else:
        if revision != HEAD_REVISION:
            raise RuntimeError(
                f"AUTO_MIGRATE=false 且数据库 revision={revision} 不是 head={HEAD_REVISION}，"
                "请先执行 alembic upgrade head。"
            )

    if settings.seed_demo_data:
        from .db import SessionLocal

        with SessionLocal() as db:
            seed_demo_data(db)

    # V1.0 demo 模式幂等播种演示管理员与项目 owner 成员（设计文档 3.4）；
    # 与 SEED_DEMO_DATA 解耦，仅影响 users / project_members。
    # V1.3 同批播种演示账号矩阵（平台管理员 + owner/editor/viewer 梯度），
    # 供密码登录与 RBAC 差异演示；已设置口令的账号重启不重置。
    if settings.auth_mode == "demo":
        from .db import SessionLocal

        with SessionLocal() as db:
            auth_mod.ensure_demo_admin(db)
            auth_mod.ensure_demo_accounts(db)
            db.commit()

    # V0.9 升级基线：旧库已有发布模型但无图谱快照时，幂等创建一次
    # migration_baseline（不伪造历史发布轨迹）。
    from .db import SessionLocal

    with SessionLocal() as db:
        _ensure_migration_baselines(db)

    yield


app = FastAPI(
    title="Data Modeling Center",
    description="数据建模中心：业务调研 → 模型设计 → 规范校验 → 评审发布 → "
                "版本差异 → DDL 预览纵向闭环 + 图谱快照与外部血缘治理。",
    version=APP_VERSION,
    lifespan=lifespan,
)
app.state.metrics = metrics


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):  # noqa: ANN001
    request_id = resolve_request_id(request)
    request.state.request_id = request_id
    request.state.actor = resolve_actor(request)
    started = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - started) * 1000
    response.headers["X-Request-ID"] = request_id
    metrics.record(
        request.method, route_template(request), response.status_code, duration_ms
    )
    return response


@app.middleware("http")
async def auth_middleware(request: Request, call_next):  # noqa: ANN001
    """V1.0 认证中间件；V1.3 起 demo 模式渐进增强。

    豁免：健康检查、bootstrap 引导、密码登录与非 API 路径（静态前端）；
    /metrics 需要认证（admin 校验在端点内）。认证失败返回统一错误
    信封，并写审计 auth.denied + 进程内计数。

    demo 模式（V1.3）：仅当请求携带 Authorization 头时才解析身份——
    无令牌匿名放行（V0.9.2 兼容，E2E/既有测试不受影响）；携带令牌但
    无效/过期/吊销照常 401（不静默回落演示管理员：否则被吊销的登录
    会话会被放大为管理员权限，与 RBAC 设计意图相悖）。
    """
    path = request.url.path
    if path in auth_mod._AUTH_EXEMPT_PATHS:
        return await call_next(request)
    if not path.startswith("/api/") and path != "/metrics":
        return await call_next(request)  # 静态前端等
    if settings.auth_mode == "demo" and not request.headers.get("authorization"):
        return await call_next(request)  # 匿名演示：V0.9.2 兼容

    result = auth_mod.authenticate(request)
    if isinstance(result, ApiError):
        auth_mod.record_auth_denied(request, result.code, result.details)
        return JSONResponse(
            status_code=result.status_code,
            content={
                "error": {
                    "code": result.code,
                    "message": result.message,
                    "details": result.details,
                },
                "request_id": getattr(request.state, "request_id", ""),
            },
        )
    request.state.identity = result
    request.state.actor = result.actor
    return await call_next(request)


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):  # noqa: ANN001
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            },
            "request_id": getattr(request.state, "request_id", ""),
        },
    )


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(request: Request, exc: RequestValidationError):  # noqa: ANN001
    """请求体/查询参数校验失败：统一错误 envelope（契约 422 REQUEST_VALIDATION_FAILED）。

    FastAPI 请求校验抛的是 RequestValidationError（非 pydantic ValidationError），
    必须显式注册，否则会回落到默认 {"detail": [...]} 格式，破坏错误体约定。
    """
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "REQUEST_VALIDATION_FAILED",
                "message": "请求参数校验失败",
                "details": {"issues": jsonable_encoder(exc.errors())},
            },
            "request_id": getattr(request.state, "request_id", ""),
        },
    )


@app.exception_handler(ValidationError)
async def validation_error_handler(request: Request, exc: ValidationError):  # noqa: ANN001
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "REQUEST_VALIDATION_FAILED",
                "message": "请求参数校验失败",
                "details": {"issues": json.loads(exc.json())},
            },
            "request_id": getattr(request.state, "request_id", ""),
        },
    )


@app.exception_handler(IntegrityError)
async def integrity_error_handler(request: Request, exc: IntegrityError):  # noqa: ANN001
    return JSONResponse(
        status_code=409,
        content={
            "error": {
                "code": "RESOURCE_CONFLICT",
                "message": "资源冲突（唯一约束）",
                "details": {},
            },
            "request_id": getattr(request.state, "request_id", ""),
        },
    )


# ---------------------------------------------------------------- 健康检查


@app.get("/api/health/live")
def health_live() -> dict[str, Any]:
    """进程存活：不依赖数据库。"""
    return {"status": "ok", "service": "data-modeling-center", "version": APP_VERSION}


@app.get("/api/health/ready")
def health_ready() -> dict[str, Any]:
    """就绪：数据库可连接且 Alembic revision 与 head 一致。"""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        raise ApiError(503, "DEPENDENCY_UNAVAILABLE", f"数据库不可用：{exc}")
    revision = _current_revision()
    if revision != HEAD_REVISION:
        raise ApiError(
            503,
            "SCHEMA_NOT_READY",
            f"数据库 revision={revision}，期望 head={HEAD_REVISION}",
        )
    return {
        "status": "ready",
        "database": "ok",
        "revision": revision,
        "head": HEAD_REVISION,
    }


@app.get("/api/health")
def health_compat() -> dict[str, Any]:
    """兼容端点：等价于 ready。"""
    return health_ready()


# ---------------------------------------------------------------- 项目


@app.get("/api/projects")
def list_projects(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    projects = db.scalars(
        select(models.Project).order_by(models.Project.id)
    )
    return [project_dict(p) for p in projects]


@app.post("/api/projects", status_code=201)
def create_project(
    payload: ProjectCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    actor = request.state.actor
    request_id = request.state.request_id
    existing = db.scalar(
        select(models.Project)
        .with_only_columns(models.Project.id)
        .where(models.Project.project_key == payload.project_key)
    )
    if existing is not None:
        raise conflict("PROJECT_KEY_EXISTS", f"项目编码 {payload.project_key} 已存在")
    project = models.Project(
        project_key=payload.project_key,
        name=payload.name,
        description=payload.description,
    )
    db.add(project)
    db.flush()
    # V1.0：创建者自动成为项目 owner（否则 editor 建项目后自己 403，
    # 授权模型不可用；设计文档 3.3 的实施补充）。demo 模式为演示管理员。
    if settings.auth_mode == "demo":
        creator = auth_mod.ensure_demo_admin(db)
    else:
        identity = auth_mod.require_authenticated(request)
        creator = db.get(models.User, identity.user_id)
    if creator is not None:
        auth_mod.upsert_project_member(
            db, project.id, creator, "owner", actor
        )
    record_event(db, project.id, "project.created", actor,
                 {"project_key": project.project_key}, request_id)
    record_audit(db, "project", str(project.id), "project.created", actor,
                 "success", request_id)
    db.commit()
    return project_dict(project)


# ---------------------------------------------------------------- 主题域


@app.get("/api/projects/{project_id}/subject-areas",
         dependencies=[Depends(auth_mod.require_project_role("viewer"))])
def list_subject_areas(
    project_id: int, db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    get_project(db, project_id)
    areas = db.scalars(
        select(models.SubjectArea)
        .where(models.SubjectArea.project_id == project_id)
        .order_by(models.SubjectArea.id)
    )
    return [subject_area_dict(a) for a in areas]


@app.post("/api/projects/{project_id}/subject-areas", status_code=201,
          dependencies=[Depends(auth_mod.require_project_role("editor"))])
def create_subject_area(
    project_id: int,
    payload: SubjectAreaCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    actor = request.state.actor
    request_id = request.state.request_id
    get_project(db, project_id)
    existing = db.scalar(
        select(models.SubjectArea)
        .with_only_columns(models.SubjectArea.id)
        .where(
            models.SubjectArea.project_id == project_id,
            models.SubjectArea.area_key == payload.area_key,
        )
    )
    if existing is not None:
        raise conflict(
            "SUBJECT_AREA_KEY_EXISTS",
            f"主题域编码 {payload.area_key} 在项目内已存在",
        )
    area = models.SubjectArea(
        project_id=project_id,
        area_key=payload.area_key,
        name=payload.name,
        description=payload.description,
        owner=payload.owner,
    )
    db.add(area)
    db.flush()
    record_event(db, project_id, "subject_area.created", actor,
                 {"area_key": area.area_key}, request_id)
    record_audit(db, "subject_area", str(area.id), "subject_area.created",
                 actor, "success", request_id)
    db.commit()
    return subject_area_dict(area)


# ---------------------------------------------------------------- 业务总线矩阵
# Kimball 总线矩阵语义：行 = 业务过程（DWD/DWS 模型），列 = 维度（DIM 模型），
# 单元格 = 过程与维度之间的直接关系。只统计显式建模的直连边——ADS 经 DWD 间接
# 使用维度不体现在矩阵中，这正是「总线」作为声明式一致性契约的意义：一致性
# 维度必须在过程上显式声明，才能被矩阵识别与治理。


def _bus_matrix_dimension(m: models.DataModel) -> dict[str, Any]:
    return {
        "id": m.id,
        "model_key": m.model_key,
        "name": m.name,
        "layer": m.layer,
        "subject_area_id": m.subject_area_id,
    }


@app.get("/api/projects/{project_id}/bus-matrix",
         dependencies=[Depends(auth_mod.require_project_role("viewer"))])
def get_bus_matrix(
    project_id: int,
    subject_area_id: int | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    get_project(db, project_id)
    areas = db.scalars(
        select(models.SubjectArea)
        .where(models.SubjectArea.project_id == project_id)
        .order_by(models.SubjectArea.id)
    ).all()
    dimensions = db.scalars(
        select(models.DataModel)
        .where(
            models.DataModel.project_id == project_id,
            models.DataModel.layer == "DIM",
        )
        .order_by(models.DataModel.model_key)
    ).all()
    processes = db.scalars(
        select(models.DataModel)
        .where(
            models.DataModel.project_id == project_id,
            models.DataModel.layer.in_(("DWD", "DWS")),
        )
        .order_by(models.DataModel.model_key)
    ).all()
    rels = db.scalars(
        select(models.ModelRelationship)
        .where(models.ModelRelationship.project_id == project_id)
    ).all()

    dim_by_id = {d.id: d for d in dimensions}
    proc_by_id = {p.id: p for p in processes}

    # 过程 ↔ 维度直连边索引：{process_id: {dimension_id: [edge, ...]}}
    edges: dict[int, dict[int, list[dict[str, Any]]]] = {}
    for r in rels:
        if r.source_model_id in proc_by_id and r.target_model_id in dim_by_id:
            pid, did = r.source_model_id, r.target_model_id
            direction = "process_to_dimension"
        elif r.source_model_id in dim_by_id and r.target_model_id in proc_by_id:
            pid, did = r.target_model_id, r.source_model_id
            direction = "dimension_to_process"
        else:
            continue
        edges.setdefault(pid, {}).setdefault(did, []).append({
            "relationship_id": r.id,
            "relationship_type": r.relationship_type,
            "cardinality": r.cardinality,
            "direction": direction,
            "description": r.description,
            "source_fields": json.loads(r.source_fields_json or "[]"),
            "target_fields": json.loads(r.target_fields_json or "[]"),
        })

    if subject_area_id is not None:
        area = db.get(models.SubjectArea, subject_area_id)
        if area is None or area.project_id != project_id:
            raise not_found(
                "SUBJECT_AREA_NOT_FOUND", f"主题域 {subject_area_id} 不存在"
            )
        area_proc_ids = {p.id for p in processes if p.subject_area_id == area.id}
        cells: list[dict[str, Any]] = []
        for p in processes:
            if p.id not in area_proc_ids:
                continue
            for d in dimensions:
                edge_list = edges.get(p.id, {}).get(d.id)
                if edge_list:
                    cells.append({
                        "process_id": p.id,
                        "dimension_id": d.id,
                        "edge_count": len(edge_list),
                        "relationship_types": sorted(
                            {e["relationship_type"] for e in edge_list}
                        ),
                        "field_pairs": edge_list,
                    })
        dim_reuse = []
        for d in dimensions:
            pids = [pid for pid, dd in edges.items() if d.id in dd]
            used_areas = {proc_by_id[pid].subject_area_id for pid in pids}
            dim_reuse.append({
                "id": d.id,
                "model_key": d.model_key,
                "name": d.name,
                "in_area_process_count": sum(1 for pid in pids if pid in area_proc_ids),
                "out_of_area_process_count": sum(
                    1 for pid in pids if pid not in area_proc_ids
                ),
                "total_process_count": len(pids),
                "area_count": len(used_areas),
                "is_shared": len(used_areas) > 1,
            })
        cross_area_relations = sum(
            1
            for c in cells
            if dim_by_id[c["dimension_id"]].subject_area_id != area.id
            for _ in c["field_pairs"]
        )
        return {
            "mode": "area",
            "subject_area": {
                "id": area.id,
                "area_key": area.area_key,
                "name": area.name,
                "owner": area.owner,
            },
            "processes": [
                {
                    "id": p.id,
                    "model_key": p.model_key,
                    "name": p.name,
                    "layer": p.layer,
                    "status": p.status,
                    "dimension_count": len(edges.get(p.id, {})),
                }
                for p in processes
                if p.id in area_proc_ids
            ],
            "dimensions": [_bus_matrix_dimension(d) for d in dimensions],
            "cells": cells,
            "dimension_reuse": dim_reuse,
            "summary": {
                "process_total": len(area_proc_ids),
                "dimension_total": len(dimensions),
                "mapped_cells": len(cells),
                "relation_total": sum(c["edge_count"] for c in cells),
                "unmapped_processes": [
                    {
                        "id": p.id,
                        "model_key": p.model_key,
                        "name": p.name,
                        "layer": p.layer,
                    }
                    for p in processes
                    if p.id in area_proc_ids and not edges.get(p.id)
                ],
                "cross_area_relations": cross_area_relations,
                "consistency_dimensions": sum(
                    1 for d in dim_reuse if d["is_shared"]
                ),
                "orphan_dimensions": sum(
                    1 for d in dim_reuse if d["total_process_count"] == 0
                ),
            },
        }

    # 企业级总览：主题域 × 维度热力图（含未归属过程行）
    matrix: list[dict[str, Any]] = []
    for a in areas:
        row_pids = [p.id for p in processes if p.subject_area_id == a.id]
        matrix.append({
            "subject_area": {
                "id": a.id,
                "area_key": a.area_key,
                "name": a.name,
                "owner": a.owner,
            },
            "unassigned": False,
            "process_count": len(row_pids),
            "counts": [
                sum(1 for pid in row_pids if d.id in edges.get(pid, {}))
                for d in dimensions
            ],
        })
    unassigned_pids = [p.id for p in processes if p.subject_area_id is None]
    if unassigned_pids:
        matrix.append({
            "subject_area": None,
            "unassigned": True,
            "process_count": len(unassigned_pids),
            "counts": [
                sum(1 for pid in unassigned_pids if d.id in edges.get(pid, {}))
                for d in dimensions
            ],
        })
    shared_dimensions = []
    orphan_dimensions = []
    for d in dimensions:
        pids = [pid for pid, dd in edges.items() if d.id in dd]
        used_areas = {proc_by_id[pid].subject_area_id for pid in pids}
        if not pids:
            orphan_dimensions.append(
                {"id": d.id, "model_key": d.model_key, "name": d.name}
            )
        elif len(used_areas) > 1:
            shared_dimensions.append({
                "id": d.id,
                "model_key": d.model_key,
                "name": d.name,
                "area_count": len(used_areas),
                "process_count": len(pids),
            })
    shared_dimensions.sort(
        key=lambda x: (-x["area_count"], -x["process_count"], x["model_key"])
    )
    return {
        "mode": "overview",
        "dimensions": [_bus_matrix_dimension(d) for d in dimensions],
        "matrix": matrix,
        "summary": {
            "subject_area_total": len(areas),
            "process_total": len(processes),
            "dimension_total": len(dimensions),
            "mapped_cells": sum(len(dd) for dd in edges.values()),
            "relation_total": sum(
                len(es) for dd in edges.values() for es in dd.values()
            ),
            "shared_dimensions": shared_dimensions,
            "orphan_dimensions": orphan_dimensions,
            "unmapped_processes": [
                {
                    "id": p.id,
                    "model_key": p.model_key,
                    "name": p.name,
                    "layer": p.layer,
                    "subject_area_id": p.subject_area_id,
                }
                for p in processes
                if not edges.get(p.id)
            ],
        },
    }


# ---------------------------------------------------------------- 业务需求


@app.get("/api/projects/{project_id}/requirements",
         dependencies=[Depends(auth_mod.require_project_role("viewer"))])
def list_requirements(
    project_id: int, db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    get_project(db, project_id)
    reqs = db.scalars(
        select(models.BusinessRequirement)
        .where(models.BusinessRequirement.project_id == project_id)
        .order_by(models.BusinessRequirement.id)
    )
    counts = dict(
        db.execute(
            text(
                "SELECT requirement_id, COUNT(*) FROM requirement_model_links "
                "GROUP BY requirement_id"
            )
        ).all()
    )
    return [requirement_dict(r, counts.get(r.id, 0)) for r in reqs]


@app.post("/api/projects/{project_id}/requirements", status_code=201,
          dependencies=[Depends(auth_mod.require_project_role("editor"))])
def create_requirement(
    project_id: int,
    payload: BusinessRequirementCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    actor = request.state.actor
    request_id = request.state.request_id
    get_project(db, project_id)
    existing = db.scalar(
        select(models.BusinessRequirement)
        .with_only_columns(models.BusinessRequirement.id)
        .where(
            models.BusinessRequirement.project_id == project_id,
            models.BusinessRequirement.requirement_key == payload.requirement_key,
        )
    )
    if existing is not None:
        raise conflict(
            "REQUIREMENT_KEY_EXISTS",
            f"需求编码 {payload.requirement_key} 在项目内已存在",
        )
    req = models.BusinessRequirement(
        project_id=project_id,
        requirement_key=payload.requirement_key,
        title=payload.title,
        business_domain=payload.business_domain,
        description=payload.description,
        acceptance_criteria=payload.acceptance_criteria,
        priority=payload.priority,
        status=payload.status,
        owner=payload.owner,
        created_by=actor,
    )
    db.add(req)
    db.flush()
    record_event(db, project_id, "requirement.created", actor,
                 {"requirement_key": req.requirement_key}, request_id)
    record_audit(db, "requirement", str(req.id), "requirement.created",
                 actor, "success", request_id)
    db.commit()
    return requirement_dict(req, 0)


@app.get("/api/requirements/{requirement_id}",
         dependencies=[Depends(auth_mod.require_requirement_role("viewer"))])
def get_requirement_detail(
    requirement_id: int, db: Session = Depends(get_db)
) -> dict[str, Any]:
    req = get_requirement(db, requirement_id)
    count = db.scalar(
        text("SELECT COUNT(*) FROM requirement_model_links WHERE requirement_id = :rid"),
        {"rid": requirement_id},
    )
    return requirement_dict(req, count or 0)


@app.put("/api/requirements/{requirement_id}",
         dependencies=[Depends(auth_mod.require_requirement_role("editor"))])
def update_requirement(
    requirement_id: int,
    payload: BusinessRequirementUpdate,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    actor = request.state.actor
    request_id = request.state.request_id
    req = get_requirement(db, requirement_id)
    dup = db.scalar(
        select(models.BusinessRequirement)
        .with_only_columns(models.BusinessRequirement.id)
        .where(
            models.BusinessRequirement.project_id == req.project_id,
            models.BusinessRequirement.requirement_key == payload.requirement_key,
            models.BusinessRequirement.id != req.id,
        )
    )
    if dup is not None:
        raise conflict(
            "REQUIREMENT_KEY_EXISTS",
            f"需求编码 {payload.requirement_key} 在项目内已存在",
        )
    req.requirement_key = payload.requirement_key
    req.title = payload.title
    req.business_domain = payload.business_domain
    req.description = payload.description
    req.acceptance_criteria = payload.acceptance_criteria
    req.priority = payload.priority
    req.status = payload.status
    req.owner = payload.owner
    db.flush()
    record_event(db, req.project_id, "requirement.updated", actor,
                 {"requirement_key": req.requirement_key}, request_id)
    record_audit(db, "requirement", str(req.id), "requirement.updated",
                 actor, "success", request_id)
    db.commit()
    count = db.scalar(
        text("SELECT COUNT(*) FROM requirement_model_links WHERE requirement_id = :rid"),
        {"rid": requirement_id},
    )
    return requirement_dict(req, count or 0)


@app.get("/api/requirements/{requirement_id}/models",
         dependencies=[Depends(auth_mod.require_requirement_role("viewer"))])
def requirement_models(
    requirement_id: int, db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    get_requirement(db, requirement_id)
    model_ids = db.scalars(
        select(models.RequirementModelLink)
        .with_only_columns(models.RequirementModelLink.model_id)
        .where(models.RequirementModelLink.requirement_id == requirement_id)
    )
    result = []
    for mid in model_ids:
        m = db.get(models.DataModel, mid)
        if m is not None:
            result.append(build_model_dict(db, m))
    return result


@app.post("/api/requirements/{requirement_id}/models/{model_id}", status_code=201,
          dependencies=[Depends(auth_mod.require_requirement_role("editor"))])
def link_requirement_model(
    requirement_id: int,
    model_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    actor = request.state.actor
    request_id = request.state.request_id
    req = get_requirement(db, requirement_id)
    model = get_model(db, model_id)
    if req.project_id != model.project_id:
        raise conflict(
            "REQUIREMENT_PROJECT_MISMATCH",
            f"需求 {requirement_id} 与模型 {model_id} 不属于同一项目",
        )
    existing = db.scalar(
        select(models.RequirementModelLink)
        .with_only_columns(models.RequirementModelLink.id)
        .where(
            models.RequirementModelLink.requirement_id == requirement_id,
            models.RequirementModelLink.model_id == model_id,
        )
    )
    if existing is not None:
        return {"status": "exists", "requirement_id": requirement_id,
                "model_id": model_id}
    db.add(models.RequirementModelLink(
        requirement_id=requirement_id, model_id=model_id, created_by=actor,
    ))
    db.flush()
    record_event(db, req.project_id, "requirement.model.linked", actor,
                 {"requirement_id": requirement_id, "model_id": model_id},
                 request_id)
    record_audit(db, "requirement_model_link", f"{requirement_id}:{model_id}",
                 "link", actor, "success", request_id)
    db.commit()
    return {"status": "linked", "requirement_id": requirement_id,
            "model_id": model_id}


@app.delete("/api/requirements/{requirement_id}/models/{model_id}",
            dependencies=[Depends(auth_mod.require_requirement_role("editor"))])
def unlink_requirement_model(
    requirement_id: int,
    model_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    actor = request.state.actor
    request_id = request.state.request_id
    req = get_requirement(db, requirement_id)
    get_model(db, model_id)
    link = db.scalar(
        select(models.RequirementModelLink)
        .where(
            models.RequirementModelLink.requirement_id == requirement_id,
            models.RequirementModelLink.model_id == model_id,
        )
    )
    if link is None:
        return {"status": "not_linked", "requirement_id": requirement_id,
                "model_id": model_id}
    db.delete(link)
    db.flush()
    record_event(db, req.project_id, "requirement.model.unlinked", actor,
                 {"requirement_id": requirement_id, "model_id": model_id},
                 request_id)
    record_audit(db, "requirement_model_link", f"{requirement_id}:{model_id}",
                 "unlink", actor, "success", request_id)
    db.commit()
    return {"status": "unlinked", "requirement_id": requirement_id,
            "model_id": model_id}


# ---------------------------------------------------------------- 模型


@app.get("/api/projects/{project_id}/models",
         dependencies=[Depends(auth_mod.require_project_role("viewer"))])
def list_models(
    project_id: int, db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    get_project(db, project_id)
    ms = db.scalars(
        select(models.DataModel)
        .where(models.DataModel.project_id == project_id)
        .order_by(models.DataModel.id)
    )
    return [build_model_dict(db, m) for m in ms]


@app.post("/api/projects/{project_id}/models", status_code=201,
          dependencies=[Depends(auth_mod.require_project_role("editor"))])
def create_model(
    project_id: int,
    payload: ModelCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    actor = request.state.actor
    request_id = request.state.request_id
    get_project(db, project_id)
    existing = db.scalar(
        select(models.DataModel)
        .with_only_columns(models.DataModel.id)
        .where(
            models.DataModel.project_id == project_id,
            models.DataModel.model_key == payload.model_key,
        )
    )
    if existing is not None:
        raise conflict("MODEL_KEY_EXISTS",
                       f"模型编码 {payload.model_key} 在项目内已存在")
    model = models.DataModel(
        project_id=project_id,
        model_key=payload.model_key,
        name=payload.name,
        description=payload.description,
        layer=payload.layer,
        model_type=payload.model_type,
        target_engine=payload.target_engine,
        subject_area_id=payload.subject_area_id,
        status="draft",
        edit_revision=1,
        created_by=actor,
    )
    db.add(model)
    db.flush()
    check_subject_area(db, model, payload.subject_area_id)
    check_requirements(db, model, payload.requirement_ids)
    replace_fields(db, model.id, payload.fields)
    sync_requirement_links(db, model, payload.requirement_ids, actor)
    db.flush()
    record_event(db, project_id, "model.draft.created", actor,
                 {"model_key": model.model_key, "model_id": model.id}, request_id)
    record_audit(db, "model", str(model.id), "model.created", actor,
                 "success", request_id)
    db.commit()
    return build_model_dict(db, model)


@app.get("/api/models/{model_id}",
         dependencies=[Depends(auth_mod.require_model_role("viewer"))])
def get_model_detail(model_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    model = get_model(db, model_id)
    return build_model_dict(db, model)


@app.put("/api/models/{model_id}",
         dependencies=[Depends(auth_mod.require_model_role("editor"))])
def update_model(
    model_id: int,
    payload: ModelUpdate,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    actor = request.state.actor
    request_id = request.state.request_id
    model = get_model(db, model_id)
    if model.status != "draft":
        raise conflict(
            "MODEL_NOT_EDITABLE",
            f"模型当前状态为 {model.status}，只有草稿可编辑；"
            "已发布模型请使用发起变更（start-change）",
            {"status": model.status},
        )
    if payload.expected_revision != model.edit_revision:
        raise conflict(
            "REVISION_CONFLICT",
            "模型已被其他操作更新，请刷新后比较变更",
            {
                "expected_revision": payload.expected_revision,
                "current_revision": model.edit_revision,
            },
        )
    dup = db.scalar(
        select(models.DataModel)
        .with_only_columns(models.DataModel.id)
        .where(
            models.DataModel.project_id == model.project_id,
            models.DataModel.model_key == payload.model_key,
            models.DataModel.id != model.id,
        )
    )
    if dup is not None:
        raise conflict("MODEL_KEY_EXISTS",
                       f"模型编码 {payload.model_key} 在项目内已存在")
    check_subject_area(db, model, payload.subject_area_id)
    check_requirements(db, model, payload.requirement_ids)

    model.model_key = payload.model_key
    model.name = payload.name
    model.description = payload.description
    model.layer = payload.layer
    model.model_type = payload.model_type
    model.target_engine = payload.target_engine
    model.subject_area_id = payload.subject_area_id
    replace_fields(db, model.id, payload.fields)
    sync_requirement_links(db, model, payload.requirement_ids, actor)
    model.edit_revision += 1
    db.flush()
    record_event(db, model.project_id, "model.draft.saved", actor,
                 {"model_id": model.id, "edit_revision": model.edit_revision,
                  "field_count": len(payload.fields)}, request_id)
    record_audit(db, "model", str(model.id), "model.updated", actor,
                 "success", request_id)
    db.commit()
    return build_model_dict(db, model)


# ---------------------------------------------------------------- 校验


@app.post("/api/models/{model_id}/validate",
          dependencies=[Depends(auth_mod.require_model_role("editor"))])
def validate_model_api(
    model_id: int, request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    actor = request.state.actor
    request_id = request.state.request_id
    model = get_model(db, model_id)
    result = run_validation(db, model)
    record_event(db, model.project_id, "model.validation.completed", actor,
                 {"model_id": model.id, "status": result["status"],
                  "summary": result["summary"]}, request_id)
    record_audit(db, "model", str(model.id), "model.validated", actor,
                 result["status"], request_id)
    db.commit()
    return result


# ---------------------------------------------------------------- 评审发布闭环


def _lineage_coverage(
    db: Session, model: models.DataModel
) -> dict[str, Any]:
    """设计出向边 vs active 外部边覆盖（M4，设计文档 6.3）。

    对模型 M 的每条出向设计关系，若不存在 active 外部边
    （source_model_id == M.id 且 target_model_id == 关系目标），该设计边
    计入 uncovered——"佐证缺失"提示，不阻断发布。
    """
    out_rels = list(db.scalars(
        select(models.ModelRelationship)
        .where(models.ModelRelationship.source_model_id == model.id)
    ))
    active_external = list(db.scalars(
        select(models.ExternalLineageEdge)
        .where(
            models.ExternalLineageEdge.project_id == model.project_id,
            models.ExternalLineageEdge.active.is_(True),
        )
    ))
    covered_pairs = {
        (e.source_model_id, e.target_model_id) for e in active_external
    }
    uncovered: list[dict[str, Any]] = []
    for r in out_rels:
        if (model.id, r.target_model_id) in covered_pairs:
            continue
        uncovered.append({
            "edge_key": (
                f"design:{model.id}:{r.target_model_id}:"
                f"{r.relationship_type}"
            ),
            "target_model_id": r.target_model_id,
            "relationship_type": r.relationship_type,
        })
    inbound = sum(
        1 for e in active_external if e.target_model_id == model.id
    )
    return {
        "out_design_edges": len(out_rels),
        "covered_by_external": len(out_rels) - len(uncovered),
        "uncovered": uncovered,
        "external_inbound_edges": inbound,
    }


def _graph_diff_summary(
    db: Session, project_id: int
) -> dict[str, Any]:
    """项目最近两次图谱快照的差异汇总（M4）；不足两次不可用。"""
    snapshots = list(db.scalars(
        select(models.ProjectGraphSnapshot)
        .where(models.ProjectGraphSnapshot.project_id == project_id)
        .order_by(models.ProjectGraphSnapshot.snapshot_no.desc())
        .limit(2)
    ))
    if len(snapshots) < 2:
        return {"available": False, "reason": "INSUFFICIENT_SNAPSHOTS"}
    latest, previous = snapshots[0], snapshots[1]
    diff = graph_snapshot_diff(
        project_id, previous.snapshot_no, latest.snapshot_no, db
    )
    return {
        "available": True,
        "from_snapshot_no": previous.snapshot_no,
        "to_snapshot_no": latest.snapshot_no,
        "summary": {
            "nodes_added": len(diff["nodes"]["added"]),
            "nodes_removed": len(diff["nodes"]["removed"]),
            "nodes_modified": len(diff["nodes"]["modified"]),
            "edges_added": len(diff["edges"]["added"]),
            "edges_removed": len(diff["edges"]["removed"]),
            "edges_modified": len(diff["edges"]["modified"]),
        },
    }


def _build_review_context(
    db: Session,
    model: models.DataModel,
    validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """发布评审上下文（M4，只读不落库）：变更、影响、图谱 diff、血缘覆盖。"""
    if validation is None:
        validation = run_validation(db, model)
    impact = _build_impact_analysis(db, model)
    coverage = _lineage_coverage(db, model)
    return {
        "model_id": model.id,
        "model_key": model.model_key,
        "status": model.status,
        "edit_revision": model.edit_revision,
        "generated_at": iso(models.utcnow()),
        "validation": {
            "status": validation["status"],
            "summary": validation["summary"],
        },
        "analysis_mode": impact["analysis_mode"],
        "change_summary": impact["change_summary"],
        "impact": {
            "impacted_count": len(impact["impacted_models"]),
            "truncated": impact["truncated"],
            "top_impacted": impact["impacted_models"][:20],
        },
        "graph_diff": _graph_diff_summary(db, model.project_id),
        "lineage_coverage": coverage,
        "direction_semantics": IMPACT_DIRECTION_SEMANTICS,
    }


def _review_context_summary(
    db: Session,
    model: models.DataModel,
    validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """submit-review 随附的评审上下文摘要（M4，评审证据留痕）。"""
    context = _build_review_context(db, model, validation)
    return {
        "impacted_count": context["impact"]["impacted_count"],
        "uncovered_design_edges": len(
            context["lineage_coverage"]["uncovered"]
        ),
        "graph_diff_available": context["graph_diff"]["available"],
        "validation_warnings": context["validation"]["summary"]["warnings"],
    }


@app.post("/api/models/{model_id}/submit-review",
          dependencies=[Depends(auth_mod.require_model_role("editor"))])
def submit_review(
    model_id: int, request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    actor = request.state.actor
    request_id = request.state.request_id
    model = get_model(db, model_id)
    if model.status != "draft":
        raise invalid_state(
            f"只有草稿可提交评审，当前状态为 {model.status}",
            {"status": model.status},
        )
    result = run_validation(db, model)
    if result["summary"]["errors"]:
        raise ApiError(
            422,
            "MODEL_VALIDATION_FAILED",
            "存在阻断级校验错误，不能提交评审",
            {"issues": result["issues"], "summary": result["summary"]},
        )
    model.status = "in_review"
    db.flush()
    # V1.0 M4：提交评审随附影响分析摘要（阻断行为不变，附加字段）
    review_context = _review_context_summary(db, model, result)
    record_event(db, model.project_id, "review.submitted", actor,
                 {"model_id": model.id, "edit_revision": model.edit_revision,
                  **review_context},
                 request_id)
    record_audit(db, "model", str(model.id), "review.submitted", actor,
                 "success", request_id)
    db.commit()
    return {"model_id": model.id, "status": model.status,
            "validation": result["summary"],
            "review_context": review_context}


@app.get("/api/models/{model_id}/review-context",
         dependencies=[Depends(auth_mod.require_model_role("viewer"))])
def model_review_context(
    model_id: int, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """发布评审上下文（M4，只读不落库）：相对已发布版本改了什么、可能
    影响哪些下游、项目图谱相对上次发布差多少、哪些设计关系还没有外部
    血缘佐证。提交评审（submit-review）随附同源摘要。
    """
    model = get_model(db, model_id)
    return _build_review_context(db, model)


@app.post("/api/models/{model_id}/approve",
          dependencies=[Depends(auth_mod.require_model_role("editor"))])
def approve_model(
    model_id: int,
    payload: ReviewDecision,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    actor = request.state.actor
    request_id = request.state.request_id
    model = get_model(db, model_id)
    if model.status != "in_review":
        raise invalid_state(
            f"只有待评审模型可执行通过，当前状态为 {model.status}",
            {"status": model.status},
        )
    model.status = "approved"
    db.flush()
    record_event(db, model.project_id, "review.decided", actor,
                 {"model_id": model.id, "decision": "approved",
                  "comment": payload.comment}, request_id)
    record_audit(db, "model", str(model.id), "review.approved", actor,
                 "success", request_id)
    db.commit()
    return {"model_id": model.id, "status": model.status,
            "decision": "approved", "comment": payload.comment}


@app.post("/api/models/{model_id}/reject",
          dependencies=[Depends(auth_mod.require_model_role("editor"))])
def reject_model(
    model_id: int,
    payload: ReviewDecision,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    actor = request.state.actor
    request_id = request.state.request_id
    model = get_model(db, model_id)
    if model.status != "in_review":
        raise invalid_state(
            f"只有待评审模型可执行退回，当前状态为 {model.status}",
            {"status": model.status},
        )
    if not payload.comment.strip():
        raise ApiError(400, "VALIDATION_ERROR", "退回必须填写意见或整改说明")
    model.status = "draft"
    db.flush()
    record_event(db, model.project_id, "review.decided", actor,
                 {"model_id": model.id, "decision": "rejected",
                  "comment": payload.comment}, request_id)
    record_audit(db, "model", str(model.id), "review.rejected", actor,
                 "success", request_id)
    db.commit()
    return {"model_id": model.id, "status": model.status,
            "decision": "rejected", "comment": payload.comment}


@app.post("/api/models/{model_id}/publish",
          dependencies=[Depends(auth_mod.require_model_role("editor"))])
def publish_model(
    model_id: int,
    payload: ReviewDecision,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    actor = request.state.actor
    request_id = request.state.request_id
    model = get_model(db, model_id)
    if model.status != "approved":
        raise invalid_state(
            f"只有评审通过的模型可发布，当前状态为 {model.status}",
            {"status": model.status},
        )
    result = run_validation(db, model)
    if result["summary"]["errors"]:
        raise ApiError(
            422,
            "MODEL_VALIDATION_FAILED",
            "发布前复检存在阻断级校验错误",
            {"issues": result["issues"], "summary": result["summary"]},
        )
    # 同一事务：版本快照 + 模型状态 + 活动事件 + 审计；任一失败整体回滚
    version_no = (model.published_version_no or 0) + 1
    snapshot_json = build_snapshot(db, model)
    version = models.ModelVersion(
        model_id=model.id,
        version_no=version_no,
        status="published",
        snapshot_json=snapshot_json,
        change_note=payload.comment or f"正式发布 V{version_no}",
        created_by=actor,
    )
    db.add(version)
    model.status = "published"
    model.published_version_no = version_no
    db.flush()
    # 同一事务内构建并写入项目图谱快照（V0.9）：任一环节失败整体回滚，
    # 避免出现"已发布但没有图快照"的半成功状态。
    snapshot = _write_graph_snapshot(
        db, model, version_no, "publish",
        payload.comment or f"正式发布 V{version_no}", actor,
    )
    record_event(db, model.project_id, "model.version.published", actor,
                 {"model_id": model.id, "version_no": version_no,
                  "graph_snapshot_no": snapshot.snapshot_no,
                  "graph_hash": snapshot.graph_hash,
                  "comment": payload.comment}, request_id)
    record_audit(db, "model", str(model.id), "model.published", actor,
                 "success", request_id)
    db.commit()
    # V1.0 M6：发布计数按 layer 标签（不用 model_key 等高基数 label）
    metrics.record_model_published(model.layer)
    return {
        "model_id": model.id,
        "version_id": version.id,
        "version_no": version_no,
        "status": model.status,
        "graph_snapshot_no": snapshot.snapshot_no,
        "graph_hash": snapshot.graph_hash,
    }


@app.post("/api/models/{model_id}/start-change",
          dependencies=[Depends(auth_mod.require_model_role("editor"))])
def start_change(
    model_id: int, request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    actor = request.state.actor
    request_id = request.state.request_id
    model = get_model(db, model_id)
    if model.status != "published":
        raise conflict(
            "NO_PUBLISHED_BASELINE",
            f"只有已发布模型可发起变更，当前状态为 {model.status}",
            {"status": model.status},
        )
    model.status = "draft"
    model.edit_revision += 1
    db.flush()
    record_event(db, model.project_id, "model.change.started", actor,
                 {"model_id": model.id,
                  "baseline_version_no": model.published_version_no,
                  "edit_revision": model.edit_revision}, request_id)
    record_audit(db, "model", str(model.id), "model.change.started", actor,
                 "success", request_id)
    db.commit()
    return build_model_dict(db, model)


# ---------------------------------------------------------------- 模型关系


@app.get("/api/models/{model_id}/relationships",
         dependencies=[Depends(auth_mod.require_model_role("viewer"))])
def list_relationships(
    model_id: int, db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    get_model(db, model_id)
    return [relationship_dict(r) for r in model_relationships(db, model_id)]


@app.post("/api/models/{model_id}/relationships", status_code=201,
          dependencies=[Depends(auth_mod.require_model_role("editor"))])
def create_relationship(
    model_id: int,
    payload: ModelRelationshipCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    actor = request.state.actor
    request_id = request.state.request_id
    source = get_model(db, model_id)
    target = get_model(db, payload.target_model_id)

    if source.id == target.id:
        raise conflict("SELF_RELATIONSHIP", "不允许建立自引用关系")
    if source.project_id != target.project_id:
        raise conflict(
            "CROSS_PROJECT_RELATIONSHIP",
            f"模型 {source.id} 与 {target.id} 不属于同一项目，不能建立关系",
        )

    source_codes = {f.field_code for f in model_fields(db, source.id)}
    target_codes = {f.field_code for f in model_fields(db, target.id)}
    for code in payload.source_fields:
        if code not in source_codes:
            raise conflict(
                "RELATIONSHIP_FIELD_NOT_FOUND",
                f"源字段 '{code}' 不存在于模型 {source.model_key}",
            )
    for code in payload.target_fields:
        if code not in target_codes:
            raise conflict(
                "RELATIONSHIP_FIELD_NOT_FOUND",
                f"目标字段 '{code}' 不存在于模型 {target.model_key}",
            )
    if len(payload.source_fields) != len(payload.target_fields):
        raise conflict(
            "RELATIONSHIP_FIELD_PAIR_MISMATCH",
            "源字段与目标字段数量不一致，无法成对映射",
        )

    dup = db.scalar(
        select(models.ModelRelationship)
        .with_only_columns(models.ModelRelationship.id)
        .where(
            models.ModelRelationship.source_model_id == source.id,
            models.ModelRelationship.target_model_id == target.id,
            models.ModelRelationship.relationship_type == payload.relationship_type,
        )
    )
    if dup is not None:
        raise conflict(
            "MODEL_RELATIONSHIP_EXISTS",
            "相同源/目标/类型的关系已存在",
        )

    rel = models.ModelRelationship(
        project_id=source.project_id,
        source_model_id=source.id,
        target_model_id=target.id,
        relationship_type=payload.relationship_type,
        source_fields_json=json.dumps(payload.source_fields, ensure_ascii=False),
        target_fields_json=json.dumps(payload.target_fields, ensure_ascii=False),
        cardinality=payload.cardinality,
        description=payload.description,
        created_by=actor,
    )
    db.add(rel)
    db.flush()
    record_event(db, source.project_id, "relationship.created", actor,
                 {"source_model_id": source.id, "target_model_id": target.id,
                  "relationship_type": payload.relationship_type}, request_id)
    record_audit(db, "model_relationship", str(rel.id), "relationship.created",
                 actor, "success", request_id)
    db.commit()
    return relationship_dict(rel)


@app.delete("/api/models/{model_id}/relationships/{relationship_id}",
            dependencies=[Depends(auth_mod.require_model_role("editor"))])
def delete_relationship(
    model_id: int,
    relationship_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    actor = request.state.actor
    request_id = request.state.request_id
    get_model(db, model_id)
    rel = db.get(models.ModelRelationship, relationship_id)
    if rel is None:
        raise not_found("RELATIONSHIP_NOT_FOUND",
                        f"关系 {relationship_id} 不存在")
    if rel.source_model_id != model_id and rel.target_model_id != model_id:
        raise not_found("RELATIONSHIP_NOT_FOUND",
                        f"关系 {relationship_id} 与模型 {model_id} 无关")
    db.delete(rel)
    db.flush()
    record_event(db, rel.project_id, "relationship.deleted", actor,
                 {"relationship_id": relationship_id}, request_id)
    record_audit(db, "model_relationship", str(relationship_id),
                 "relationship.deleted", actor, "success", request_id)
    db.commit()
    return {"status": "deleted", "relationship_id": relationship_id}


# ---------------------------------------------------------------- 模型关系图 / 影响分析（V0.8）

IMPACT_DIRECTION_SEMANTICS = (
    "按 source_model_id → target_model_id 有向边遍历（方向为平台配置语义，"
    "需与团队规范一致）；结果为候选影响分析，不能替代 SQL/ETL/调度/BI 的真实血缘。"
    "关系未配置、方向配置错误或存在未建模的外部依赖时，结果可能漏报，评审人员必须人工核对。"
)


@app.get("/api/projects/{project_id}/model-graph",
         dependencies=[Depends(auth_mod.require_project_role("viewer"))])
def project_model_graph(
    project_id: int,
    view: str = Query(default="current", pattern="^(current|published_latest|snapshot|lineage)$"),
    snapshot_no: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """项目模型关系图（V0.9 视图切换）。

    - current：草稿实时图（DataModel + 当前 ModelRelationship）
    - published_latest：最近一次项目图谱快照（发布触发或迁移基线）
    - snapshot：按 snapshot_no 读取的不可变历史快照
    - lineage：当前设计关系 + active 外部观测边（分域展示）
    """
    return _graph_view(db, project_id, view, snapshot_no)


# ---------------------------------------------------------------- V0.9 图谱快照与外部血缘


def _canonical_json(obj: Any) -> str:
    """规范化 JSON：ensure_ascii=False, sort_keys=True, 紧凑分隔符。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _edge_key(source_model_id: int, target_model_id: int, relationship_type: str) -> str:
    """关系稳定标识：design:source:target:type（属性变化不产生新边）。"""
    return f"design:{source_model_id}:{target_model_id}:{relationship_type}"


def _graph_body_hash(nodes: list[dict[str, Any]], edges: list[dict[str, Any]],
                     omitted_edge_count: int) -> str:
    body = {
        "nodes": nodes,
        "edges": edges,
        "omitted_edge_count": omitted_edge_count,
    }
    return hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()


def _published_project_graph(
    db: Session, project_id: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """构建项目已发布图：节点取各模型 published_version_no 指向的正式版本，
    边从各模型已发布快照的相邻关系中过滤出向关系；目标无正式版本的边
    累加 omitted_edge_count（不进入快照）。
    """
    project_models = list(db.scalars(
        select(models.DataModel)
        .where(models.DataModel.project_id == project_id)
        .order_by(models.DataModel.id)
    ))
    model_by_id = {m.id: m for m in project_models}
    version_by_model: dict[int, models.ModelVersion] = {}
    for m in project_models:
        if m.published_version_no:
            v = db.scalar(select(models.ModelVersion).where(
                models.ModelVersion.model_id == m.id,
                models.ModelVersion.version_no == m.published_version_no,
            ))
            if v is not None:
                version_by_model[m.id] = v

    nodes = []
    for model_id, v in version_by_model.items():
        m = model_by_id[model_id]
        snap = json.loads(v.snapshot_json or "{}")
        model_info = snap.get("model", {})
        nodes.append({
            "id": m.id,
            "model_key": model_info.get("model_key", m.model_key),
            "name": model_info.get("name", m.name),
            "layer": model_info.get("layer", m.layer),
            "model_type": model_info.get("model_type", m.model_type),
            "target_engine": model_info.get("target_engine", m.target_engine),
            "version_no": v.version_no,
            "field_count": len(snap.get("fields", [])),
        })

    edges: list[dict[str, Any]] = []
    omitted = 0
    for model_id, v in version_by_model.items():
        snap = json.loads(v.snapshot_json or "{}")
        for rel in snap.get("relationships", []):
            if rel.get("source_model_id") != model_id:
                continue  # 快照含相邻入边；只取本模型出向关系，避免重复
            target_id = rel.get("target_model_id")
            if target_id in version_by_model:
                edges.append({
                    "edge_key": _edge_key(
                        model_id, target_id, rel.get("relationship_type", "")
                    ),
                    "source_model_id": model_id,
                    "target_model_id": target_id,
                    "relationship_type": rel.get("relationship_type", ""),
                    "source_fields": rel.get("source_fields", []),
                    "target_fields": rel.get("target_fields", []),
                    "cardinality": rel.get("cardinality", "unspecified"),
                    "description": rel.get("description", ""),
                    "source_version_no": v.version_no,
                    "target_version_no": version_by_model[target_id].version_no,
                })
            else:
                omitted += 1
    edges.sort(key=lambda e: e["edge_key"])
    return nodes, edges, omitted


def _write_graph_snapshot(
    db: Session,
    trigger_model: models.DataModel,
    version_no: int,
    snapshot_type: str,
    change_note: str,
    actor: str,
) -> models.ProjectGraphSnapshot:
    """在当前事务内写入项目图谱快照（调用方负责 commit）。"""
    nodes, edges, omitted = _published_project_graph(db, trigger_model.project_id)
    graph_hash = _graph_body_hash(nodes, edges, omitted)
    graph = {
        "schema_version": "v0.9",
        "nodes": nodes,
        "edges": edges,
        "omitted_edge_count": omitted,
        "graph_hash": graph_hash,
    }
    next_no = db.scalar(
        select(func.max(models.ProjectGraphSnapshot.snapshot_no))
        .where(models.ProjectGraphSnapshot.project_id == trigger_model.project_id)
    )
    snapshot = models.ProjectGraphSnapshot(
        project_id=trigger_model.project_id,
        snapshot_no=(next_no or 0) + 1,
        trigger_model_id=trigger_model.id,
        trigger_model_version_no=version_no,
        snapshot_type=snapshot_type,
        graph_json=_canonical_json(graph),
        graph_hash=graph_hash,
        node_count=len(nodes),
        edge_count=len(edges),
        omitted_edge_count=omitted,
        change_note=change_note,
        created_by=actor,
    )
    db.add(snapshot)
    db.flush()
    # V1.0 M6：快照创建计数（publish/migration_baseline 均经过此函数）
    metrics.record_graph_snapshot_created(snapshot_type)
    return snapshot


def _ensure_migration_baselines(db: Session) -> int:
    """V0.9 启动基线（幂等）：项目有已发布模型但没有任何图谱快照时，
    按升级当下各模型已发布版本重建一次 migration_baseline 快照。
    已有任意快照的项目跳过；不伪造历史发布轨迹。
    """
    created = 0
    project_ids = {
        row[0]
        for row in db.execute(
            select(models.DataModel.project_id)
            .where(models.DataModel.published_version_no > 0)
            .distinct()
        )
    }
    for project_id in sorted(project_ids):
        has_snapshot = db.scalar(
            select(models.ProjectGraphSnapshot.id)
            .where(models.ProjectGraphSnapshot.project_id == project_id)
            .limit(1)
        )
        if has_snapshot is not None:
            continue
        project = db.get(models.Project, project_id)
        if project is None:
            continue
        # 取任一已发布模型作为触发器占位（基线不代表某次发布）
        trigger = db.scalar(
            select(models.DataModel)
            .where(
                models.DataModel.project_id == project_id,
                models.DataModel.published_version_no > 0,
            )
            .order_by(models.DataModel.id)
            .limit(1)
        )
        if trigger is None:
            continue
        _write_graph_snapshot(
            db, trigger, trigger.published_version_no or 0,
            "migration_baseline",
            "V0.9 升级基线：按当前已发布版本组合重建",
            "system",
        )
        record_event(
            db, project_id, "graph.snapshot.migration_baseline", "system",
            {"snapshot_no": 1, "trigger_model_id": trigger.id},
            "",
        )
        created += 1
    if created:
        db.commit()
    return created


def graph_snapshot_dict(s: models.ProjectGraphSnapshot) -> dict[str, Any]:
    return {
        "snapshot_no": s.snapshot_no,
        "snapshot_type": s.snapshot_type,
        "trigger_model_id": s.trigger_model_id,
        "trigger_model_version_no": s.trigger_model_version_no,
        "node_count": s.node_count,
        "edge_count": s.edge_count,
        "omitted_edge_count": s.omitted_edge_count,
        "graph_hash": s.graph_hash,
        "change_note": s.change_note,
        "created_by": s.created_by,
        "created_at": iso(s.created_at),
    }


def _load_snapshot(
    db: Session, project_id: int, snapshot_no: int
) -> models.ProjectGraphSnapshot:
    snapshot = db.scalar(
        select(models.ProjectGraphSnapshot).where(
            models.ProjectGraphSnapshot.project_id == project_id,
            models.ProjectGraphSnapshot.snapshot_no == snapshot_no,
        )
    )
    if snapshot is None:
        raise not_found(
            "GRAPH_SNAPSHOT_NOT_FOUND",
            f"项目 {project_id} 不存在快照 #{snapshot_no}",
            {"snapshot_no": snapshot_no},
        )
    return snapshot


def _external_edge_dict(e: models.ExternalLineageEdge) -> dict[str, Any]:
    return {
        "id": e.id,
        "project_id": e.project_id,
        "source_system": e.source_system,
        "scope_key": e.scope_key,
        "source_ref": e.source_ref,
        "source_model_id": e.source_model_id,
        "target_model_id": e.target_model_id,
        "job_key": e.job_key,
        "lineage_type": e.lineage_type,
        "transformation_type": e.transformation_type,
        "source_fields": json.loads(e.source_fields_json or "[]"),
        "target_fields": json.loads(e.target_fields_json or "[]"),
        "confidence": e.confidence,
        "active": e.active,
        "observed_at": iso(e.observed_at),
        "last_seen_at": iso(e.last_seen_at),
        "metadata": json.loads(e.metadata_json or "{}"),
        "created_at": iso(e.created_at),
        "updated_at": iso(e.updated_at),
    }


def _import_run_dict(r: models.LineageImportRun) -> dict[str, Any]:
    return {
        "id": r.id,
        "source_system": r.source_system,
        "scope_key": r.scope_key,
        "run_id": r.run_id,
        "scan_mode": r.scan_mode,
        "complete_scan": r.complete_scan,
        "status": r.status,
        "received_count": r.received_count,
        "inserted_count": r.inserted_count,
        "updated_count": r.updated_count,
        "deactivated_count": r.deactivated_count,
        "attempt_count": r.attempt_count,
        "failure_count": r.failure_count,
        "deactivation_confirmed": r.deactivation_confirmed,
        "payload_hash": r.payload_hash,
        "error_summary": json.loads(r.error_summary_json or "[]"),
        "actor": r.actor,
        "request_id": r.request_id,
        "created_at": iso(r.created_at),
        "updated_at": iso(r.updated_at),
    }


def _graph_view(
    db: Session, project_id: int, view: str, snapshot_no: int | None
) -> dict[str, Any]:
    """按视图加载项目图：current / published_latest / snapshot / lineage。"""
    if view == "current":
        get_project(db, project_id)
        project_models = list(db.scalars(
            select(models.DataModel)
            .where(models.DataModel.project_id == project_id)
            .order_by(models.DataModel.id)
        ))
        field_counts: dict[int, int] = {}
        for row in db.execute(
            select(models.ModelField.model_id, func.count(models.ModelField.id))
            .where(models.ModelField.model_id.in_([m.id for m in project_models] or [-1]))
            .group_by(models.ModelField.model_id)
        ):
            field_counts[row[0]] = row[1]

        nodes = [
            {
                "id": m.id,
                "model_key": m.model_key,
                "name": m.name,
                "layer": m.layer,
                "target_engine": m.target_engine,
                "status": m.status,
                "field_count": field_counts.get(m.id, 0),
                "published_version_no": m.published_version_no,
            }
            for m in project_models
        ]
        edges = []
        for r in db.scalars(
            select(models.ModelRelationship)
            .where(models.ModelRelationship.project_id == project_id)
            .order_by(models.ModelRelationship.id)
        ):
            d = relationship_dict(r)
            d.pop("project_id", None)
            d.pop("created_by", None)
            d.pop("created_at", None)
            d["edge_key"] = _edge_key(
                r.source_model_id, r.target_model_id, r.relationship_type
            )
            edges.append(d)
        return {
            "project_id": project_id,
            "view": "current",
            "nodes": nodes,
            "edges": edges,
        }

    if view == "published_latest":
        get_project(db, project_id)
        snapshot = db.scalar(
            select(models.ProjectGraphSnapshot)
            .where(models.ProjectGraphSnapshot.project_id == project_id)
            .order_by(models.ProjectGraphSnapshot.snapshot_no.desc())
            .limit(1)
        )
        if snapshot is None:
            raise not_found(
                "GRAPH_SNAPSHOT_NOT_FOUND",
                f"项目 {project_id} 尚无图谱快照，请先发布模型",
            )
        graph = json.loads(snapshot.graph_json)
        return {
            "project_id": project_id,
            "view": "published_latest",
            "snapshot_no": snapshot.snapshot_no,
            "snapshot_type": snapshot.snapshot_type,
            "graph_hash": snapshot.graph_hash,
            "graph": graph,
            "nodes": graph.get("nodes", []),
            "edges": graph.get("edges", []),
        }

    if view == "snapshot":
        get_project(db, project_id)
        if snapshot_no is None:
            raise ApiError(
                422,
                "GRAPH_SNAPSHOT_REQUIRED",
                "view=snapshot 必须提供 snapshot_no",
            )
        snapshot = _load_snapshot(db, project_id, snapshot_no)
        graph = json.loads(snapshot.graph_json)
        return {
            "project_id": project_id,
            "view": "snapshot",
            "snapshot_no": snapshot.snapshot_no,
            "snapshot_type": snapshot.snapshot_type,
            "graph_hash": snapshot.graph_hash,
            "graph": graph,
            "nodes": graph.get("nodes", []),
            "edges": graph.get("edges", []),
        }

    if view == "lineage":
        # 当前设计关系 + active 外部观测边（分域展示）
        result = _graph_view(db, project_id, "current", None)
        result["view"] = "lineage"
        external = list(db.scalars(
            select(models.ExternalLineageEdge)
            .where(
                models.ExternalLineageEdge.project_id == project_id,
                models.ExternalLineageEdge.active.is_(True),
            )
            .order_by(models.ExternalLineageEdge.id)
        ))
        result["external_edges"] = [_external_edge_dict(e) for e in external]
        return result

    raise ApiError(422, "GRAPH_VIEW_INVALID", f"不支持的视图 {view}")


@app.get("/api/projects/{project_id}/model-graph/snapshots",
         dependencies=[Depends(auth_mod.require_project_role("viewer"))])
def list_graph_snapshots(
    project_id: int,
    limit: int = Query(default=100, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """项目图谱快照历史（按 snapshot_no 倒序）。"""
    get_project(db, project_id)
    snapshots = list(db.scalars(
        select(models.ProjectGraphSnapshot)
        .where(models.ProjectGraphSnapshot.project_id == project_id)
        .order_by(models.ProjectGraphSnapshot.snapshot_no.desc())
        .limit(limit)
    ))
    return [graph_snapshot_dict(s) for s in snapshots]


@app.get("/api/projects/{project_id}/model-graph/snapshots/{snapshot_no}",
         dependencies=[Depends(auth_mod.require_project_role("viewer"))])
def get_graph_snapshot(
    project_id: int, snapshot_no: int, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """读取完整的不可变图快照 JSON。"""
    get_project(db, project_id)
    snapshot = _load_snapshot(db, project_id, snapshot_no)
    return {**graph_snapshot_dict(snapshot), "graph": json.loads(snapshot.graph_json)}


@app.get("/api/projects/{project_id}/model-graph/diff",
         dependencies=[Depends(auth_mod.require_project_role("viewer"))])
def graph_snapshot_diff(
    project_id: int,
    from_snapshot_no: int = Query(ge=1),
    to_snapshot_no: int = Query(ge=1),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """A/B 快照差异：节点按稳定模型 id 差集；边按 edge_key 对齐。"""
    get_project(db, project_id)
    src = _load_snapshot(db, project_id, from_snapshot_no)
    dst = _load_snapshot(db, project_id, to_snapshot_no)
    src_graph = json.loads(src.graph_json)
    dst_graph = json.loads(dst.graph_json)

    src_nodes = {n["id"]: n for n in src_graph.get("nodes", [])}
    dst_nodes = {n["id"]: n for n in dst_graph.get("nodes", [])}
    node_attrs = ["model_key", "name", "layer", "model_type",
                  "target_engine", "version_no", "field_count"]
    nodes_added = [dst_nodes[i] for i in sorted(set(dst_nodes) - set(src_nodes))]
    nodes_removed = [src_nodes[i] for i in sorted(set(src_nodes) - set(dst_nodes))]
    nodes_modified = []
    for i in sorted(set(src_nodes) & set(dst_nodes)):
        for attr in node_attrs:
            if src_nodes[i].get(attr) != dst_nodes[i].get(attr):
                nodes_modified.append({
                    "model_id": i,
                    "model_key": dst_nodes[i].get("model_key"),
                    "attribute": attr,
                    "from": src_nodes[i].get(attr),
                    "to": dst_nodes[i].get(attr),
                })

    src_edges = {e["edge_key"]: e for e in src_graph.get("edges", [])}
    dst_edges = {e["edge_key"]: e for e in dst_graph.get("edges", [])}
    edge_attrs = ["source_fields", "target_fields", "cardinality",
                  "description", "source_version_no", "target_version_no"]
    edges_added = [dst_edges[k] for k in sorted(set(dst_edges) - set(src_edges))]
    edges_removed = [src_edges[k] for k in sorted(set(src_edges) - set(dst_edges))]
    edges_modified = []
    for k in sorted(set(src_edges) & set(dst_edges)):
        for attr in edge_attrs:
            if src_edges[k].get(attr) != dst_edges[k].get(attr):
                edges_modified.append({
                    "edge_key": k,
                    "attribute": attr,
                    "from": src_edges[k].get(attr),
                    "to": dst_edges[k].get(attr),
                })

    return {
        "project_id": project_id,
        "from_snapshot_no": from_snapshot_no,
        "to_snapshot_no": to_snapshot_no,
        "from_hash": src.graph_hash,
        "to_hash": dst.graph_hash,
        "nodes": {
            "added": nodes_added,
            "removed": nodes_removed,
            "modified": nodes_modified,
        },
        "edges": {
            "added": edges_added,
            "removed": edges_removed,
            "modified": edges_modified,
        },
    }


@app.get("/api/projects/{project_id}/model-graph/path",
         dependencies=[Depends(auth_mod.require_project_role("viewer"))])
def model_graph_path(
    project_id: int,
    source_model_id: int = Query(ge=1),
    target_model_id: int = Query(ge=1),
    direction: str = Query(default="downstream"),
    view: str = Query(default="current"),
    snapshot_no: int | None = Query(default=None, ge=1),
    max_depth: int = Query(default=20, ge=1, le=50),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """最短路径（BFS，边数最少）：downstream 正向 / upstream 反向（标记
    traversal_direction=reverse）/ any 双向。深度上限处仍有未探索邻居时
    truncated=true、reason=MAX_DEPTH_REACHED；完整探索无路径时
    reason=NO_PATH。
    """
    if direction not in {"downstream", "upstream", "any"}:
        raise ApiError(
            422,
            "GRAPH_DIRECTION_INVALID",
            f"direction 必须是 downstream / upstream / any，收到 {direction!r}",
            {"direction": direction},
        )
    graph = _graph_view(db, project_id, view, snapshot_no)
    nodes = graph.get("nodes", [])
    edges = list(graph.get("edges", []))
    if view == "lineage":
        for e in graph.get("external_edges", []):
            edges.append({
                "edge_key": f"external:{e['id']}",
                "source_model_id": e["source_model_id"],
                "target_model_id": e["target_model_id"],
                "relationship_type": e["lineage_type"],
                "external": True,
            })
    node_by_id = {n["id"]: n for n in nodes}

    if source_model_id not in node_by_id:
        raise not_found(
            "GRAPH_NODE_NOT_FOUND",
            f"起点模型 {source_model_id} 不在所选图谱视图（{view}）中",
            {"source_model_id": source_model_id},
        )
    if target_model_id not in node_by_id:
        raise not_found(
            "GRAPH_NODE_NOT_FOUND",
            f"终点模型 {target_model_id} 不在所选图谱视图（{view}）中",
            {"target_model_id": target_model_id},
        )

    # 邻接表：并列最短路径按模型 key/edge key 排序，保证可复现
    adjacency: dict[int, list[tuple[int, dict[str, Any], str]]] = {}
    for e in edges:
        s, t = e["source_model_id"], e["target_model_id"]
        if direction in {"downstream", "any"}:
            adjacency.setdefault(s, []).append(
                (t, e, "forward")
            )
        if direction in {"upstream", "any"}:
            adjacency.setdefault(t, []).append(
                (s, e, "reverse")
            )
    for neighbors in adjacency.values():
        neighbors.sort(key=lambda item: (
            node_by_id.get(item[0], {}).get("model_key", ""), item[1]["edge_key"]
        ))

    previous: dict[int, tuple[int, dict[str, Any], str] | None] = {
        source_model_id: None
    }
    queue: deque[tuple[int, int]] = deque([(source_model_id, 0)])
    found = source_model_id == target_model_id
    depth_at_boundary: dict[int, bool] = {}
    while queue and not found:
        node, depth = queue.popleft()
        if depth >= max_depth:
            depth_at_boundary[node] = any(
                nb[0] not in previous for nb in adjacency.get(node, [])
            )
            continue
        for neighbor, edge, traversal in adjacency.get(node, []):
            if neighbor in previous:
                continue
            previous[neighbor] = (node, edge, traversal)
            if neighbor == target_model_id:
                found = True
                break
            queue.append((neighbor, depth + 1))

    if found:
        chain: list[int] = []
        chain_edges: list[dict[str, Any]] = []
        node = target_model_id
        while node is not None:
            chain.append(node)
            step = previous[node]
            if step is None:
                break
            prev_node, edge, traversal = step
            chain_edges.append({"edge_key": edge["edge_key"],
                                "traversal_direction": traversal})
            node = prev_node
        chain.reverse()
        chain_edges.reverse()
        return {
            "found": True,
            "distance": len(chain_edges),
            "view": view,
            "snapshot_no": graph.get("snapshot_no", snapshot_no),
            "direction": direction,
            "nodes": [node_by_id[n] for n in chain],
            "edges": chain_edges,
        }

    return {
        "found": False,
        "distance": None,
        "view": view,
        "snapshot_no": graph.get("snapshot_no", snapshot_no),
        "direction": direction,
        "nodes": [],
        "edges": [],
        "truncated": bool(depth_at_boundary and any(depth_at_boundary.values())),
        "reason": ("MAX_DEPTH_REACHED"
                   if (depth_at_boundary and any(depth_at_boundary.values()))
                   else "NO_PATH"),
    }


def _diff_snapshot_category(
    baseline: dict[str, Any],
    draft: dict[str, Any],
    key: str,
    diff_fn,
) -> dict[str, list[Any]]:
    """按类别比较快照；V0.8 之前的快照缺少该键时整体跳过该类别差异。

    缺失键（legacy 快照）→ 视为不可比较，返回空差异：避免把历史快照
    未捕获的对象误报为新增、把分析范围错误扩大为模型级。
    键存在（含空列表）→ 正常 diff，空列表表示发布时确实没有该类对象。
    """
    if key not in baseline:
        return {"added": [], "removed": [], "modified": []}
    return diff_fn(baseline[key], draft.get(key, []))


def _compute_change_summary(
    draft: dict[str, Any], baseline: dict[str, Any] | None
) -> tuple[dict[str, Any], list[str]]:
    """草稿快照 vs 最近发布快照的结构化差异 + 模型级范围理由列表。

    返回 (change_summary, model_level_reasons)。baseline 为 None 时
    （无发布基线）返回空差异 + "无发布基线" 理由。
    """
    if baseline is None:
        return {
            "fields": {"added": [], "removed": [], "modified": []},
            "model_attributes": [],
            "constraints": {"added": [], "removed": [], "modified": []},
            "indexes": {"added": [], "removed": [], "modified": []},
            "relationships": {"added": [], "removed": [], "modified": []},
            "model_level_reasons": ["无发布基线，按全模型范围处理"],
        }, ["无发布基线"]

    model_attrs = [
        "model_key", "name", "description", "layer", "model_type",
        "target_engine", "subject_area_id",
    ]
    model_changes = [
        {"attribute": attr,
         "from": baseline["model"].get(attr),
         "to": draft["model"].get(attr)}
        for attr in model_attrs
        if baseline["model"].get(attr) != draft["model"].get(attr)
    ]

    base_fields = {f["field_code"]: f for f in baseline.get("fields", [])}
    draft_fields = {f["field_code"]: f for f in draft.get("fields", [])}
    field_attrs = [
        "field_name", "logical_type", "length", "precision", "scale",
        "nullable", "is_primary_key", "is_unique", "default_value",
        "description", "source_table", "source_field", "sensitivity",
    ]
    fields_modified = []
    for code, bf in base_fields.items():
        if code in draft_fields:
            changes = [
                {"attribute": attr, "from": bf.get(attr), "to": draft_fields[code].get(attr)}
                for attr in field_attrs
                if bf.get(attr) != draft_fields[code].get(attr)
            ]
            if changes:
                fields_modified.append({"field_code": code, "changes": changes})

    constraints_diff = _diff_snapshot_category(
        baseline, draft, "constraints",
        lambda a, b: _diff_named_objects(
            a, b, "constraint_name", CONSTRAINT_DIFF_ATTRS
        ),
    )
    indexes_diff = _diff_snapshot_category(
        baseline, draft, "indexes",
        lambda a, b: _diff_named_objects(
            a, b, "index_name", INDEX_DIFF_ATTRS
        ),
    )

    def rel_key(rel: dict[str, Any]) -> str:
        return (
            f"{rel.get('source_model_id')}->{rel.get('target_model_id')}"
            f":{rel.get('relationship_type')}"
        )

    relationships_diff = _diff_snapshot_category(
        baseline, draft, "relationships",
        lambda a, b: _diff_relationship_lists(a, b, rel_key),
    )

    summary = {
        "fields": {
            "added": [draft_fields[c] for c in draft_fields if c not in base_fields],
            "removed": [base_fields[c] for c in base_fields if c not in draft_fields],
            "modified": fields_modified,
        },
        "model_attributes": model_changes,
        "constraints": constraints_diff,
        "indexes": indexes_diff,
        "relationships": relationships_diff,
    }
    reasons: list[str] = []
    if model_changes:
        reasons.append("模型属性变化")
    if constraints_diff["added"] or constraints_diff["removed"] or constraints_diff["modified"]:
        reasons.append("表级约束变化")
    if (
        relationships_diff["added"]
        or relationships_diff["removed"]
        or relationships_diff["modified"]
    ):
        reasons.append("模型关系变化")
    summary["model_level_reasons"] = reasons
    # 索引变化仅性能相关，不单独推断数据语义变化（文档 10.1）
    return summary, reasons


def _impact_traverse(
    root_model_id: int,
    root_fields: frozenset[str] | None,
    out_edges: dict[int, list[dict[str, Any]]],
    models_by_id: dict[int, models.DataModel],
    max_nodes: int,
) -> tuple[list[dict[str, Any]], bool]:
    """按 source→target 有向边做有限 BFS，聚合候选下游影响。

    root_fields 为 None 表示模型级范围（全量传播）；字段级变更仅在边
    source_fields 命中时传播配对的 target_fields；缺少字段映射时
    下游按模型级范围处理并要求人工确认。
    返回 (impacted_models, truncated)。
    """
    # 状态：(模型, 受影响字段 frozenset|None, 路径边 key 元组, 是否经过基线边)
    queue: list[tuple[int, frozenset[str] | None, tuple[str, ...], bool]] = [
        (root_model_id, root_fields, (), False)
    ]
    agg: dict[int, dict[str, Any]] = {}
    truncated = False
    expansions = 0
    max_expansions = 2000  # 稠密图防爆上限
    max_paths_per_model = 3  # 有限保留备用路径

    while queue:
        mid, fields, path, has_baseline = queue.pop(0)
        for edge in out_edges.get(mid, []):
            ekey = edge["edge_key"]
            if ekey in path:
                continue  # 路径去重防循环
            if fields is None:
                propagate: frozenset[str] | None = None  # 模型级传播
            else:
                src = set(edge["source_fields"])
                if not src:
                    propagate = None  # 缺少字段映射 → 模型级，需人工确认
                else:
                    if not (src & fields):
                        continue  # 未命中变更字段
                    propagate = frozenset(edge["target_fields"])
            target = edge["target_model_id"]
            entry = agg.get(target)
            if entry is None:
                if len(agg) >= max_nodes:
                    truncated = True
                    queue.clear()
                    break
                entry = {
                    "paths": [],
                    "affected_fields": set(),
                    "model_level": False,
                    "relationship_keys": set(),
                    "baseline": False,
                }
                agg[target] = entry
            entry["paths"].append(path + (ekey,))
            entry["relationship_keys"].add(edge["relationship_key"])
            entry["baseline"] = entry["baseline"] or edge["is_baseline"]
            if propagate is None:
                entry["model_level"] = True
            else:
                entry["affected_fields"].update(propagate)
            expansions += 1
            if expansions >= max_expansions:
                truncated = True
                queue.clear()
                break
            queue.append((target, propagate, path + (ekey,), has_baseline))

    impacted = []
    for mid in sorted(agg):
        entry = agg[mid]
        m = models_by_id.get(mid)
        paths = entry["paths"][:max_paths_per_model]
        impacted.append({
            "model_id": mid,
            "model_key": m.model_key if m else None,
            "name": m.name if m else None,
            "layer": m.layer if m else None,
            "status": m.status if m else None,
            "affected_fields": (
                None if entry["model_level"] else sorted(entry["affected_fields"])
            ),
            "paths": [list(p) for p in paths],
            "alternate_paths_count": max(0, len(entry["paths"]) - max_paths_per_model),
            "relationship_keys": sorted(entry["relationship_keys"]),
            "includes_baseline_edge": entry["baseline"],
            "requires_manual_confirmation": entry["model_level"],
        })
    return impacted, truncated


def _build_impact_analysis(
    db: Session,
    model: models.DataModel,
    requested: set[str] | None = None,
    max_nodes: int = 300,
) -> dict[str, Any]:
    """变更影响分析核心（model_impact 与 M4 评审上下文共用）。

    field_codes: 将字段级变更范围收窄到指定字段（模型级范围不受影响）。
    max_nodes: 下游模型节点上限（默认 300，上限 1000）。
    """
    max_nodes = max(1, min(max_nodes, 1000))

    latest = db.scalar(
        select(models.ModelVersion)
        .where(models.ModelVersion.model_id == model.id)
        .order_by(models.ModelVersion.version_no.desc())
        .limit(1)
    )
    draft = json.loads(build_snapshot(db, model))
    baseline = json.loads(latest.snapshot_json) if latest is not None else None

    change_summary, model_level_reasons = _compute_change_summary(draft, baseline)
    if baseline is None:
        analysis_mode = "no_published_baseline"
        changed_field_codes: list[str] | None = None  # 无基线 → 模型级范围
    else:
        analysis_mode = "draft_vs_latest_published"
        if model_level_reasons:
            changed_field_codes = None  # 模型级范围
        else:
            changed = set()
            changed |= {f["field_code"] for f in change_summary["fields"]["added"]}
            changed |= {f["field_code"] for f in change_summary["fields"]["removed"]}
            changed |= {f["field_code"] for f in change_summary["fields"]["modified"]}
            changed_field_codes = sorted(changed)

    # field_codes 查询参数收窄字段级范围（模型级范围不受影响）
    if requested is not None and changed_field_codes is not None:
        changed_field_codes = sorted(set(changed_field_codes) & requested)

    # 构建出边集：当前关系 + 根模型最近发布快照中的旧出边（历史基线边合并）
    project_models = list(db.scalars(
        select(models.DataModel)
        .where(models.DataModel.project_id == model.project_id)
    ))
    models_by_id = {m.id: m for m in project_models}
    out_edges: dict[int, list[dict[str, Any]]] = {}
    for r in db.scalars(
        select(models.ModelRelationship)
        .where(models.ModelRelationship.project_id == model.project_id)
    ):
        if r.source_model_id == r.target_model_id:
            continue
        out_edges.setdefault(r.source_model_id, []).append({
            "edge_key": f"rel:{r.id}",
            "relationship_key": (
                f"{r.source_model_id}->{r.target_model_id}:{r.relationship_type}"
            ),
            "source_model_id": r.source_model_id,
            "target_model_id": r.target_model_id,
            "source_fields": json.loads(r.source_fields_json or "[]"),
            "target_fields": json.loads(r.target_fields_json or "[]"),
            "is_baseline": False,
        })
    if baseline is not None:
        for rel in baseline.get("relationships", []):
            if rel.get("source_model_id") != model.id:
                continue
            rkey = (
                f"{rel.get('source_model_id')}->{rel.get('target_model_id')}"
                f":{rel.get('relationship_type')}"
            )
            # 与当前出边去重：仍存在的边以当前定义为准
            if any(
                e["relationship_key"] == rkey
                for e in out_edges.get(model.id, [])
            ):
                continue
            out_edges.setdefault(model.id, []).append({
                "edge_key": f"baseline:{rkey}",
                "relationship_key": rkey,
                "source_model_id": rel.get("source_model_id"),
                "target_model_id": rel.get("target_model_id"),
                "source_fields": rel.get("source_fields", []),
                "target_fields": rel.get("target_fields", []),
                "is_baseline": True,
            })

    root_fields = None if changed_field_codes is None else frozenset(changed_field_codes)
    impacted, truncated = _impact_traverse(
        model.id, root_fields, out_edges, models_by_id, max_nodes
    )

    return {
        "model_id": model.id,
        "model_key": model.model_key,
        "analysis_mode": analysis_mode,
        "changed_field_codes": changed_field_codes,
        "change_summary": change_summary,
        "impacted_models": impacted,
        "truncated": truncated,
        "direction_semantics": IMPACT_DIRECTION_SEMANTICS,
    }


@app.get("/api/models/{model_id}/impact",
         dependencies=[Depends(auth_mod.require_model_role("viewer"))])
def model_impact(
    model_id: int,
    field_codes: str | None = None,
    max_nodes: int = 300,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """变更影响分析（候选）：当前草稿 vs 最近发布快照，沿有向出边传播。

    field_codes: 逗号分隔，将字段级变更范围收窄到指定字段（模型级范围不受影响）。
    max_nodes: 下游模型节点上限（默认 300，上限 1000），超出时 truncated=true。
    """
    model = get_model(db, model_id)
    requested = (
        {c.strip() for c in field_codes.split(",") if c.strip()}
        if field_codes
        else None
    )
    return _build_impact_analysis(db, model, requested, max_nodes)


# ---------------------------------------------------------------- 表级约束（V0.8）


def _require_draft(model: models.DataModel) -> None:
    """设计对象（字段/约束/索引）仅草稿可改；非草稿返回 409 MODEL_NOT_EDITABLE。"""
    if model.status != "draft":
        raise conflict(
            "MODEL_NOT_EDITABLE",
            f"模型当前状态为 {model.status}，只有草稿可编辑；"
            "已发布模型请使用发起变更（start-change）",
            {"status": model.status},
        )


def _constraint_name_taken(
    db: Session, model_id: int, name: str, exclude_id: int | None = None
) -> bool:
    q = (
        select(models.ModelConstraint)
        .with_only_columns(models.ModelConstraint.id)
        .where(
            models.ModelConstraint.model_id == model_id,
            models.ModelConstraint.constraint_name == name,
        )
    )
    if exclude_id is not None:
        q = q.where(models.ModelConstraint.id != exclude_id)
    return db.scalar(q) is not None


def _apply_constraint_payload(
    c: models.ModelConstraint, payload: ModelConstraintCreate
) -> None:
    """按约束类型归一化落库：非适用属性清空，避免旧值被意外提交（文档 7.2）。"""
    c.constraint_name = payload.constraint_name
    c.constraint_type = payload.constraint_type
    c.field_codes_json = json.dumps(payload.field_codes, ensure_ascii=False)
    c.expression = payload.expression if payload.constraint_type == "check" else ""
    is_fk = payload.constraint_type == "foreign_key"
    c.referenced_model_id = payload.referenced_model_id if is_fk else None
    c.referenced_field_codes_json = json.dumps(
        payload.referenced_field_codes if is_fk else [], ensure_ascii=False
    )
    c.on_delete = payload.on_delete if is_fk else "NO ACTION"
    c.description = payload.description


def _validate_constraint_definition(
    db: Session,
    model: models.DataModel,
    payload: ModelConstraintCreate,
    exclude_id: int | None = None,
) -> list[str]:
    """表级约束服务端规则（文档 §4/§5 + Table 8）；返回警告列表。

    primary_key：≥2 字段、无重复、字段存在、与字段级主键互斥、模型仅一个；
    参与字段随后由调用方置为非空（删除主键不自动复原，文档 Table 10）。
    unique：≥1 字段、顺序保留、名称模型内唯一。
    check：表达式必填且字段列表为空；受限 token 语法；引用字段必须存在。
    foreign_key：目标同项目、字段一一对应、ON DELETE 白名单、
    SET NULL 要求源字段可空；目标键未声明唯一性时给警告。
    """
    warnings: list[str] = []
    fields = {f.field_code: f for f in model_fields(db, model.id)}
    codes = payload.field_codes

    if _constraint_name_taken(db, model.id, payload.constraint_name, exclude_id):
        raise conflict(
            "DUPLICATE_CONSTRAINT_NAME",
            f"约束名 '{payload.constraint_name}' 在模型内已存在",
        )

    if payload.constraint_type == "check":
        if codes:
            raise ApiError(
                422,
                "INVALID_CONSTRAINT_DEFINITION",
                "CHECK 约束不需要字段列表，请把列条件写入表达式",
            )
        msg = validate_check_expression(payload.expression, set(fields))
        if msg:
            raise ApiError(422, "INVALID_CHECK_EXPRESSION", msg)
        return warnings

    # PK / unique / foreign_key 共用字段列表校验
    if not codes:
        raise ApiError(
            422,
            "INVALID_CONSTRAINT_DEFINITION",
            f"{payload.constraint_type} 约束需要至少一个字段",
        )
    if len(set(codes)) != len(codes):
        raise ApiError(
            422, "INVALID_CONSTRAINT_DEFINITION", "约束字段列表存在重复编码"
        )
    for code in codes:
        if code not in fields:
            raise ApiError(
                422,
                "CONSTRAINT_FIELD_NOT_FOUND",
                f"字段 '{code}' 不存在于模型 {model.model_key}",
            )

    if payload.constraint_type == "primary_key":
        if len(codes) < 2:
            raise ApiError(
                422,
                "INVALID_CONSTRAINT_DEFINITION",
                "表级主键需要至少 2 个字段（单列主键请使用字段级开关）",
            )
        field_pk = [f.field_code for f in fields.values() if f.is_primary_key]
        if field_pk:
            raise conflict(
                "PRIMARY_KEY_CONFLICT",
                f"模型已存在字段级主键 {field_pk}，不能同时定义表级主键",
            )
        existing_pk = db.scalar(
            select(models.ModelConstraint)
            .with_only_columns(models.ModelConstraint.id)
            .where(
                models.ModelConstraint.model_id == model.id,
                models.ModelConstraint.constraint_type == "primary_key",
            )
        )
        if existing_pk is not None and existing_pk != exclude_id:
            raise conflict(
                "PRIMARY_KEY_CONFLICT", "模型已存在表级主键约束，同一模型只能有一个主键"
            )

    if payload.constraint_type == "foreign_key":
        if not payload.referenced_model_id:
            raise ApiError(422, "FK_TARGET_MODEL_INVALID", "外键必须指定目标模型")
        target = get_model(db, payload.referenced_model_id)
        if target.project_id != model.project_id:
            raise ApiError(
                422,
                "FK_TARGET_MODEL_INVALID",
                f"目标模型 {target.id} 与源模型不属于同一项目，不能跨项目引用",
            )
        ref_codes = payload.referenced_field_codes
        if not ref_codes:
            raise ApiError(422, "FK_FIELD_PAIR_MISMATCH", "外键必须指定目标字段")
        if len(ref_codes) != len(codes):
            raise ApiError(
                422,
                "FK_FIELD_PAIR_MISMATCH",
                "源字段与目标字段数量不一致，需按顺序一一对应",
            )
        if len(set(ref_codes)) != len(ref_codes):
            raise ApiError(
                422, "INVALID_CONSTRAINT_DEFINITION", "外键目标字段列表存在重复编码"
            )
        target_fields = {f.field_code for f in model_fields(db, target.id)}
        for code in ref_codes:
            if code not in target_fields:
                raise ApiError(
                    422,
                    "FK_TARGET_FIELD_NOT_FOUND",
                    f"目标字段 '{code}' 不存在于模型 {target.model_key}",
                )
        if payload.on_delete == "SET NULL":
            non_nullable = [c for c in codes if not fields[c].nullable]
            if non_nullable:
                raise ApiError(
                    422,
                    "FK_SET_NULL_REQUIRES_NULLABLE",
                    f"ON DELETE SET NULL 要求源字段可空：{non_nullable}",
                )
        if not _referenced_key_is_unique(db, target, ref_codes):
            warnings.append(
                f"外键目标字段 {ref_codes} 未声明主键或唯一约束，目标引擎执行 DDL 可能失败"
            )
    return warnings


def _set_pk_fields_not_null(db: Session, model_id: int, field_codes: list[str]) -> None:
    """表级主键创建/更新后，参与字段置为非空（删除主键不自动复原）。"""
    for code in field_codes:
        f = db.scalar(
            select(models.ModelField).where(
                models.ModelField.model_id == model_id,
                models.ModelField.field_code == code,
            )
        )
        if f is not None:
            f.nullable = False


@app.get("/api/models/{model_id}/constraints",
         dependencies=[Depends(auth_mod.require_model_role("viewer"))])
def list_constraints(
    model_id: int, db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    get_model(db, model_id)
    return [constraint_dict(c) for c in model_constraints(db, model_id)]


@app.post("/api/models/{model_id}/constraints", status_code=201,
          dependencies=[Depends(auth_mod.require_model_role("editor"))])
def create_constraint(
    model_id: int,
    payload: ModelConstraintCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    actor = request.state.actor
    request_id = request.state.request_id
    model = get_model(db, model_id)
    _require_draft(model)
    warnings = _validate_constraint_definition(db, model, payload)
    constraint = models.ModelConstraint(
        model_id=model.id, created_by=actor
    )
    _apply_constraint_payload(constraint, payload)
    db.add(constraint)
    db.flush()
    if payload.constraint_type == "primary_key":
        _set_pk_fields_not_null(db, model.id, payload.field_codes)
    model.edit_revision += 1
    db.flush()
    record_event(
        db, model.project_id, "constraint.created", actor,
        {"model_id": model.id, "constraint_id": constraint.id,
         "constraint_name": constraint.constraint_name,
         "constraint_type": constraint.constraint_type},
        request_id,
    )
    record_audit(db, "model_constraint", str(constraint.id),
                 "constraint.created", actor, "success", request_id)
    db.commit()
    result = constraint_dict(constraint)
    if warnings:
        result["warnings"] = warnings
    return result


@app.put("/api/models/{model_id}/constraints/{constraint_id}",
         dependencies=[Depends(auth_mod.require_model_role("editor"))])
def update_constraint(
    model_id: int,
    constraint_id: int,
    payload: ModelConstraintCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    actor = request.state.actor
    request_id = request.state.request_id
    model = get_model(db, model_id)
    _require_draft(model)
    constraint = db.get(models.ModelConstraint, constraint_id)
    if constraint is None or constraint.model_id != model_id:
        raise not_found("CONSTRAINT_NOT_FOUND", f"约束 {constraint_id} 不存在")
    warnings = _validate_constraint_definition(
        db, model, payload, exclude_id=constraint_id
    )
    _apply_constraint_payload(constraint, payload)
    if payload.constraint_type == "primary_key":
        _set_pk_fields_not_null(db, model.id, payload.field_codes)
    model.edit_revision += 1
    db.flush()
    record_event(
        db, model.project_id, "constraint.updated", actor,
        {"model_id": model.id, "constraint_id": constraint.id,
         "constraint_name": constraint.constraint_name,
         "constraint_type": constraint.constraint_type},
        request_id,
    )
    record_audit(db, "model_constraint", str(constraint_id),
                 "constraint.updated", actor, "success", request_id)
    db.commit()
    result = constraint_dict(constraint)
    if warnings:
        result["warnings"] = warnings
    return result


@app.delete("/api/models/{model_id}/constraints/{constraint_id}",
            dependencies=[Depends(auth_mod.require_model_role("editor"))])
def delete_constraint(
    model_id: int,
    constraint_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    actor = request.state.actor
    request_id = request.state.request_id
    model = get_model(db, model_id)
    _require_draft(model)
    constraint = db.get(models.ModelConstraint, constraint_id)
    if constraint is None or constraint.model_id != model_id:
        raise not_found("CONSTRAINT_NOT_FOUND", f"约束 {constraint_id} 不存在")
    db.delete(constraint)
    db.flush()
    # 删除表级主键不自动复原字段 nullable（文档 Table 10：非空属性可能仍被需要）
    model.edit_revision += 1
    db.flush()
    record_event(
        db, model.project_id, "constraint.deleted", actor,
        {"model_id": model.id, "constraint_id": constraint_id,
         "constraint_name": constraint.constraint_name},
        request_id,
    )
    record_audit(db, "model_constraint", str(constraint_id),
                 "constraint.deleted", actor, "success", request_id)
    db.commit()
    return {"status": "deleted", "constraint_id": constraint_id}


# ---------------------------------------------------------------- 索引（V0.8）


def _index_name_taken(
    db: Session, model_id: int, name: str, exclude_id: int | None = None
) -> bool:
    q = (
        select(models.ModelIndex)
        .with_only_columns(models.ModelIndex.id)
        .where(
            models.ModelIndex.model_id == model_id,
            models.ModelIndex.index_name == name,
        )
    )
    if exclude_id is not None:
        q = q.where(models.ModelIndex.id != exclude_id)
    if db.scalar(q) is not None:
        return True
    # 索引名不能与同模型约束名冲突（DDL 标识符命名空间，文档 5.4）
    c = db.scalar(
        select(models.ModelConstraint)
        .with_only_columns(models.ModelConstraint.id)
        .where(
            models.ModelConstraint.model_id == model_id,
            models.ModelConstraint.constraint_name == name,
        )
    )
    return c is not None


def _validate_index_definition(
    db: Session,
    model: models.DataModel,
    payload: ModelIndexCreate,
    exclude_id: int | None = None,
) -> None:
    """索引服务端规则（文档 §6.1）：字段非空无重复且存在；名称模型内唯一
    （含约束名冲突）；索引类型受目标引擎能力矩阵约束。"""
    fields = {f.field_code for f in model_fields(db, model.id)}
    codes = payload.field_codes
    if _index_name_taken(db, model.id, payload.index_name, exclude_id):
        raise conflict(
            "DUPLICATE_INDEX_NAME",
            f"索引名 '{payload.index_name}' 在模型内已存在（含与约束名冲突）",
        )
    if len(set(codes)) != len(codes):
        raise ApiError(422, "INVALID_INDEX_DEFINITION", "索引字段列表存在重复编码")
    for code in codes:
        if code not in fields:
            raise ApiError(
                422,
                "INDEX_FIELD_NOT_FOUND",
                f"字段 '{code}' 不存在于模型 {model.model_key}",
            )
    supported, reason = engine_supports_index(
        model.target_engine, payload.index_type, payload.is_unique
    )
    if not supported:
        code = (
            "GIN_UNIQUE_INDEX_UNSUPPORTED"
            if payload.index_type == "gin" and payload.is_unique
            else "INDEX_TYPE_NOT_SUPPORTED"
        )
        raise ApiError(422, code, reason)


@app.get("/api/models/{model_id}/indexes",
         dependencies=[Depends(auth_mod.require_model_role("viewer"))])
def list_indexes(
    model_id: int, db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    get_model(db, model_id)
    return [index_dict(i) for i in model_indexes(db, model_id)]


@app.post("/api/models/{model_id}/indexes", status_code=201,
          dependencies=[Depends(auth_mod.require_model_role("editor"))])
def create_index(
    model_id: int,
    payload: ModelIndexCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    actor = request.state.actor
    request_id = request.state.request_id
    model = get_model(db, model_id)
    _require_draft(model)
    _validate_index_definition(db, model, payload)
    index = models.ModelIndex(
        model_id=model.id,
        index_name=payload.index_name,
        index_type=payload.index_type,
        field_codes_json=json.dumps(payload.field_codes, ensure_ascii=False),
        is_unique=payload.is_unique,
        description=payload.description,
        created_by=actor,
    )
    db.add(index)
    db.flush()
    model.edit_revision += 1
    db.flush()
    record_event(
        db, model.project_id, "index.created", actor,
        {"model_id": model.id, "index_id": index.id,
         "index_name": index.index_name, "index_type": index.index_type},
        request_id,
    )
    record_audit(db, "model_index", str(index.id),
                 "index.created", actor, "success", request_id)
    db.commit()
    return index_dict(index)


@app.put("/api/models/{model_id}/indexes/{index_id}",
         dependencies=[Depends(auth_mod.require_model_role("editor"))])
def update_index(
    model_id: int,
    index_id: int,
    payload: ModelIndexCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    actor = request.state.actor
    request_id = request.state.request_id
    model = get_model(db, model_id)
    _require_draft(model)
    index = db.get(models.ModelIndex, index_id)
    if index is None or index.model_id != model_id:
        raise not_found("INDEX_NOT_FOUND", f"索引 {index_id} 不存在")
    _validate_index_definition(db, model, payload, exclude_id=index_id)
    index.index_name = payload.index_name
    index.index_type = payload.index_type
    index.field_codes_json = json.dumps(payload.field_codes, ensure_ascii=False)
    index.is_unique = payload.is_unique
    index.description = payload.description
    model.edit_revision += 1
    db.flush()
    record_event(
        db, model.project_id, "index.updated", actor,
        {"model_id": model.id, "index_id": index.id,
         "index_name": index.index_name, "index_type": index.index_type},
        request_id,
    )
    record_audit(db, "model_index", str(index_id),
                 "index.updated", actor, "success", request_id)
    db.commit()
    return index_dict(index)


@app.delete("/api/models/{model_id}/indexes/{index_id}",
            dependencies=[Depends(auth_mod.require_model_role("editor"))])
def delete_index(
    model_id: int,
    index_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    actor = request.state.actor
    request_id = request.state.request_id
    model = get_model(db, model_id)
    _require_draft(model)
    index = db.get(models.ModelIndex, index_id)
    if index is None or index.model_id != model_id:
        raise not_found("INDEX_NOT_FOUND", f"索引 {index_id} 不存在")
    db.delete(index)
    db.flush()
    model.edit_revision += 1
    db.flush()
    record_event(
        db, model.project_id, "index.deleted", actor,
        {"model_id": model.id, "index_id": index_id,
         "index_name": index.index_name},
        request_id,
    )
    record_audit(db, "model_index", str(index_id),
                 "index.deleted", actor, "success", request_id)
    db.commit()
    return {"status": "deleted", "index_id": index_id}


# ---------------------------------------------------------------- 版本与差异


@app.get("/api/models/{model_id}/versions",
         dependencies=[Depends(auth_mod.require_model_role("viewer"))])
def list_versions(model_id: int, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    get_model(db, model_id)
    versions = db.scalars(
        select(models.ModelVersion)
        .where(models.ModelVersion.model_id == model_id)
        .order_by(models.ModelVersion.version_no.desc())
    )
    return [version_dict(v) for v in versions]


def _load_version(db: Session, model_id: int, version_no: int) -> models.ModelVersion:
    v = db.scalar(
        select(models.ModelVersion).where(
            models.ModelVersion.model_id == model_id,
            models.ModelVersion.version_no == version_no,
        )
    )
    if v is None:
        raise not_found(
            "MODEL_VERSION_NOT_FOUND",
            f"模型 {model_id} 不存在版本 V{version_no}",
        )
    return v


def _diff_named_objects(
    from_items: list[dict[str, Any]],
    to_items: list[dict[str, Any]],
    name_field: str,
    compare_attrs: list[str],
) -> dict[str, list[Any]]:
    """按名称键比较两组规范化对象（约束/索引），输出 added/removed/modified。"""
    from_map = {item[name_field]: item for item in from_items}
    to_map = {item[name_field]: item for item in to_items}
    added = [to_map[k] for k in to_map if k not in from_map]
    removed = [from_map[k] for k in from_map if k not in to_map]
    modified = []
    for k, ti in to_map.items():
        if k in from_map:
            fi = from_map[k]
            changes = [
                {"attribute": attr, "from": fi.get(attr), "to": ti.get(attr)}
                for attr in compare_attrs
                if fi.get(attr) != ti.get(attr)
            ]
            if changes:
                modified.append({name_field: k, "changes": changes})
    return {"added": added, "removed": removed, "modified": modified}


def _diff_relationship_lists(
    from_items: list[dict[str, Any]],
    to_items: list[dict[str, Any]],
    key_fn,
) -> dict[str, list[Any]]:
    from_map = {key_fn(r): r for r in from_items}
    to_map = {key_fn(r): r for r in to_items}
    added = [to_map[k] for k in to_map if k not in from_map]
    removed = [from_map[k] for k in from_map if k not in to_map]
    modified = []
    compare_attrs = ["source_fields", "target_fields", "cardinality", "description"]
    for k, ti in to_map.items():
        if k in from_map:
            fi = from_map[k]
            changes = [
                {"attribute": attr, "from": fi.get(attr), "to": ti.get(attr)}
                for attr in compare_attrs
                if fi.get(attr) != ti.get(attr)
            ]
            if changes:
                modified.append({"relationship_key": k, "changes": changes})
    return {"added": added, "removed": removed, "modified": modified}


CONSTRAINT_DIFF_ATTRS = [
    "constraint_type", "field_codes", "expression",
    "referenced_model_id", "referenced_field_codes", "on_delete", "description",
]
INDEX_DIFF_ATTRS = [
    "index_type", "field_codes", "is_unique", "description",
]


@app.get("/api/models/{model_id}/versions/diff",
         dependencies=[Depends(auth_mod.require_model_role("viewer"))])
def diff_versions(
    model_id: int,
    from_version: int,
    to_version: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    get_model(db, model_id)
    if from_version == to_version:
        raise ApiError(400, "VALIDATION_ERROR", "比较的两个版本必须不同")
    v_from = _load_version(db, model_id, from_version)
    v_to = _load_version(db, model_id, to_version)
    snap_from = json.loads(v_from.snapshot_json)
    snap_to = json.loads(v_to.snapshot_json)

    model_attrs = [
        "model_key", "name", "description", "layer", "model_type",
        "target_engine", "subject_area_id",
    ]
    model_changes = [
        {"attribute": attr,
         "from": snap_from["model"].get(attr),
         "to": snap_to["model"].get(attr)}
        for attr in model_attrs
        if snap_from["model"].get(attr) != snap_to["model"].get(attr)
    ]

    from_fields = {f["field_code"]: f for f in snap_from["fields"]}
    to_fields = {f["field_code"]: f for f in snap_to["fields"]}
    added = [to_fields[c] for c in to_fields if c not in from_fields]
    removed = [from_fields[c] for c in from_fields if c not in to_fields]
    field_attrs = [
        "field_name", "logical_type", "length", "precision", "scale",
        "nullable", "is_primary_key", "is_unique", "default_value",
        "description", "source_table", "source_field", "sensitivity",
    ]
    modified = []
    for code, ff in from_fields.items():
        if code in to_fields:
            changes = [
                {"attribute": attr, "from": ff.get(attr), "to": to_fields[code].get(attr)}
                for attr in field_attrs
                if ff.get(attr) != to_fields[code].get(attr)
            ]
            if changes:
                modified.append({"field_code": code, "changes": changes})

    from_reqs = set(snap_from.get("requirement_ids", []))
    to_reqs = set(snap_to.get("requirement_ids", []))

    # V0.8：约束/索引/相邻关系差异（V0.8 之前的快照缺键时跳过该类别，向后兼容）
    constraints_diff = _diff_snapshot_category(
        snap_from, snap_to, "constraints",
        lambda a, b: _diff_named_objects(
            a, b, "constraint_name", CONSTRAINT_DIFF_ATTRS
        ),
    )
    indexes_diff = _diff_snapshot_category(
        snap_from, snap_to, "indexes",
        lambda a, b: _diff_named_objects(
            a, b, "index_name", INDEX_DIFF_ATTRS
        ),
    )

    def rel_key(rel: dict[str, Any]) -> str:
        return (
            f"{rel.get('source_model_id')}->{rel.get('target_model_id')}"
            f":{rel.get('relationship_type')}"
        )

    relationships_diff = _diff_snapshot_category(
        snap_from, snap_to, "relationships",
        lambda a, b: _diff_relationship_lists(a, b, rel_key),
    )

    return {
        "model_id": model_id,
        "from_version": from_version,
        "to_version": to_version,
        "model_changes": model_changes,
        "fields": {
            "added": added,
            "removed": removed,
            "modified": modified,
        },
        "constraints": constraints_diff,
        "indexes": indexes_diff,
        "relationships": relationships_diff,
        "requirements": {
            "added": sorted(to_reqs - from_reqs),
            "removed": sorted(from_reqs - to_reqs),
        },
    }


# ---------------------------------------------------------------- DDL 预览


@app.post("/api/models/{model_id}/ddl",
          dependencies=[Depends(auth_mod.require_model_role("viewer"))])
def ddl_preview(
    model_id: int,
    payload: DDLRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    actor = request.state.actor
    request_id = request.state.request_id
    model = get_model(db, model_id)
    result = run_validation(db, model)
    if result["summary"]["errors"]:
        raise ApiError(
            422,
            "MODEL_VALIDATION_FAILED",
            "存在阻断级校验错误，不能生成可交付 DDL",
            {"issues": result["issues"], "summary": result["summary"]},
        )
    fields = model_fields(db, model.id)
    constraints = model_constraints(db, model.id)
    indexes = model_indexes(db, model.id)
    table_names, unique_keys = _project_fk_context(db, model.project_id)
    try:
        artifact = generate_create_table(
            model, fields, payload.target_engine, payload.schema_name,
            constraints=constraints,
            indexes=indexes,
            referenced_table_names=table_names,
            fk_target_unique_keys=unique_keys,
        )
    except DDLGenerationError as exc:
        raise ApiError(422, "DDL_GENERATION_FAILED", str(exc))
    record_event(db, model.project_id, "ddl.generated", actor,
                 {"model_id": model.id, "target_engine": payload.target_engine,
                  "published_version_no": model.published_version_no},
                 request_id)
    record_audit(db, "model", str(model.id), "ddl.generated", actor,
                 "success", request_id)
    db.commit()
    data = artifact.to_dict()
    data.update({
        "model_id": model.id,
        "model_key": model.model_key,
        "published_version_no": model.published_version_no,
        "schema_name": payload.schema_name,
    })
    return data


# ---------------------------------------------------------------- 事件 / 看板 / 观测


@app.get("/api/projects/{project_id}/events",
         dependencies=[Depends(auth_mod.require_project_role("viewer"))])
def list_events(
    project_id: int,
    limit: int = 50,
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    get_project(db, project_id)
    limit = max(1, min(limit, 200))
    events = db.scalars(
        select(models.ActivityEvent)
        .where(models.ActivityEvent.project_id == project_id)
        .order_by(models.ActivityEvent.created_at.desc(), models.ActivityEvent.id.desc())
        .limit(limit)
    )
    return [event_dict(e) for e in events]


@app.get("/api/dashboard/{project_id}",
         dependencies=[Depends(auth_mod.require_project_role("viewer"))])
def dashboard(project_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    get_project(db, project_id)
    models_rows = db.execute(
        text("SELECT status, COUNT(*) FROM data_models WHERE project_id = :pid GROUP BY status"),
        {"pid": project_id},
    ).all()
    req_rows = db.execute(
        text("SELECT status, COUNT(*) FROM business_requirements WHERE project_id = :pid GROUP BY status"),
        {"pid": project_id},
    ).all()
    published_versions = db.scalar(
        text(
            "SELECT COUNT(*) FROM model_versions v JOIN data_models m ON m.id = v.model_id "
            "WHERE m.project_id = :pid"
        ),
        {"pid": project_id},
    )
    relationships = db.scalar(
        text("SELECT COUNT(*) FROM model_relationships WHERE project_id = :pid"),
        {"pid": project_id},
    )
    recent = db.scalars(
        select(models.ActivityEvent)
        .where(models.ActivityEvent.project_id == project_id)
        .order_by(models.ActivityEvent.created_at.desc(), models.ActivityEvent.id.desc())
        .limit(10)
    )
    return {
        "project_id": project_id,
        "models": {
            "total": sum(c for _, c in models_rows),
            "by_status": {s: c for s, c in models_rows},
        },
        "requirements": {
            "total": sum(c for _, c in req_rows),
            "by_status": {s: c for s, c in req_rows},
        },
        "pending_review": dict(models_rows).get("in_review", 0),
        "published_versions": published_versions or 0,
        "relationships": relationships or 0,
        "recent_events": [event_dict(e) for e in recent],
    }


@app.get("/api/observability/summary")
def observability_summary(db: Session = Depends(get_db)) -> dict[str, Any]:
    uptime = time.time() - STARTED_AT
    avg = metrics.duration_sum_ms / metrics.requests_total if metrics.requests_total else 0.0
    # V1.0 M6 增量：版本与迁移信息 + 认证拒绝计数 + 血缘导入健康摘要
    lineage = _lineage_summary(db)
    return {
        "uptime_seconds": round(uptime, 1),
        "requests_total": metrics.requests_total,
        "requests_by_status_class": metrics.by_status_class,
        "errors_total": metrics.errors_total,
        "avg_duration_ms": round(avg, 2),
        "app_version": APP_VERSION,
        "head_revision": HEAD_REVISION,
        "auth": {
            "mode": settings.auth_mode,
            "denied_total": sum(metrics.auth_denied_by_reason.values()),
        },
        "lineage": lineage,
        "note": "进程内计数，重启归零；不可用于多实例聚合或 SLO。",
    }


def _lineage_summary(db: Session) -> dict[str, int]:
    """血缘导入健康摘要（M6）：批次状态、边总量与陈旧候选数。

    stale 口径与 lineage health 端点默认一致（1440 分钟未观察到）。
    """
    runs = dict(db.execute(
        select(models.LineageImportRun.status,
               func.count(models.LineageImportRun.id))
        .group_by(models.LineageImportRun.status)
    ).all())
    edges = dict(db.execute(
        select(models.ExternalLineageEdge.active,
               func.count(models.ExternalLineageEdge.id))
        .group_by(models.ExternalLineageEdge.active)
    ).all())
    basis = func.coalesce(
        models.ExternalLineageEdge.last_seen_at,
        models.ExternalLineageEdge.observed_at,
    )
    cutoff = models.utcnow() - timedelta(minutes=1440)
    stale = db.scalar(
        select(func.count(models.ExternalLineageEdge.id)).where(
            models.ExternalLineageEdge.active.is_(True),
            or_(basis.is_(None), basis < cutoff),
        )
    ) or 0
    return {
        "import_runs_completed": runs.get("completed", 0),
        "import_runs_failed": runs.get("failed", 0),
        "active_edges": edges.get(True, 0),
        "inactive_edges": edges.get(False, 0),
        "stale_candidates": stale,
    }


def _prometheus_label(value: str) -> str:
    """Prometheus label 值转义（防注入/格式破坏）。"""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _metrics_gauges(db: Session) -> dict[str, int]:
    """M6 gauge 指标抓取值（查库；失败由调用方降级为 0）。"""
    latest = (
        select(
            models.ProjectGraphSnapshot.project_id.label("project_id"),
            func.max(models.ProjectGraphSnapshot.snapshot_no).label("max_no"),
        ).group_by(models.ProjectGraphSnapshot.project_id).subquery()
    )
    node_sum, edge_sum = db.execute(
        select(
            func.coalesce(func.sum(models.ProjectGraphSnapshot.node_count), 0),
            func.coalesce(func.sum(models.ProjectGraphSnapshot.edge_count), 0),
        ).join(
            latest,
            (models.ProjectGraphSnapshot.project_id == latest.c.project_id)
            & (models.ProjectGraphSnapshot.snapshot_no == latest.c.max_no),
        )
    ).one()
    snapshot_total = db.scalar(
        select(func.count(models.ProjectGraphSnapshot.id))
    ) or 0
    edges = dict(db.execute(
        select(models.ExternalLineageEdge.active,
               func.count(models.ExternalLineageEdge.id))
        .group_by(models.ExternalLineageEdge.active)
    ).all())
    return {
        "modeling_graph_snapshots_current": snapshot_total,
        "modeling_graph_snapshot_nodes": node_sum,
        "modeling_graph_snapshot_edges": edge_sum,
        "modeling_lineage_edges_active": edges.get(True, 0),
        "modeling_lineage_edges_inactive": edges.get(False, 0),
    }


@app.get("/metrics", response_class=PlainTextResponse)
def prometheus_metrics(
    request: Request, db: Session = Depends(get_db)
) -> str:
    """Prometheus 文本指标（V1.0 附加 modeling_* 业务指标族，M6）。

    demo 模式开放；api_key 模式需平台 admin（设计文档 3.3 授权矩阵）。
    gauge 抓取失败降级为 0，不影响 counter 渲染（观测不阻断业务）。
    """
    if settings.auth_mode == "api_key":
        identity = auth_mod.require_authenticated(request)
        if not identity.is_admin:
            raise ApiError(
                403, "PROJECT_ROLE_INSUFFICIENT",
                "/metrics 仅平台管理员可访问",
            )
    lines = [
        "# HELP http_requests_total Total HTTP requests",
        "# TYPE http_requests_total counter",
    ]
    for (method, route, cls), count in sorted(metrics.by_route.items()):
        lines.append(
            f'http_requests_total{{method="{method}",route="{route}",status_class="{cls}"}} {count}'
        )
    lines.extend([
        "# HELP process_uptime_seconds Process uptime",
        "# TYPE process_uptime_seconds gauge",
        f"process_uptime_seconds {time.time() - STARTED_AT:.1f}",
    ])

    def _counter(
        name: str, help_text: str, label_name: str, labels: dict[str, int]
    ) -> None:
        if not labels:
            return
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} counter")
        for label, count in sorted(labels.items()):
            lines.append(
                f'{name}{{{label_name}="{_prometheus_label(label)}"}} {count}'
            )

    _counter("modeling_auth_denied_total",
             "Auth denials by reason", "reason",
             metrics.auth_denied_by_reason)
    _counter("modeling_model_published_total",
             "Model publishes by layer", "layer",
             metrics.model_published_by_layer)
    _counter("modeling_graph_snapshot_created_total",
             "Graph snapshots created by type", "snapshot_type",
             metrics.graph_snapshot_created_by_type)
    _counter("modeling_lineage_import_runs_total",
             "Lineage import runs by status", "status",
             metrics.lineage_import_runs_by_status)
    _counter("modeling_lineage_import_records_total",
             "Lineage import edge records by result", "result",
             metrics.lineage_import_records_by_result)
    if metrics.lineage_import_conflicts_total:
        lines.append("# HELP modeling_lineage_import_conflicts_total "
                     "Lineage import run_id conflicts")
        lines.append("# TYPE modeling_lineage_import_conflicts_total counter")
        lines.append(
            f"modeling_lineage_import_conflicts_total "
            f"{metrics.lineage_import_conflicts_total}"
        )

    try:
        gauges = _metrics_gauges(db)
    except Exception:  # noqa: BLE001  观测降级：gauge 失败不阻断 counter
        logger.warning("metrics gauge 抓取失败，降级为 0",
                       exc_info=True)
        gauges = {}
    for name, value in sorted(gauges.items()):
        lines.append(f"# HELP {name} Gauge {name}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {value}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 外部血缘导入（V0.9.2）

_MAX_METADATA_KEYS = 50
_MAX_METADATA_BYTES = 8000


def _as_aware(dt: datetime | None) -> datetime | None:
    """SQLite 读回的 DateTime(timezone=True) 为 naive：按 UTC 补时区。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _lineage_field_codes(
    db: Session, model_ids: Iterable[int]
) -> dict[int, set[str]]:
    codes: dict[int, set[str]] = {mid: set() for mid in model_ids}
    for row in db.execute(
        select(models.ModelField.model_id, models.ModelField.field_code)
        .where(models.ModelField.model_id.in_(list(model_ids) or [-1]))
    ):
        codes.setdefault(row[0], set()).add(row[1])
    return codes


def _plan_lineage_import(
    db: Session,
    project: models.Project,
    payload: ExternalLineageImport,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """整批原子校验 + 变更计划（不写入）。errors 非空时调用方不得写入。

    校验顺序：扫描模式规则 → 模型目录解析 → 自环 → 字段存在性与配对 →
    metadata 限流 → 批内 source_ref 去重（同 scope）。
    """
    errors: list[dict[str, Any]] = []

    if payload.scan_mode == "incremental":
        if payload.complete_scan:
            errors.append({
                "row_index": None, "field": "complete_scan",
                "code": "SCAN_MODE_RULE_VIOLATION",
                "message": "incremental 模式要求 complete_scan=false；"
                           "完整扫描请使用 scan_mode=full_snapshot",
            })
        if not payload.edges:
            errors.append({
                "row_index": None, "field": "edges",
                "code": "EMPTY_INCREMENTAL_BATCH",
                "message": "incremental 模式至少需要 1 条边；"
                           "空批次请使用 full_snapshot",
            })
    else:  # full_snapshot
        if not payload.complete_scan:
            errors.append({
                "row_index": None, "field": "complete_scan",
                "code": "SCAN_MODE_RULE_VIOLATION",
                "message": "full_snapshot 模式必须显式提供 complete_scan=true",
            })
        if payload.scope_key == "default":
            errors.append({
                "row_index": None, "field": "scope_key",
                "code": "FULL_SNAPSHOT_SCOPE_REQUIRED",
                "message": "full_snapshot 必须使用非 default 的 scope_key，"
                           "避免误停用默认范围的历史边",
            })

    model_by_key = {
        m.model_key: m
        for m in db.scalars(
            select(models.DataModel).where(
                models.DataModel.project_id == project.id
            )
        )
    }
    seen_source_refs: set[str] = set()

    # 先解析全部模型端点，收集涉及的模型 id，再一次性取字段编码
    resolved: list[tuple[int, models.DataModel, models.DataModel]] = []
    for i, edge in enumerate(payload.edges):
        src = model_by_key.get(edge.source_model_key)
        if src is None:
            errors.append({
                "row_index": i, "field": "source_model_key",
                "code": "LINEAGE_MODEL_NOT_FOUND",
                "message": f"源模型 '{edge.source_model_key}' 不存在于项目"
                           f" {project.project_key}",
            })
        tgt = model_by_key.get(edge.target_model_key)
        if tgt is None:
            errors.append({
                "row_index": i, "field": "target_model_key",
                "code": "LINEAGE_MODEL_NOT_FOUND",
                "message": f"目标模型 '{edge.target_model_key}' 不存在于项目"
                           f" {project.project_key}",
            })
        if src is not None and tgt is not None:
            if src.id == tgt.id:
                errors.append({
                    "row_index": i, "field": "target_model_key",
                    "code": "LINEAGE_SELF_EDGE",
                    "message": f"边两端为同一模型 '{edge.source_model_key}'",
                })
            resolved.append((i, src, tgt))

    field_codes = _lineage_field_codes(
        db, {m.id for _, src, tgt in resolved for m in (src, tgt)}
    )
    for i, src, tgt in resolved:
        edge = payload.edges[i]
        for code in edge.source_fields:
            if code not in field_codes.get(src.id, set()):
                errors.append({
                    "row_index": i, "field": "source_fields",
                    "code": "LINEAGE_FIELD_NOT_FOUND",
                    "message": f"源字段 '{code}' 不存在于模型 "
                               f"{src.model_key}",
                })
        for code in edge.target_fields:
            if code not in field_codes.get(tgt.id, set()):
                errors.append({
                    "row_index": i, "field": "target_fields",
                    "code": "LINEAGE_FIELD_NOT_FOUND",
                    "message": f"目标字段 '{code}' 不存在于模型 "
                               f"{tgt.model_key}",
                })
        if len(edge.source_fields) != len(edge.target_fields):
            errors.append({
                "row_index": i, "field": "target_fields",
                "code": "LINEAGE_FIELD_PAIR_MISMATCH",
                "message": f"源字段 {len(edge.source_fields)} 个与目标字段 "
                           f"{len(edge.target_fields)} 个不成对",
            })
        if len(edge.metadata) > _MAX_METADATA_KEYS:
            errors.append({
                "row_index": i, "field": "metadata",
                "code": "LINEAGE_METADATA_TOO_LARGE",
                "message": f"metadata 最多 {_MAX_METADATA_KEYS} 个键，"
                           f"收到 {len(edge.metadata)} 个",
            })
        else:
            size = len(_canonical_json(edge.metadata).encode("utf-8"))
            if size > _MAX_METADATA_BYTES:
                errors.append({
                    "row_index": i, "field": "metadata",
                    "code": "LINEAGE_METADATA_TOO_LARGE",
                    "message": f"metadata 序列化后 {size} 字节，超过 "
                               f"{_MAX_METADATA_BYTES} 字节上限",
                })
        if edge.source_ref in seen_source_refs:
            errors.append({
                "row_index": i, "field": "source_ref",
                "code": "LINEAGE_SOURCE_REF_DUPLICATE",
                "message": f"批内重复 source_ref '{edge.source_ref}'"
                           f"（同 scope 内必须唯一）",
            })
        seen_source_refs.add(edge.source_ref)

    existing_edges = {
        e.source_ref: e
        for e in db.scalars(
            select(models.ExternalLineageEdge).where(
                models.ExternalLineageEdge.project_id == project.id,
                models.ExternalLineageEdge.source_system == payload.source_system,
                models.ExternalLineageEdge.scope_key == payload.scope_key,
            )
        )
    }
    batch_refs = {edge.source_ref for edge in payload.edges}
    to_deactivate = [
        e for ref, e in existing_edges.items()
        if e.active and ref not in batch_refs
    ] if payload.scan_mode == "full_snapshot" else []
    inserted = sum(1 for ref in batch_refs if ref not in existing_edges)
    updated = sum(1 for ref in batch_refs if ref in existing_edges)

    # V1.0 M5 停用人工确认门槛（在任何写入前计算）：连接器一次空扫描或
    # 配置错误不应悄悄停用大量血缘边；超阈值必须 confirm_deactivation=true。
    active_existing = sum(
        1 for e in existing_edges.values() if e.active
    )
    planned_deactivate_ratio = (
        round(len(to_deactivate) / max(1, active_existing), 4)
        if payload.scan_mode == "full_snapshot" else 0.0
    )
    confirmation_required = bool(
        payload.scan_mode == "full_snapshot"
        and planned_deactivate_ratio
        > settings.full_snapshot_deactivate_ratio_threshold
        and not payload.confirm_deactivation
    )

    plan = {
        "received": len(payload.edges),
        "inserted": inserted,
        "updated": updated,
        "deactivated": len(to_deactivate),
        "active_existing": active_existing,
        "planned_deactivate_ratio": planned_deactivate_ratio,
        "confirmation_required": confirmation_required,
    }
    return plan, errors


def _lineage_import_payload_hash(payload: ExternalLineageImport) -> str:
    return hashlib.sha256(
        _canonical_json(payload.model_dump(mode="json")).encode("utf-8")
    ).hexdigest()


def _import_result(
    run: models.LineageImportRun | None,
    replayed: bool,
    request_id: str,
) -> dict[str, Any]:
    if run is None:
        return {"replayed": replayed, "request_id": request_id}
    # V1.0 M5：从 result_json 还原停用比例预告与确认门槛（回执可见，
    # 兼容 V0.9.2 历史批次无该键的情况）
    plan = json.loads(run.result_json or "{}")
    return {
        "replayed": replayed,
        "id": run.id,
        "run_id": run.run_id,
        "source_system": run.source_system,
        "scope_key": run.scope_key,
        "scan_mode": run.scan_mode,
        "complete_scan": run.complete_scan,
        "status": run.status,
        "received": run.received_count,
        "inserted": run.inserted_count,
        "updated": run.updated_count,
        "deactivated": run.deactivated_count,
        "planned_deactivate_ratio": plan.get("planned_deactivate_ratio", 0.0),
        "confirmation_required": plan.get("confirmation_required", False),
        "attempt_count": run.attempt_count,
        "failure_count": run.failure_count,
        "deactivation_confirmed": run.deactivation_confirmed,
        "request_id": run.request_id or request_id,
    }


@app.post("/api/projects/{project_id}/lineage/preview",
          dependencies=[Depends(auth_mod.require_project_role("editor"))])
def preview_lineage_import(
    project_id: int,
    payload: ExternalLineageImport,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """只读预检：估算新增/更新/停用数量，不写入边、批次或事件。

    估算不是锁：真正提交时须重新校验。
    """
    project = get_project(db, project_id)
    plan, errors = _plan_lineage_import(db, project, payload)
    if errors:
        raise ApiError(
            422,
            "LINEAGE_IMPORT_VALIDATION_FAILED",
            f"批次校验失败（{len(errors)} 条问题），未写入任何数据",
            {"errors": errors[:50], "error_count": len(errors)},
        )
    return {"writes_performed": False, **plan}


def _execute_lineage_import(
    db: Session,
    project: models.Project,
    payload: ExternalLineageImport,
    actor: str,
    request_id: str,
    extra_result: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    """外部血缘导入事务（replay 检查 → 原子校验 → upsert → run 落库）。

    V1.0 从 import_lineage 抽取，供 dbt manifest 连接器复用（设计文档
    4.3.1「复用 _plan_lineage_import 与导入事务」）。返回 (响应体,
    状态码)；409/422 以 ApiError 抛出。extra_result 合入 run 的
    result_json 与响应体（连接器信息、跳过明细等）。
    """
    payload_hash = _lineage_import_payload_hash(payload)

    run = db.scalar(
        select(models.LineageImportRun).where(
            models.LineageImportRun.project_id == project.id,
            models.LineageImportRun.source_system == payload.source_system,
            models.LineageImportRun.scope_key == payload.scope_key,
            models.LineageImportRun.run_id == payload.run_id,
        )
    )
    if run is not None and run.status == "completed":
        if run.payload_hash == payload_hash:
            result = _import_result(run, replayed=True, request_id=request_id)
            saved = json.loads(run.result_json or "{}")
            if "connector" in saved:
                result["connector"] = saved["connector"]
            return result, 200
        metrics.record_lineage_import_conflict()
        raise conflict(
            "LINEAGE_RUN_ID_REUSED",
            f"批次 '{payload.run_id}' 已被不同 payload 使用"
            f"（scope={payload.scope_key}），请为新内容生成新 run_id",
            {"run_id": payload.run_id, "scope_key": payload.scope_key},
        )

    plan, errors = _plan_lineage_import(db, project, payload)
    if errors:
        if run is None:
            run = models.LineageImportRun(
                project_id=project.id,
                source_system=payload.source_system,
                scope_key=payload.scope_key,
                run_id=payload.run_id,
                scan_mode=payload.scan_mode,
                complete_scan=payload.complete_scan,
                payload_hash=payload_hash,
                status="failed",
                received_count=len(payload.edges),
                attempt_count=1,
                failure_count=1,
                error_summary_json=json.dumps(
                    errors[:50], ensure_ascii=False
                ),
                result_json="{}",
                actor=actor,
                request_id=request_id,
            )
            db.add(run)
        else:
            run.attempt_count += 1
            run.failure_count += 1
            run.scan_mode = payload.scan_mode
            run.complete_scan = payload.complete_scan
            run.payload_hash = payload_hash
            run.error_summary_json = json.dumps(
                errors[:50], ensure_ascii=False
            )
            run.actor = actor
            run.request_id = request_id
        db.flush()
        db.commit()  # 失败批次落库（校验为只读，此前无其他写入）
        metrics.record_lineage_import_run("failed")
        raise ApiError(
            422,
            "LINEAGE_IMPORT_VALIDATION_FAILED",
            f"批次校验失败（{len(errors)} 条问题），未写入任何边",
            {"errors": errors[:50], "error_count": len(errors)},
        )

    # V1.0 M5 停用人工确认门槛（校验通过后、任何写入前）：超阈值未确认
    # → 422，落 failed 批次（与校验失败同一哲学：同 payload 可再次尝试）。
    if plan["confirmation_required"]:
        threshold = settings.full_snapshot_deactivate_ratio_threshold
        if run is None:
            run = models.LineageImportRun(
                project_id=project.id,
                source_system=payload.source_system,
                scope_key=payload.scope_key,
                run_id=payload.run_id,
                scan_mode=payload.scan_mode,
                complete_scan=payload.complete_scan,
                payload_hash=payload_hash,
                status="failed",
                received_count=len(payload.edges),
                attempt_count=1,
                failure_count=1,
                error_summary_json=json.dumps([{
                    "code": "LINEAGE_DEACTIVATION_CONFIRM_REQUIRED",
                    "planned_deactivated": plan["deactivated"],
                    "ratio": plan["planned_deactivate_ratio"],
                    "threshold": threshold,
                }], ensure_ascii=False),
                result_json="{}",
                deactivation_confirmed=payload.confirm_deactivation,
                actor=actor,
                request_id=request_id,
            )
            db.add(run)
        else:
            run.attempt_count += 1
            run.failure_count += 1
            run.scan_mode = payload.scan_mode
            run.complete_scan = payload.complete_scan
            run.payload_hash = payload_hash
            run.error_summary_json = json.dumps([{
                "code": "LINEAGE_DEACTIVATION_CONFIRM_REQUIRED",
                "planned_deactivated": plan["deactivated"],
                "ratio": plan["planned_deactivate_ratio"],
                "threshold": threshold,
            }], ensure_ascii=False)
            run.deactivation_confirmed = payload.confirm_deactivation
            run.actor = actor
            run.request_id = request_id
        db.flush()
        db.commit()
        metrics.record_lineage_import_run("failed")
        raise ApiError(
            422,
            "LINEAGE_DEACTIVATION_CONFIRM_REQUIRED",
            f"full_snapshot 计划停用 {plan['deactivated']} 条边，占现有 "
            f"active 边比例 {plan['planned_deactivate_ratio']}，超过阈值 "
            f"{threshold}；请确认后带 confirm_deactivation=true 重新提交",
            {"planned_deactivated": plan["deactivated"],
             "active_existing": plan["active_existing"],
             "ratio": plan["planned_deactivate_ratio"],
             "threshold": threshold,
             "scope_key": payload.scope_key},
        )

    # --- 校验通过：同一事务内执行变更 ---
    if payload.scan_mode == "full_snapshot":
        existing_edges = {
            e.source_ref: e
            for e in db.scalars(
                select(models.ExternalLineageEdge).where(
                    models.ExternalLineageEdge.project_id == project.id,
                    models.ExternalLineageEdge.source_system == payload.source_system,
                    models.ExternalLineageEdge.scope_key == payload.scope_key,
                )
            )
        }
        batch_refs = {edge.source_ref for edge in payload.edges}
        deactivated = 0
        for ref, e in existing_edges.items():
            if e.active and ref not in batch_refs:
                e.active = False
                deactivated += 1
                record_event(
                    db, project.id, "lineage.edge.deactivated", actor,
                    {"edge_id": e.id, "source_ref": e.source_ref,
                     "reason": "full_snapshot_not_in_batch",
                     "scope_key": payload.scope_key},
                    request_id,
                )
        plan["deactivated"] = deactivated

    model_by_key = {
        m.model_key: m
        for m in db.scalars(
            select(models.DataModel).where(
                models.DataModel.project_id == project.id
            )
        )
    }
    now = models.utcnow()
    batch_observed = payload.observed_at or now
    for edge in payload.edges:
        src = model_by_key[edge.source_model_key]
        tgt = model_by_key[edge.target_model_key]
        existing = db.scalar(
            select(models.ExternalLineageEdge).where(
                models.ExternalLineageEdge.project_id == project.id,
                models.ExternalLineageEdge.source_system == payload.source_system,
                models.ExternalLineageEdge.scope_key == payload.scope_key,
                models.ExternalLineageEdge.source_ref == edge.source_ref,
            )
        )
        observed = edge.observed_at or batch_observed
        if existing is None:
            db.add(models.ExternalLineageEdge(
                project_id=project.id,
                source_system=payload.source_system,
                scope_key=payload.scope_key,
                source_ref=edge.source_ref,
                source_model_id=src.id,
                target_model_id=tgt.id,
                job_key=edge.job_key,
                lineage_type=edge.lineage_type,
                transformation_type=edge.transformation_type,
                source_fields_json=json.dumps(
                    edge.source_fields, ensure_ascii=False
                ),
                target_fields_json=json.dumps(
                    edge.target_fields, ensure_ascii=False
                ),
                confidence=edge.confidence,
                active=True,
                observed_at=observed,
                last_seen_at=now,
                metadata_json=_canonical_json(edge.metadata),
            ))
        else:
            existing.source_model_id = src.id
            existing.target_model_id = tgt.id
            existing.job_key = edge.job_key
            existing.lineage_type = edge.lineage_type
            existing.transformation_type = edge.transformation_type
            existing.source_fields_json = json.dumps(
                edge.source_fields, ensure_ascii=False
            )
            existing.target_fields_json = json.dumps(
                edge.target_fields, ensure_ascii=False
            )
            existing.confidence = edge.confidence
            existing.active = True
            existing.observed_at = observed
            existing.last_seen_at = now
            existing.metadata_json = _canonical_json(edge.metadata)

    result_json = dict(plan)
    if extra_result:
        result_json.update(extra_result)
    if run is None:
        run = models.LineageImportRun(
            project_id=project.id,
            source_system=payload.source_system,
            scope_key=payload.scope_key,
            run_id=payload.run_id,
            scan_mode=payload.scan_mode,
            complete_scan=payload.complete_scan,
            payload_hash=payload_hash,
            status="completed",
            received_count=len(payload.edges),
            inserted_count=plan["inserted"],
            updated_count=plan["updated"],
            deactivated_count=plan["deactivated"],
            attempt_count=1,
            failure_count=0,
            result_json=json.dumps(result_json, ensure_ascii=False),
            deactivation_confirmed=payload.confirm_deactivation,
            actor=actor,
            request_id=request_id,
        )
        db.add(run)
    else:
        # 之前失败的批次重试成功：保留 attempt/failure 历史
        run.attempt_count += 1
        run.scan_mode = payload.scan_mode
        run.complete_scan = payload.complete_scan
        run.payload_hash = payload_hash
        run.status = "completed"
        run.received_count = len(payload.edges)
        run.inserted_count = plan["inserted"]
        run.updated_count = plan["updated"]
        run.deactivated_count = plan["deactivated"]
        run.error_summary_json = "[]"
        run.result_json = json.dumps(result_json, ensure_ascii=False)
        run.deactivation_confirmed = payload.confirm_deactivation
        run.actor = actor
        run.request_id = request_id
    db.flush()
    record_event(
        db, project.id, "lineage.import.completed", actor,
        {"source_system": payload.source_system, "run_id": payload.run_id,
         "scope_key": payload.scope_key, "scan_mode": payload.scan_mode,
         "complete_scan": payload.complete_scan, **plan},
        request_id,
    )
    record_audit(
        db, "lineage_import", f"{payload.source_system}/{payload.run_id}",
        "lineage.import.completed", actor, "success", request_id,
    )
    db.commit()
    metrics.record_lineage_import_run("completed")
    metrics.record_lineage_import_records(
        plan["inserted"], plan["updated"], plan["deactivated"]
    )
    result = _import_result(run, replayed=False, request_id=request_id)
    if extra_result:
        result.update(extra_result)
    return result, 201


@app.post("/api/projects/{project_id}/lineage/import", status_code=201,
          dependencies=[Depends(auth_mod.require_project_role("editor"))])
def import_lineage(
    project_id: int,
    payload: ExternalLineageImport,
    request: Request,
    db: Session = Depends(get_db),
    response: Response = None,  # noqa: ANN001
) -> dict[str, Any]:
    """外部血缘批次导入：原子校验、幂等 upsert、整批失败不留痕。

    - 首次合法提交：201，创建边与 completed 批次
    - 同身份同 payload 重放：200，replayed=true，不重复写入
    - 同身份不同 payload：409 LINEAGE_RUN_ID_REUSED
    - 校验失败：422，记录 failed 批次（attempt/failure 计数），
      不写入任何边；同 payload 可再次尝试
    """
    actor = request.state.actor
    request_id = request.state.request_id
    project = get_project(db, project_id)
    result, status = _execute_lineage_import(
        db, project, payload, actor, request_id
    )
    if response is not None and status != 201:
        response.status_code = status
    return result


# ---------------------------------------------------------------- dbt manifest 连接器（V1.0 M3）

_DBT_CONNECTOR_NAME = "dbt-manifest"
_DBT_CONNECTOR_VERSION = "1.0.0"


def _dbt_collect_nodes(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """收集参与血缘推导的 dbt 节点：nodes 中 resource_type=model +
    sources 全量；忽略 test/seed/analysis/snapshot/operation/exposure。"""
    collected: dict[str, dict[str, Any]] = {}
    for uid, node in (manifest.get("nodes") or {}).items():
        if isinstance(node, dict) and node.get("resource_type") == "model":
            collected[uid] = node
    for uid, src in (manifest.get("sources") or {}).items():
        if isinstance(src, dict):
            collected[uid] = src
    return collected


def _dbt_node_metadata(
    metadata: dict[str, Any], node: dict[str, Any]
) -> dict[str, Any]:
    """边元数据白名单（设计文档 4.3.1）：仅 6 个键，缺省跳过。"""
    out: dict[str, Any] = {}
    for key, value in (
        ("dbt_schema_version", metadata.get("dbt_schema_version")),
        ("project_name", metadata.get("project_name")),
        ("node_materialized", node.get("materialized")),
        ("node_database", node.get("database")),
        ("node_schema", node.get("schema")),
        ("node_alias", node.get("alias")),
    ):
        if value not in (None, ""):
            out[key] = value
    return out


def _dbt_derive_edges(
    manifest: dict[str, Any], collected: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """按 parent_map 推导边：对每个 model/source 节点 N，父节点 P（仅
    model/source 类）各产生一条 P→N 边；source_ref 在 manifest 内稳定，
    支持 upsert 与幂等重放。"""
    metadata = manifest.get("metadata") or {}
    edges: list[dict[str, Any]] = []
    seen: set[str] = set()
    for target_uid, parents in (manifest.get("parent_map") or {}).items():
        target = collected.get(target_uid)
        if target is None or not isinstance(parents, list):
            continue
        for source_uid in parents:
            source = collected.get(source_uid)
            if source is None or source_uid == target_uid:
                continue
            source_ref = f"dbt:{source_uid}=>{target_uid}"
            if source_ref in seen:
                continue
            seen.add(source_ref)
            edges.append({
                "source_ref": source_ref,
                "source_model_key": source.get("name", ""),
                "target_model_key": target.get("name", ""),
                "lineage_type": "dbt_dependency",
                "transformation_type": "sql",
                "confidence": 100,
                "job_key": f"dbt:{metadata.get('project_name', '')}",
                "metadata": _dbt_node_metadata(metadata, target),
                "source_unique_id": source_uid,
                "target_unique_id": target_uid,
            })
    edges.sort(key=lambda e: e["source_ref"])
    return edges


@app.post("/api/projects/{project_id}/lineage/import/dbt-manifest",
          status_code=201,
          dependencies=[Depends(auth_mod.require_project_role("editor"))])
def import_dbt_manifest(
    project_id: int,
    payload: DbtManifestImport,
    request: Request,
    db: Session = Depends(get_db),
    response: Response = None,  # noqa: ANN001
) -> dict[str, Any]:
    """dbt manifest 参考连接器（M3）：从 parent_map 推导模型级依赖边，
    复用外部血缘导入事务（身份键 source_ref 与幂等重放语义一致）。

    - 首次合法提交 201；同 run_id 同 payload 重放 200 replayed=true；
      不同 payload 409 LINEAGE_RUN_ID_REUSED
    - manifest 结构不符 422 DBT_MANIFEST_INVALID；体积/边数超限 422
      DBT_MANIFEST_TOO_LARGE；未匹配节点默认 422 DBT_NODES_UNMATCHED，
      skip_unmatched=true 时跳过并计数
    """
    actor = request.state.actor
    request_id = request.state.request_id
    project = get_project(db, project_id)
    manifest = payload.manifest

    # 1. 结构校验（metadata/nodes/sources/parent_map 必填，schema 版本缺失）
    missing = [k for k in ("metadata", "nodes", "sources", "parent_map")
               if not isinstance(manifest.get(k), dict)]
    if missing:
        raise ApiError(
            422, "DBT_MANIFEST_INVALID",
            f"manifest 缺少必需对象字段：{', '.join(missing)}",
            {"missing_fields": missing},
        )
    metadata = manifest["metadata"]
    if not metadata.get("dbt_schema_version"):
        raise ApiError(
            422, "DBT_MANIFEST_INVALID",
            "manifest.metadata.dbt_schema_version 缺失",
            {"missing_fields": ["metadata.dbt_schema_version"]},
        )
    project_name = str(metadata.get("project_name") or "").strip()
    if not project_name:
        raise ApiError(
            422, "DBT_MANIFEST_INVALID",
            "manifest.metadata.project_name 缺失",
            {"missing_fields": ["metadata.project_name"]},
        )

    # 2. 体积上限（配置 DBT_MANIFEST_MAX_BYTES，默认 8 MiB）
    size = len(_canonical_json(manifest).encode("utf-8"))
    if size > settings.dbt_manifest_max_bytes:
        raise ApiError(
            422, "DBT_MANIFEST_TOO_LARGE",
            f"manifest 序列化后 {size} 字节，超过 "
            f"{settings.dbt_manifest_max_bytes} 字节上限；请按 scope/文件夹拆批",
            {"size_bytes": size, "max_bytes": settings.dbt_manifest_max_bytes},
        )

    # 3. 推导边 + 边数上限（配置 DBT_MANIFEST_MAX_EDGES，默认 1000）
    collected = _dbt_collect_nodes(manifest)
    nodes_scanned = sum(
        1 for n in collected.values() if n.get("resource_type") == "model"
    )
    sources_scanned = len(collected) - nodes_scanned
    derived = _dbt_derive_edges(manifest, collected)
    if len(derived) > settings.dbt_manifest_max_edges:
        raise ApiError(
            422, "DBT_MANIFEST_TOO_LARGE",
            f"推导边数 {len(derived)} 超过 {settings.dbt_manifest_max_edges} "
            "上限；请按 scope/文件夹拆批",
            {"edges_built": len(derived),
             "max_edges": settings.dbt_manifest_max_edges},
        )

    # 4. model_key 匹配（平台必须先以相同 model_key 登记模型）
    model_by_key = {
        m.model_key: m.id
        for m in db.scalars(
            select(models.DataModel).where(
                models.DataModel.project_id == project.id
            )
        )
    }
    matched: list[dict[str, Any]] = []
    unmatched_keys: list[str] = []
    skipped_details: list[dict[str, Any]] = []
    for edge in derived:
        src_ok = edge["source_model_key"] in model_by_key
        tgt_ok = edge["target_model_key"] in model_by_key
        if src_ok and tgt_ok:
            matched.append(edge)
            continue
        if not payload.skip_unmatched:
            for key, ok, uid in (
                (edge["source_model_key"], src_ok, edge["source_unique_id"]),
                (edge["target_model_key"], tgt_ok, edge["target_unique_id"]),
            ):
                if not ok and key not in unmatched_keys:
                    unmatched_keys.append(key)
            continue
        skipped_details.append({
            "source_ref": edge["source_ref"],
            "source_model_key": edge["source_model_key"],
            "target_model_key": edge["target_model_key"],
            "source_matched": src_ok, "target_matched": tgt_ok,
        })
    if unmatched_keys:
        raise ApiError(
            422, "DBT_NODES_UNMATCHED",
            f"{len(unmatched_keys)} 个 dbt 节点的 model_key 未在项目 "
            f"{project.project_key} 登记；请先以相同 model_key 创建模型，"
            "或使用 skip_unmatched=true 跳过",
            {"unmatched_count": len(unmatched_keys),
             "unmatched_sample": sorted(unmatched_keys)[:50]},
        )

    # 5. 组装 ExternalLineageImport（强制 incremental，不停用任何边）
    scope_key = payload.scope_key or f"dbt:{project_name}"
    source_system = payload.source_system or "dbt"
    received_at = models.utcnow()
    run_id = payload.run_id or (
        f"dbt-manifest:{scope_key}:{received_at.isoformat()}"
    )
    connector_info = {
        "name": _DBT_CONNECTOR_NAME,
        "version": _DBT_CONNECTOR_VERSION,
        "manifest_schema_version": str(
            metadata.get("dbt_schema_version") or ""
        ),
        "project_name": project_name,
        "nodes_scanned": nodes_scanned,
        "sources_scanned": sources_scanned,
        "edges_built": len(derived),
        "skipped_unmatched": len(skipped_details),
    }
    import_payload = ExternalLineageImport(
        source_system=source_system,
        run_id=run_id,
        observed_at=payload.observed_at,
        scan_mode="incremental",
        scope_key=scope_key,
        complete_scan=False,
        edges=[
            ExternalLineageEdgeIn(
                source_ref=e["source_ref"],
                source_model_key=e["source_model_key"],
                target_model_key=e["target_model_key"],
                lineage_type=e["lineage_type"],
                transformation_type=e["transformation_type"],
                confidence=e["confidence"],
                job_key=e["job_key"],
                metadata=e["metadata"],
                observed_at=payload.observed_at,
            )
            for e in matched
        ],
    )

    # 6. 复用导入事务；连接器信息与跳过明细落 run.result_json
    extra: dict[str, Any] = {"connector": connector_info}
    if skipped_details:
        extra["skipped_unmatched_details"] = skipped_details[:200]
    result, status = _execute_lineage_import(
        db, project, import_payload, actor, request_id, extra_result=extra
    )
    if response is not None and status != 201:
        response.status_code = status
    return result


@app.get("/api/projects/{project_id}/lineage/edges",
         dependencies=[Depends(auth_mod.require_project_role("viewer"))])
def list_external_lineage_edges(
    project_id: int,
    active_only: bool = Query(default=True),
    limit: int = Query(default=500, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    source_system: str | None = Query(default=None, max_length=64),
    scope_key: str | None = Query(default=None, max_length=128),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """查询外部边（数组兼容形状；分页信息在 X-Total-Count/X-Limit/X-Offset）。"""
    get_project(db, project_id)
    conditions = [models.ExternalLineageEdge.project_id == project_id]
    if active_only:
        conditions.append(models.ExternalLineageEdge.active.is_(True))
    if source_system:
        conditions.append(
            models.ExternalLineageEdge.source_system == source_system
        )
    if scope_key:
        conditions.append(models.ExternalLineageEdge.scope_key == scope_key)

    total = db.scalar(
        select(func.count(models.ExternalLineageEdge.id)).where(*conditions)
    ) or 0
    rows = list(db.scalars(
        select(models.ExternalLineageEdge)
        .where(*conditions)
        .order_by(models.ExternalLineageEdge.id)
        .limit(limit)
        .offset(offset)
    ))
    return JSONResponse(
        content=[_external_edge_dict(e) for e in rows],
        headers={
            "X-Total-Count": str(total),
            "X-Limit": str(limit),
            "X-Offset": str(offset),
        },
    )


@app.post("/api/projects/{project_id}/lineage/edges/{edge_id}/deactivate",
          dependencies=[Depends(auth_mod.require_project_role("editor"))])
def deactivate_external_lineage_edge(
    project_id: int, edge_id: int, request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """显式停用外部观测边（软停用，保留可追溯；幂等）。"""
    actor = request.state.actor
    request_id = request.state.request_id
    get_project(db, project_id)
    edge = db.get(models.ExternalLineageEdge, edge_id)
    if edge is None or edge.project_id != project_id:
        raise not_found(
            "EXTERNAL_LINEAGE_EDGE_NOT_FOUND",
            f"项目 {project_id} 下不存在外部边 {edge_id}",
            {"edge_id": edge_id},
        )
    if edge.active:
        edge.active = False
        db.flush()
        record_event(
            db, project_id, "lineage.edge.deactivated", actor,
            {"edge_id": edge.id, "source_ref": edge.source_ref,
             "source_system": edge.source_system,
             "scope_key": edge.scope_key},
            request_id,
        )
        record_audit(
            db, "external_lineage_edge", str(edge.id),
            "lineage.edge.deactivated", actor, "success", request_id,
        )
    db.commit()
    return _external_edge_dict(edge)


@app.get("/api/projects/{project_id}/lineage/import-runs",
         dependencies=[Depends(auth_mod.require_project_role("viewer"))])
def list_lineage_import_runs(
    project_id: int,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    source_system: str | None = Query(default=None, max_length=64),
    status: str | None = Query(
        default=None, pattern="^(completed|failed)$"
    ),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """查询导入批次：状态、次数、错误摘要与请求 ID。"""
    get_project(db, project_id)
    conditions = [models.LineageImportRun.project_id == project_id]
    if source_system:
        conditions.append(models.LineageImportRun.source_system == source_system)
    if status:
        conditions.append(models.LineageImportRun.status == status)
    runs = list(db.scalars(
        select(models.LineageImportRun)
        .where(*conditions)
        .order_by(models.LineageImportRun.created_at.desc(),
                  models.LineageImportRun.id.desc())
        .limit(limit)
        .offset(offset)
    ))
    return [_import_run_dict(r) for r in runs]


@app.get("/api/projects/{project_id}/lineage/health",
         dependencies=[Depends(auth_mod.require_project_role("viewer"))])
def lineage_health(
    project_id: int,
    stale_after_minutes: int = Query(default=1440, ge=1, le=525600),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """按 source_system + scope 汇总边与运行健康度（只读）。

    V1.0 M6 性能修复（设计文档 4.6.3）：边统计改 SQL 聚合
    （GROUP BY source_system, scope_key, active），不再全量加载边到
    内存；批次仍取最近 200 条计算 last_successful/last_failed/
    consecutive_failures（与原实现语义一致）。响应形状不变。
    """
    get_project(db, project_id)
    now = models.utcnow()
    cutoff = now - timedelta(minutes=stale_after_minutes)
    basis = func.coalesce(
        models.ExternalLineageEdge.last_seen_at,
        models.ExternalLineageEdge.observed_at,
    )

    # 边统计：按 (source_system, scope_key, active) 聚合计数与最新观察时间
    edge_rows = db.execute(
        select(
            models.ExternalLineageEdge.source_system,
            models.ExternalLineageEdge.scope_key,
            models.ExternalLineageEdge.active,
            func.count(models.ExternalLineageEdge.id),
            func.max(basis),
        )
        .where(models.ExternalLineageEdge.project_id == project_id)
        .group_by(
            models.ExternalLineageEdge.source_system,
            models.ExternalLineageEdge.scope_key,
            models.ExternalLineageEdge.active,
        )
    ).all()
    # stale 候选：active 且（从未观察到 或 早于 cutoff）
    stale_rows = {
        (source_system, scope_key): count
        for source_system, scope_key, count in db.execute(
            select(
                models.ExternalLineageEdge.source_system,
                models.ExternalLineageEdge.scope_key,
                func.count(models.ExternalLineageEdge.id),
            )
            .where(
                models.ExternalLineageEdge.project_id == project_id,
                models.ExternalLineageEdge.active.is_(True),
                or_(basis.is_(None), basis < cutoff),
            )
            .group_by(
                models.ExternalLineageEdge.source_system,
                models.ExternalLineageEdge.scope_key,
            )
        )
    }

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for source_system, scope_key, active, count, latest in edge_rows:
        g = groups.setdefault((source_system, scope_key), {
            "source_system": source_system,
            "scope_key": scope_key,
            "active_edges": 0,
            "inactive_edges": 0,
            "latest_observed_at": None,
            "stale_candidates": 0,
            "last_successful_run_at": None,
            "last_failed_run_at": None,
            "consecutive_failures": 0,
        })
        if active:
            g["active_edges"] += count
        else:
            g["inactive_edges"] += count
        aware_latest = _as_aware(latest)
        if aware_latest is not None and (
            g["latest_observed_at"] is None
            or aware_latest > g["latest_observed_at"]
        ):
            g["latest_observed_at"] = aware_latest

    for (source_system, scope_key), count in stale_rows.items():
        g = groups.setdefault((source_system, scope_key), {
            "source_system": source_system,
            "scope_key": scope_key,
            "active_edges": 0,
            "inactive_edges": 0,
            "latest_observed_at": None,
            "stale_candidates": 0,
            "last_successful_run_at": None,
            "last_failed_run_at": None,
            "consecutive_failures": 0,
        })
        g["stale_candidates"] += count

    # 批次：仅取最近 200 条（created_at desc, id desc），语义与旧实现一致
    runs = list(db.scalars(
        select(models.LineageImportRun)
        .where(models.LineageImportRun.project_id == project_id)
        .order_by(models.LineageImportRun.created_at.desc(),
                  models.LineageImportRun.id.desc())
        .limit(200)
    ))
    for r in runs:
        g = groups.setdefault((r.source_system, r.scope_key), {
            "source_system": r.source_system,
            "scope_key": r.scope_key,
            "active_edges": 0,
            "inactive_edges": 0,
            "latest_observed_at": None,
            "stale_candidates": 0,
            "last_successful_run_at": None,
            "last_failed_run_at": None,
            "consecutive_failures": 0,
        })
        if r.status == "completed":
            if g["last_successful_run_at"] is None:
                g["last_successful_run_at"] = r.created_at
        else:
            if g["last_failed_run_at"] is None:
                g["last_failed_run_at"] = r.created_at
            if g["last_successful_run_at"] is None or (
                r.created_at > g["last_successful_run_at"]
            ):
                g["consecutive_failures"] += 1

    result_groups = []
    for g in groups.values():
        g["latest_observed_at"] = iso(g["latest_observed_at"])
        g["last_successful_run_at"] = iso(g["last_successful_run_at"])
        g["last_failed_run_at"] = iso(g["last_failed_run_at"])
        result_groups.append(g)

    return {
        "project_id": project_id,
        "stale_after_minutes": stale_after_minutes,
        "groups": result_groups,
        "total_active_edges": sum(g["active_edges"] for g in result_groups),
        "total_stale_candidates": sum(g["stale_candidates"] for g in result_groups),
    }


@app.get("/api/projects/{project_id}/lineage/stale-candidates",
         dependencies=[Depends(auth_mod.require_project_role("viewer"))])
def lineage_stale_candidates(
    project_id: int,
    stale_after_minutes: int = Query(default=1440, ge=1, le=525600),
    source_system: str | None = Query(default=None, max_length=64),
    scope_key: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """长时间未观察到的 active 边候选（只读；不自动停用）。

    阈值外仍应核验该 scope 最近扫描是否成功且完整，再决定停用。
    """
    get_project(db, project_id)
    now = models.utcnow()
    conditions = [models.ExternalLineageEdge.project_id == project_id,
                  models.ExternalLineageEdge.active.is_(True)]
    if source_system:
        conditions.append(
            models.ExternalLineageEdge.source_system == source_system
        )
    if scope_key:
        conditions.append(models.ExternalLineageEdge.scope_key == scope_key)
    candidates = []
    for e in db.scalars(
        select(models.ExternalLineageEdge).where(*conditions)
        .order_by(models.ExternalLineageEdge.id).limit(limit)
    ):
        basis = _as_aware(e.last_seen_at or e.observed_at)
        age_minutes = (
            (now - basis).total_seconds() / 60 if basis is not None else None
        )
        if age_minutes is None or age_minutes > stale_after_minutes:
            candidates.append({
                "edge_id": e.id,
                "source_system": e.source_system,
                "scope_key": e.scope_key,
                "source_ref": e.source_ref,
                "source_model_id": e.source_model_id,
                "target_model_id": e.target_model_id,
                "lineage_type": e.lineage_type,
                "observed_at": iso(e.observed_at),
                "last_seen_at": iso(e.last_seen_at),
                "age_minutes": round(age_minutes, 1) if age_minutes else None,
            })
    return candidates


# ---------------------------------------------------------------- V1.0 认证与项目成员（M1/M2）


def api_key_dict(k: models.ApiKey) -> dict[str, Any]:
    """密钥视图（不含 secret/hash）。"""
    return {
        "key_id": k.key_id,
        "name": k.name,
        "status": k.status,
        "created_at": iso(k.created_at),
        "last_used_at": iso(k.last_used_at),
        "expires_at": iso(k.expires_at),
    }


def _current_user(db: Session, request: Request) -> models.User:
    """取当前认证主体。

    V1.3：携带有效 Bearer 令牌（两种模式）→ 真实主体；demo 模式匿名
    → 回落演示管理员（幂等播种，V0.9.2 兼容）。
    """
    identity = getattr(request.state, "identity", None)
    if identity is not None:
        user = db.get(models.User, identity.user_id)
        if user is None:
            raise ApiError(401, "AUTH_REQUIRED", "身份主体不存在，请重新引导")
        return user
    if settings.auth_mode == "demo":
        return auth_mod.ensure_demo_admin(db)
    raise ApiError(401, "AUTH_REQUIRED", "缺少身份信息")


@app.get("/api/auth/me")
def auth_me(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    """当前身份、项目角色与有效能力（demo 模式返回演示管理员）。

    V1.2 增强：status 字段 + capabilities（平台级/项目级能力族），
    供前端"按权限显隐"与用户自解释（设计文档第 3 章）。
    """
    user = _current_user(db, request)
    rows = db.execute(
        select(models.ProjectMember, models.Project)
        .join(models.Project, models.Project.id == models.ProjectMember.project_id)
        .where(models.ProjectMember.user_id == user.id)
        .order_by(models.ProjectMember.project_id)
    ).all()
    return {
        "user_id": user.id,
        "user_key": user.user_key,
        "display_name": user.display_name,
        "kind": user.kind,
        "auth_mode": settings.auth_mode,
        "is_admin": bool(user.is_admin),
        "status": user.status,
        "has_password": user.password_hash is not None,
        "capabilities": {
            "platform": auth_mod.platform_capabilities(bool(user.is_admin)),
            "projects": [
                {"project_id": p.id, "role": m.role,
                 "capabilities": auth_mod.project_capabilities(m.role)}
                for m, p in rows
            ],
        },
        "projects": [
            {"project_id": p.id, "project_key": p.project_key,
             "name": p.name, "role": m.role}
            for m, p in rows
        ],
    }


@app.post("/api/auth/api-keys", status_code=201)
def create_api_key_endpoint(
    payload: ApiKeyCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """创建 API Key：secret 仅在本次响应返回，服务端只存哈希。

    admin 可通过 user_key 为指定主体（成员/连接器服务账号）代建密钥；
    他人代建与本人自建均写审计。
    """
    current = _current_user(db, request)
    target = current
    on_behalf = False
    if payload.user_key:
        if not current.is_admin:
            raise ApiError(
                403, "PROJECT_ROLE_INSUFFICIENT",
                "仅平台管理员可为他人创建密钥",
            )
        target = db.scalar(
            select(models.User).where(models.User.user_key == payload.user_key)
        )
        if target is None:
            # admin 代建即主体开通（provisioning）：按 user_key 前缀推断
            # 账号类型（sa: → 服务账号，其余按人）。升级指南流程：先由
            # admin 代建密钥开通主体，再登记项目成员（POST members 对
            # 不存在主体返回 404 USER_NOT_FOUND，见需求说明书错误表）。
            kind = ("service_account"
                    if payload.user_key.startswith("sa:") else "human")
            target, _ = auth_mod.get_or_create_user(
                db, payload.user_key, payload.user_key, kind=kind,
                created_by=current.user_key,
            )
            record_audit(
                db, "user", target.user_key, "user.provisioned",
                current.user_key, "success", request.state.request_id,
            )
        on_behalf = True
    if target.status != "active":
        raise conflict("USER_DISABLED", f"主体 {target.user_key} 已禁用")
    key, secret = auth_mod.create_api_key(
        db, target, payload.name, payload.expires_in_days, current.user_key
    )
    record_audit(
        db, "api_key", key.key_id,
        "api_key.created_on_behalf" if on_behalf else "api_key.created",
        current.user_key, "success", request.state.request_id,
    )
    db.commit()
    return {
        "key_id": key.key_id,
        "secret": f"dmc_{key.key_id}_{secret}",
        "name": key.name,
        "status": key.status,
        "user_key": target.user_key,
        "created_at": iso(key.created_at),
        "expires_at": iso(key.expires_at),
    }


@app.get("/api/auth/api-keys")
def list_api_keys(
    request: Request,
    user_key: str | None = Query(default=None, max_length=80),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """列出本人密钥（不含 secret）；admin 可用 user_key 查他人（留审计）。"""
    current = _current_user(db, request)
    target = current
    if user_key:
        if not current.is_admin:
            raise ApiError(
                403, "PROJECT_ROLE_INSUFFICIENT",
                "仅平台管理员可查询他人密钥",
            )
        target = db.scalar(
            select(models.User).where(models.User.user_key == user_key)
        )
        if target is None:
            raise not_found("USER_NOT_FOUND", f"主体 {user_key} 不存在")
        record_audit(db, "api_key", target.user_key, "api_key.list_others",
                     current.user_key, "success", request.state.request_id)
    keys = db.scalars(
        select(models.ApiKey)
        .where(models.ApiKey.user_id == target.id)
        .order_by(models.ApiKey.id.desc())
    )
    db.commit()
    return [api_key_dict(k) for k in keys]


@app.post("/api/auth/api-keys/{key_id}/revoke")
def revoke_api_key_endpoint(
    key_id: str, request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """吊销密钥（幂等）；吊销后旧令牌即时 401（缓存主动失效）。"""
    current = _current_user(db, request)
    key = db.scalar(
        select(models.ApiKey).where(models.ApiKey.key_id == key_id)
    )
    if key is None or (key.user_id != current.id and not current.is_admin):
        raise not_found(
            "API_KEY_NOT_FOUND",
            f"密钥 {key_id} 不存在或不属于当前主体",
        )
    already = key.status == "revoked"
    auth_mod.revoke_api_key(db, key)
    record_audit(db, "api_key", key.key_id, "api_key.revoked",
                 current.user_key, "success", request.state.request_id)
    db.commit()
    return {"key_id": key.key_id, "status": key.status,
            "idempotent": already}


@app.post("/api/auth/bootstrap", status_code=201)
def bootstrap_endpoint(
    payload: BootstrapIn, request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """一次性引导（仅 api_key 模式需要）：创建首个平台管理员并返回密钥。

    - BOOTSTRAP_TOKEN 未配置 → 409 BOOTSTRAP_NOT_CONFIGURED
    - 令牌不匹配 → 401 BOOTSTRAP_TOKEN_INVALID（常量时间比较）
    - users 非空 → 409 BOOTSTRAP_ALREADY_DONE（永久关闭）
    """
    if not settings.bootstrap_token:
        raise conflict(
            "BOOTSTRAP_NOT_CONFIGURED",
            "未配置 BOOTSTRAP_TOKEN 环境变量，引导端点不可用",
        )
    if not hmac.compare_digest(settings.bootstrap_token, payload.bootstrap_token):
        raise ApiError(401, "BOOTSTRAP_TOKEN_INVALID", "引导令牌不匹配")
    if db.scalar(select(models.User.id).limit(1)) is not None:
        raise conflict(
            "BOOTSTRAP_ALREADY_DONE",
            "已存在用户，引导端点永久关闭",
        )
    user, key, secret = auth_mod.bootstrap_admin(
        db, payload.user_key, payload.display_name
    )
    record_event(db, 0, "auth.bootstrap.completed", user.user_key,
                 {"user_key": user.user_key, "key_id": key.key_id},
                 request.state.request_id)
    record_audit(db, "user", user.user_key, "auth.bootstrap",
                 user.user_key, "success", request.state.request_id)
    db.commit()
    return {
        "user_key": user.user_key,
        "key_id": key.key_id,
        "secret": f"dmc_{key.key_id}_{secret}",
    }


# ---------------------------------------------------------------- V1.3 密码登录


@app.post("/api/auth/login")
def login_endpoint(
    payload: LoginIn, request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """账号密码登录：成功签发会话密钥（Bearer dmc_<key_id>_<secret>）。

    - 用户名不存在 / 未设密码 / 口令错误 / 主体禁用：统一
      401 INVALID_CREDENTIALS（不泄露主体存在性）；
    - 会话密钥 name=``session:web``，expires_at 按 AUTH_SESSION_HOURS
      （0 表示不过期）；退出登录（POST /api/auth/logout）即时吊销；
    - demo 与 api_key 模式均可用（登录本身免认证，见豁免路径）；
    - 登录成功/失败均写审计（auth.login / auth.login_failed）。
    """
    try:
        user, key, secret = auth_mod.login_with_password(
            db, payload.login, payload.password
        )
    except ApiError:
        record_audit(
            db, "auth", f"login={payload.login}", "auth.login_failed",
            "", "denied", request.state.request_id,
        )
        db.commit()
        raise
    record_event(db, 0, "auth.login", user.user_key,
                 {"user_key": user.user_key, "key_id": key.key_id},
                 request.state.request_id)
    record_audit(db, "auth", user.user_key, "auth.login",
                 user.user_key, "success", request.state.request_id)
    db.commit()
    return {
        "token": f"dmc_{key.key_id}_{secret}",
        "user_key": user.user_key,
        "display_name": user.display_name,
        "is_admin": bool(user.is_admin),
        "auth_mode": settings.auth_mode,
        "expires_at": iso(key.expires_at),
    }


@app.post("/api/auth/logout")
def logout_endpoint(
    request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """退出登录：吊销当前 Bearer 令牌对应的会话密钥。

    仅吊销 name=``session:web`` 的会话密钥——长期 API Key 不受影响
    （避免把登出误用为密钥吊销通道）；无令牌或非会话密钥时
    revoked=false（demo 匿名调用同样安全幂等）。
    """
    identity = getattr(request.state, "identity", None)
    if identity is None:
        return {"revoked": False}
    key_id = auth_mod.key_id_of_token(request.headers.get("authorization"))
    if key_id is None:
        return {"revoked": False}
    key = db.scalar(
        select(models.ApiKey).where(
            models.ApiKey.key_id == key_id,
            models.ApiKey.user_id == identity.user_id,
        )
    )
    if key is None or key.name != auth_mod.SESSION_KEY_NAME:
        return {"revoked": False}
    auth_mod.revoke_api_key(db, key)
    record_audit(db, "auth", key.key_id, "auth.logout",
                 identity.user_key, "success", request.state.request_id)
    db.commit()
    return {"revoked": True, "key_id": key.key_id}


@app.post("/api/auth/password")
def change_password_endpoint(
    payload: PasswordChangeIn,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """修改本人密码：校验当前口令后重设（V1.3）。

    新密码 ≥8 位且不得与当前相同；修改成功后该主体既有会话密钥
    不自动吊销（改密不等于踢人；如需全端下线走用户管理禁用/启用的
    缓存即时失效路径）。
    """
    user = _current_user(db, request)
    auth_mod.change_password(
        db, user, payload.current_password, payload.new_password
    )
    record_audit(db, "auth", user.user_key, "auth.password_changed",
                 user.user_key, "success", request.state.request_id)
    db.commit()
    return {"user_key": user.user_key, "changed": True}


# ---------------------------------------------------------------- V1.2 用户管理（平台级）


def _user_dict(
    u: models.User, project_count: int, active_key_count: int
) -> dict[str, Any]:
    """用户视图（不含任何密钥材料）；附项目数与 active 密钥数摘要。"""
    return {
        "user_id": u.id,
        "user_key": u.user_key,
        "display_name": u.display_name,
        "kind": u.kind,
        "is_admin": bool(u.is_admin),
        "status": u.status,
        "auth_source": u.auth_source,
        "created_at": iso(u.created_at),
        "project_count": project_count,
        "active_key_count": active_key_count,
    }


def _user_summaries(
    db: Session, users: list[models.User]
) -> list[dict[str, Any]]:
    """批量补全项目数与 active 密钥数（两次 group-by，避免 N+1）。"""
    proj_counts = dict(db.execute(
        select(models.ProjectMember.user_id,
               func.count(models.ProjectMember.id))
        .group_by(models.ProjectMember.user_id)
    ).all())
    key_counts = dict(db.execute(
        select(models.ApiKey.user_id, func.count(models.ApiKey.id))
        .where(models.ApiKey.status == "active")
        .group_by(models.ApiKey.user_id)
    ).all())
    return [
        _user_dict(u, int(proj_counts.get(u.id, 0)),
                   int(key_counts.get(u.id, 0)))
        for u in users
    ]


@app.get("/api/users", dependencies=[Depends(auth_mod.require_admin)])
def list_users(
    status: str | None = Query(default=None, max_length=24),
    q: str | None = Query(default=None, max_length=80),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """用户列表（平台管理员）：status 过滤 + q 模糊匹配（user_key/名称）。"""
    stmt = select(models.User)
    if status in ("active", "disabled"):
        stmt = stmt.where(models.User.status == status)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(
            models.User.user_key.like(like),
            models.User.display_name.like(like),
        ))
    users = list(db.scalars(stmt.order_by(models.User.id)))
    return _user_summaries(db, users)


@app.post("/api/users", status_code=201,
          dependencies=[Depends(auth_mod.require_admin)])
def create_user_endpoint(
    payload: UserCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """创建主体（平台管理员）：创建即最低权限，无任何项目成员关系。

    与"代建密钥即开通"互补：本端点管"人"，POST /api/auth/api-keys
    （带 user_key）管"凭据"。user_key 重复 → 409 USER_KEY_EXISTS。
    """
    actor = request.state.actor
    if db.scalar(select(models.User.id).where(
        models.User.user_key == payload.user_key
    )) is not None:
        raise conflict("USER_KEY_EXISTS", f"主体 {payload.user_key} 已存在")
    user = models.User(
        user_key=payload.user_key,
        display_name=payload.display_name,
        kind=payload.kind,
        is_admin=payload.is_admin,
        auth_source="local",
        status="active",
    )
    db.add(user)
    db.flush()
    record_event(db, 0, "user.created", actor,
                 {"user_key": user.user_key, "kind": user.kind,
                  "is_admin": user.is_admin}, request.state.request_id)
    record_audit(db, "user", user.user_key, "user.created",
                 actor, "success", request.state.request_id)
    db.commit()
    return _user_dict(user, 0, 0)


@app.put("/api/users/{user_id}",
         dependencies=[Depends(auth_mod.require_admin)])
def update_user_endpoint(
    user_id: int,
    payload: UserUpdate,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """更新主体（平台管理员）：display_name / is_admin / status。

    防护（设计文档 2.4）：
    - P1 最后 admin 保护：降级或禁用最后一个 active admin → 409 ADMIN_LAST_ADMIN
    - P2 禁自降级：admin 取消自身 is_admin → 409 ADMIN_SELF_DEMOTE
    - P3 禁自禁用：admin 禁用自身 → 409 ADMIN_SELF_DISABLE
    - P6 禁用即时失效：status→disabled 时 invalidate 全部密钥缓存
    """
    current = auth_mod.require_admin(request)
    user = db.get(models.User, user_id)
    if user is None:
        raise not_found("USER_NOT_FOUND", f"主体 {user_id} 不存在")
    if (payload.display_name is None
            and payload.is_admin is None
            and payload.status is None):
        raise ApiError(422, "USER_UPDATE_EMPTY", "没有需要更新的字段")

    demoting_admin = (
        payload.is_admin is False and user.is_admin
    )
    disabling = payload.status == "disabled" and user.status == "active"

    if (demoting_admin or disabling) and user.is_admin:
        # P1 先于 P2/P3：最后一个 admin 时任何降级/禁用都先撞 LAST_ADMIN
        # （"请先提升新的管理员"比"请找其他管理员"更可操作）。
        if auth_mod.count_active_admins(db) <= 1:
            raise conflict(
                "ADMIN_LAST_ADMIN",
                "不能降级或禁用最后一个平台管理员；请先提升新的管理员",
            )
        if user.id == current.user_id:
            if demoting_admin:
                raise conflict(
                    "ADMIN_SELF_DEMOTE",
                    "不能取消自己的平台管理员身份；请由其他管理员操作",
                )
            raise conflict(
                "ADMIN_SELF_DISABLE",
                "不能禁用自己；请由其他管理员操作",
            )

    changes: list[str] = []
    if payload.display_name is not None:
        user.display_name = payload.display_name
        changes.append("display_name")
    if payload.is_admin is not None and payload.is_admin != user.is_admin:
        user.is_admin = payload.is_admin
        changes.append("is_admin")
    if payload.status is not None and payload.status != user.status:
        user.status = payload.status
        changes.append("status")
    db.flush()

    invalidated = 0
    # status/is_admin 变更必须即时生效：缓存 entry 携带 user_status 与
    # is_admin，不失效则降级/禁用后旧令牌在 TTL 内仍按旧身份放行（P6）。
    if "status" in changes or "is_admin" in changes:
        invalidated = auth_mod.invalidate_user_keys(db, user.id)

    record_event(db, 0, "user.updated", current.user_key,
                 {"user_key": user.user_key, "changes": changes,
                  "keys_invalidated": invalidated},
                 request.state.request_id)
    record_audit(db, "user", user.user_key, "user.updated",
                 current.user_key, "success", request.state.request_id)
    db.commit()
    db.refresh(user)
    return _user_dict(user, 0, 0)


def _member_dict(m: models.ProjectMember, u: models.User) -> dict[str, Any]:
    return {
        "user_id": u.id,
        "user_key": u.user_key,
        "display_name": u.display_name,
        "kind": u.kind,
        "role": m.role,
        "created_at": iso(m.created_at),
    }


@app.get("/api/projects/{project_id}/members",
         dependencies=[Depends(auth_mod.require_project_role("viewer"))])
def list_project_members(
    project_id: int, db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    """项目成员列表（viewer+ 或 admin）。"""
    get_project(db, project_id)
    rows = db.execute(
        select(models.ProjectMember, models.User)
        .join(models.User, models.User.id == models.ProjectMember.user_id)
        .where(models.ProjectMember.project_id == project_id)
        .order_by(models.ProjectMember.id)
    ).all()
    return [_member_dict(m, u) for m, u in rows]


@app.post("/api/projects/{project_id}/members", status_code=201,
          dependencies=[Depends(auth_mod.require_project_role("owner"))])
def upsert_project_member_endpoint(
    project_id: int,
    payload: ProjectMemberIn,
    request: Request,
    db: Session = Depends(get_db),
    response: Response = None,  # noqa: ANN001
) -> dict[str, Any]:
    """新增或改角色（upsert，幂等）；owner 或 admin。"""
    get_project(db, project_id)
    actor = request.state.actor
    user = db.scalar(
        select(models.User).where(models.User.user_key == payload.user_key)
    )
    if user is None:
        raise not_found(
            "USER_NOT_FOUND",
            f"主体 {payload.user_key} 不存在；请先由平台管理员代建密钥"
            "完成主体开通（POST /api/auth/api-keys 带 user_key）",
        )
    if user.status != "active":
        raise conflict("USER_DISABLED", f"主体 {user.user_key} 已禁用")
    # P4：upsert 把项目唯一 owner 改成低角色 → 409（与 DELETE 路径同一
    # 错误码；V1.0 仅 DELETE 有保护，upsert 可绕过，V1.2 对齐）。
    if payload.role != "owner":
        current_role = db.scalar(
            select(models.ProjectMember.role).where(
                models.ProjectMember.project_id == project_id,
                models.ProjectMember.user_id == user.id,
            )
        )
        if current_role == "owner":
            owners = int(db.scalar(
                select(func.count(models.ProjectMember.id)).where(
                    models.ProjectMember.project_id == project_id,
                    models.ProjectMember.role == "owner",
                )
            ) or 0)
            if owners <= 1:
                raise conflict(
                    "MEMBER_LAST_OWNER",
                    "不能把最后一个 owner 降级；请先移交项目所有权",
                )
    member, created = auth_mod.upsert_project_member(
        db, project_id, user, payload.role, actor
    )
    record_event(db, project_id, "project.member.upserted", actor,
                 {"user_key": user.user_key, "role": payload.role,
                  "created": created}, request.state.request_id)
    record_audit(db, "project_member", str(member.id),
                 "project.member.upserted", actor, "success",
                 request.state.request_id)
    db.commit()
    if not created and response is not None:
        response.status_code = 200
    return {"user_id": user.id, "user_key": user.user_key,
            "role": member.role, "created": created}


@app.delete("/api/projects/{project_id}/members/{user_id}",
            dependencies=[Depends(auth_mod.require_project_role("owner"))])
def remove_project_member_endpoint(
    project_id: int, user_id: int, request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """移除成员；最后一个 owner 受保护（409 MEMBER_LAST_OWNER）。"""
    get_project(db, project_id)
    actor = request.state.actor
    member = db.scalar(
        select(models.ProjectMember).where(
            models.ProjectMember.project_id == project_id,
            models.ProjectMember.user_id == user_id,
        )
    )
    if member is None:
        raise not_found(
            "MEMBER_NOT_FOUND",
            f"项目 {project_id} 下不存在成员 {user_id}",
        )
    if member.role == "owner":
        owners = list(db.scalars(
            select(models.ProjectMember).where(
                models.ProjectMember.project_id == project_id,
                models.ProjectMember.role == "owner",
            )
        ))
        if len(owners) <= 1:
            raise conflict(
                "MEMBER_LAST_OWNER",
                "不能移除最后一个 owner；请先移交项目所有权",
            )
    db.delete(member)
    record_event(db, project_id, "project.member.removed", actor,
                 {"user_id": user_id}, request.state.request_id)
    record_audit(db, "project_member", str(member.id),
                 "project.member.removed", actor, "success",
                 request.state.request_id)
    db.commit()
    return {"status": "removed", "user_id": user_id}


# ---------------------------------------------------------------- 静态前端

_frontend_dir = BASE_DIR / "frontend" / "static"
if _frontend_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_frontend_dir), html=True), name="frontend")
