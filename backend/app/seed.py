"""数据建模中心 MVP — 演示数据种子。

仅在空库且 SEED_DEMO_DATA 开启时注入演示项目/主题域/需求/模型，
便于首次启动即可看到完整工作台效果。正式业务库必须禁用。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models


def seed_demo_data(db: Session, actor: str = "demo.user") -> dict[str, int]:
    """注入演示数据；已存在项目时跳过。返回各类对象数量。"""
    existing = db.scalar(select(models.Project.id).limit(1))
    if existing is not None:
        return {"skipped": 1}

    project = models.Project(
        project_key="PRJ_RETAIL",
        name="零售数据平台试点",
        description="零售业务数仓试点项目：合同与订单域建模演示。",
    )
    db.add(project)
    db.flush()

    area_contract = models.SubjectArea(
        project_id=project.id, area_key="CONTRACT", name="合同主题域",
        description="合同签订、履约与金额相关业务对象。", owner="data.architect",
    )
    area_customer = models.SubjectArea(
        project_id=project.id, area_key="CUSTOMER", name="客户主题域",
        description="客户主数据与客户维度。", owner="data.architect",
    )
    db.add_all([area_contract, area_customer])
    db.flush()

    req = models.BusinessRequirement(
        project_id=project.id,
        requirement_key="REQ_CONTRACT_001",
        title="合同经营分析",
        business_domain="销售经营",
        description="分析合同签订量、合同金额和履约状态，支持按日期与客户筛选。",
        acceptance_criteria="支持按日期与客户筛选；T+1 更新；金额口径含税。",
        priority="P1",
        status="in_analysis",
        owner="business.owner",
        created_by=actor,
    )
    db.add(req)
    db.flush()

    contract_model = models.DataModel(
        project_id=project.id,
        model_key="dwd_contract",
        name="合同明细",
        description="每行代表一份合同的当前版本。",
        layer="DWD",
        model_type="TABLE",
        target_engine="postgresql",
        subject_area_id=area_contract.id,
        status="published",
        edit_revision=2,
        published_version_no=1,
        created_by=actor,
    )
    db.add(contract_model)
    db.flush()

    contract_fields = [
        models.ModelField(model_id=contract_model.id, ordinal=1, field_code="contract_id",
                          field_name="合同编号", logical_type="BIGINT", nullable=False,
                          is_primary_key=True, description="平台内合同的稳定业务标识",
                          source_table="ods_contract_raw", source_field="contract_id"),
        models.ModelField(model_id=contract_model.id, ordinal=2, field_code="contract_no",
                          field_name="合同业务号", logical_type="VARCHAR", length=64,
                          nullable=False, is_unique=True,
                          description="合同业务编号，候选业务唯一键",
                          source_table="ods_contract_raw", source_field="contract_no"),
        models.ModelField(model_id=contract_model.id, ordinal=3, field_code="customer_id",
                          field_name="客户编号", logical_type="BIGINT", nullable=False,
                          description="签约客户标识，关联客户维表",
                          source_table="ods_contract_raw", source_field="customer_id"),
        models.ModelField(model_id=contract_model.id, ordinal=4, field_code="contract_status",
                          field_name="合同状态", logical_type="VARCHAR", length=30,
                          description="合同当前履约状态", source_table="ods_contract_raw",
                          source_field="status"),
        models.ModelField(model_id=contract_model.id, ordinal=5, field_code="total_amount",
                          field_name="合同总金额", logical_type="DECIMAL", precision=18,
                          scale=2, nullable=False, description="含税合同总金额",
                          source_table="ods_contract_raw", source_field="amount"),
        models.ModelField(model_id=contract_model.id, ordinal=6, field_code="signed_date",
                          field_name="签订日期", logical_type="DATE",
                          description="合同签订日期", source_table="ods_contract_raw",
                          source_field="signed_at"),
        models.ModelField(model_id=contract_model.id, ordinal=7, field_code="created_at",
                          field_name="记录创建时间", logical_type="TIMESTAMP",
                          description="记录入库时间", source_table="ods_contract_raw",
                          source_field="etl_time"),
    ]
    db.add_all(contract_fields)

    customer_model = models.DataModel(
        project_id=project.id,
        model_key="dim_customer",
        name="客户维表",
        description="每行代表一个客户。",
        layer="DIM",
        model_type="DIMENSION",
        target_engine="postgresql",
        subject_area_id=area_customer.id,
        status="draft",
        edit_revision=1,
        created_by=actor,
    )
    db.add(customer_model)
    db.flush()

    customer_fields = [
        models.ModelField(model_id=customer_model.id, ordinal=1, field_code="customer_id",
                          field_name="客户编号", logical_type="BIGINT", nullable=False,
                          is_primary_key=True, description="客户稳定标识",
                          source_table="ods_customer_raw", source_field="customer_id"),
        models.ModelField(model_id=customer_model.id, ordinal=2, field_code="customer_name",
                          field_name="客户名称", logical_type="VARCHAR", length=200,
                          description="客户注册名称", source_table="ods_customer_raw",
                          source_field="name"),
        models.ModelField(model_id=customer_model.id, ordinal=3, field_code="customer_level",
                          field_name="客户等级", logical_type="VARCHAR", length=20,
                          description="客户分级：A/B/C", source_table="ods_customer_raw",
                          source_field="level"),
    ]
    db.add_all(customer_fields)

    # 需求-模型关联
    db.add(models.RequirementModelLink(
        requirement_id=req.id, model_id=contract_model.id, created_by=actor,
    ))

    # 模型关系：合同明细 joins_to 客户维表
    db.add(models.ModelRelationship(
        project_id=project.id,
        source_model_id=contract_model.id,
        target_model_id=customer_model.id,
        relationship_type="joins_to",
        source_fields_json='["customer_id"]',
        target_fields_json='["customer_id"]',
        cardinality="many_to_one",
        description="合同明细通过客户编号关联客户维表",
        created_by=actor,
    ))

    # 已发布版本快照（V1）
    import json

    snapshot = {
        "model": {
            "model_key": contract_model.model_key,
            "name": contract_model.name,
            "description": contract_model.description,
            "layer": contract_model.layer,
            "model_type": contract_model.model_type,
            "target_engine": contract_model.target_engine,
            "subject_area_id": contract_model.subject_area_id,
        },
        "fields": [
            {
                "field_code": f.field_code, "field_name": f.field_name,
                "logical_type": f.logical_type, "length": f.length,
                "precision": f.precision, "scale": f.scale,
                "nullable": f.nullable, "is_primary_key": f.is_primary_key,
                "is_unique": f.is_unique,
                "default_value": f.default_value, "description": f.description,
                "source_table": f.source_table, "source_field": f.source_field,
                "sensitivity": f.sensitivity,
            }
            for f in contract_fields
        ],
        "requirement_ids": [req.id],
        # V0.8 表级对象类别：种子未定义约束/索引（置空）；
        # 相邻关系纳入快照，支持影响分析的基线边合并与版本差异。
        "constraints": [],
        "indexes": [],
        "relationships": [
            {
                "source_model_id": contract_model.id,
                "target_model_id": customer_model.id,
                "relationship_type": "joins_to",
                "source_fields": ["customer_id"],
                "target_fields": ["customer_id"],
                "cardinality": "many_to_one",
                "description": "合同明细通过客户编号关联客户维表",
            }
        ],
    }
    db.add(models.ModelVersion(
        model_id=contract_model.id,
        version_no=1,
        status="published",
        snapshot_json=json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
        change_note="初始发布：合同明细模型 V1",
        created_by=actor,
    ))

    db.add(models.ActivityEvent(
        project_id=project.id, event_type="seed.completed",
        actor=actor, detail_json='{"note": "demo data seeded"}',
    ))

    db.commit()
    return {
        "projects": 1,
        "subject_areas": 2,
        "requirements": 1,
        "models": 2,
        "fields": len(contract_fields) + len(customer_fields),
        "versions": 1,
        "relationships": 1,
    }
