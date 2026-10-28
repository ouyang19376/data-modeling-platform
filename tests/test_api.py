"""数据建模中心 MVP — API 集成测试（P9 验收 + V0.7 字段设计器）。

覆盖文档验收清单：
- 主链路：项目 → 主题域 → 需求 → 模型草稿 → 校验 → 评审 → 发布 → 变更 → 再发布
- 乐观并发：expected_revision 冲突返回 409 REVISION_CONFLICT
- 状态守卫：非法流转 422/409；退回必须填意见；非草稿保存 409 MODEL_NOT_EDITABLE
- 边界：跨项目主题域/需求/关系、自引用、字段对不匹配
- 版本差异：V1→V2 字段增删改（含 is_unique）
- DDL：三引擎适配 + 校验阻断 + 单列 UNIQUE 约束
- V0.7 字段设计器：is_unique 读写/快照、字段 ID 稳定性、默认值白名单、
  类型参数校验（VARCHAR/DECIMAL）、警告级规则
- V0.8 表级约束/索引：CRUD、类型规则（PK≥2 字段/互斥、CHECK 表达式、
  FK 目标与 ON DELETE）、DDL 三引擎约束输出（PG 命名 CONSTRAINT +
  CREATE INDEX USING、MySQL 仅 btree、Hive 逻辑注释）、只读守卫
- V0.8 模型关系图：节点/边结构与字段映射
- V0.8 变更影响分析：无发布基线、字段级传播、模型级范围、字段收窄、
  基线边合并
- V0.8 快照差异：constraints/indexes/relationships 类别 + legacy 快照跳过
- 可观测：request_id 传播、metrics、看板、事件
- 迁移烟测：alembic upgrade/downgrade/upgrade（独立子进程 + 独立临时库）

运行：python -m pytest tests/ -v
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------- 环境准备
# settings 为模块级单例（导入时读取环境变量），必须在导入 app 之前设置。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
_TMP_DIR = tempfile.mkdtemp(prefix="dmc_test_")
os.environ.setdefault("APP_ENV", "test")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DIR}/platform.db"
os.environ["AUTO_MIGRATE"] = "true"
os.environ["SEED_DEMO_DATA"] = "false"
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["ALLOW_DEMO_ACTOR_HEADER"] = "true"

sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend.app import auth as auth_mod  # noqa: E402
from backend.app import models as models_mod  # noqa: E402
from backend.app.config import settings  # noqa: E402
from backend.app.main import app, iso as iso_mod  # noqa: E402
from backend.app.main import metrics as metrics_mod  # noqa: E402


# ---------------------------------------------------------------- 夹具

@pytest.fixture(scope="session")
def client() -> TestClient:
    """会话级客户端：lifespan 触发迁移建表（SEED_DEMO_DATA=false，数据干净）。"""
    with TestClient(app) as c:
        yield c


def api(c: TestClient, method: str, url: str, **kw):
    """统一请求入口：默认带演示身份头，返回 response。"""
    kw.setdefault("headers", {"X-Actor": "pytest.bot"})
    return c.request(method, url, **kw)


def create_project(c: TestClient, key: str = "PRJ_TEST", name: str = "测试项目") -> dict:
    r = api(c, "POST", "/api/projects", json={
        "project_key": key, "name": name, "description": "pytest",
    })
    assert r.status_code == 201, r.text
    return r.json()


def create_subject_area(c: TestClient, pid: int, key: str = "AREA_TEST") -> dict:
    r = api(c, "POST", f"/api/projects/{pid}/subject-areas", json={
        "area_key": key, "name": "测试主题域", "owner": "pytest.bot",
    })
    assert r.status_code == 201, r.text
    return r.json()


def create_requirement(c: TestClient, pid: int, key: str = "REQ_TEST_001") -> dict:
    r = api(c, "POST", f"/api/projects/{pid}/requirements", json={
        "requirement_key": key, "title": "测试需求",
        "business_domain": "测试域", "description": "pytest 需求描述",
        "acceptance_criteria": "验收标准", "priority": "P1",
    })
    assert r.status_code == 201, r.text
    return r.json()


def good_fields() -> list[dict]:
    """一组可通过全部阻断级校验的字段（snake_case、主键非空、DECIMAL 带精度）。"""
    return [
        {
            "field_code": "order_id", "field_name": "订单ID",
            "logical_type": "BIGINT", "nullable": False, "is_primary_key": True,
            "description": "订单唯一标识", "source_table": "ods.orders",
            "source_field": "id",
        },
        {
            "field_code": "order_amount", "field_name": "订单金额",
            "logical_type": "DECIMAL", "precision": 18, "scale": 2,
            "nullable": False, "description": "订单金额（元）",
            "source_table": "ods.orders", "source_field": "amount",
        },
        {
            "field_code": "order_status", "field_name": "订单状态",
            "logical_type": "VARCHAR", "length": 32, "nullable": False,
            "description": "订单状态编码", "source_table": "ods.orders",
            "source_field": "status",
        },
        {
            "field_code": "created_at", "field_name": "创建时间",
            "logical_type": "TIMESTAMP", "nullable": False,
            "description": "记录创建时间", "source_table": "ods.orders",
            "source_field": "created_at",
        },
    ]


def create_model(c: TestClient, pid: int, key: str = "dwd_test_order",
                 fields: list[dict] | None = None, **extra) -> dict:
    body = {
        "model_key": key, "name": "测试订单明细", "layer": "DWD",
        "model_type": "TABLE", "target_engine": "postgresql",
        "fields": fields if fields is not None else good_fields(),
    }
    body.update(extra)
    r = api(c, "POST", f"/api/projects/{pid}/models", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def publish_model(c: TestClient, model_id: int, comment: str = "pytest 发布") -> dict:
    """草稿 → 提交评审 → 通过 → 发布 的完整推进。"""
    r = api(c, "POST", f"/api/models/{model_id}/submit-review")
    assert r.status_code == 200, r.text
    r = api(c, "POST", f"/api/models/{model_id}/approve", json={"comment": "同意"})
    assert r.status_code == 200, r.text
    r = api(c, "POST", f"/api/models/{model_id}/publish", json={"comment": comment})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- 健康检查

class TestHealth:
    def test_live(self, client):
        r = client.get("/api/health/live")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_ready(self, client):
        r = client.get("/api/health/ready")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ready"
        assert body["database"] == "ok"
        assert body["revision"] == "7f0c38e854e5"
        assert body["head"] == "7f0c38e854e5"

    def test_compat_health(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200


# ---------------------------------------------------------------- 可观测性

class TestObservability:
    def test_request_id_generated_and_echoed(self, client):
        r = client.get("/api/health/live")
        rid = r.headers.get("X-Request-ID")
        assert rid and len(rid) >= 8

        custom = "test-req-id-0001"
        r = client.get("/api/health/live", headers={"X-Request-ID": custom})
        assert r.headers["X-Request-ID"] == custom

    def test_error_body_contains_request_id(self, client):
        r = client.get("/api/models/999999")
        assert r.status_code == 404
        body = r.json()
        assert body["error"]["code"] == "MODEL_NOT_FOUND"
        assert body["request_id"]

    def test_actor_header_recorded_in_events(self, client):
        r = api(client, "POST", "/api/projects", json={
            "project_key": "PRJ_ACTOR", "name": "测试项目",
        }, headers={"X-Actor": "alice.tester"})
        assert r.status_code == 201
        events = client.get(f"/api/projects/{r.json()['id']}/events").json()
        assert events and events[0]["actor"] == "alice.tester"
        assert events[0]["event_type"] == "project.created"

    def test_metrics_and_summary(self, client):
        r = client.get("/metrics")
        assert r.status_code == 200
        assert "http_requests_total" in r.text
        assert "process_uptime_seconds" in r.text

        r = client.get("/api/observability/summary")
        assert r.status_code == 200
        body = r.json()
        assert body["requests_total"] > 0
        assert "requests_by_status_class" in body


# ---------------------------------------------------------------- 项目 / 主题域 / 需求

class TestProjectAndMetadata:
    def test_project_create_and_duplicate(self, client):
        p = create_project(client, "PRJ_DUP")
        assert p["project_key"] == "PRJ_DUP"
        r = api(client, "POST", "/api/projects", json={
            "project_key": "PRJ_DUP", "name": "重复项目",
        })
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "PROJECT_KEY_EXISTS"

    def test_project_key_pattern_rejected(self, client):
        r = api(client, "POST", "/api/projects", json={
            "project_key": "1bad-key", "name": "非法编码",
        })
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"

    def test_project_not_found(self, client):
        r = client.get("/api/projects/999999/models")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "PROJECT_NOT_FOUND"

    def test_subject_area_duplicate(self, client):
        p = create_project(client, "PRJ_AREA")
        create_subject_area(client, p["id"], "AREA_DUP")
        r = api(client, "POST", f"/api/projects/{p['id']}/subject-areas", json={
            "area_key": "AREA_DUP", "name": "重复主题域",
        })
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "SUBJECT_AREA_KEY_EXISTS"

    def test_requirement_crud_and_link_count(self, client):
        p = create_project(client, "PRJ_REQ")
        req = create_requirement(client, p["id"], "REQ_CRUD_001")
        assert req["linked_model_count"] == 0

        r = api(client, "PUT", f"/api/requirements/{req['id']}", json={
            "requirement_key": "REQ_CRUD_001", "title": "更新后的标题",
            "priority": "P0", "status": "in_analysis",
        })
        assert r.status_code == 200
        assert r.json()["title"] == "更新后的标题"
        assert r.json()["status"] == "in_analysis"

        # 关联模型后计数 +1
        m = create_model(client, p["id"], "dwd_req_link")
        r = api(client, "POST", f"/api/requirements/{req['id']}/models/{m['id']}")
        assert r.status_code == 201
        r = client.get(f"/api/requirements/{req['id']}")
        assert r.json()["linked_model_count"] == 1

        r = client.get(f"/api/requirements/{req['id']}/models")
        assert [x["id"] for x in r.json()] == [m["id"]]

        r = api(client, "DELETE",
                f"/api/requirements/{req['id']}/models/{m['id']}")
        assert r.json()["status"] == "unlinked"
        r = client.get(f"/api/requirements/{req['id']}")
        assert r.json()["linked_model_count"] == 0

    def test_requirement_model_cross_project_link(self, client):
        p1 = create_project(client, "PRJ_LINK_A")
        p2 = create_project(client, "PRJ_LINK_B")
        req = create_requirement(client, p1["id"], "REQ_LINK_A")
        m = create_model(client, p2["id"], "dwd_link_b")
        r = api(client, "POST", f"/api/requirements/{req['id']}/models/{m['id']}")
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "REQUIREMENT_PROJECT_MISMATCH"

    def test_model_cross_project_subject_area(self, client):
        p1 = create_project(client, "PRJ_SA_A")
        p2 = create_project(client, "PRJ_SA_B")
        area = create_subject_area(client, p1["id"], "AREA_SA_A")
        r = api(client, "POST", f"/api/projects/{p2['id']}/models", json={
            "model_key": "dwd_sa_cross", "name": "跨项目主题域",
            "subject_area_id": area["id"], "fields": good_fields(),
        })
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "SUBJECT_AREA_PROJECT_MISMATCH"

    def test_model_cross_project_requirement(self, client):
        p1 = create_project(client, "PRJ_RQ_A")
        p2 = create_project(client, "PRJ_RQ_B")
        req = create_requirement(client, p1["id"], "REQ_RQ_A")
        r = api(client, "POST", f"/api/projects/{p2['id']}/models", json={
            "model_key": "dwd_rq_cross", "name": "跨项目需求",
            "requirement_ids": [req["id"]], "fields": good_fields(),
        })
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "REQUIREMENT_PROJECT_MISMATCH"


# ---------------------------------------------------------------- 校验引擎

class TestValidation:
    def test_clean_model_passes(self, client):
        p = create_project(client, "PRJ_VAL_OK")
        m = create_model(client, p["id"], "dwd_val_ok")
        r = api(client, "POST", f"/api/models/{m['id']}/validate")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "PASSED"
        assert body["summary"]["errors"] == 0
        assert body["run_id"]

    def test_rule_violations_detected(self, client):
        p = create_project(client, "PRJ_VAL_BAD")
        bad_fields = [
            {   # INVALID_FIELD_CODE：非 snake_case
                "field_code": "OrderID", "field_name": "坏编码",
                "logical_type": "BIGINT", "nullable": False,
                "is_primary_key": True, "description": "x",
            },
            {   # PK_NULLABLE：主键可空
                "field_code": "pk_two", "field_name": "主键2",
                "logical_type": "INTEGER", "nullable": True,
                "is_primary_key": True, "description": "x",
            },
            {   # DECIMAL_PRECISION_REQUIRED + DECIMAL_SCALE_REQUIRED
                "field_code": "amount_bad", "field_name": "金额",
                "logical_type": "DECIMAL", "nullable": True,
                "description": "x",
            },
            {   # FIELD_DESCRIPTION_MISSING 警告（不阻断）
                "field_code": "no_desc", "field_name": "无描述",
                "logical_type": "STRING", "nullable": True,
            },
        ]
        m = create_model(client, p["id"], "dwd_val_bad", fields=bad_fields)
        r = api(client, "POST", f"/api/models/{m['id']}/validate")
        body = r.json()
        assert body["status"] == "FAILED"
        rule_ids = {i["rule_id"] for i in body["issues"]}
        assert {
            "INVALID_FIELD_CODE", "PK_NULLABLE",
            "DECIMAL_PRECISION_REQUIRED", "DECIMAL_SCALE_REQUIRED",
            "FIELD_DESCRIPTION_MISSING",
        } <= rule_ids
        assert body["summary"]["errors"] >= 4
        assert body["summary"]["warnings"] >= 1

    def test_duplicate_field_code_rejected_on_create(self, client):
        """DUPLICATE_FIELD_CODE：重复字段编码在落库前被拦截，返回结构化校验 issue（422）。"""
        p = create_project(client, "PRJ_VAL_DUP")
        dup_fields = [
            {"field_code": "id_a", "logical_type": "BIGINT",
             "nullable": False, "is_primary_key": True, "description": "x"},
            {"field_code": "id_a", "logical_type": "BIGINT",
             "nullable": True, "description": "x"},
        ]
        r = api(client, "POST", f"/api/projects/{p['id']}/models", json={
            "model_key": "dwd_val_dup", "name": "重复字段",
            "fields": dup_fields,
        })
        assert r.status_code == 422
        body = r.json()
        assert body["error"]["code"] == "MODEL_VALIDATION_FAILED"
        rule_ids = {i["rule_id"] for i in body["error"]["details"]["issues"]}
        assert "DUPLICATE_FIELD_CODE" in rule_ids

    def test_duplicate_field_code_rejected_on_update(self, client):
        p = create_project(client, "PRJ_VAL_DUP2")
        m = create_model(client, p["id"], "dwd_val_dup2")
        dup_fields = good_fields()
        dup_fields.append(dict(dup_fields[0]))  # 复制 order_id
        r = api(client, "PUT", f"/api/models/{m['id']}", json={
            "model_key": "dwd_val_dup2", "name": "重复字段更新",
            "fields": dup_fields, "expected_revision": 1,
        })
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "MODEL_VALIDATION_FAILED"

    def test_validation_blocks_submit_review(self, client):
        p = create_project(client, "PRJ_VAL_BLOCK")
        m = create_model(client, p["id"], "dwd_val_block", fields=[
            {"field_code": "BadCode", "logical_type": "STRING",
             "nullable": True, "description": "x"},
        ])
        r = api(client, "POST", f"/api/models/{m['id']}/submit-review")
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "MODEL_VALIDATION_FAILED"
        assert r.json()["error"]["details"]["issues"]


# ---------------------------------------------------------------- V0.7 字段设计器

class TestV07FieldDesigner:
    """V0.7 验收：is_unique 元数据、字段 ID 稳定性、默认值白名单、
    类型参数校验、唯一约束 DDL 与警告级规则（文档 Table 13 / Table 28）。"""

    def test_is_unique_roundtrip_and_snapshot(self, client):
        p = create_project(client, "PRJ_V07_UNIQUE")
        fields = good_fields()
        fields[1]["is_unique"] = True  # order_amount 单列唯一
        m = create_model(client, p["id"], "dwd_v07_unique", fields=fields)
        by_code = {f["field_code"]: f for f in m["fields"]}
        assert by_code["order_amount"]["is_unique"] is True
        assert by_code["order_id"]["is_unique"] is False  # 缺省 false

        # 发布 V1 → 变更 is_unique → 发布 V2：diff 能观察到快照中的 is_unique
        publish_model(client, m["id"])
        api(client, "POST", f"/api/models/{m['id']}/start-change")
        fields2 = good_fields()
        fields2[1]["is_unique"] = False
        r = api(client, "PUT", f"/api/models/{m['id']}", json={
            "model_key": "dwd_v07_unique", "name": "测试订单明细",
            "fields": fields2, "expected_revision": 2,
        })
        assert r.status_code == 200
        publish_model(client, m["id"], "V2 取消唯一")

        r = client.get(
            f"/api/models/{m['id']}/versions/diff",
            params={"from_version": 1, "to_version": 2},
        )
        modified = {x["field_code"]: x["changes"]
                    for x in r.json()["fields"]["modified"]}
        assert "order_amount" in modified
        assert any(
            c["attribute"] == "is_unique" and c["from"] is True and c["to"] is False
            for c in modified["order_amount"]
        )

    def test_field_id_stability_on_update(self, client):
        """编码不变 → 保留字段 id；改名 → 删旧增新（文档 7.3）。"""
        p = create_project(client, "PRJ_V07_ID")
        m = create_model(client, p["id"], "dwd_v07_id")
        ids_before = {f["field_code"]: f["id"] for f in m["fields"]}

        # 只改描述：id 全部保留
        fields = good_fields()
        fields[1]["description"] = "订单金额（人民币元）"
        r = api(client, "PUT", f"/api/models/{m['id']}", json={
            "model_key": "dwd_v07_id", "name": "测试订单明细",
            "fields": fields, "expected_revision": 1,
        })
        assert r.status_code == 200
        ids_after = {f["field_code"]: f["id"] for f in r.json()["fields"]}
        assert ids_after == ids_before

        # 改名：旧编码消失，新编码获得新 id
        fields = good_fields()
        fields[0]["field_code"] = "order_id_renamed"
        r = api(client, "PUT", f"/api/models/{m['id']}", json={
            "model_key": "dwd_v07_id", "name": "测试订单明细",
            "fields": fields, "expected_revision": 2,
        })
        assert r.status_code == 200
        ids_after2 = {f["field_code"]: f["id"] for f in r.json()["fields"]}
        assert "order_id" not in ids_after2
        assert ids_after2["order_id_renamed"] not in ids_before.values()
        assert ids_after2["order_amount"] == ids_before["order_amount"]

    def test_unique_constraint_in_ddl(self, client):
        p = create_project(client, "PRJ_V07_DDL_UNIQUE")
        fields = good_fields()
        fields[1]["is_unique"] = True
        m = create_model(client, p["id"], "dwd_v07_ddl_unique", fields=fields)
        publish_model(client, m["id"])

        r = api(client, "POST", f"/api/models/{m['id']}/ddl",
                json={"target_engine": "postgresql"})
        assert r.status_code == 200
        assert 'UNIQUE ("order_amount")' in r.json()["ddl"]

        r = api(client, "POST", f"/api/models/{m['id']}/ddl",
                json={"target_engine": "mysql"})
        assert "UNIQUE (`order_amount`)" in r.json()["ddl"]
        # MySQL 列定义输出 COMMENT（文档 Table 18）
        assert "COMMENT '订单唯一标识'" in r.json()["ddl"]

        r = api(client, "POST", f"/api/models/{m['id']}/ddl",
                json={"target_engine": "hive"})
        ddl = r.json()["ddl"]
        assert "逻辑唯一约束" in ddl
        assert "UNIQUE (" not in ddl  # Hive 不输出强制 UNIQUE

    def test_pk_unique_redundant_warning_and_ddl(self, client):
        """主键 + 唯一：警告且 DDL 不重复输出 UNIQUE（主键已隐含唯一）。"""
        p = create_project(client, "PRJ_V07_PK_UNIQUE")
        fields = good_fields()
        fields[0]["is_unique"] = True  # order_id 同时是主键
        m = create_model(client, p["id"], "dwd_v07_pk_unique", fields=fields)
        r = api(client, "POST", f"/api/models/{m['id']}/validate")
        body = r.json()
        assert body["status"] == "PASSED"  # 警告不阻断
        rule_ids = {i["rule_id"] for i in body["issues"]}
        assert "PK_UNIQUE_REDUNDANT" in rule_ids

        publish_model(client, m["id"])
        r = api(client, "POST", f"/api/models/{m['id']}/ddl",
                json={"target_engine": "postgresql"})
        ddl = r.json()["ddl"]
        assert 'PRIMARY KEY ("order_id")' in ddl
        assert 'UNIQUE ("order_id")' not in ddl

    def test_default_value_whitelist(self, client):
        p = create_project(client, "PRJ_V07_DEFAULT_OK")
        fields = good_fields()
        fields[2]["default_value"] = "'pending'"          # 简单单引号常量
        fields[3]["default_value"] = "CURRENT_TIMESTAMP"  # 日期时间常量
        m = create_model(client, p["id"], "dwd_v07_default_ok", fields=fields)
        r = api(client, "POST", f"/api/models/{m['id']}/validate")
        assert r.json()["status"] == "PASSED"

        # 不安全默认值：SQL 注入片段 → 阻断错误 + 阻断提交评审
        p2 = create_project(client, "PRJ_V07_DEFAULT_BAD")
        fields = good_fields()
        fields[2]["default_value"] = "x'; DROP TABLE users; --"
        m2 = create_model(client, p2["id"], "dwd_v07_default_bad", fields=fields)
        r = api(client, "POST", f"/api/models/{m2['id']}/validate")
        body = r.json()
        assert body["status"] == "FAILED"
        assert "UNSAFE_DEFAULT_VALUE" in {i["rule_id"] for i in body["issues"]}
        r = api(client, "POST", f"/api/models/{m2['id']}/submit-review")
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "MODEL_VALIDATION_FAILED"

    def test_type_parameter_rules(self, client):
        p = create_project(client, "PRJ_V07_TYPE")
        fields = good_fields()
        fields[2]["length"] = None      # VARCHAR 缺长度
        fields[1]["precision"] = 5      # DECIMAL scale > precision
        fields[1]["scale"] = 10
        m = create_model(client, p["id"], "dwd_v07_type", fields=fields)
        r = api(client, "POST", f"/api/models/{m['id']}/validate")
        body = r.json()
        assert body["status"] == "FAILED"
        rule_ids = {i["rule_id"] for i in body["issues"]}
        assert "VARCHAR_LENGTH_REQUIRED" in rule_ids
        assert "DECIMAL_SCALE_GT_PRECISION" in rule_ids

    def test_warning_level_rules(self, client):
        p = create_project(client, "PRJ_V07_WARN")
        # DWD 无主键/唯一 → PRIMARY_KEY_RECOMMENDED；来源只填一半 → INCOMPLETE_SOURCE_MAPPING
        fields = [
            {"field_code": "name", "field_name": "名称",
             "logical_type": "VARCHAR", "length": 64, "nullable": True,
             "description": "客户名称", "source_table": "ods_customer"},
        ]
        m = create_model(client, p["id"], "dwd_v07_warn", fields=fields)
        r = api(client, "POST", f"/api/models/{m['id']}/validate")
        body = r.json()
        assert body["status"] == "PASSED"  # 警告不阻断
        rule_ids = {i["rule_id"] for i in body["issues"]}
        assert "PRIMARY_KEY_RECOMMENDED" in rule_ids
        assert "INCOMPLETE_SOURCE_MAPPING" in rule_ids

    def test_no_fields_blocked(self, client):
        p = create_project(client, "PRJ_V07_EMPTY")
        r = api(client, "POST", f"/api/projects/{p['id']}/models", json={
            "model_key": "dwd_v07_empty", "name": "空模型",
        })
        assert r.status_code == 201
        mid = r.json()["id"]
        r = api(client, "POST", f"/api/models/{mid}/validate")
        assert "NO_FIELDS" in {i["rule_id"] for i in r.json()["issues"]}
        r = api(client, "POST", f"/api/models/{mid}/submit-review")
        assert r.status_code == 422

    def test_non_draft_update_rejected(self, client):
        """待评审状态保存返回 409 MODEL_NOT_EDITABLE（文档 Table 15）。"""
        p = create_project(client, "PRJ_V07_NOT_EDITABLE")
        m = create_model(client, p["id"], "dwd_v07_not_editable")
        api(client, "POST", f"/api/models/{m['id']}/submit-review")
        r = api(client, "PUT", f"/api/models/{m['id']}", json={
            "model_key": "dwd_v07_not_editable", "name": "改名",
            "fields": good_fields(), "expected_revision": 1,
        })
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "MODEL_NOT_EDITABLE"


# ---------------------------------------------------------------- 主链路：评审发布闭环

class TestLifecycle:
    def test_full_lifecycle_with_change_cycle(self, client):
        p = create_project(client, "PRJ_LIFE")
        m = create_model(client, p["id"], "dwd_life_order")
        assert m["status"] == "draft"
        assert m["edit_revision"] == 1
        assert m["published_version_no"] is None

        # 非法流转：草稿直接发布（INVALID_STATE 为 409）
        r = api(client, "POST", f"/api/models/{m['id']}/publish", json={})
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "INVALID_STATE"

        # 草稿直接发起变更
        r = api(client, "POST", f"/api/models/{m['id']}/start-change")
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "NO_PUBLISHED_BASELINE"

        # 提交评审 → 通过 → 发布 V1
        r = api(client, "POST", f"/api/models/{m['id']}/submit-review")
        assert r.json()["status"] == "in_review"
        r = api(client, "POST", f"/api/models/{m['id']}/approve",
                json={"comment": "同意发布"})
        assert r.json()["status"] == "approved"
        pub = api(client, "POST", f"/api/models/{m['id']}/publish",
                  json={"comment": "V1 正式发布"})
        assert pub.json()["version_no"] == 1
        assert pub.json()["status"] == "published"

        # 已发布模型不可直接编辑（MODEL_NOT_EDITABLE 为 409）
        r = api(client, "PUT", f"/api/models/{m['id']}", json={
            "model_key": "dwd_life_order", "name": "改名",
            "fields": good_fields(), "expected_revision": 1,
        })
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "MODEL_NOT_EDITABLE"

        # 发起变更 → 草稿 → 修改字段 → 再走闭环 → V2
        r = api(client, "POST", f"/api/models/{m['id']}/start-change")
        assert r.json()["status"] == "draft"
        assert r.json()["edit_revision"] == 2

        new_fields = good_fields()
        new_fields.append({
            "field_code": "pay_channel", "field_name": "支付渠道",
            "logical_type": "VARCHAR", "length": 32, "nullable": True,
            "description": "支付渠道编码",
        })
        r = api(client, "PUT", f"/api/models/{m['id']}", json={
            "model_key": "dwd_life_order", "name": "测试订单明细",
            "fields": new_fields, "expected_revision": 2,
        })
        assert r.status_code == 200
        assert r.json()["edit_revision"] == 3
        assert len(r.json()["fields"]) == 5

        pub2 = publish_model(client, m["id"], "V2 增加支付渠道")
        assert pub2["version_no"] == 2

        versions = client.get(f"/api/models/{m['id']}/versions").json()
        assert [v["version_no"] for v in versions] == [2, 1]

    def test_reject_returns_to_draft_and_requires_comment(self, client):
        p = create_project(client, "PRJ_REJECT")
        m = create_model(client, p["id"], "dwd_reject")
        api(client, "POST", f"/api/models/{m['id']}/submit-review")

        r = api(client, "POST", f"/api/models/{m['id']}/reject",
                json={"comment": "   "})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"

        r = api(client, "POST", f"/api/models/{m['id']}/reject",
                json={"comment": "字段口径需补充"})
        assert r.json()["status"] == "draft"
        assert r.json()["decision"] == "rejected"

        # 退回后重新提交评审
        r = api(client, "POST", f"/api/models/{m['id']}/submit-review")
        assert r.json()["status"] == "in_review"

    def test_double_submit_rejected(self, client):
        p = create_project(client, "PRJ_DBL_SUB")
        m = create_model(client, p["id"], "dwd_dbl_sub")
        api(client, "POST", f"/api/models/{m['id']}/submit-review")
        r = api(client, "POST", f"/api/models/{m['id']}/submit-review")
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "INVALID_STATE"

    def test_approve_requires_in_review(self, client):
        p = create_project(client, "PRJ_APPR_GUARD")
        m = create_model(client, p["id"], "dwd_appr_guard")
        r = api(client, "POST", f"/api/models/{m['id']}/approve", json={})
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "INVALID_STATE"

    def test_publish_requires_approved(self, client):
        p = create_project(client, "PRJ_PUB_GUARD")
        m = create_model(client, p["id"], "dwd_pub_guard")
        api(client, "POST", f"/api/models/{m['id']}/submit-review")
        r = api(client, "POST", f"/api/models/{m['id']}/publish", json={})
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "INVALID_STATE"


# ---------------------------------------------------------------- 乐观并发

class TestOptimisticConcurrency:
    def test_revision_conflict(self, client):
        p = create_project(client, "PRJ_CONC")
        m = create_model(client, p["id"], "dwd_conc")
        assert m["edit_revision"] == 1

        # 过期修订号 → 409
        r = api(client, "PUT", f"/api/models/{m['id']}", json={
            "model_key": "dwd_conc", "name": "A 的修改",
            "fields": good_fields(), "expected_revision": 99,
        })
        assert r.status_code == 409
        body = r.json()
        assert body["error"]["code"] == "REVISION_CONFLICT"
        assert body["error"]["details"]["current_revision"] == 1

        # 正确修订号 → 200，revision +1
        r = api(client, "PUT", f"/api/models/{m['id']}", json={
            "model_key": "dwd_conc", "name": "B 的修改",
            "fields": good_fields(), "expected_revision": 1,
        })
        assert r.status_code == 200
        assert r.json()["edit_revision"] == 2

        # 再次使用旧修订号仍冲突
        r = api(client, "PUT", f"/api/models/{m['id']}", json={
            "model_key": "dwd_conc", "name": "C 的修改",
            "fields": good_fields(), "expected_revision": 1,
        })
        assert r.status_code == 409

    def test_model_key_duplicate_on_update(self, client):
        p = create_project(client, "PRJ_KEY_DUP")
        create_model(client, p["id"], "dwd_key_a")
        m = create_model(client, p["id"], "dwd_key_b")
        r = api(client, "PUT", f"/api/models/{m['id']}", json={
            "model_key": "dwd_key_a", "name": "抢编码",
            "fields": good_fields(), "expected_revision": 1,
        })
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "MODEL_KEY_EXISTS"


# ---------------------------------------------------------------- 模型关系

class TestRelationships:
    def test_relationship_guards_and_crud(self, client):
        p = create_project(client, "PRJ_REL")
        m1 = create_model(client, p["id"], "dwd_rel_a")
        m2 = create_model(client, p["id"], "dwd_rel_b")

        # 自引用
        r = api(client, "POST", f"/api/models/{m1['id']}/relationships", json={
            "target_model_id": m1["id"], "relationship_type": "depends_on",
            "source_fields": ["order_id"], "target_fields": ["order_id"],
        })
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "SELF_RELATIONSHIP"

        # 字段不存在
        r = api(client, "POST", f"/api/models/{m1['id']}/relationships", json={
            "target_model_id": m2["id"], "relationship_type": "depends_on",
            "source_fields": ["not_exist"], "target_fields": ["order_id"],
        })
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "RELATIONSHIP_FIELD_NOT_FOUND"

        # 字段对数量不一致
        r = api(client, "POST", f"/api/models/{m1['id']}/relationships", json={
            "target_model_id": m2["id"], "relationship_type": "joins_to",
            "source_fields": ["order_id", "order_status"],
            "target_fields": ["order_id"],
        })
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "RELATIONSHIP_FIELD_PAIR_MISMATCH"

        # 正常创建
        r = api(client, "POST", f"/api/models/{m1['id']}/relationships", json={
            "target_model_id": m2["id"], "relationship_type": "joins_to",
            "source_fields": ["order_id"], "target_fields": ["order_id"],
            "cardinality": "one_to_many", "description": "订单关联",
        })
        assert r.status_code == 201
        rel = r.json()
        assert rel["source_fields"] == ["order_id"]

        # 重复关系
        r = api(client, "POST", f"/api/models/{m1['id']}/relationships", json={
            "target_model_id": m2["id"], "relationship_type": "joins_to",
            "source_fields": ["order_id"], "target_fields": ["order_id"],
        })
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "MODEL_RELATIONSHIP_EXISTS"

        # 列表 + 删除
        rels = client.get(f"/api/models/{m1['id']}/relationships").json()
        assert len(rels) == 1
        r = api(client, "DELETE",
                f"/api/models/{m1['id']}/relationships/{rel['id']}")
        assert r.json()["status"] == "deleted"
        assert client.get(f"/api/models/{m1['id']}/relationships").json() == []

    def test_cross_project_relationship(self, client):
        p1 = create_project(client, "PRJ_REL_X_A")
        p2 = create_project(client, "PRJ_REL_X_B")
        m1 = create_model(client, p1["id"], "dwd_rel_x_a")
        m2 = create_model(client, p2["id"], "dwd_rel_x_b")
        r = api(client, "POST", f"/api/models/{m1['id']}/relationships", json={
            "target_model_id": m2["id"], "relationship_type": "depends_on",
            "source_fields": ["order_id"], "target_fields": ["order_id"],
        })
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "CROSS_PROJECT_RELATIONSHIP"


# ---------------------------------------------------------------- 版本差异

class TestVersionDiff:
    def test_diff_between_versions(self, client):
        p = create_project(client, "PRJ_DIFF")
        m = create_model(client, p["id"], "dwd_diff_order")
        publish_model(client, m["id"], "V1")

        # 变更：删一个字段、改一个字段、加一个字段
        api(client, "POST", f"/api/models/{m['id']}/start-change")
        new_fields = [
            f for f in good_fields() if f["field_code"] != "order_status"
        ]
        new_fields[1]["description"] = "订单金额（人民币元）"  # 修改描述
        new_fields.append({
            "field_code": "pay_channel", "field_name": "支付渠道",
            "logical_type": "VARCHAR", "length": 32, "nullable": True,
            "description": "支付渠道编码",
        })
        r = api(client, "PUT", f"/api/models/{m['id']}", json={
            "model_key": "dwd_diff_order", "name": "测试订单明细",
            "fields": new_fields, "expected_revision": 2,
        })
        assert r.status_code == 200
        publish_model(client, m["id"], "V2")

        r = client.get(
            f"/api/models/{m['id']}/versions/diff",
            params={"from_version": 1, "to_version": 2},
        )
        assert r.status_code == 200
        body = r.json()
        added = {f["field_code"] for f in body["fields"]["added"]}
        removed = {f["field_code"] for f in body["fields"]["removed"]}
        assert added == {"pay_channel"}
        assert removed == {"order_status"}
        modified = {x["field_code"]: x["changes"] for x in body["fields"]["modified"]}
        assert "order_amount" in modified
        assert any(c["attribute"] == "description" for c in modified["order_amount"])

    def test_diff_same_version_rejected(self, client):
        p = create_project(client, "PRJ_DIFF_SAME")
        m = create_model(client, p["id"], "dwd_diff_same")
        publish_model(client, m["id"])
        r = client.get(
            f"/api/models/{m['id']}/versions/diff",
            params={"from_version": 1, "to_version": 1},
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_diff_missing_version(self, client):
        p = create_project(client, "PRJ_DIFF_MISS")
        m = create_model(client, p["id"], "dwd_diff_miss")
        publish_model(client, m["id"])
        r = client.get(
            f"/api/models/{m['id']}/versions/diff",
            params={"from_version": 1, "to_version": 9},
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "MODEL_VERSION_NOT_FOUND"


# ---------------------------------------------------------------- DDL 预览

class TestDDL:
    def test_three_engines(self, client):
        p = create_project(client, "PRJ_DDL")
        r = api(client, "POST", f"/api/projects/{p['id']}/models", json={
            "model_key": "dwd_ddl_order", "name": "测试订单明细",
            "description": "测试订单明细表（pytest）",
            "layer": "DWD", "model_type": "TABLE",
            "target_engine": "postgresql", "fields": good_fields(),
        })
        assert r.status_code == 201, r.text
        m = r.json()
        publish_model(client, m["id"])

        # PostgreSQL：schema 限定 + COMMENT ON
        r = api(client, "POST", f"/api/models/{m['id']}/ddl", json={
            "target_engine": "postgresql", "schema_name": "analytics",
        })
        assert r.status_code == 200
        ddl = r.json()["ddl"]
        assert 'CREATE TABLE IF NOT EXISTS "analytics"."dwd_ddl_order"' in ddl
        assert "COMMENT ON TABLE" in ddl
        assert "COMMENT ON COLUMN" in ddl
        assert "NUMERIC(18,2)" in ddl
        assert "PRIMARY KEY" in ddl
        assert r.json()["published_version_no"] == 1

        # MySQL：反引号 + ENGINE=InnoDB
        r = api(client, "POST", f"/api/models/{m['id']}/ddl", json={
            "target_engine": "mysql", "schema_name": "dwd",
        })
        ddl = r.json()["ddl"]
        assert "`dwd`.`dwd_ddl_order`" in ddl
        assert "ENGINE=InnoDB" in ddl
        assert "DECIMAL(18,2)" in ddl

        # Hive：列注释
        r = api(client, "POST", f"/api/models/{m['id']}/ddl", json={
            "target_engine": "hive", "schema_name": None,
        })
        ddl = r.json()["ddl"]
        assert "CREATE TABLE" in ddl
        assert "COMMENT '订单唯一标识'" in ddl

    def test_ddl_blocked_by_validation_errors(self, client):
        p = create_project(client, "PRJ_DDL_BAD")
        m = create_model(client, p["id"], "dwd_ddl_bad", fields=[
            {"field_code": "BadCol", "logical_type": "STRING",
             "nullable": True, "description": "x"},
        ])
        r = api(client, "POST", f"/api/models/{m['id']}/ddl", json={
            "target_engine": "postgresql", "schema_name": "analytics",
        })
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "MODEL_VALIDATION_FAILED"


# ---------------------------------------------------------------- 看板 / 事件 / 前端

class TestDashboardAndFrontend:
    def test_dashboard_counts(self, client):
        p = create_project(client, "PRJ_DASH")
        create_requirement(client, p["id"], "REQ_DASH_001")
        m = create_model(client, p["id"], "dwd_dash")
        publish_model(client, m["id"])

        r = client.get(f"/api/dashboard/{p['id']}")
        assert r.status_code == 200
        body = r.json()
        assert body["models"]["total"] == 1
        assert body["models"]["by_status"]["published"] == 1
        assert body["requirements"]["total"] == 1
        assert body["published_versions"] == 1
        assert body["pending_review"] == 0
        assert len(body["recent_events"]) > 0

    def test_events_contain_full_chain(self, client):
        p = create_project(client, "PRJ_EVT")
        m = create_model(client, p["id"], "dwd_evt")
        publish_model(client, m["id"])
        events = client.get(f"/api/projects/{p['id']}/events").json()
        types = [e["event_type"] for e in events]
        for expected in ("project.created", "model.draft.created",
                         "review.submitted", "review.decided",
                         "model.version.published"):
            assert expected in types

    def test_frontend_index_served(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "数据建模中心" in r.text or "Data Modeling" in r.text

        r = client.get("/app.js")
        assert r.status_code == 200


# ---------------------------------------------------------------- V0.8 表级约束 / 索引 / 关系图 / 影响分析

def _no_field_pk_fields() -> list[dict]:
    """移除字段级主键的一组字段（用于表级主键场景）。"""
    fields = good_fields()
    for f in fields:
        f["is_primary_key"] = False
    return fields


def _start_change(client: TestClient, model_id: int) -> int:
    """发起变更并返回当前 edit_revision（用于 PUT 的 expected_revision）。"""
    r = api(client, "POST", f"/api/models/{model_id}/start-change")
    assert r.status_code == 200, r.text
    m = client.get(f"/api/models/{model_id}").json()
    return m["edit_revision"]


def _put_model(client: TestClient, model_id: int, model: dict,
               fields: list[dict], revision: int, **extra) -> dict:
    body = {
        "model_key": model["model_key"], "name": model["name"],
        "description": model.get("description", ""),
        "layer": model["layer"], "model_type": model["model_type"],
        "target_engine": model["target_engine"],
        "subject_area_id": model.get("subject_area_id"),
        "requirement_ids": model.get("requirement_ids", []),
        "fields": fields, "expected_revision": revision,
    }
    body.update(extra)
    r = api(client, "PUT", f"/api/models/{model_id}", json=body)
    assert r.status_code == 200, r.text
    return r.json()


class TestV08ConstraintsIndexes:
    def test_constraint_crud_and_guards(self, client):
        p = create_project(client, "PRJ_CK")
        m = create_model(client, p["id"], "dwd_ck_order")
        mid = m["id"]

        # unique 约束创建
        r = api(client, "POST", f"/api/models/{mid}/constraints", json={
            "constraint_name": "uq_order_amount", "constraint_type": "unique",
            "field_codes": ["order_amount"], "description": "金额唯一",
        })
        assert r.status_code == 201, r.text
        c = r.json()
        assert c["field_codes"] == ["order_amount"]
        assert c["constraint_type"] == "unique"

        # 模型内重名 → 409
        r = api(client, "POST", f"/api/models/{mid}/constraints", json={
            "constraint_name": "uq_order_amount", "constraint_type": "unique",
            "field_codes": ["order_status"],
        })
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "DUPLICATE_CONSTRAINT_NAME"

        # check 约束：表达式引用模型字段
        r = api(client, "POST", f"/api/models/{mid}/constraints", json={
            "constraint_name": "ck_amount_positive", "constraint_type": "check",
            "expression": "order_amount > 0",
        })
        assert r.status_code == 201
        assert r.json()["expression"] == "order_amount > 0"

        # check 约束不允许带字段列表 → 422
        r = api(client, "POST", f"/api/models/{mid}/constraints", json={
            "constraint_name": "ck_bad_fields", "constraint_type": "check",
            "expression": "order_amount > 0", "field_codes": ["order_amount"],
        })
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "INVALID_CONSTRAINT_DEFINITION"

        # check 表达式引用不存在字段 → 422
        r = api(client, "POST", f"/api/models/{mid}/constraints", json={
            "constraint_name": "ck_bad_ref", "constraint_type": "check",
            "expression": "not_a_field > 0",
        })
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "INVALID_CHECK_EXPRESSION"

        # 约束字段不存在 → 422
        r = api(client, "POST", f"/api/models/{mid}/constraints", json={
            "constraint_name": "uq_bad_field", "constraint_type": "unique",
            "field_codes": ["ghost"],
        })
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "CONSTRAINT_FIELD_NOT_FOUND"

        # 更新（整载荷 PUT）
        r = api(client, "PUT", f"/api/models/{mid}/constraints/{c['id']}", json={
            "constraint_name": "uq_order_amount", "constraint_type": "unique",
            "field_codes": ["order_amount"], "description": "金额唯一（修订）",
        })
        assert r.status_code == 200
        assert r.json()["description"] == "金额唯一（修订）"

        # 列表
        cons = client.get(f"/api/models/{mid}/constraints").json()
        assert {x["constraint_name"] for x in cons} == {
            "uq_order_amount", "ck_amount_positive"}

        # 删除
        r = api(client, "DELETE", f"/api/models/{mid}/constraints/{c['id']}")
        assert r.status_code == 200
        cons = client.get(f"/api/models/{mid}/constraints").json()
        assert [x["constraint_name"] for x in cons] == ["ck_amount_positive"]

    def test_constraint_pk_rules_and_not_null_effect(self, client):
        p = create_project(client, "PRJ_PK")
        m = create_model(client, p["id"], "dwd_pk_order",
                         fields=_no_field_pk_fields())
        mid = m["id"]

        # 单列表级主键 → 422
        r = api(client, "POST", f"/api/models/{mid}/constraints", json={
            "constraint_name": "pk_single", "constraint_type": "primary_key",
            "field_codes": ["order_id"],
        })
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "INVALID_CONSTRAINT_DEFINITION"

        # 与字段级主键互斥：另建一个带字段级主键的模型
        m2 = create_model(client, p["id"], "dwd_pk_conflict")
        r = api(client, "POST", f"/api/models/{m2['id']}/constraints", json={
            "constraint_name": "pk_two", "constraint_type": "primary_key",
            "field_codes": ["order_id", "order_status"],
        })
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "PRIMARY_KEY_CONFLICT"

        # 复合表级主键（≥2 字段）+ 参与字段自动置为非空
        r = api(client, "POST", f"/api/models/{mid}/constraints", json={
            "constraint_name": "pk_order", "constraint_type": "primary_key",
            "field_codes": ["order_id", "order_amount"],
        })
        assert r.status_code == 201
        m = client.get(f"/api/models/{mid}").json()
        nullable = {f["field_code"]: f["nullable"] for f in m["fields"]}
        assert nullable["order_id"] is False
        assert nullable["order_amount"] is False

    def test_constraint_fk_rules(self, client):
        p = create_project(client, "PRJ_FK")
        m1 = create_model(client, p["id"], "dwd_fk_order")
        m2 = create_model(client, p["id"], "dim_fk_cust")

        # 跨项目目标 → 422
        p2 = create_project(client, "PRJ_FK_X")
        mx = create_model(client, p2["id"], "dwd_fk_x")
        r = api(client, "POST", f"/api/models/{m1['id']}/constraints", json={
            "constraint_name": "fk_cross", "constraint_type": "foreign_key",
            "field_codes": ["order_id"], "referenced_model_id": mx["id"],
            "referenced_field_codes": ["order_id"],
        })
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "FK_TARGET_MODEL_INVALID"

        # 字段对数量不一致 → 422
        r = api(client, "POST", f"/api/models/{m1['id']}/constraints", json={
            "constraint_name": "fk_mismatch", "constraint_type": "foreign_key",
            "field_codes": ["order_id", "order_amount"],
            "referenced_model_id": m2["id"],
            "referenced_field_codes": ["order_id"],
        })
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "FK_FIELD_PAIR_MISMATCH"

        # 正常外键（ON DELETE CASCADE）
        r = api(client, "POST", f"/api/models/{m1['id']}/constraints", json={
            "constraint_name": "fk_cust", "constraint_type": "foreign_key",
            "field_codes": ["order_id"], "referenced_model_id": m2["id"],
            "referenced_field_codes": ["order_id"], "on_delete": "CASCADE",
        })
        assert r.status_code == 201
        assert r.json()["on_delete"] == "CASCADE"

        # SET NULL 要求源字段可空
        m3 = create_model(client, p["id"], "dwd_fk_strict", fields=[
            {"field_code": "order_id", "logical_type": "BIGINT",
             "nullable": False, "description": "订单ID"},
            {"field_code": "cust_id", "logical_type": "BIGINT",
             "nullable": False, "description": "客户ID"},
        ])
        r = api(client, "POST", f"/api/models/{m3['id']}/constraints", json={
            "constraint_name": "fk_setnull", "constraint_type": "foreign_key",
            "field_codes": ["cust_id"], "referenced_model_id": m2["id"],
            "referenced_field_codes": ["order_id"], "on_delete": "SET NULL",
        })
        assert r.status_code == 422

    def test_index_crud_and_guards(self, client):
        p = create_project(client, "PRJ_IX")
        m = create_model(client, p["id"], "dwd_ix_order")
        mid = m["id"]

        # 创建索引
        r = api(client, "POST", f"/api/models/{mid}/indexes", json={
            "index_name": "idx_order_status", "index_type": "btree",
            "field_codes": ["order_status"], "is_unique": False,
            "description": "状态筛选",
        })
        assert r.status_code == 201
        ix = r.json()
        assert ix["index_type"] == "btree"

        # 重名 → 409
        r = api(client, "POST", f"/api/models/{mid}/indexes", json={
            "index_name": "idx_order_status", "index_type": "btree",
            "field_codes": ["order_status"],
        })
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "DUPLICATE_INDEX_NAME"

        # 字段不存在 → 422
        r = api(client, "POST", f"/api/models/{mid}/indexes", json={
            "index_name": "idx_bad", "index_type": "btree",
            "field_codes": ["ghost"],
        })
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "INDEX_FIELD_NOT_FOUND"

        # 复合索引（多字段顺序保留）
        r = api(client, "POST", f"/api/models/{mid}/indexes", json={
            "index_name": "idx_multi", "index_type": "btree",
            "field_codes": ["order_status", "created_at"],
        })
        assert r.status_code == 201
        assert r.json()["field_codes"] == ["order_status", "created_at"]

        # 列表 + 删除
        idxs = client.get(f"/api/models/{mid}/indexes").json()
        assert len(idxs) == 2
        r = api(client, "DELETE", f"/api/models/{mid}/indexes/{ix['id']}")
        assert r.status_code == 200
        idxs = client.get(f"/api/models/{mid}/indexes").json()
        assert [x["index_name"] for x in idxs] == ["idx_multi"]

    def test_constraints_readonly_when_not_draft(self, client):
        p = create_project(client, "PRJ_CK_RO")
        m = create_model(client, p["id"], "dwd_ck_ro")
        publish_model(client, m["id"])

        # 已发布模型：创建约束 → 409
        r = api(client, "POST", f"/api/models/{m['id']}/constraints", json={
            "constraint_name": "uq_ro", "constraint_type": "unique",
            "field_codes": ["order_amount"],
        })
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "MODEL_NOT_EDITABLE"

        # 已发布模型：创建索引 → 409
        r = api(client, "POST", f"/api/models/{m['id']}/indexes", json={
            "index_name": "idx_ro", "index_type": "btree",
            "field_codes": ["order_status"],
        })
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "MODEL_NOT_EDITABLE"

        # 发起变更后恢复可编辑
        api(client, "POST", f"/api/models/{m['id']}/start-change")
        r = api(client, "POST", f"/api/models/{m['id']}/indexes", json={
            "index_name": "idx_ro", "index_type": "btree",
            "field_codes": ["order_status"],
        })
        assert r.status_code == 201


class TestV08DDLConstraints:
    def _model_with_constraints(self, client, key="dwd_ddl_ck",
                                with_hash=False):
        """uq 约束 + check 约束 + btree 索引（可选 hash 索引）的模型。"""
        p = create_project(client, "PRJ_" + key.upper())
        m = create_model(client, p["id"], key)
        api(client, "POST", f"/api/models/{m['id']}/constraints", json={
            "constraint_name": "uq_order_amount", "constraint_type": "unique",
            "field_codes": ["order_amount"],
        })
        api(client, "POST", f"/api/models/{m['id']}/constraints", json={
            "constraint_name": "ck_amount_positive", "constraint_type": "check",
            "expression": "order_amount > 0",
        })
        api(client, "POST", f"/api/models/{m['id']}/indexes", json={
            "index_name": "idx_order_status", "index_type": "btree",
            "field_codes": ["order_status"],
        })
        if with_hash:
            api(client, "POST", f"/api/models/{m['id']}/indexes", json={
                "index_name": "idx_hash_amount", "index_type": "hash",
                "field_codes": ["order_amount"],
            })
        publish_model(client, m["id"])
        return m

    def test_pg_named_constraints_and_indexes(self, client):
        m = self._model_with_constraints(client)
        r = api(client, "POST", f"/api/models/{m['id']}/ddl", json={
            "target_engine": "postgresql", "schema_name": "analytics",
        })
        assert r.status_code == 200, r.text
        ddl = r.json()["ddl"]
        # 命名表级约束
        assert 'CONSTRAINT "uq_order_amount" UNIQUE ("order_amount")' in ddl
        assert 'CONSTRAINT "ck_amount_positive" CHECK (order_amount > 0)' in ddl
        # CREATE INDEX 语句（PG btree）
        assert 'CREATE INDEX "idx_order_status" ON "analytics"."dwd_ddl_ck"' \
               ' USING btree ("order_status")' in ddl
        # 顺序：CREATE TABLE → CREATE INDEX → COMMENT ON
        assert ddl.index("CREATE TABLE") < ddl.index("CREATE INDEX")
        assert ddl.index("CREATE INDEX") < ddl.index("COMMENT ON")

    def test_pg_hash_index_supported(self, client):
        m = self._model_with_constraints(client, "dwd_ddl_hash",
                                         with_hash=True)
        r = api(client, "POST", f"/api/models/{m['id']}/ddl", json={
            "target_engine": "postgresql", "schema_name": "analytics",
        })
        assert r.status_code == 200, r.text
        assert 'USING hash ("order_amount")' in r.json()["ddl"]

    def test_mysql_btree_only_and_hash_rejected(self, client):
        # 纯 btree 模型：MySQL 正常生成
        m = self._model_with_constraints(client, "dwd_ddl_my")
        r = api(client, "POST", f"/api/models/{m['id']}/ddl", json={
            "target_engine": "mysql", "schema_name": "dwd",
        })
        assert r.status_code == 200, r.text
        ddl = r.json()["ddl"]
        assert "CONSTRAINT `uq_order_amount` UNIQUE (`order_amount`)" in ddl
        assert "USING btree" not in ddl  # MySQL 无 USING 子句
        assert "CREATE INDEX `idx_order_status` ON `dwd`.`dwd_ddl_my`" in ddl
        assert "ENGINE=InnoDB" in ddl

        # 含 hash 索引的模型：MySQL 生成前拒绝 → 422
        m2 = self._model_with_constraints(client, "dwd_ddl_my_hash",
                                          with_hash=True)
        r = api(client, "POST", f"/api/models/{m2['id']}/ddl", json={
            "target_engine": "mysql", "schema_name": "dwd",
        })
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "DDL_GENERATION_FAILED"

    def test_hive_logical_constraints(self, client):
        m = self._model_with_constraints(client, "dwd_ddl_hive")
        r = api(client, "POST", f"/api/models/{m['id']}/ddl", json={
            "target_engine": "hive", "schema_name": None,
        })
        assert r.status_code == 200, r.text
        body = r.json()
        ddl = body["ddl"]
        # Hive：约束与索引仅逻辑注释
        assert "uq_order_amount" in ddl
        assert "idx_order_status" in ddl
        assert body["unsupported_features"]

    def test_unique_gin_rejected_at_creation(self, client):
        p = create_project(client, "PRJ_DDL_GIN")
        m = create_model(client, p["id"], "dwd_ddl_gin")
        # PG 模型上创建唯一 GIN 索引 → 422（能力矩阵前置拒绝）
        r = api(client, "POST", f"/api/models/{m['id']}/indexes", json={
            "index_name": "idx_gin_unique", "index_type": "gin",
            "field_codes": ["order_status"], "is_unique": True,
        })
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "GIN_UNIQUE_INDEX_UNSUPPORTED"

    def test_unique_gin_rejected_at_ddl_generation(self, client):
        # Hive 模型：唯一 GIN 作为逻辑元数据允许创建
        p = create_project(client, "PRJ_DDL_GIN2")
        m = create_model(client, p["id"], "dwd_ddl_gin2",
                         target_engine="hive")
        r = api(client, "POST", f"/api/models/{m['id']}/indexes", json={
            "index_name": "idx_gin_unique", "index_type": "gin",
            "field_codes": ["order_status"], "is_unique": True,
        })
        assert r.status_code == 201, r.text
        publish_model(client, m["id"])
        # 显式请求 PG 生成：DDL 层防线拒绝唯一 GIN → 422
        r = api(client, "POST", f"/api/models/{m['id']}/ddl", json={
            "target_engine": "postgresql", "schema_name": "analytics",
        })
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "DDL_GENERATION_FAILED"


class TestV08ModelGraph:
    def test_graph_nodes_and_edges(self, client):
        p = create_project(client, "PRJ_GRAPH")
        m1 = create_model(client, p["id"], "dwd_graph_order")
        m2 = create_model(client, p["id"], "dim_graph_cust")
        publish_model(client, m2["id"])

        r = api(client, "POST", f"/api/models/{m1['id']}/relationships", json={
            "target_model_id": m2["id"], "relationship_type": "joins_to",
            "source_fields": ["order_id"], "target_fields": ["order_id"],
            "cardinality": "many_to_one", "description": "订单关联客户",
        })
        assert r.status_code == 201

        r = client.get(f"/api/projects/{p['id']}/model-graph")
        assert r.status_code == 200
        g = r.json()
        assert len(g["nodes"]) == 2
        nodes = {n["model_key"]: n for n in g["nodes"]}
        assert nodes["dwd_graph_order"]["status"] == "draft"
        assert nodes["dwd_graph_order"]["field_count"] == 4
        assert nodes["dwd_graph_order"]["published_version_no"] is None
        assert nodes["dim_graph_cust"]["status"] == "published"
        assert nodes["dim_graph_cust"]["published_version_no"] == 1
        assert nodes["dim_graph_cust"]["layer"] == "DWD"

        assert len(g["edges"]) == 1
        e = g["edges"][0]
        assert e["source_model_id"] == m1["id"]
        assert e["target_model_id"] == m2["id"]
        assert e["relationship_type"] == "joins_to"
        assert e["source_fields"] == ["order_id"]
        assert e["target_fields"] == ["order_id"]
        # 边不含项目/审计字段
        assert "project_id" not in e
        assert "created_by" not in e


class TestV08Impact:
    def _two_models_with_edge(self, client, key="dwd_imp_order"):
        p = create_project(client, "PRJ_" + key.upper())
        m1 = create_model(client, p["id"], key)
        m2 = create_model(client, p["id"], "dim_imp_cust")
        r = api(client, "POST", f"/api/models/{m1['id']}/relationships", json={
            "target_model_id": m2["id"], "relationship_type": "joins_to",
            "source_fields": ["order_id"], "target_fields": ["order_id"],
        })
        assert r.status_code == 201, r.text
        return p, m1, m2

    def test_no_published_baseline(self, client):
        p, m1, m2 = self._two_models_with_edge(client, "dwd_imp_nb")
        r = client.get(f"/api/models/{m1['id']}/impact")
        assert r.status_code == 200
        body = r.json()
        assert body["analysis_mode"] == "no_published_baseline"
        assert body["changed_field_codes"] is None  # 无基线 → 模型级范围
        # 模型级范围沿出边全量传播：m2 受影响
        assert [i["model_key"] for i in body["impacted_models"]] == ["dim_imp_cust"]
        assert body["impacted_models"][0]["requires_manual_confirmation"] is True
        assert body["direction_semantics"]

    def test_field_level_propagation(self, client):
        p, m1, m2 = self._two_models_with_edge(client, "dwd_imp_fld")
        publish_model(client, m1["id"], "V1")

        # 草稿仅改 order_id 描述
        rev = _start_change(client, m1["id"])
        fields = good_fields()
        fields[0]["description"] = "订单唯一标识（修订）"
        _put_model(client, m1["id"], client.get(
            f"/api/models/{m1['id']}").json(), fields, rev)

        r = client.get(f"/api/models/{m1['id']}/impact")
        assert r.status_code == 200
        body = r.json()
        assert body["analysis_mode"] == "draft_vs_latest_published"
        assert body["changed_field_codes"] == ["order_id"]
        assert len(body["impacted_models"]) == 1
        im = body["impacted_models"][0]
        assert im["model_key"] == "dim_imp_cust"
        assert im["affected_fields"] == ["order_id"]
        assert len(im["paths"]) == 1  # 单条传播路径
        assert im["includes_baseline_edge"] is False
        assert im["requires_manual_confirmation"] is False

    def test_model_level_scope(self, client):
        p, m1, m2 = self._two_models_with_edge(client, "dwd_imp_ml")
        publish_model(client, m1["id"], "V1")

        # 草稿仅改名（模型属性变化 → 模型级范围）
        rev = _start_change(client, m1["id"])
        cur = client.get(f"/api/models/{m1['id']}").json()
        _put_model(client, m1["id"], cur, cur["fields"], rev,
                   name="测试订单明细（更名）")

        r = client.get(f"/api/models/{m1['id']}/impact")
        body = r.json()
        assert body["analysis_mode"] == "draft_vs_latest_published"
        assert body["changed_field_codes"] is None  # 模型级
        assert body["change_summary"]["model_attributes"]
        im = body["impacted_models"][0]
        assert im["affected_fields"] is None  # 下游模型级
        assert im["requires_manual_confirmation"] is True

    def test_field_codes_narrowing(self, client):
        p, m1, m2 = self._two_models_with_edge(client, "dwd_imp_narrow")
        publish_model(client, m1["id"], "V1")

        # 草稿改两个字段描述
        rev = _start_change(client, m1["id"])
        fields = good_fields()
        fields[0]["description"] = "订单唯一标识（修订）"
        fields[1]["description"] = "订单金额（修订）"
        _put_model(client, m1["id"], client.get(
            f"/api/models/{m1['id']}").json(), fields, rev)

        # 全量：order_id 命中边 → m2 受影响
        body = client.get(f"/api/models/{m1['id']}/impact").json()
        assert body["changed_field_codes"] == ["order_amount", "order_id"]
        assert len(body["impacted_models"]) == 1

        # 收窄到 order_amount：不命中边 → 无影响
        body = client.get(
            f"/api/models/{m1['id']}/impact",
            params={"field_codes": "order_amount"}).json()
        assert body["changed_field_codes"] == ["order_amount"]
        assert body["impacted_models"] == []

        # 收窄到 order_id：命中边 → m2 受影响
        body = client.get(
            f"/api/models/{m1['id']}/impact",
            params={"field_codes": "order_id"}).json()
        assert body["changed_field_codes"] == ["order_id"]
        assert len(body["impacted_models"]) == 1

    def test_baseline_edge_merge(self, client):
        p, m1, m2 = self._two_models_with_edge(client, "dwd_imp_base")
        publish_model(client, m1["id"], "V1")  # V1 快照含关系

        # 发布后删除关系（当前出边为空，但基线快照仍有）
        rels = client.get(f"/api/models/{m1['id']}/relationships").json()
        api(client, "DELETE", f"/api/models/{m1['id']}/relationships/{rels[0]['id']}")

        # 发起变更并改 order_id（关系删除本身已触发模型级范围）
        rev = _start_change(client, m1["id"])
        fields = good_fields()
        fields[0]["description"] = "订单唯一标识（修订）"
        _put_model(client, m1["id"], client.get(
            f"/api/models/{m1['id']}").json(), fields, rev)

        body = client.get(f"/api/models/{m1['id']}/impact").json()
        # 关系删除 → 模型级范围（changed_field_codes=None）
        assert body["changed_field_codes"] is None
        assert body["change_summary"]["model_level_reasons"]
        assert len(body["impacted_models"]) == 1
        im = body["impacted_models"][0]
        assert im["model_key"] == "dim_imp_cust"
        assert im["affected_fields"] is None  # 模型级传播
        assert im["includes_baseline_edge"] is True  # 历史基线边仍参与遍历
        assert im["requires_manual_confirmation"] is True


class TestV08SnapshotDiff:
    def test_constraints_indexes_relationships_in_snapshot_and_diff(self, client):
        p = create_project(client, "PRJ_SNAP")
        m1 = create_model(client, p["id"], "dwd_snap_order")
        m2 = create_model(client, p["id"], "dim_snap_cust")

        # V1 前：唯一约束 + 索引 + 关系
        api(client, "POST", f"/api/models/{m1['id']}/constraints", json={
            "constraint_name": "uq_order_amount", "constraint_type": "unique",
            "field_codes": ["order_amount"], "description": "金额唯一",
        })
        api(client, "POST", f"/api/models/{m1['id']}/indexes", json={
            "index_name": "idx_order_status", "index_type": "btree",
            "field_codes": ["order_status"],
        })
        api(client, "POST", f"/api/models/{m1['id']}/relationships", json={
            "target_model_id": m2["id"], "relationship_type": "joins_to",
            "source_fields": ["order_id"], "target_fields": ["order_id"],
        })
        publish_model(client, m1["id"], "V1")

        # V2 前：新增约束/索引 + 修改约束描述
        rev = _start_change(client, m1["id"])
        api(client, "POST", f"/api/models/{m1['id']}/constraints", json={
            "constraint_name": "ck_amount_positive", "constraint_type": "check",
            "expression": "order_amount > 0",
        })
        api(client, "POST", f"/api/models/{m1['id']}/indexes", json={
            "index_name": "idx_multi", "index_type": "btree",
            "field_codes": ["order_status", "created_at"],
        })
        cons = client.get(f"/api/models/{m1['id']}/constraints").json()
        uq = next(c for c in cons if c["constraint_name"] == "uq_order_amount")
        api(client, "PUT", f"/api/models/{m1['id']}/constraints/{uq['id']}", json={
            "constraint_name": "uq_order_amount", "constraint_type": "unique",
            "field_codes": ["order_amount"], "description": "金额唯一（修订）",
        })
        publish_model(client, m1["id"], "V2")

        body = client.get(
            f"/api/models/{m1['id']}/versions/diff",
            params={"from_version": 1, "to_version": 2}).json()
        # 约束：新增 check + 修改 unique 描述
        ck_names = {c["constraint_name"] for c in body["constraints"]["added"]}
        assert ck_names == {"ck_amount_positive"}
        mod = {c["constraint_name"]: c for c in body["constraints"]["modified"]}
        assert "uq_order_amount" in mod
        attrs = {ch["attribute"] for ch in mod["uq_order_amount"]["changes"]}
        assert "description" in attrs
        # 索引：新增复合索引
        ix_names = {i["index_name"] for i in body["indexes"]["added"]}
        assert ix_names == {"idx_multi"}
        # 关系：V1/V2 均有 → 无变化
        assert body["relationships"]["added"] == []
        assert body["relationships"]["removed"] == []
        assert body["relationships"]["modified"] == []

    def test_legacy_snapshot_skips_categories(self, client):
        """V0.8 前发布的快照缺 constraints/indexes/relationships 键时，
        差异必须整体跳过该类别（避免把 V2 的新对象误报为新增）。"""
        import json

        from sqlalchemy import select

        from backend.app import models as db_models
        from backend.app.db import SessionLocal

        p = create_project(client, "PRJ_SNAP_LEGACY")
        m1 = create_model(client, p["id"], "dwd_snap_legacy")
        m2 = create_model(client, p["id"], "dim_snap_legacy")
        api(client, "POST", f"/api/models/{m1['id']}/constraints", json={
            "constraint_name": "uq_legacy", "constraint_type": "unique",
            "field_codes": ["order_amount"],
        })
        api(client, "POST", f"/api/models/{m1['id']}/relationships", json={
            "target_model_id": m2["id"], "relationship_type": "joins_to",
            "source_fields": ["order_id"], "target_fields": ["order_id"],
        })
        publish_model(client, m1["id"], "V1")

        # 将 V1 快照降级为 V0.7 格式（剥离三个新类别键）
        with SessionLocal() as db:
            ver = db.scalar(
                select(db_models.ModelVersion)
                .where(db_models.ModelVersion.model_id == m1["id"])
            )
            snap = json.loads(ver.snapshot_json)
            for key in ("constraints", "indexes", "relationships"):
                snap.pop(key, None)
            ver.snapshot_json = json.dumps(snap, ensure_ascii=False)
            db.commit()

        # V2：无任何变更（仅重新发布）
        api(client, "POST", f"/api/models/{m1['id']}/start-change")
        publish_model(client, m1["id"], "V2")

        body = client.get(
            f"/api/models/{m1['id']}/versions/diff",
            params={"from_version": 1, "to_version": 2}).json()
        # 三个类别整体跳过：即使 V2 快照含这些对象，也不得报新增
        assert body["constraints"]["added"] == []
        assert body["indexes"]["added"] == []
        assert body["relationships"]["added"] == []
        assert body["model_changes"] == []
        assert body["fields"]["added"] == []



# ---------------------------------------------------------------- 迁移烟测

class TestMigrationSmoke:
    def test_upgrade_downgrade_upgrade(self):
        """独立临时库 + 子进程跑真实 alembic CLI：upgrade → current → downgrade → upgrade。"""
        db_file = Path(_TMP_DIR) / "migration_smoke.db"
        env = dict(os.environ)
        env["DATABASE_URL"] = f"sqlite:///{db_file}"
        env["APP_ENV"] = "test"

        def alembic(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                [sys.executable, "-m", "alembic", *args],
                cwd=str(PROJECT_ROOT), env=env,
                capture_output=True, text=True, timeout=120,
            )

        r = alembic("upgrade", "head")
        assert r.returncode == 0, r.stderr

        r = alembic("current")
        assert r.returncode == 0, r.stderr
        assert "7f0c38e854e5" in r.stdout

        r = alembic("downgrade", "-1")
        assert r.returncode == 0, r.stderr

        r = alembic("upgrade", "head")
        assert r.returncode == 0, r.stderr

        r = alembic("current")
        assert "7f0c38e854e5" in r.stdout


# ---------------------------------------------------------------- V0.9：图谱快照 / 视图 / diff / path / 外部血缘

def _v09_chain(client: TestClient, prefix: str):
    """项目 + 三模型链 m1 -> m2 -> m3（关系在发布前建立，链式 BFS 用）。"""
    p = create_project(client, f"PRJ_{prefix.upper()}")
    m1 = create_model(client, p["id"], f"dwd_{prefix}_a")
    m2 = create_model(client, p["id"], f"dwd_{prefix}_b")
    m3 = create_model(client, p["id"], f"dwd_{prefix}_c")
    for src, tgt in ((m1, m2), (m2, m3)):
        r = api(client, "POST", f"/api/models/{src['id']}/relationships", json={
            "target_model_id": tgt["id"], "relationship_type": "depends_on",
        })
        assert r.status_code == 201, r.text
    return p, m1, m2, m3


def _v09_two_models(client: TestClient, prefix: str):
    """项目 + 两个无关系模型（血缘导入专用，边只来自外部扫描）。"""
    p = create_project(client, f"PRJ_{prefix.upper()}")
    m1 = create_model(client, p["id"], f"ods_{prefix}_src")
    m2 = create_model(client, p["id"], f"dwd_{prefix}_tgt")
    return p, m1, m2


def _v09_import(client: TestClient, pid: int, run_id: str, edges: list[dict],
                source_system: str = "airflow", scope_key: str = "default",
                scan_mode: str = "incremental", complete_scan: bool | None = None,
                **extra):
    """血缘导入快捷封装。"""
    body = {
        "source_system": source_system, "run_id": run_id,
        "scan_mode": scan_mode, "scope_key": scope_key, "edges": edges,
    }
    if complete_scan is not None:
        body["complete_scan"] = complete_scan
    body.update(extra)
    return api(client, "POST", f"/api/projects/{pid}/lineage/import", json=body)


def _v09_edge(source_model_key: str, target_model_key: str,
              source_ref: str = "ref-1", **extra) -> dict:
    edge = {"source_ref": source_ref, "source_model_key": source_model_key,
            "target_model_key": target_model_key}
    edge.update(extra)
    return edge


def _backdate_edge(edge_id: int, days: int = 3) -> None:
    """把外部边的 last_seen_at 改到过去（直接写测试库，模拟"N 天没有扫描"）。"""
    db_url = os.environ["DATABASE_URL"]
    db_path = db_url.removeprefix("sqlite:///")
    old = (datetime.now(timezone.utc) - timedelta(days=days)).replace(
        tzinfo=None).isoformat(sep=" ")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE external_lineage_edges SET last_seen_at = ? WHERE id = ?",
            (old, edge_id),
        )
        conn.commit()


def _validation_codes(r) -> set[str]:
    """422 响应 details.errors 中的错误码集合。"""
    assert r.status_code == 422, r.text
    return {e["code"] for e in r.json()["error"]["details"]["errors"]}


class TestV09GraphSnapshot:
    def test_publish_writes_snapshot_in_transaction(self, client):
        p, m1, m2, m3 = _v09_chain(client, "v09snap")
        r = publish_model(client, m1["id"], "V1")
        assert r["graph_snapshot_no"] == 1
        assert len(r["graph_hash"]) == 64

        snaps = client.get(
            f"/api/projects/{p['id']}/model-graph/snapshots").json()
        assert len(snaps) == 1
        s = snaps[0]
        assert s["snapshot_type"] == "publish"
        assert s["trigger_model_id"] == m1["id"]
        assert s["trigger_model_version_no"] == 1
        assert s["node_count"] == 1
        assert s["edge_count"] == 0
        assert s["omitted_edge_count"] == 1  # m2 未发布 → 出向边被省略

        r2 = publish_model(client, m2["id"], "V2")
        assert r2["graph_snapshot_no"] == 2
        snaps = client.get(
            f"/api/projects/{p['id']}/model-graph/snapshots").json()
        assert [s["snapshot_no"] for s in snaps] == [2, 1]  # 倒序
        s2 = snaps[0]
        assert s2["node_count"] == 2
        assert s2["edge_count"] == 1
        assert s2["omitted_edge_count"] == 1  # m2->m3 目标未发布

    def test_snapshot_detail_returns_immutable_graph(self, client):
        p, m1, m2, m3 = _v09_chain(client, "snapdet")
        publish_model(client, m1["id"])
        publish_model(client, m2["id"])
        r = client.get(f"/api/projects/{p['id']}/model-graph/snapshots/2")
        assert r.status_code == 200
        body = r.json()
        assert body["snapshot_no"] == 2
        g = body["graph"]
        assert g["schema_version"] == "v0.9"
        assert len(g["nodes"]) == 2
        nodes = {n["model_key"]: n for n in g["nodes"]}
        assert nodes[m2["model_key"]]["version_no"] == 1
        assert nodes[m2["model_key"]]["field_count"] == 4
        assert len(g["edges"]) == 1
        e = g["edges"][0]
        assert e["edge_key"] == f"design:{m1['id']}:{m2['id']}:depends_on"
        assert e["source_version_no"] == 1
        assert e["target_version_no"] == 1
        assert body["graph_hash"] == g["graph_hash"]
        assert len(body["graph_hash"]) == 64

    def test_snapshot_not_found(self, client):
        p, m1, m2, m3 = _v09_chain(client, "snapnf")
        publish_model(client, m1["id"])
        r = client.get(f"/api/projects/{p['id']}/model-graph/snapshots/99")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "GRAPH_SNAPSHOT_NOT_FOUND"

    def test_snapshot_view_requires_snapshot_no(self, client):
        p, m1, m2, m3 = _v09_chain(client, "snapreq")
        publish_model(client, m1["id"])
        r = client.get(f"/api/projects/{p['id']}/model-graph?view=snapshot")
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "GRAPH_SNAPSHOT_REQUIRED"

    def test_published_latest_without_snapshot_404(self, client):
        p = create_project(client, "PRJ_SNAPEMPTY")
        create_model(client, p["id"], "dwd_snapempty_order")
        r = client.get(
            f"/api/projects/{p['id']}/model-graph?view=published_latest")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "GRAPH_SNAPSHOT_NOT_FOUND"

    def test_published_latest_view(self, client):
        p, m1, m2, m3 = _v09_chain(client, "publatest")
        publish_model(client, m1["id"])
        publish_model(client, m2["id"])
        r = client.get(
            f"/api/projects/{p['id']}/model-graph?view=published_latest")
        assert r.status_code == 200
        g = r.json()
        assert g["view"] == "published_latest"
        assert g["snapshot_no"] == 2
        assert g["snapshot_type"] == "publish"
        assert len(g["nodes"]) == 2
        assert len(g["edges"]) == 1
        assert len(g["graph_hash"]) == 64

    def test_current_view_default_and_invalid_view(self, client):
        p, m1, m2, m3 = _v09_chain(client, "curview")
        g = client.get(f"/api/projects/{p['id']}/model-graph").json()
        assert g["view"] == "current"
        assert len(g["nodes"]) == 3  # 草稿实时：无需发布
        assert len(g["edges"]) == 2
        r = client.get(f"/api/projects/{p['id']}/model-graph?view=bogus")
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"


class TestV09GraphDiff:
    def test_diff_added_nodes_and_edges(self, client):
        p, m1, m2, m3 = _v09_chain(client, "diffadd")
        publish_model(client, m1["id"])  # 快照 1：1 节点
        publish_model(client, m2["id"])  # 快照 2：2 节点 1 边
        r = client.get(
            f"/api/projects/{p['id']}/model-graph/diff"
            f"?from_snapshot_no=1&to_snapshot_no=2")
        assert r.status_code == 200
        d = r.json()
        assert d["from_hash"] != d["to_hash"]
        assert [n["model_key"] for n in d["nodes"]["added"]] == [m2["model_key"]]
        assert d["nodes"]["removed"] == []
        assert [e["edge_key"] for e in d["edges"]["added"]] == [
            f"design:{m1['id']}:{m2['id']}:depends_on"]
        assert d["edges"]["removed"] == []

    def test_diff_modified_attributes_on_republish(self, client):
        p, m1, m2, m3 = _v09_chain(client, "diffmod")
        publish_model(client, m2["id"], "V1")  # m2 单独发布（快照 1）
        rev = _start_change(client, m2["id"])
        cur = client.get(f"/api/models/{m2['id']}").json()
        _put_model(client, m2["id"], cur, cur["fields"], rev,
                   name="下游模型（更名）")
        r = publish_model(client, m2["id"], "V2")  # 快照 2：V2
        assert r["graph_snapshot_no"] == 2
        d = client.get(
            f"/api/projects/{p['id']}/model-graph/diff"
            f"?from_snapshot_no=1&to_snapshot_no=2").json()
        attrs = {(m["attribute"], m["from"], m["to"])
                 for m in d["nodes"]["modified"]}
        assert ("version_no", 1, 2) in attrs
        assert ("name", "测试订单明细", "下游模型（更名）") in attrs
        assert d["nodes"]["added"] == []
        assert d["edges"]["added"] == []

    def test_diff_same_snapshot_empty(self, client):
        p, m1, m2, m3 = _v09_chain(client, "diffsame")
        publish_model(client, m1["id"])
        d = client.get(
            f"/api/projects/{p['id']}/model-graph/diff"
            f"?from_snapshot_no=1&to_snapshot_no=1").json()
        for section in (d["nodes"], d["edges"]):
            assert section["added"] == []
            assert section["removed"] == []
            assert section["modified"] == []
        assert d["from_hash"] == d["to_hash"]

    def test_diff_snapshot_not_found(self, client):
        p, m1, m2, m3 = _v09_chain(client, "diffnf")
        publish_model(client, m1["id"])
        r = client.get(
            f"/api/projects/{p['id']}/model-graph/diff"
            f"?from_snapshot_no=1&to_snapshot_no=99")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "GRAPH_SNAPSHOT_NOT_FOUND"


class TestV09GraphPath:
    def test_path_downstream_forward(self, client):
        p, m1, m2, m3 = _v09_chain(client, "pathds")
        r = client.get(
            f"/api/projects/{p['id']}/model-graph/path"
            f"?source_model_id={m1['id']}&target_model_id={m3['id']}"
            f"&direction=downstream")
        assert r.status_code == 200
        b = r.json()
        assert b["found"] is True
        assert b["direction"] == "downstream"
        assert [n["id"] for n in b["nodes"]] == [m1["id"], m2["id"], m3["id"]]
        assert b["distance"] == 2
        assert len(b["edges"]) == 2
        assert all(e["traversal_direction"] == "forward" for e in b["edges"])

    def test_path_upstream_reverse(self, client):
        p, m1, m2, m3 = _v09_chain(client, "pathus")
        r = client.get(
            f"/api/projects/{p['id']}/model-graph/path"
            f"?source_model_id={m3['id']}&target_model_id={m1['id']}"
            f"&direction=upstream")
        assert r.status_code == 200
        b = r.json()
        assert b["found"] is True
        assert [n["id"] for n in b["nodes"]] == [m3["id"], m2["id"], m1["id"]]
        assert all(e["traversal_direction"] == "reverse" for e in b["edges"])

    def test_path_any_bidirectional(self, client):
        p, m1, m2, m3 = _v09_chain(client, "pathany")
        r = client.get(
            f"/api/projects/{p['id']}/model-graph/path"
            f"?source_model_id={m3['id']}&target_model_id={m1['id']}"
            f"&direction=any")
        b = r.json()
        assert b["found"] is True
        assert b["distance"] == 2

    def test_path_no_path(self, client):
        p, m1, m2, m3 = _v09_chain(client, "pathnp")
        create_model(client, p["id"], "dwd_pathnp_iso")  # 孤立模型
        iso = client.get(
            f"/api/projects/{p['id']}/models").json()
        iso_id = next(m["id"] for m in iso
                      if m["model_key"] == "dwd_pathnp_iso")
        r = client.get(
            f"/api/projects/{p['id']}/model-graph/path"
            f"?source_model_id={m1['id']}&target_model_id={iso_id}"
            f"&direction=downstream")
        b = r.json()
        assert b["found"] is False
        assert b["truncated"] is False
        assert b["reason"] == "NO_PATH"
        assert b["distance"] is None
        assert b["nodes"] == []

    def test_path_max_depth_truncated(self, client):
        p, m1, m2, m3 = _v09_chain(client, "pathmd")
        r = client.get(
            f"/api/projects/{p['id']}/model-graph/path"
            f"?source_model_id={m1['id']}&target_model_id={m3['id']}"
            f"&direction=downstream&max_depth=1")
        b = r.json()
        assert b["found"] is False
        assert b["truncated"] is True
        assert b["reason"] == "MAX_DEPTH_REACHED"

    def test_path_direction_invalid(self, client):
        p, m1, m2, m3 = _v09_chain(client, "pathdir")
        r = client.get(
            f"/api/projects/{p['id']}/model-graph/path"
            f"?source_model_id={m1['id']}&target_model_id={m2['id']}"
            f"&direction=sideways")
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "GRAPH_DIRECTION_INVALID"

    def test_path_node_not_found(self, client):
        p, m1, m2, m3 = _v09_chain(client, "pathnf")
        r = client.get(
            f"/api/projects/{p['id']}/model-graph/path"
            f"?source_model_id=999999&target_model_id={m2['id']}"
            f"&direction=downstream")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "GRAPH_NODE_NOT_FOUND"

    def test_path_max_depth_bounds(self, client):
        p, m1, m2, m3 = _v09_chain(client, "pathbd")
        for bad in ("0", "51"):
            r = client.get(
                f"/api/projects/{p['id']}/model-graph/path"
                f"?source_model_id={m1['id']}&target_model_id={m2['id']}"
                f"&direction=downstream&max_depth={bad}")
            assert r.status_code == 422
            assert r.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"

    def test_path_on_published_latest_view(self, client):
        p, m1, m2, m3 = _v09_chain(client, "pathpub")
        publish_model(client, m1["id"])
        publish_model(client, m2["id"])
        publish_model(client, m3["id"])
        r = client.get(
            f"/api/projects/{p['id']}/model-graph/path"
            f"?source_model_id={m1['id']}&target_model_id={m3['id']}"
            f"&direction=downstream&view=published_latest")
        b = r.json()
        assert b["found"] is True
        assert b["distance"] == 2
        assert b["view"] == "published_latest"
        assert b["snapshot_no"] == 3  # 解析出的最新快照号（非仅回显请求参数）

    def test_path_on_snapshot_view_404_for_absent_node(self, client):
        p, m1, m2, m3 = _v09_chain(client, "pathsnap")
        publish_model(client, m1["id"])
        publish_model(client, m2["id"])  # 快照 2 只含 m1/m2
        r = client.get(
            f"/api/projects/{p['id']}/model-graph/path"
            f"?source_model_id={m1['id']}&target_model_id={m3['id']}"
            f"&direction=downstream&view=snapshot&snapshot_no=2")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "GRAPH_NODE_NOT_FOUND"
        # 同快照内可达
        r = client.get(
            f"/api/projects/{p['id']}/model-graph/path"
            f"?source_model_id={m1['id']}&target_model_id={m2['id']}"
            f"&direction=downstream&view=snapshot&snapshot_no=2")
        assert r.json()["found"] is True

    def test_path_via_external_edge_in_lineage_view(self, client):
        p, m1, m2 = _v09_two_models(client, "linpath")
        r = _v09_import(client, p["id"], "run-lp",
                        [_v09_edge(m1["model_key"], m2["model_key"],
                                   source_ref="ref-lp")])
        assert r.status_code == 201
        r = client.get(
            f"/api/projects/{p['id']}/model-graph/path"
            f"?source_model_id={m1['id']}&target_model_id={m2['id']}"
            f"&direction=downstream&view=lineage")
        b = r.json()
        assert b["found"] is True
        assert b["edges"][0]["edge_key"].startswith("external:")


class TestV09LineageImport:
    def test_import_replay_and_conflict(self, client):
        p, m1, m2 = _v09_two_models(client, "replay")
        payload = {
            "source_system": "airflow", "run_id": "run-rp",
            "edges": [_v09_edge(m1["model_key"], m2["model_key"],
                                source_ref="ref-rp", confidence=90)],
        }
        r = api(client, "POST", f"/api/projects/{p['id']}/lineage/import",
                json=payload)
        assert r.status_code == 201
        j = r.json()
        assert j["inserted"] == 1 and j["updated"] == 0 and j["deactivated"] == 0
        assert j["replayed"] is False
        assert j["status"] == "completed"
        assert j["attempt_count"] == 1 and j["failure_count"] == 0

        # 同身份同 payload → 200 重放
        r = api(client, "POST", f"/api/projects/{p['id']}/lineage/import",
                json=payload)
        assert r.status_code == 200
        assert r.json()["replayed"] is True
        assert r.json()["run_id"] == "run-rp"

        # 同身份不同 payload → 409
        conflict = json.loads(json.dumps(payload))
        conflict["edges"][0]["confidence"] = 50
        r = api(client, "POST", f"/api/projects/{p['id']}/lineage/import",
                json=conflict)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "LINEAGE_RUN_ID_REUSED"

    def test_import_upsert_updates_existing_edge(self, client):
        p, m1, m2 = _v09_two_models(client, "upsert")
        edge = {"source_model_key": m1["model_key"],
                "target_model_key": m2["model_key"], "source_ref": "ref-up"}
        r = _v09_import(client, p["id"], "run-u1",
                        [dict(edge, confidence=90)])
        assert r.status_code == 201
        assert r.json()["inserted"] == 1
        r = _v09_import(client, p["id"], "run-u2",
                        [dict(edge, confidence=50)])
        assert r.status_code == 201
        j = r.json()
        assert j["inserted"] == 0 and j["updated"] == 1
        edges = api(client, "GET",
                    f"/api/projects/{p['id']}/lineage/edges").json()
        assert len(edges) == 1
        assert edges[0]["confidence"] == 50
        assert edges[0]["active"] is True
        assert edges[0]["last_seen_at"] is not None

    def test_failed_batch_is_atomic_and_records_run(self, client):
        p, m1, m2 = _v09_two_models(client, "atomic")
        good = [_v09_edge(m1["model_key"], m2["model_key"],
                          source_ref="ref-ok")]
        r = _v09_import(client, p["id"], "run-ok", good)
        assert r.status_code == 201

        bad = [_v09_edge("no_such_model", m2["model_key"],
                         source_ref="ref-bad")]
        r = _v09_import(client, p["id"], "run-bad", bad)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "LINEAGE_IMPORT_VALIDATION_FAILED"
        # 原子性：边数不变
        edges = api(client, "GET",
                    f"/api/projects/{p['id']}/lineage/edges").json()
        assert len(edges) == 1
        # 失败批次落库
        runs = api(client, "GET",
                   f"/api/projects/{p['id']}/lineage/import-runs").json()
        failed = next(x for x in runs if x["run_id"] == "run-bad")
        assert failed["status"] == "failed"
        assert failed["attempt_count"] == 1
        assert failed["failure_count"] == 1
        assert failed["error_summary"]  # 非空错误摘要

    def test_failed_batch_retry_keeps_counters(self, client):
        p, m1, m2 = _v09_two_models(client, "retry")
        bad = [_v09_edge("no_such_model", m2["model_key"],
                         source_ref="ref-rt")]
        r = _v09_import(client, p["id"], "run-rt", bad)
        assert r.status_code == 422
        r = _v09_import(client, p["id"], "run-rt", bad)  # 再次失败
        assert r.status_code == 422
        fixed = [_v09_edge(m1["model_key"], m2["model_key"],
                           source_ref="ref-rt")]
        r = _v09_import(client, p["id"], "run-rt", fixed)  # 修正后重试
        assert r.status_code == 201
        j = r.json()
        assert j["status"] == "completed"
        assert j["attempt_count"] == 3  # 历史保留
        assert j["failure_count"] == 2
        runs = api(client, "GET",
                   f"/api/projects/{p['id']}/lineage/import-runs").json()
        run = next(x for x in runs if x["run_id"] == "run-rt")
        assert run["status"] == "completed"
        assert run["attempt_count"] == 3 and run["failure_count"] == 2
        assert run["error_summary"] == []

    def test_incremental_rules(self, client):
        p, m1, m2 = _v09_two_models(client, "incr")
        edge = _v09_edge(m1["model_key"], m2["model_key"])
        # incremental 空批次
        r = _v09_import(client, p["id"], "run-i1", [])
        assert "EMPTY_INCREMENTAL_BATCH" in _validation_codes(r)
        # incremental + complete_scan=true
        r = _v09_import(client, p["id"], "run-i2", [edge],
                        complete_scan=True)
        assert "SCAN_MODE_RULE_VIOLATION" in _validation_codes(r)
        # full_snapshot 未显式 complete_scan（默认 false）
        r = _v09_import(client, p["id"], "run-i3", [edge],
                        scan_mode="full_snapshot", scope_key="zone")
        assert "SCAN_MODE_RULE_VIOLATION" in _validation_codes(r)
        # full_snapshot + default scope（避免误停用）
        r = _v09_import(client, p["id"], "run-i4", [edge],
                        scan_mode="full_snapshot", complete_scan=True)
        assert "FULL_SNAPSHOT_SCOPE_REQUIRED" in _validation_codes(r)
        # 整批拒绝：无任何边写入
        edges = api(client, "GET",
                    f"/api/projects/{p['id']}/lineage/edges").json()
        assert edges == []

    def test_full_snapshot_scope_isolation(self, client):
        """V0.9.2：跨 scope 复用 source_ref/run_id 合法；full_snapshot
        空批次只停用本 scope 的 active 边。"""
        p, m1, m2 = _v09_two_models(client, "fsscope")
        r = _v09_import(client, p["id"], "run-a1",
                        [_v09_edge(m1["model_key"], m2["model_key"],
                                   source_ref="ref-a")],
                        scope_key="dag_a")
        assert r.status_code == 201 and r.json()["inserted"] == 1
        # 同 source_ref + 同 run_id 换个 scope → 合法插入（非 409/非更新）
        r = _v09_import(client, p["id"], "run-a1",
                        [_v09_edge(m1["model_key"], m2["model_key"],
                                   source_ref="ref-a")],
                        scope_key="dag_b")
        assert r.status_code == 201 and r.json()["inserted"] == 1
        # full_snapshot 空批次 → 只停用 dag_a
        # （V1.0 M5：比例 1/1 超阈值，适配新契约带 confirm_deactivation；
        #  本测试意图仍是 scope 隔离，不因确认门槛改变断言对象）
        r = _v09_import(client, p["id"], "run-a2", [],
                        scan_mode="full_snapshot", scope_key="dag_a",
                        complete_scan=True, confirm_deactivation=True)
        assert r.status_code == 201
        assert r.json()["deactivated"] == 1
        active = api(client, "GET",
                     f"/api/projects/{p['id']}/lineage/edges?active_only=true"
                     ).json()
        assert len(active) == 1
        assert active[0]["scope_key"] == "dag_b"
        all_edges = api(client, "GET",
                        f"/api/projects/{p['id']}/lineage/edges?active_only=false"
                        ).json()
        assert len(all_edges) == 2

    def test_full_snapshot_empty_batch_on_fresh_scope(self, client):
        p, m1, m2 = _v09_two_models(client, "fsempty")
        r = _v09_import(client, p["id"], "run-x1", [],
                        scan_mode="full_snapshot", scope_key="zone_x",
                        complete_scan=True)
        assert r.status_code == 201
        assert r.json()["deactivated"] == 0
        assert r.json()["received"] == 0

    def test_self_edge_rejected(self, client):
        p, m1, m2 = _v09_two_models(client, "selfedge")
        r = _v09_import(client, p["id"], "run-se",
                        [_v09_edge(m1["model_key"], m1["model_key"],
                                   source_ref="ref-se")])
        assert "LINEAGE_SELF_EDGE" in _validation_codes(r)

    def test_duplicate_source_ref_in_batch(self, client):
        p, m1, m2 = _v09_two_models(client, "dupref")
        r = _v09_import(client, p["id"], "run-dup", [
            _v09_edge(m1["model_key"], m2["model_key"],
                      source_ref="ref-d"),
            _v09_edge(m1["model_key"], m2["model_key"],
                      source_ref="ref-d"),
        ])
        assert "LINEAGE_SOURCE_REF_DUPLICATE" in _validation_codes(r)

    def test_field_validation(self, client):
        p, m1, m2 = _v09_two_models(client, "fldval")
        # 字段不存在
        r = _v09_import(client, p["id"], "run-f1", [
            _v09_edge(m1["model_key"], m2["model_key"],
                      source_ref="ref-f1", source_fields=["no_such_field"])])
        assert "LINEAGE_FIELD_NOT_FOUND" in _validation_codes(r)
        # 字段不成对
        r = _v09_import(client, p["id"], "run-f2", [
            _v09_edge(m1["model_key"], m2["model_key"],
                      source_ref="ref-f2", source_fields=["order_id"])])
        assert "LINEAGE_FIELD_PAIR_MISMATCH" in _validation_codes(r)
        # 成对合法
        r = _v09_import(client, p["id"], "run-f3", [
            _v09_edge(m1["model_key"], m2["model_key"],
                      source_ref="ref-f3", source_fields=["order_id"],
                      target_fields=["order_id"])])
        assert r.status_code == 201 and r.json()["inserted"] == 1

    def test_metadata_limits(self, client):
        p, m1, m2 = _v09_two_models(client, "meta")
        r = _v09_import(client, p["id"], "run-mk", [
            _v09_edge(m1["model_key"], m2["model_key"],
                      source_ref="ref-mk",
                      metadata={f"k{i}": "v" for i in range(51)})])
        assert "LINEAGE_METADATA_TOO_LARGE" in _validation_codes(r)
        r = _v09_import(client, p["id"], "run-mb", [
            _v09_edge(m1["model_key"], m2["model_key"],
                      source_ref="ref-mb",
                      metadata={"big": "x" * 9000})])
        assert "LINEAGE_METADATA_TOO_LARGE" in _validation_codes(r)

    def test_import_unknown_project_404(self, client):
        r = api(client, "POST", "/api/projects/999999/lineage/import",
                json={"source_system": "airflow", "run_id": "run-np",
                      "edges": [_v09_edge("ods_x", "dwd_y")]})
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "PROJECT_NOT_FOUND"

    def test_schema_validation_errors(self, client):
        """schema 层（pydantic）对边字段的硬性约束。"""
        p, m1, m2 = _v09_two_models(client, "schemaval")
        # source_ref 为空
        r = _v09_import(client, p["id"], "run-sv1", [
            {"source_ref": "", "source_model_key": m1["model_key"],
             "target_model_key": m2["model_key"]}])
        assert r.status_code == 422
        # confidence 越界
        r = _v09_import(client, p["id"], "run-sv2", [
            _v09_edge(m1["model_key"], m2["model_key"],
                      source_ref="ref-sv2", confidence=101)])
        assert r.status_code == 422
        # lineage_type 非法枚举
        r = _v09_import(client, p["id"], "run-sv3", [
            _v09_edge(m1["model_key"], m2["model_key"],
                      source_ref="ref-sv3", lineage_type="bogus_type")])
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"


class TestV09LineageEdges:
    def test_edges_filters_and_pagination_headers(self, client):
        p, m1, m2 = _v09_two_models(client, "edgeflt")
        _v09_import(client, p["id"], "run-e1",
                    [_v09_edge(m1["model_key"], m2["model_key"],
                               source_ref="ref-1")])
        _v09_import(client, p["id"], "run-e2",
                    [_v09_edge(m1["model_key"], m2["model_key"],
                               source_ref="ref-2")],
                    source_system="dbt", scope_key="prod")
        r = api(client, "GET", f"/api/projects/{p['id']}/lineage/edges")
        assert r.status_code == 200
        assert len(r.json()) == 2
        assert r.headers["X-Total-Count"] == "2"
        assert r.headers["X-Limit"] == "500"
        assert r.headers["X-Offset"] == "0"
        r = api(client, "GET",
                f"/api/projects/{p['id']}/lineage/edges?source_system=dbt")
        assert len(r.json()) == 1
        assert r.json()[0]["source_system"] == "dbt"
        r = api(client, "GET",
                f"/api/projects/{p['id']}/lineage/edges?scope_key=prod")
        assert len(r.json()) == 1
        assert r.json()[0]["scope_key"] == "prod"
        r = api(client, "GET",
                f"/api/projects/{p['id']}/lineage/edges?limit=1")
        assert len(r.json()) == 1
        assert r.headers["X-Total-Count"] == "2"

    def test_deactivate_idempotent_and_404(self, client):
        p, m1, m2 = _v09_two_models(client, "deact")
        _v09_import(client, p["id"], "run-d1",
                    [_v09_edge(m1["model_key"], m2["model_key"],
                               source_ref="ref-d")])
        edges = api(client, "GET",
                    f"/api/projects/{p['id']}/lineage/edges").json()
        edge_id = edges[0]["id"]
        r = api(client, "POST",
                f"/api/projects/{p['id']}/lineage/edges/{edge_id}/deactivate")
        assert r.status_code == 200
        assert r.json()["active"] is False
        # 幂等：重复停用仍 200
        r = api(client, "POST",
                f"/api/projects/{p['id']}/lineage/edges/{edge_id}/deactivate")
        assert r.status_code == 200
        assert r.json()["active"] is False
        # 不存在 → 404
        r = api(client, "POST",
                f"/api/projects/{p['id']}/lineage/edges/999999/deactivate")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "EXTERNAL_LINEAGE_EDGE_NOT_FOUND"
        # active_only 过滤生效
        active = api(client, "GET",
                     f"/api/projects/{p['id']}/lineage/edges?active_only=true"
                     ).json()
        assert active == []
        all_edges = api(client, "GET",
                        f"/api/projects/{p['id']}/lineage/edges?active_only=false"
                        ).json()
        assert len(all_edges) == 1

    def test_reimport_reactivates_deactivated_edge(self, client):
        p, m1, m2 = _v09_two_models(client, "react")
        edge = [_v09_edge(m1["model_key"], m2["model_key"],
                          source_ref="ref-ra")]
        _v09_import(client, p["id"], "run-ra1", edge)
        edges = api(client, "GET",
                    f"/api/projects/{p['id']}/lineage/edges").json()
        edge_id = edges[0]["id"]
        api(client, "POST",
            f"/api/projects/{p['id']}/lineage/edges/{edge_id}/deactivate")
        r = _v09_import(client, p["id"], "run-ra2", edge)
        assert r.status_code == 201
        assert r.json()["updated"] == 1
        edges = api(client, "GET",
                    f"/api/projects/{p['id']}/lineage/edges").json()
        assert len(edges) == 1 and edges[0]["active"] is True

    def test_lineage_view_excludes_inactive(self, client):
        p, m1, m2 = _v09_two_models(client, "linview")
        _v09_import(client, p["id"], "run-lv1",
                    [_v09_edge(m1["model_key"], m2["model_key"],
                               source_ref="ref-lv1")])
        _v09_import(client, p["id"], "run-lv2",
                    [_v09_edge(m1["model_key"], m2["model_key"],
                               source_ref="ref-lv2")])
        edges = api(client, "GET",
                    f"/api/projects/{p['id']}/lineage/edges").json()
        target = next(e for e in edges if e["source_ref"] == "ref-lv1")
        api(client, "POST",
            f"/api/projects/{p['id']}/lineage/edges/{target['id']}/deactivate")
        g = client.get(
            f"/api/projects/{p['id']}/model-graph?view=lineage").json()
        assert g["view"] == "lineage"
        assert len(g["external_edges"]) == 1
        assert g["external_edges"][0]["source_ref"] == "ref-lv2"
        assert all(e["active"] for e in g["external_edges"])
        assert g["edges"] == []  # 无设计关系


class TestV09LineageOps:
    def test_import_runs_filters(self, client):
        p, m1, m2 = _v09_two_models(client, "runflt")
        _v09_import(client, p["id"], "run-r1",
                    [_v09_edge(m1["model_key"], m2["model_key"])])
        _v09_import(client, p["id"], "run-r2",
                    [_v09_edge(m1["model_key"], m2["model_key"],
                               source_ref="ref-r2")],
                    source_system="dbt", scope_key="prod")
        _v09_import(client, p["id"], "run-r3",
                    [_v09_edge("no_such", m2["model_key"],
                               source_ref="ref-r3")])  # 失败批次
        all_runs = api(client, "GET",
                       f"/api/projects/{p['id']}/lineage/import-runs").json()
        assert len(all_runs) == 3
        completed = api(client, "GET",
                        f"/api/projects/{p['id']}/lineage/import-runs"
                        f"?status=completed").json()
        assert len(completed) == 2
        failed = api(client, "GET",
                     f"/api/projects/{p['id']}/lineage/import-runs"
                     f"?status=failed").json()
        assert len(failed) == 1
        assert failed[0]["error_summary"]
        dbt_runs = api(client, "GET",
                       f"/api/projects/{p['id']}/lineage/import-runs"
                       f"?source_system=dbt").json()
        assert len(dbt_runs) == 1
        assert dbt_runs[0]["scope_key"] == "prod"

    def test_health_groups(self, client):
        p, m1, m2 = _v09_two_models(client, "health")
        _v09_import(client, p["id"], "run-h1",
                    [_v09_edge(m1["model_key"], m2["model_key"],
                               source_ref="ref-h1")])
        _v09_import(client, p["id"], "run-h2",
                    [_v09_edge(m1["model_key"], m2["model_key"],
                               source_ref="ref-h2")],
                    source_system="dbt", scope_key="prod",
                    observed_at=(datetime.now(timezone.utc)
                                 - timedelta(days=3)).isoformat())
        # airflow/default 上一次成功之后再失败一次 → consecutive=1
        _v09_import(client, p["id"], "run-h3",
                    [_v09_edge("no_such", m2["model_key"],
                               source_ref="ref-h3")])
        # 把 dbt/prod 的边改到 3 天前未扫描
        edges = api(client, "GET",
                    f"/api/projects/{p['id']}/lineage/edges"
                    f"?source_system=dbt").json()
        _backdate_edge(edges[0]["id"], days=3)
        h = api(client, "GET",
                f"/api/projects/{p['id']}/lineage/health").json()
        assert h["project_id"] == p["id"]
        groups = {(g["source_system"], g["scope_key"]): g
                  for g in h["groups"]}
        assert set(groups) == {("airflow", "default"), ("dbt", "prod")}
        adg = groups[("airflow", "default")]
        assert adg["active_edges"] == 1
        assert adg["inactive_edges"] == 0
        assert adg["last_successful_run_at"] is not None
        assert adg["last_failed_run_at"] is not None
        assert adg["consecutive_failures"] == 1
        assert adg["stale_candidates"] == 0  # 刚导入
        ddg = groups[("dbt", "prod")]
        assert ddg["active_edges"] == 1
        assert ddg["consecutive_failures"] == 0
        assert ddg["stale_candidates"] == 1  # 3 天未扫描
        assert h["total_active_edges"] == 2
        assert h["total_stale_candidates"] == 1

    def test_stale_candidates_threshold(self, client):
        p, m1, m2 = _v09_two_models(client, "stalec")
        _v09_import(client, p["id"], "run-sc1",
                    [_v09_edge(m1["model_key"], m2["model_key"],
                               source_ref="ref-fresh")])
        r = _v09_import(client, p["id"], "run-sc2",
                        [_v09_edge(m1["model_key"], m2["model_key"],
                                   source_ref="ref-old")],
                        source_system="dbt", scope_key="prod")
        assert r.status_code == 201
        old = api(client, "GET",
                  f"/api/projects/{p['id']}/lineage/edges"
                  f"?source_system=dbt").json()
        _backdate_edge(old[0]["id"], days=3)
        # 阈值 1 分钟：仅旧边入列
        sc = api(client, "GET",
                 f"/api/projects/{p['id']}/lineage/stale-candidates"
                 f"?stale_after_minutes=1").json()
        assert len(sc) == 1
        assert sc[0]["source_ref"] == "ref-old"
        assert sc[0]["age_minutes"] > 1
        # 阈值上限（1 年）：无候选
        sc = api(client, "GET",
                 f"/api/projects/{p['id']}/lineage/stale-candidates"
                 f"?stale_after_minutes=525600").json()
        assert sc == []
        # 来源过滤
        sc = api(client, "GET",
                 f"/api/projects/{p['id']}/lineage/stale-candidates"
                 f"?stale_after_minutes=1&source_system=dbt").json()
        assert len(sc) == 1
        sc = api(client, "GET",
                 f"/api/projects/{p['id']}/lineage/stale-candidates"
                 f"?stale_after_minutes=1&source_system=airflow").json()
        assert sc == []
        # 阈值越界
        r = api(client, "GET",
                f"/api/projects/{p['id']}/lineage/stale-candidates"
                f"?stale_after_minutes=0")
        assert r.status_code == 422

    def test_preview_is_readonly(self, client):
        p, m1, m2 = _v09_two_models(client, "preview")
        edge = [_v09_edge(m1["model_key"], m2["model_key"],
                          source_ref="ref-pv")]
        _v09_import(client, p["id"], "run-pv0", edge)  # 预置一条
        before_runs = len(api(client, "GET",
                              f"/api/projects/{p['id']}/lineage/import-runs"
                              ).json())
        before_edges = len(api(client, "GET",
                               f"/api/projects/{p['id']}/lineage/edges"
                               ).json())
        payload = {
            "source_system": "airflow", "run_id": "run-pv1",
            "edges": [_v09_edge(m1["model_key"], m2["model_key"],
                                source_ref="ref-pv1")],
        }
        r = api(client, "POST", f"/api/projects/{p['id']}/lineage/preview",
                json=payload)
        assert r.status_code == 200
        b = r.json()
        assert b["writes_performed"] is False
        assert b["received"] == 1 and b["inserted"] == 1
        assert b["updated"] == 0 and b["deactivated"] == 0
        # 无任何写入
        assert len(api(client, "GET",
                       f"/api/projects/{p['id']}/lineage/import-runs"
                       ).json()) == before_runs
        assert len(api(client, "GET",
                       f"/api/projects/{p['id']}/lineage/edges"
                       ).json()) == before_edges
        # preview 对校验失败同样 422
        bad = {
            "source_system": "airflow", "run_id": "run-pv2",
            "edges": [_v09_edge("no_such", m2["model_key"],
                                source_ref="ref-pv2")],
        }
        r = api(client, "POST", f"/api/projects/{p['id']}/lineage/preview",
                json=bad)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "LINEAGE_IMPORT_VALIDATION_FAILED"

    def test_lineage_unknown_project_404(self, client):
        for url in ("lineage/edges", "lineage/import-runs",
                    "lineage/health", "lineage/stale-candidates"):
            r = api(client, "GET", f"/api/projects/999999/{url}")
            assert r.status_code == 404, url


class TestV09MigrationDowngradeGuard:
    def test_downgrade_blocked_by_cross_scope_duplicates(self):
        """V0.9.2 降级保护：存在跨 scope 复用 source_ref/run_id 时，
        alembic downgrade -1 必须失败且 revision 保持 head；清理冲突后
        才能成功降级。"""
        db_file = Path(_TMP_DIR) / "downgrade_guard.db"
        env = dict(os.environ)
        env["DATABASE_URL"] = f"sqlite:///{db_file}"
        env["APP_ENV"] = "test"

        def alembic(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                [sys.executable, "-m", "alembic", *args],
                cwd=str(PROJECT_ROOT), env=env,
                capture_output=True, text=True, timeout=120,
            )

        r = alembic("upgrade", "head")
        assert r.returncode == 0, r.stderr

        # 直接写入跨 scope 复用行（模拟 V0.9.2 连接器数据）
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute(
                "INSERT INTO external_lineage_edges "
                "(project_id, source_system, scope_key, source_ref, "
                "source_model_id, target_model_id, job_key, lineage_type, "
                "transformation_type, source_fields_json, "
                "target_fields_json, confidence, active, observed_at, "
                "last_seen_at, metadata_json, created_at, updated_at) "
                "VALUES "
                "(1,'airflow','dag_a','ref-dup',1,2,'','etl_data_flow',"
                "'unknown','[]','[]',100,1,'2026-01-01 00:00:00',"
                "'2026-01-01 00:00:00','{}','2026-01-01 00:00:00',"
                "'2026-01-01 00:00:00'),"
                "(1,'airflow','dag_b','ref-dup',1,2,'','etl_data_flow',"
                "'unknown','[]','[]',100,1,'2026-01-01 00:00:00',"
                "'2026-01-01 00:00:00','{}','2026-01-01 00:00:00',"
                "'2026-01-01 00:00:00')"
            )
            conn.execute(
                "INSERT INTO lineage_import_runs "
                "(project_id, source_system, scope_key, run_id, scan_mode, "
                "complete_scan, payload_hash, status, received_count, "
                "inserted_count, updated_count, deactivated_count, "
                "attempt_count, failure_count, deactivation_confirmed, "
                "error_summary_json, result_json, actor, request_id, "
                "created_at, updated_at) "
                "VALUES "
                "(1,'airflow','dag_a','run-dup','incremental',0,'h1',"
                "'completed',1,1,0,0,1,0,0,'[]','{}','t','r',"
                "'2026-01-01 00:00:00','2026-01-01 00:00:00'),"
                "(1,'airflow','dag_b','run-dup','incremental',0,'h1',"
                "'completed',1,1,0,0,1,0,0,'[]','{}','t','r',"
                "'2026-01-01 00:00:00','2026-01-01 00:00:00')"
            )
            conn.commit()

        # V1.0 迁移降级：三张新表为空，保护不触发，成功回到 V0.9.2 head
        r = alembic("downgrade", "-1")
        assert r.returncode == 0, r.stderr
        r = alembic("current")
        assert "b5f2c4a9d703" in r.stdout

        # 再降一级触发 V0.9.2 跨 scope 复用保护：必须被中止
        r = alembic("downgrade", "-1")
        assert r.returncode != 0
        output = r.stdout + r.stderr
        assert "中止降级" in output
        assert "ref-dup" in output and "run-dup" in output
        r = alembic("current")
        assert "b5f2c4a9d703" in r.stdout  # revision 未变

        # 清理冲突行后降级应成功
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute(
                "DELETE FROM external_lineage_edges WHERE scope_key='dag_b'")
            conn.execute(
                "DELETE FROM lineage_import_runs WHERE scope_key='dag_b'")
            conn.commit()
        r = alembic("downgrade", "-1")
        assert r.returncode == 0, r.stderr
        r = alembic("current")
        assert "a4d7c92f5e10" in r.stdout


# ---------------------------------------------------------------- V1.0：认证 / 授权（M1/M2）

_V10_BOOTSTRAP_TOKEN = "v10-bootstrap-token-0001"


def _v10_db_path() -> str:
    return os.environ["DATABASE_URL"].removeprefix("sqlite:///")


def _v10_clear_auth_tables() -> None:
    """清空认证三表（测试库直连；bootstrap 成功分支的前置）。"""
    with sqlite3.connect(_v10_db_path()) as conn:
        conn.execute("DELETE FROM api_keys")
        conn.execute("DELETE FROM project_members")
        conn.execute("DELETE FROM users")
        conn.commit()


def _v10_switch(monkeypatch) -> None:
    """切到 api_key 模式（monkeypatch 自动恢复）。"""
    monkeypatch.setattr(settings, "auth_mode", "api_key")
    monkeypatch.setattr(settings, "bootstrap_token", _V10_BOOTSTRAP_TOKEN)


def _v10_bootstrap_admin(c: TestClient, monkeypatch) -> str:
    """清空认证表 → api_key 模式引导管理员；返回 admin secret。"""
    _v10_clear_auth_tables()
    _v10_switch(monkeypatch)
    r = c.post("/api/auth/bootstrap", json={
        "bootstrap_token": _V10_BOOTSTRAP_TOKEN,
        "user_key": "u:v10admin", "display_name": "V10 管理员",
    })
    assert r.status_code == 201, r.text
    return r.json()["secret"]


def _v10_secret_request(c: TestClient, method: str, url: str, secret: str, **kw):
    """带 Bearer 令牌的请求。"""
    headers = kw.pop("headers", {})
    headers["Authorization"] = f"Bearer {secret}"
    return c.request(method, url, headers=headers, **kw)


def _v10_create_project(c: TestClient, secret: str, key: str) -> dict:
    r = _v10_secret_request(c, "POST", "/api/projects", secret, json={
        "project_key": key, "name": "V10 测试项目", "description": "pytest v10",
    })
    assert r.status_code == 201, r.text
    return r.json()


def _v10_create_model(c: TestClient, secret: str, pid: int,
                      key: str = "dwd_v10_order") -> dict:
    r = _v10_secret_request(c, "POST", f"/api/projects/{pid}/models", secret,
                            json={
                                "model_key": key, "name": "V10 订单明细",
                                "layer": "DWD", "model_type": "TABLE",
                                "target_engine": "postgresql",
                                "fields": good_fields(),
                            })
    assert r.status_code == 201, r.text
    return r.json()


def _v10_provision_user(c: TestClient, admin_secret: str,
                        user_key: str) -> str:
    """admin 代建密钥开通主体（自动 provisioning）；返回 secret。"""
    r = _v10_secret_request(c, "POST", "/api/auth/api-keys", admin_secret,
                            json={"name": f"{user_key}-key",
                                  "user_key": user_key})
    assert r.status_code == 201, r.text
    return r.json()["secret"]


def _v10_member_secret(c: TestClient, admin_secret: str, user_key: str,
                       project_id: int, role: str) -> str:
    """admin 开通主体 + 登记成员 + 成员密钥（成员/连接器服务账号实战路径：
    先发密钥建主体，再加成员——与升级指南流程一致）。"""
    secret = _v10_provision_user(c, admin_secret, user_key)
    r = _v10_secret_request(c, "POST",
                            f"/api/projects/{project_id}/members",
                            admin_secret,
                            json={"user_key": user_key, "role": role})
    assert r.status_code in (200, 201), r.text
    return secret


class TestV10Auth:
    """M1 认证与 API Key：bootstrap 三分支、密钥生命周期、Bearer 解析。"""

    def test_missing_token_401(self, client, monkeypatch):
        _v10_switch(monkeypatch)
        r = client.get("/api/auth/me")
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "AUTH_REQUIRED"

    def test_malformed_token_401(self, client, monkeypatch):
        _v10_switch(monkeypatch)
        for header in ("abc", "Bearer abc", "Basic dmc_x", "bearer"):
            r = client.get("/api/auth/me", headers={"Authorization": header})
            assert r.status_code == 401, header
            assert r.json()["error"]["code"] == "AUTH_REQUIRED"

    def test_bootstrap_not_configured(self, client, monkeypatch):
        _v10_clear_auth_tables()
        monkeypatch.setattr(settings, "auth_mode", "api_key")
        monkeypatch.setattr(settings, "bootstrap_token", "")
        r = client.post("/api/auth/bootstrap", json={
            "bootstrap_token": "whatever",
            "user_key": "u:admin", "display_name": "管理员",
        })
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "BOOTSTRAP_NOT_CONFIGURED"

    def test_bootstrap_token_invalid(self, client, monkeypatch):
        _v10_clear_auth_tables()
        _v10_switch(monkeypatch)
        r = client.post("/api/auth/bootstrap", json={
            "bootstrap_token": "wrong-token",
            "user_key": "u:admin", "display_name": "管理员",
        })
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "BOOTSTRAP_TOKEN_INVALID"

    def test_bootstrap_already_done(self, client, monkeypatch):
        # 先引导一个管理员（users 非空）→ 引导端点永久关闭
        _v10_bootstrap_admin(client, monkeypatch)
        r = client.post("/api/auth/bootstrap", json={
            "bootstrap_token": _V10_BOOTSTRAP_TOKEN,
            "user_key": "u:other", "display_name": "另一个管理员",
        })
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "BOOTSTRAP_ALREADY_DONE"

    def test_bootstrap_success_and_authenticated_use(self, client, monkeypatch):
        secret = _v10_bootstrap_admin(client, monkeypatch)
        me = _v10_secret_request(client, "GET", "/api/auth/me", secret)
        assert me.status_code == 200
        body = me.json()
        assert body["user_key"] == "u:v10admin"
        assert body["is_admin"] is True
        assert body["auth_mode"] == "api_key"
        # 引导密钥本身可用（创建项目 → 自动成为 owner）
        p = _v10_create_project(client, secret, "PRJ_V10AUTH")
        assert p["project_key"] == "PRJ_V10AUTH"

    def test_key_lifecycle_create_list_revoke(self, client, monkeypatch):
        secret = _v10_bootstrap_admin(client, monkeypatch)
        # 创建：secret 仅此一次
        r = _v10_secret_request(client, "POST", "/api/auth/api-keys", secret,
                                json={"name": "v10-ci-key",
                                      "expires_in_days": 30})
        assert r.status_code == 201
        created = r.json()
        assert created["secret"].startswith("dmc_ak_")
        # 令牌可被服务端解析（key_id/secret 切分契约；secret 可含下划线）
        parsed = auth_mod._parse_bearer(f"Bearer {created['secret']}")
        assert parsed == (created["key_id"],
                          created["secret"].split("_", 3)[3])
        assert created["expires_at"]
        assert "key_hash" not in created
        # 新密钥可用
        me = _v10_secret_request(client, "GET", "/api/auth/me",
                                 created["secret"])
        assert me.status_code == 200
        # 列表：不含 secret/hash
        r = _v10_secret_request(client, "GET", "/api/auth/api-keys", secret)
        assert r.status_code == 200
        keys = r.json()
        assert any(k["key_id"] == created["key_id"] for k in keys)
        assert all("secret" not in k and "key_hash" not in k for k in keys)
        # 吊销 → 旧 secret 即时 401
        r = _v10_secret_request(
            client, "POST",
            f"/api/auth/api-keys/{created['key_id']}/revoke", secret)
        assert r.status_code == 200
        assert r.json()["status"] == "revoked"
        r = _v10_secret_request(client, "GET", "/api/auth/me",
                                created["secret"])
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "API_KEY_REVOKED"
        # 重复吊销幂等
        r = _v10_secret_request(
            client, "POST",
            f"/api/auth/api-keys/{created['key_id']}/revoke", secret)
        assert r.status_code == 200
        assert r.json()["idempotent"] is True

    def test_invalid_secret_401(self, client, monkeypatch):
        secret = _v10_bootstrap_admin(client, monkeypatch)
        r = _v10_secret_request(client, "POST", "/api/auth/api-keys", secret,
                                json={"name": "v10-k"})
        key_id = r.json()["key_id"]
        forged = f"dmc_{key_id}_" + "A" * 43
        r = _v10_secret_request(client, "GET", "/api/auth/me", forged)
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "API_KEY_INVALID"

    def test_revoke_unknown_key_404(self, client, monkeypatch):
        secret = _v10_bootstrap_admin(client, monkeypatch)
        r = _v10_secret_request(
            client, "POST", "/api/auth/api-keys/ak_doesnotexist00/revoke",
            secret)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "API_KEY_NOT_FOUND"

    def test_expired_key_401(self, client, monkeypatch):
        secret = _v10_bootstrap_admin(client, monkeypatch)
        r = _v10_secret_request(client, "POST", "/api/auth/api-keys", secret,
                                json={"name": "v10-exp"})
        assert r.status_code == 201
        key_id = r.json()["key_id"]
        new_secret = r.json()["secret"]
        past = (datetime.now(timezone.utc) - timedelta(days=1)).replace(
            tzinfo=None).isoformat(sep=" ")
        with sqlite3.connect(_v10_db_path()) as conn:
            conn.execute("UPDATE api_keys SET expires_at = ? WHERE key_id = ?",
                         (past, key_id))
            conn.commit()
        auth_mod.key_cache.invalidate(key_id)
        # admin 引导密钥未过期仍可用
        r = _v10_secret_request(client, "GET", "/api/auth/me", secret)
        assert r.status_code == 200
        # 过期密钥即时 401（不依赖 TTL：每次请求实时校验 expires_at）
        r = _v10_secret_request(client, "GET", "/api/auth/me", new_secret)
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "API_KEY_EXPIRED"

    def test_user_disabled_403(self, client, monkeypatch):
        admin_secret = _v10_bootstrap_admin(client, monkeypatch)
        p = _v10_create_project(client, admin_secret, "PRJ_V10DIS")
        member_secret = _v10_member_secret(
            client, admin_secret, "u:v10disabled", p["id"], "editor")
        # 直接禁用主体（无用户管理端点，测试库直改；认证路径实时校验）
        with sqlite3.connect(_v10_db_path()) as conn:
            conn.execute(
                "UPDATE users SET status='disabled' WHERE user_key='u:v10disabled'")
            conn.commit()
        auth_mod.key_cache._entries.clear()
        r = _v10_secret_request(client, "GET", "/api/auth/me", member_secret)
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "USER_DISABLED"

    def test_admin_on_behalf_key_and_list_others(self, client, monkeypatch):
        admin_secret = _v10_bootstrap_admin(client, monkeypatch)
        # admin 代建他人密钥
        r = _v10_secret_request(client, "POST", "/api/auth/api-keys",
                                admin_secret,
                                json={"name": "sa-key",
                                      "user_key": "u:v10sa"})
        assert r.status_code == 201
        assert r.json()["user_key"] == "u:v10sa"
        # admin 可查他人列表
        r = _v10_secret_request(client, "GET", "/api/auth/api-keys",
                                admin_secret,
                                params={"user_key": "u:v10sa"})
        assert r.status_code == 200
        assert len(r.json()) == 1
        # 非 admin 查他人 403
        p = _v10_create_project(client, admin_secret, "PRJ_V10SA")
        viewer_secret = _v10_member_secret(
            client, admin_secret, "u:v10viewer", p["id"], "viewer")
        r = _v10_secret_request(client, "GET", "/api/auth/api-keys",
                                viewer_secret,
                                params={"user_key": "u:v10admin"})
        assert r.status_code == 403
        # 非 admin 代建他人密钥 403
        r = _v10_secret_request(client, "POST", "/api/auth/api-keys",
                                viewer_secret,
                                json={"name": "x", "user_key": "u:v10admin"})
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "PROJECT_ROLE_INSUFFICIENT"

    def test_demo_mode_me_and_bootstrap(self, client):
        """demo 模式：/api/auth/me 回落演示管理员；bootstrap 已关闭。"""
        r = api(client, "GET", "/api/auth/me")
        assert r.status_code == 200
        body = r.json()
        assert body["auth_mode"] == "demo"
        assert body["user_key"] == "u:demo.admin"
        assert body["is_admin"] is True
        r = api(client, "POST", "/api/auth/bootstrap", json={
            "bootstrap_token": "x",
            "user_key": "u:someone", "display_name": "x",
        })
        assert r.status_code == 409
        assert r.json()["error"]["code"] in (
            "BOOTSTRAP_ALREADY_DONE", "BOOTSTRAP_NOT_CONFIGURED")


class TestV10Authz:
    """M2 授权矩阵：三角色边界 + admin 旁路 + 非成员 + 未知对象 404。"""

    def test_viewer_read_ok_write_denied(self, client, monkeypatch):
        admin_secret = _v10_bootstrap_admin(client, monkeypatch)
        p = _v10_create_project(client, admin_secret, "PRJ_V10AUTHZ_V")
        viewer_secret = _v10_member_secret(
            client, admin_secret, "u:v10v", p["id"], "viewer")
        # viewer 读 OK
        r = _v10_secret_request(
            client, "GET", f"/api/projects/{p['id']}/subject-areas",
            viewer_secret)
        assert r.status_code == 200
        # viewer 写 403
        r = _v10_secret_request(
            client, "POST", f"/api/projects/{p['id']}/subject-areas",
            viewer_secret,
            json={"area_key": "AREA_V10V", "name": "域", "owner": "x"})
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "PROJECT_ROLE_INSUFFICIENT"

    def test_editor_write_ok_member_manage_denied(self, client, monkeypatch):
        admin_secret = _v10_bootstrap_admin(client, monkeypatch)
        p = _v10_create_project(client, admin_secret, "PRJ_V10AUTHZ_E")
        editor_secret = _v10_member_secret(
            client, admin_secret, "u:v10e", p["id"], "editor")
        # editor 写 OK
        r = _v10_secret_request(
            client, "POST", f"/api/projects/{p['id']}/subject-areas",
            editor_secret,
            json={"area_key": "AREA_V10E", "name": "域", "owner": "x"})
        assert r.status_code == 201
        # editor 管成员 403（owner 专属）
        r = _v10_secret_request(
            client, "POST", f"/api/projects/{p['id']}/members",
            editor_secret, json={"user_key": "u:v10ex", "role": "viewer"})
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "PROJECT_ROLE_INSUFFICIENT"

    def test_owner_manage_members_ok(self, client, monkeypatch):
        admin_secret = _v10_bootstrap_admin(client, monkeypatch)
        p = _v10_create_project(client, admin_secret, "PRJ_V10AUTHZ_O")
        _v10_provision_user(client, admin_secret, "u:v10om")
        owner_secret = _v10_member_secret(
            client, admin_secret, "u:v10o", p["id"], "owner")
        r = _v10_secret_request(
            client, "POST", f"/api/projects/{p['id']}/members",
            owner_secret, json={"user_key": "u:v10om", "role": "viewer"})
        assert r.status_code == 201
        r = _v10_secret_request(
            client, "GET", f"/api/projects/{p['id']}/members", owner_secret)
        assert r.status_code == 200
        keys = {m["user_key"] for m in r.json()}
        assert {"u:v10admin", "u:v10o", "u:v10om"} <= keys

    def test_non_member_access_denied(self, client, monkeypatch):
        admin_secret = _v10_bootstrap_admin(client, monkeypatch)
        p = _v10_create_project(client, admin_secret, "PRJ_V10AUTHZ_N")
        # 有合法密钥但无成员身份的主体
        r = _v10_secret_request(client, "POST", "/api/auth/api-keys",
                                admin_secret,
                                json={"name": "outsider",
                                      "user_key": "u:v10out"})
        outsider = r.json()["secret"]
        r = _v10_secret_request(
            client, "GET", f"/api/projects/{p['id']}/subject-areas",
            outsider)
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "PROJECT_ACCESS_DENIED"

    def test_admin_bypasses_project_checks(self, client, monkeypatch):
        admin_secret = _v10_bootstrap_admin(client, monkeypatch)
        p1 = _v10_create_project(client, admin_secret, "PRJ_V10AUTHZ_B1")
        owner_secret = _v10_member_secret(
            client, admin_secret, "u:v10b", p1["id"], "owner")
        # 非 admin owner 自建项目 → admin 不是该项目成员
        p2 = _v10_create_project(client, owner_secret, "PRJ_V10AUTHZ_B2")
        r = _v10_secret_request(
            client, "GET", f"/api/projects/{p2['id']}/subject-areas",
            admin_secret)
        assert r.status_code == 200
        # admin 旁路 owner 检查直接管成员
        _v10_provision_user(client, admin_secret, "u:v10bb")
        r = _v10_secret_request(
            client, "POST", f"/api/projects/{p2['id']}/members",
            admin_secret, json={"user_key": "u:v10bb", "role": "editor"})
        assert r.status_code == 201

    def test_unknown_project_404(self, client, monkeypatch):
        admin_secret = _v10_bootstrap_admin(client, monkeypatch)
        p = _v10_create_project(client, admin_secret, "PRJ_V10AUTHZ_U")
        member_secret = _v10_member_secret(
            client, admin_secret, "u:v10u", p["id"], "viewer")
        for secret in (admin_secret, member_secret):
            r = _v10_secret_request(
                client, "GET", "/api/projects/999999/subject-areas", secret)
            assert r.status_code == 404
            assert r.json()["error"]["code"] == "PROJECT_NOT_FOUND"

    def test_model_and_requirement_level_authz(self, client, monkeypatch):
        admin_secret = _v10_bootstrap_admin(client, monkeypatch)
        p = _v10_create_project(client, admin_secret, "PRJ_V10AUTHZ_M")
        m = _v10_create_model(client, admin_secret, p["id"])
        r = _v10_secret_request(
            client, "POST", f"/api/projects/{p['id']}/requirements",
            admin_secret,
            json={"requirement_key": "REQ_V10AUTHZ", "title": "需求",
                  "business_domain": "域", "description": "d",
                  "acceptance_criteria": "a", "priority": "P1"})
        assert r.status_code == 201
        req_id = r.json()["id"]
        r = _v10_secret_request(client, "POST", "/api/auth/api-keys",
                                admin_secret,
                                json={"name": "out2",
                                      "user_key": "u:v10out2"})
        outsider = r.json()["secret"]
        viewer_secret = _v10_member_secret(
            client, admin_secret, "u:v10mv", p["id"], "viewer")
        # 模型级：非成员 403
        r = _v10_secret_request(client, "GET", f"/api/models/{m['id']}",
                                outsider)
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "PROJECT_ACCESS_DENIED"
        # 模型级：viewer 写 403
        r = _v10_secret_request(
            client, "PUT", f"/api/models/{m['id']}", viewer_secret,
            json={"model_key": m["model_key"], "name": "改名",
                  "fields": good_fields(),
                  "expected_revision": m["edit_revision"]})
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "PROJECT_ROLE_INSUFFICIENT"
        # 模型级：未知模型 404
        r = _v10_secret_request(client, "GET", "/api/models/999999",
                                viewer_secret)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "MODEL_NOT_FOUND"
        # 需求级：非成员 403
        r = _v10_secret_request(client, "GET", f"/api/requirements/{req_id}",
                                outsider)
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "PROJECT_ACCESS_DENIED"

    def test_lineage_import_requires_editor(self, client, monkeypatch):
        admin_secret = _v10_bootstrap_admin(client, monkeypatch)
        p = _v10_create_project(client, admin_secret, "PRJ_V10AUTHZ_L")
        m1 = _v10_create_model(client, admin_secret, p["id"],
                               "ods_v10l_src")
        m2 = _v10_create_model(client, admin_secret, p["id"],
                               "dwd_v10l_tgt")
        viewer_secret = _v10_member_secret(
            client, admin_secret, "u:v10lv", p["id"], "viewer")
        editor_secret = _v10_member_secret(
            client, admin_secret, "u:v10le", p["id"], "editor")
        body = {"source_system": "airflow", "run_id": "run-v10l",
                "edges": [{"source_ref": "ref-v10l",
                           "source_model_key": m1["model_key"],
                           "target_model_key": m2["model_key"]}]}
        r = _v10_secret_request(
            client, "POST", f"/api/projects/{p['id']}/lineage/import",
            viewer_secret, json=body)
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "PROJECT_ROLE_INSUFFICIENT"
        r = _v10_secret_request(
            client, "POST", f"/api/projects/{p['id']}/lineage/import",
            editor_secret, json=body)
        assert r.status_code == 201

    def test_metrics_admin_only(self, client, monkeypatch):
        admin_secret = _v10_bootstrap_admin(client, monkeypatch)
        p = _v10_create_project(client, admin_secret, "PRJ_V10AUTHZ_MT")
        viewer_secret = _v10_member_secret(
            client, admin_secret, "u:v10mt", p["id"], "viewer")
        r = _v10_secret_request(client, "GET", "/metrics", viewer_secret)
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "PROJECT_ROLE_INSUFFICIENT"
        r = _v10_secret_request(client, "GET", "/metrics", admin_secret)
        assert r.status_code == 200
        assert "http_requests_total" in r.text


class TestV10Members:
    """M2 项目成员管理：upsert 幂等、最后一个 owner 保护、事件与审计留痕。"""

    def test_upsert_idempotent_and_list(self, client, monkeypatch):
        admin_secret = _v10_bootstrap_admin(client, monkeypatch)
        p = _v10_create_project(client, admin_secret, "PRJ_V10MEM_U")
        _v10_provision_user(client, admin_secret, "u:v10mu")
        # 首次新增 201
        r = _v10_secret_request(
            client, "POST", f"/api/projects/{p['id']}/members",
            admin_secret, json={"user_key": "u:v10mu", "role": "editor"})
        assert r.status_code == 201
        assert r.json()["created"] is True
        # 重复 upsert 200 幂等
        r = _v10_secret_request(
            client, "POST", f"/api/projects/{p['id']}/members",
            admin_secret, json={"user_key": "u:v10mu", "role": "viewer"})
        assert r.status_code == 200
        assert r.json()["created"] is False
        assert r.json()["role"] == "viewer"
        # 列表反映最终角色且不重复
        r = _v10_secret_request(
            client, "GET", f"/api/projects/{p['id']}/members", admin_secret)
        rows = [m for m in r.json() if m["user_key"] == "u:v10mu"]
        assert len(rows) == 1 and rows[0]["role"] == "viewer"

    def test_member_not_found_404(self, client, monkeypatch):
        admin_secret = _v10_bootstrap_admin(client, monkeypatch)
        p = _v10_create_project(client, admin_secret, "PRJ_V10MEM_N")
        r = _v10_secret_request(
            client, "DELETE",
            f"/api/projects/{p['id']}/members/999999", admin_secret)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "MEMBER_NOT_FOUND"

    def test_add_unknown_user_404(self, client, monkeypatch):
        """成员登记严格要求主体已存在（需求说明书错误表 USER_NOT_FOUND）；
        主体开通由 admin 代建密钥完成（升级指南流程）。"""
        admin_secret = _v10_bootstrap_admin(client, monkeypatch)
        p = _v10_create_project(client, admin_secret, "PRJ_V10MEM_UNK")
        r = _v10_secret_request(
            client, "POST", f"/api/projects/{p['id']}/members",
            admin_secret, json={"user_key": "u:v10ghost", "role": "viewer"})
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "USER_NOT_FOUND"
        # admin 代建密钥自动开通主体（sa: 前缀 → 服务账号）
        secret = _v10_provision_user(client, admin_secret, "sa:v10ghost")
        me = _v10_secret_request(client, "GET", "/api/auth/me", secret).json()
        assert me["kind"] == "service_account"
        # 开通后再登记成员成功
        r = _v10_secret_request(
            client, "POST", f"/api/projects/{p['id']}/members",
            admin_secret, json={"user_key": "sa:v10ghost", "role": "viewer"})
        assert r.status_code == 201

    def test_last_owner_protected_409(self, client, monkeypatch):
        admin_secret = _v10_bootstrap_admin(client, monkeypatch)
        p = _v10_create_project(client, admin_secret, "PRJ_V10MEM_LO")
        # 项目当前唯一 owner 是 admin（创建者自动 owner）
        me = _v10_secret_request(client, "GET", "/api/auth/me",
                                 admin_secret).json()
        admin_user_id = None
        r = _v10_secret_request(
            client, "GET", f"/api/projects/{p['id']}/members", admin_secret)
        for m in r.json():
            if m["user_key"] == "u:v10admin":
                admin_user_id = m["user_id"]
        assert admin_user_id is not None and me["is_admin"] is True
        # 移除唯一 owner → 409
        r = _v10_secret_request(
            client, "DELETE",
            f"/api/projects/{p['id']}/members/{admin_user_id}", admin_secret)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "MEMBER_LAST_OWNER"
        # 增补第二个 owner 后可移除旧的
        owner_secret = _v10_member_secret(
            client, admin_secret, "u:v10lo", p["id"], "owner")
        r = _v10_secret_request(
            client, "DELETE",
            f"/api/projects/{p['id']}/members/{admin_user_id}", owner_secret)
        assert r.status_code == 200
        assert r.json()["status"] == "removed"

    def test_member_events_and_audit(self, client, monkeypatch):
        admin_secret = _v10_bootstrap_admin(client, monkeypatch)
        p = _v10_create_project(client, admin_secret, "PRJ_V10MEM_EA")
        _v10_provision_user(client, admin_secret, "u:v10mea")
        r = _v10_secret_request(
            client, "POST", f"/api/projects/{p['id']}/members",
            admin_secret, json={"user_key": "u:v10mea", "role": "viewer"})
        assert r.status_code == 201
        # 业务事件落库
        r = _v10_secret_request(
            client, "GET", f"/api/projects/{p['id']}/events", admin_secret)
        types = [e["event_type"] for e in r.json()]
        assert "project.member.upserted" in types
        # 安全审计落库（直接查测试库：无审计查询端点，M10 交付）
        with sqlite3.connect(_v10_db_path()) as conn:
            rows = conn.execute(
                "SELECT action, object_type, outcome FROM audit_logs "
                "WHERE object_type='project_member'").fetchall()
        assert ("project.member.upserted", "project_member",
                "success") in rows

    def test_add_disabled_user_conflict(self, client, monkeypatch):
        admin_secret = _v10_bootstrap_admin(client, monkeypatch)
        p = _v10_create_project(client, admin_secret, "PRJ_V10MEM_DIS")
        # 先建主体再禁用（无用户管理端点，测试库直改）
        r = _v10_secret_request(client, "POST", "/api/auth/api-keys",
                                admin_secret,
                                json={"name": "dis",
                                      "user_key": "u:v10mdis"})
        assert r.status_code == 201
        with sqlite3.connect(_v10_db_path()) as conn:
            conn.execute(
                "UPDATE users SET status='disabled' "
                "WHERE user_key='u:v10mdis'")
            conn.commit()
        r = _v10_secret_request(
            client, "POST", f"/api/projects/{p['id']}/members",
            admin_secret, json={"user_key": "u:v10mdis", "role": "viewer"})
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "USER_DISABLED"

    def test_auth_denied_audit_trail(self, client, monkeypatch):
        """认证失败留痕：中间件写审计 auth.denied（M1/M6 契约）。"""
        admin_secret = _v10_bootstrap_admin(client, monkeypatch)
        r = _v10_secret_request(client, "GET", "/api/projects",
                                admin_secret)
        assert r.status_code == 200
        # 缺令牌 → 401 + 审计 auth.denied
        r = client.get("/api/projects")
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "AUTH_REQUIRED"
        with sqlite3.connect(_v10_db_path()) as conn:
            rows = conn.execute(
                "SELECT object_id, action, outcome FROM audit_logs "
                "WHERE object_type='auth' AND action='auth.denied'"
            ).fetchall()
        assert any(row[0] == "reason=AUTH_REQUIRED" and row[2] == "denied"
                   for row in rows)


class TestV10Migration:
    """V1.0 迁移降级保护：认证三表有数据时 downgrade 必须中止。"""

    def test_downgrade_blocked_with_auth_data(self):
        db_file = Path(_TMP_DIR) / "v10_downgrade_guard.db"
        env = dict(os.environ)
        env["DATABASE_URL"] = f"sqlite:///{db_file}"
        env["APP_ENV"] = "test"

        def alembic(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                [sys.executable, "-m", "alembic", *args],
                cwd=str(PROJECT_ROOT), env=env,
                capture_output=True, text=True, timeout=120,
            )

        r = alembic("upgrade", "head")
        assert r.returncode == 0, r.stderr

        # 写入认证三表数据（模拟已引导管理员 + 已签发密钥）
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute(
                "INSERT INTO users (id, user_key, display_name, kind, "
                "is_admin, auth_source, status, created_at) VALUES "
                "(1,'u:guard','守卫','human',1,'local','active',"
                "'2026-01-01 00:00:00')")
            conn.execute(
                "INSERT INTO api_keys (id, key_id, key_hash, user_id, "
                "name, status, created_by, created_at) VALUES "
                "(1,'ak_guard0000000000000000','deadbeef',1,'k','active',"
                "'bootstrap','2026-01-01 00:00:00')")
            conn.commit()

        # 降级必须被中止，revision 保持 head
        r = alembic("downgrade", "-1")
        assert r.returncode != 0
        output = r.stdout + r.stderr
        assert "中止降级" in output
        assert "users=1" in output and "api_keys=1" in output
        r = alembic("current")
        assert "7f0c38e854e5" in r.stdout

        # 清理数据后降级成功（不删数据的策略由人工导出台账保证）
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute("DELETE FROM api_keys")
            conn.execute("DELETE FROM users")
            conn.commit()
        r = alembic("downgrade", "-1")
        assert r.returncode == 0, r.stderr
        r = alembic("current")
        assert "b5f2c4a9d703" in r.stdout


# ---------------------------------------------------------------- V1.0：dbt manifest 连接器（M3）

def _dbt_manifest(project: str = "retail", *, extra_nodes: dict | None = None,
                  drop_parent_map: list | None = None) -> dict:
    """fixture manifest：3 model + 2 source，parent_map 推导 4 条边。

    model_key 与平台模型同名（匹配契约）；source 节点按源表命名。
    """
    nodes = {
        "model.retail.stg_orders": {
            "resource_type": "model", "name": "stg_orders_dbt",
            "materialized": "view", "database": "retail_db",
            "schema": "analytics", "alias": "stg_orders",
        },
        "model.retail.stg_customers": {
            "resource_type": "model", "name": "stg_customers_dbt",
            "materialized": "view", "database": "retail_db",
            "schema": "analytics", "alias": "stg_customers",
        },
        "model.retail.dwd_order_fact": {
            "resource_type": "model", "name": "dwd_order_fact_dbt",
            "materialized": "table", "database": "retail_db",
            "schema": "warehouse", "alias": "dwd_order_fact",
        },
        # 非 model 节点必须被忽略
        "test.retail.not_null_stg_orders_order_id": {
            "resource_type": "test", "name": "not_null_stg_orders_order_id",
        },
        "seed.retail.country_codes": {
            "resource_type": "seed", "name": "country_codes",
        },
    }
    if extra_nodes:
        nodes.update(extra_nodes)
    sources = {
        "source.retail.raw.raw_orders": {
            "name": "raw_orders_dbt", "source_name": "raw",
            "database": "retail_db", "schema": "raw",
        },
        "source.retail.raw.raw_customers": {
            "name": "raw_customers_dbt", "source_name": "raw",
            "database": "retail_db", "schema": "raw",
        },
    }
    parent_map = {
        "model.retail.stg_orders": ["source.retail.raw.raw_orders"],
        "model.retail.stg_customers": ["source.retail.raw.raw_customers"],
        "model.retail.dwd_order_fact": ["model.retail.stg_orders",
                                        "model.retail.stg_customers"],
        # 非 model/source 父节点被忽略
        "model.retail.dwd_order_fact_ignored": ["test.retail.x"],
    }
    if drop_parent_map:
        for k in drop_parent_map:
            parent_map.pop(k, None)
    return {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
            "project_name": project,
        },
        "nodes": nodes,
        "sources": sources,
        "parent_map": parent_map,
    }


def _dbt_project(client: TestClient, prefix: str = "DBT") -> dict:
    """项目 + 5 个与 dbt 节点同名的平台模型（2 source + 3 model）。"""
    p = create_project(client, f"PRJ_{prefix}")
    for key in ("raw_orders_dbt", "raw_customers_dbt", "stg_orders_dbt",
                "stg_customers_dbt", "dwd_order_fact_dbt"):
        create_model(client, p["id"], key)
    return p


class TestV10DbtConnector:
    """M3 dbt manifest 参考连接器：parent_map 推导边、幂等重放、匹配契约。"""

    def test_import_derives_edges_and_maps_models(self, client):
        p = _dbt_project(client, "DBT01")
        r = api(client, "POST",
                f"/api/projects/{p['id']}/lineage/import/dbt-manifest",
                json={"manifest": _dbt_manifest(), "run_id": "dbt-run-001"})
        assert r.status_code == 201, r.text
        b = r.json()
        assert b["replayed"] is False
        assert b["run_id"] == "dbt-run-001"
        assert b["source_system"] == "dbt"
        assert b["scope_key"] == "dbt:retail"
        assert b["scan_mode"] == "incremental"
        assert b["status"] == "completed"
        assert b["received"] == 4
        assert b["inserted"] == 4 and b["updated"] == 0
        assert b["deactivated"] == 0
        c = b["connector"]
        assert c["name"] == "dbt-manifest"
        assert c["version"] == "1.0.0"
        assert c["project_name"] == "retail"
        assert c["nodes_scanned"] == 3 and c["sources_scanned"] == 2
        assert c["edges_built"] == 4 and c["skipped_unmatched"] == 0
        assert c["manifest_schema_version"].endswith("v12.json")
        # 边落库：血缘属性符合连接器合同
        edges = api(client, "GET",
                    f"/api/projects/{p['id']}/lineage/edges"
                    f"?source_system=dbt&scope_key=dbt:retail").json()
        assert len(edges) == 4
        for e in edges:
            assert e["lineage_type"] == "dbt_dependency"
            assert e["transformation_type"] == "sql"
            assert e["confidence"] == 100
            assert e["job_key"] == "dbt:retail"
            assert e["active"] is True
        refs = {e["source_ref"] for e in edges}
        assert ("dbt:source.retail.raw.raw_orders=>"
                "model.retail.stg_orders") in refs
        assert ("dbt:model.retail.stg_customers=>"
                "model.retail.dwd_order_fact") in refs
        # 边元数据白名单（6 键内，取目标节点属性）
        fact = next(e for e in edges if e["source_ref"] ==
                    "dbt:model.retail.stg_orders=>"
                    "model.retail.dwd_order_fact")
        md = fact["metadata"]
        assert md["project_name"] == "retail"
        assert md["node_materialized"] == "table"
        assert md["node_schema"] == "warehouse"
        assert set(md) <= {"dbt_schema_version", "project_name",
                           "node_materialized", "node_database",
                           "node_schema", "node_alias"}

    def test_replay_same_run_id_200_no_double_edges(self, client):
        p = _dbt_project(client, "DBT02")
        manifest = _dbt_manifest()
        body = {"manifest": manifest, "run_id": "dbt-run-replay"}
        r1 = api(client, "POST",
                 f"/api/projects/{p['id']}/lineage/import/dbt-manifest",
                 json=body)
        assert r1.status_code == 201
        r2 = api(client, "POST",
                 f"/api/projects/{p['id']}/lineage/import/dbt-manifest",
                 json=body)
        assert r2.status_code == 200
        assert r2.json()["replayed"] is True
        edges = api(client, "GET",
                    f"/api/projects/{p['id']}/lineage/edges").json()
        assert len(edges) == 4  # 不翻倍

    def test_same_run_id_different_payload_409(self, client):
        p = _dbt_project(client, "DBT03")
        body = {"manifest": _dbt_manifest(), "run_id": "dbt-run-dup"}
        assert api(client, "POST",
                   f"/api/projects/{p['id']}/lineage/import/dbt-manifest",
                   json=body).status_code == 201
        # 改动目标节点物化类型 → 边元数据变化 → 推导 payload 不同
        changed = _dbt_manifest()
        changed["nodes"]["model.retail.dwd_order_fact"]["materialized"] = \
            "incremental"
        body["manifest"] = changed
        r = api(client, "POST",
                f"/api/projects/{p['id']}/lineage/import/dbt-manifest",
                json=body)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "LINEAGE_RUN_ID_REUSED"

    def test_unmatched_default_422_with_sample(self, client):
        p = _dbt_project(client, "DBT04")
        manifest = _dbt_manifest(extra_nodes={
            "model.retail.stg_payments_dbt": {
                "resource_type": "model", "name": "stg_payments_dbt",
                "materialized": "view"}})
        # stg_payments_dbt 无平台模型 → 默认整批拒绝
        manifest["parent_map"]["model.retail.stg_payments_dbt"] = [
            "source.retail.raw.raw_orders"]
        r = api(client, "POST",
                f"/api/projects/{p['id']}/lineage/import/dbt-manifest",
                json={"manifest": manifest, "run_id": "dbt-run-unmatched"})
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "DBT_NODES_UNMATCHED"
        details = r.json()["error"]["details"]
        assert details["unmatched_count"] == 1
        assert details["unmatched_sample"] == ["stg_payments_dbt"]
        # 未写入任何边
        edges = api(client, "GET",
                    f"/api/projects/{p['id']}/lineage/edges").json()
        assert edges == []

    def test_skip_unmatched_true_counts(self, client):
        p = _dbt_project(client, "DBT05")
        manifest = _dbt_manifest(extra_nodes={
            "model.retail.stg_payments_dbt": {
                "resource_type": "model", "name": "stg_payments_dbt",
                "materialized": "view"}})
        manifest["parent_map"]["model.retail.stg_payments_dbt"] = [
            "source.retail.raw.raw_orders"]
        r = api(client, "POST",
                f"/api/projects/{p['id']}/lineage/import/dbt-manifest",
                json={"manifest": manifest, "run_id": "dbt-run-skip",
                      "skip_unmatched": True})
        assert r.status_code == 201, r.text
        b = r.json()
        assert b["received"] == 4  # 未匹配边被跳过
        assert b["inserted"] == 4
        assert b["connector"]["edges_built"] == 5
        assert b["connector"]["skipped_unmatched"] == 1
        edges = api(client, "GET",
                    f"/api/projects/{p['id']}/lineage/edges").json()
        assert len(edges) == 4

    def test_too_many_edges_422(self, client):
        p = create_project(client, "PRJ_DBT06")
        # 替换目标节点的父列表为 1001 个 → 推导 1001 条边，超过上限
        # （匹配前即拒绝，无需登记平台模型）
        nodes = {}
        parents = []
        for i in range(1001):
            uid = f"model.retail.src_{i}"
            nodes[uid] = {"resource_type": "model", "name": f"src_{i}"}
            parents.append(uid)
        manifest = _dbt_manifest(extra_nodes=nodes)
        manifest["parent_map"] = {"model.retail.dwd_order_fact": parents}
        r = api(client, "POST",
                f"/api/projects/{p['id']}/lineage/import/dbt-manifest",
                json={"manifest": manifest, "run_id": "dbt-run-big"})
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "DBT_MANIFEST_TOO_LARGE"
        assert r.json()["error"]["details"]["edges_built"] == 1001

    def test_invalid_manifest_422(self, client):
        p = _dbt_project(client, "DBT07")
        base = _dbt_manifest()
        # 缺 parent_map
        bad = {k: v for k, v in base.items() if k != "parent_map"}
        r = api(client, "POST",
                f"/api/projects/{p['id']}/lineage/import/dbt-manifest",
                json={"manifest": bad})
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "DBT_MANIFEST_INVALID"
        assert "parent_map" in r.json()["error"]["details"]["missing_fields"]
        # 缺 dbt_schema_version
        bad2 = dict(base)
        bad2["metadata"] = {"project_name": "retail"}
        r = api(client, "POST",
                f"/api/projects/{p['id']}/lineage/import/dbt-manifest",
                json={"manifest": bad2})
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "DBT_MANIFEST_INVALID"

    def test_scope_and_source_system_override(self, client):
        p = _dbt_project(client, "DBT08")
        r = api(client, "POST",
                f"/api/projects/{p['id']}/lineage/import/dbt-manifest",
                json={"manifest": _dbt_manifest(), "run_id": "dbt-run-ovr",
                      "scope_key": "dbt:retail:ci",
                      "source_system": "dbt-ci"})
        assert r.status_code == 201
        b = r.json()
        assert b["scope_key"] == "dbt:retail:ci"
        assert b["source_system"] == "dbt-ci"
        edges = api(client, "GET",
                    f"/api/projects/{p['id']}/lineage/edges"
                    f"?source_system=dbt-ci").json()
        assert len(edges) == 4
        # 默认 scope 无数据（两个 scope 相互隔离）
        default_edges = api(client, "GET",
                            f"/api/projects/{p['id']}/lineage/edges"
                            f"?source_system=dbt").json()
        assert default_edges == []

    def test_upsert_new_run_id_no_duplicates(self, client):
        """同 manifest 不同 run_id：按 source_ref 身份 upsert，边不重复。"""
        p = _dbt_project(client, "DBT09")
        manifest = _dbt_manifest()
        r1 = api(client, "POST",
                 f"/api/projects/{p['id']}/lineage/import/dbt-manifest",
                 json={"manifest": manifest, "run_id": "dbt-run-a"})
        assert r1.status_code == 201
        assert r1.json()["inserted"] == 4
        r2 = api(client, "POST",
                 f"/api/projects/{p['id']}/lineage/import/dbt-manifest",
                 json={"manifest": manifest, "run_id": "dbt-run-b"})
        assert r2.status_code == 201
        assert r2.json()["inserted"] == 0
        assert r2.json()["updated"] == 4
        edges = api(client, "GET",
                    f"/api/projects/{p['id']}/lineage/edges").json()
        assert len(edges) == 4

    def test_viewer_denied_editor_ok(self, client, monkeypatch):
        """授权矩阵：viewer 403 / editor 201（api_key 模式）。"""
        admin_secret = _v10_bootstrap_admin(client, monkeypatch)
        p = _v10_create_project(client, admin_secret, "PRJ_DBT10")
        for key in ("raw_orders_dbt10", "stg_orders_dbt10"):
            _v10_create_model(client, admin_secret, p["id"], key)
        viewer_secret = _v10_member_secret(
            client, admin_secret, "u:dbtv", p["id"], "viewer")
        editor_secret = _v10_member_secret(
            client, admin_secret, "u:dbte", p["id"], "editor")
        manifest = _dbt_manifest()
        # 换成与平台模型同名的键
        manifest["nodes"]["model.retail.stg_orders"]["name"] = \
            "stg_orders_dbt10"
        manifest["sources"]["source.retail.raw.raw_orders"]["name"] = \
            "raw_orders_dbt10"
        for uid in ("model.retail.stg_customers",
                    "model.retail.dwd_order_fact"):
            manifest["nodes"].pop(uid)
        manifest["parent_map"] = {
            "model.retail.stg_orders": ["source.retail.raw.raw_orders"]}
        body = {"manifest": manifest, "run_id": "dbt-run-authz"}
        r = _v10_secret_request(
            client, "POST",
            f"/api/projects/{p['id']}/lineage/import/dbt-manifest",
            viewer_secret, json=body)
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "PROJECT_ROLE_INSUFFICIENT"
        r = _v10_secret_request(
            client, "POST",
            f"/api/projects/{p['id']}/lineage/import/dbt-manifest",
            editor_secret, json=body)
        assert r.status_code == 201, r.text
        assert r.json()["inserted"] == 1

# ---------------------------------------------------------------- V1.0：发布评审上下文（M4）

class TestV10ReviewContext:
    """M4 评审上下文：区块计数、无基线、快照不足、血缘覆盖、submit 摘要。"""

    def test_context_no_baseline_and_uncovered(self, client):
        p = create_project(client, "PRJ_V10RC1")
        m1 = create_model(client, p["id"], "dwd_v10rc_src")
        m2 = create_model(client, p["id"], "dwd_v10rc_tgt")
        r = api(client, "POST", f"/api/models/{m1['id']}/relationships",
                json={"target_model_id": m2["id"],
                      "relationship_type": "depends_on"})
        assert r.status_code == 201
        r = api(client, "GET", f"/api/models/{m1['id']}/review-context")
        assert r.status_code == 200
        b = r.json()
        assert b["model_id"] == m1["id"]
        assert b["model_key"] == "dwd_v10rc_src"
        assert b["status"] == "draft"
        assert b["generated_at"]
        assert b["validation"]["status"] == "PASSED"
        assert b["validation"]["summary"]["errors"] == 0
        # 无发布基线 → 模型级范围，下游 m2 命中
        assert b["analysis_mode"] == "no_published_baseline"
        assert b["impact"]["impacted_count"] == 1
        assert b["impact"]["top_impacted"][0]["model_id"] == m2["id"]
        assert b["impact"]["truncated"] is False
        # 快照不足两次
        assert b["graph_diff"]["available"] is False
        assert b["graph_diff"]["reason"] == "INSUFFICIENT_SNAPSHOTS"
        # 血缘覆盖：1 条设计出向边无外部佐证
        cov = b["lineage_coverage"]
        assert cov["out_design_edges"] == 1
        assert cov["covered_by_external"] == 0
        assert cov["uncovered"] == [{
            "edge_key": f"design:{m1['id']}:{m2['id']}:depends_on",
            "target_model_id": m2["id"],
            "relationship_type": "depends_on"}]
        assert cov["external_inbound_edges"] == 0
        assert "direction_semantics" in b

    def test_context_with_baseline_and_graph_diff(self, client):
        p = create_project(client, "PRJ_V10RC2")
        m1 = create_model(client, p["id"], "dwd_v10rc2_src")
        m2 = create_model(client, p["id"], "dwd_v10rc2_tgt")
        r = api(client, "POST", f"/api/models/{m1['id']}/relationships",
                json={"target_model_id": m2["id"],
                      "relationship_type": "depends_on"})
        assert r.status_code == 201
        publish_model(client, m1["id"], "V1")   # 快照 1
        publish_model(client, m2["id"], "V2")   # 快照 2
        r = api(client, "GET", f"/api/models/{m1['id']}/review-context")
        assert r.status_code == 200
        b = r.json()
        assert b["analysis_mode"] == "draft_vs_latest_published"
        # 自发布后无改动 → 差异为空
        cs = b["change_summary"]
        assert cs["fields"] == {"added": [], "removed": [], "modified": []}
        assert cs["model_attributes"] == []
        assert cs["model_level_reasons"] == []
        # 图谱 diff：快照 1 → 2，m2 发布带来节点与边
        gd = b["graph_diff"]
        assert gd["available"] is True
        assert gd["from_snapshot_no"] == 1
        assert gd["to_snapshot_no"] == 2
        assert gd["summary"]["nodes_added"] == 1
        assert gd["summary"]["nodes_removed"] == 0
        assert gd["summary"]["edges_added"] == 1
        # m2 已发布，出向设计边仍无外部佐证
        assert b["lineage_coverage"]["covered_by_external"] == 0

    def test_context_change_summary_after_modification(self, client):
        p = create_project(client, "PRJ_V10RC3")
        m1 = create_model(client, p["id"], "dwd_v10rc3_src")
        m2 = create_model(client, p["id"], "dwd_v10rc3_tgt")
        api(client, "POST", f"/api/models/{m1['id']}/relationships",
            json={"target_model_id": m2["id"],
                  "relationship_type": "depends_on"})
        publish_model(client, m1["id"], "V1")
        # 发起变更并改名 → 模型属性变化 → 模型级影响范围
        rev = _start_change(client, m1["id"])
        cur = client.get(f"/api/models/{m1['id']}").json()
        _put_model(client, m1["id"], cur, cur["fields"], rev,
                   name="V10RC3 源模型（更名）")
        b = api(client, "GET",
                f"/api/models/{m1['id']}/review-context").json()
        assert b["analysis_mode"] == "draft_vs_latest_published"
        attrs = {(c["attribute"], c["from"], c["to"])
                 for c in b["change_summary"]["model_attributes"]}
        assert ("name", "测试订单明细", "V10RC3 源模型（更名）") in attrs
        assert b["change_summary"]["model_level_reasons"] == ["模型属性变化"]
        # 模型级范围 → 下游 m2 受影响
        assert b["impact"]["impacted_count"] == 1
        assert b["impact"]["top_impacted"][0]["model_id"] == m2["id"]

    def test_covered_by_external_edge(self, client):
        p = create_project(client, "PRJ_V10RC4")
        m1 = create_model(client, p["id"], "dwd_v10rc4_src")
        m2 = create_model(client, p["id"], "dwd_v10rc4_tgt")
        api(client, "POST", f"/api/models/{m1['id']}/relationships",
            json={"target_model_id": m2["id"],
                  "relationship_type": "depends_on"})
        # 外部血缘佐证 m1→m2（active）
        _v09_import(client, p["id"], "run-v10rc4",
                    [_v09_edge(m1["model_key"], m2["model_key"],
                               source_ref="ref-v10rc4")])
        b = api(client, "GET",
                f"/api/models/{m1['id']}/review-context").json()
        cov = b["lineage_coverage"]
        assert cov["out_design_edges"] == 1
        assert cov["covered_by_external"] == 1
        assert cov["uncovered"] == []
        # m1 的出向边来自外部 → m2 的入向外部边 1 条
        b2 = api(client, "GET",
                 f"/api/models/{m2['id']}/review-context").json()
        assert b2["lineage_coverage"]["external_inbound_edges"] == 1

    def test_submit_review_attaches_summary(self, client):
        p = create_project(client, "PRJ_V10RC5")
        m1 = create_model(client, p["id"], "dwd_v10rc5_src")
        m2 = create_model(client, p["id"], "dwd_v10rc5_tgt")
        api(client, "POST", f"/api/models/{m1['id']}/relationships",
            json={"target_model_id": m2["id"],
                  "relationship_type": "depends_on"})
        r = api(client, "POST", f"/api/models/{m1['id']}/submit-review")
        assert r.status_code == 200
        b = r.json()
        assert b["status"] == "in_review"
        ctx = b["review_context"]
        assert ctx["impacted_count"] == 1
        assert ctx["uncovered_design_edges"] == 1
        assert ctx["graph_diff_available"] is False
        assert ctx["validation_warnings"] == 0
        # 事件 detail_json 留痕同名字段（评审证据）
        events = api(client, "GET",
                     f"/api/projects/{p['id']}/events").json()
        submitted = next(e for e in events
                         if e["event_type"] == "review.submitted")
        detail = submitted["detail"]
        assert detail["impacted_count"] == 1
        assert detail["uncovered_design_edges"] == 1
        assert detail["graph_diff_available"] is False

    def test_unknown_model_404(self, client):
        r = api(client, "GET", "/api/models/999999/review-context")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "MODEL_NOT_FOUND"

    def test_non_member_403(self, client, monkeypatch):
        admin_secret = _v10_bootstrap_admin(client, monkeypatch)
        p = _v10_create_project(client, admin_secret, "PRJ_V10RC6")
        m = _v10_create_model(client, admin_secret, p["id"])
        r = _v10_secret_request(client, "POST", "/api/auth/api-keys",
                                admin_secret,
                                json={"name": "rc-out",
                                      "user_key": "u:v10rcout"})
        outsider = r.json()["secret"]
        r = _v10_secret_request(
            client, "GET", f"/api/models/{m['id']}/review-context",
            outsider)
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "PROJECT_ACCESS_DENIED"
        # 成员可读
        viewer_secret = _v10_member_secret(
            client, admin_secret, "u:v10rcv", p["id"], "viewer")
        r = _v10_secret_request(
            client, "GET", f"/api/models/{m['id']}/review-context",
            viewer_secret)
        assert r.status_code == 200


class TestV10DeactivationConfirm:
    """M5 full_snapshot 停用人工确认门槛（设计文档 4.5 / 验收 564 行）。"""

    def test_below_threshold_no_confirm_success(self, client):
        p, m1, m2 = _v09_two_models(client, "v10dc1")
        pid = p["id"]
        edges = [_v09_edge(m1["model_key"], m2["model_key"],
                           source_ref=f"ref-dc1-{i}")
                 for i in range(1, 5)]
        r = _v09_import(client, pid, "run-dc1-seed", edges, scope_key="dag_x")
        assert r.status_code == 201 and r.json()["inserted"] == 4
        # 保留 3/4 → 计划停用 1/4 = 0.25 < 0.5，无需确认
        keep = edges[:3]
        r = _v09_import(client, pid, "run-dc1-fs", keep,
                        scan_mode="full_snapshot", scope_key="dag_x",
                        complete_scan=True)
        assert r.status_code == 201
        j = r.json()
        assert j["deactivated"] == 1
        assert j["deactivation_confirmed"] is False
        # run 记录确认标志=false（低于阈值未确认也成功）
        runs = api(client, "GET",
                   f"/api/projects/{pid}/lineage/import-runs").json()
        run = next(x for x in runs if x["run_id"] == "run-dc1-fs")
        assert run["deactivation_confirmed"] is False

    def test_boundary_ratio_equal_threshold_not_triggered(self, client):
        p, m1, m2 = _v09_two_models(client, "v10dc2")
        pid = p["id"]
        edges = [_v09_edge(m1["model_key"], m2["model_key"],
                           source_ref=f"ref-dc2-{i}")
                 for i in range(1, 5)]
        r = _v09_import(client, pid, "run-dc2-seed", edges, scope_key="dag_x")
        assert r.status_code == 201
        # 保留 2/4 → 计划停用 2/4 = 0.5，阈值判断为严格大于，不触发
        r = _v09_import(client, pid, "run-dc2-fs", edges[:2],
                        scan_mode="full_snapshot", scope_key="dag_x",
                        complete_scan=True)
        assert r.status_code == 201
        assert r.json()["deactivated"] == 2

    def test_above_threshold_no_confirm_422_with_details(self, client):
        p, m1, m2 = _v09_two_models(client, "v10dc3")
        pid = p["id"]
        edges = [_v09_edge(m1["model_key"], m2["model_key"],
                           source_ref=f"ref-dc3-{i}")
                 for i in range(1, 5)]
        r = _v09_import(client, pid, "run-dc3-seed", edges, scope_key="dag_x")
        assert r.status_code == 201
        # 保留 1/4 → 计划停用 3/4 = 0.75 > 0.5，无确认 → 422
        r = _v09_import(client, pid, "run-dc3-fs", edges[:1],
                        scan_mode="full_snapshot", scope_key="dag_x",
                        complete_scan=True)
        assert r.status_code == 422
        body = r.json()
        assert body["error"]["code"] == "LINEAGE_DEACTIVATION_CONFIRM_REQUIRED"
        d = body["error"]["details"]
        assert d["planned_deactivated"] == 3
        assert d["active_existing"] == 4
        assert d["ratio"] == 0.75
        assert d["threshold"] == 0.5
        assert d["scope_key"] == "dag_x"
        # 原子性：一条边都没停用
        active = api(client, "GET",
                     f"/api/projects/{pid}/lineage/edges?active_only=true"
                     ).json()
        assert len(active) == 4
        # 失败批次落库（attempt=1, failure=1，error_summary 记门槛码）
        runs = api(client, "GET",
                   f"/api/projects/{pid}/lineage/import-runs").json()
        run = next(x for x in runs if x["run_id"] == "run-dc3-fs")
        assert run["status"] == "failed"
        assert run["attempt_count"] == 1 and run["failure_count"] == 1
        assert run["error_summary"][0]["code"] == \
            "LINEAGE_DEACTIVATION_CONFIRM_REQUIRED"

    def test_above_threshold_with_confirm_success(self, client):
        p, m1, m2 = _v09_two_models(client, "v10dc4")
        pid = p["id"]
        edges = [_v09_edge(m1["model_key"], m2["model_key"],
                           source_ref=f"ref-dc4-{i}")
                 for i in range(1, 5)]
        r = _v09_import(client, pid, "run-dc4-seed", edges, scope_key="dag_x")
        assert r.status_code == 201
        r = _v09_import(client, pid, "run-dc4-fs", edges[:1],
                        scan_mode="full_snapshot", scope_key="dag_x",
                        complete_scan=True, confirm_deactivation=True)
        assert r.status_code == 201
        j = r.json()
        assert j["deactivated"] == 3
        assert j["deactivation_confirmed"] is True
        runs = api(client, "GET",
                   f"/api/projects/{pid}/lineage/import-runs").json()
        run = next(x for x in runs if x["run_id"] == "run-dc4-fs")
        assert run["deactivation_confirmed"] is True
        active = api(client, "GET",
                     f"/api/projects/{pid}/lineage/edges?active_only=true"
                     ).json()
        assert len(active) == 1
        assert active[0]["source_ref"] == "ref-dc4-1"

    def test_empty_full_snapshot_no_confirm_422(self, client):
        """空完整扫描天然落入门槛（需求说明书 46 行）。"""
        p, m1, m2 = _v09_two_models(client, "v10dc5")
        pid = p["id"]
        r = _v09_import(client, pid, "run-dc5-seed",
                        [_v09_edge(m1["model_key"], m2["model_key"],
                                   source_ref="ref-dc5-1")],
                        scope_key="dag_x")
        assert r.status_code == 201
        r = _v09_import(client, pid, "run-dc5-fs", [],
                        scan_mode="full_snapshot", scope_key="dag_x",
                        complete_scan=True)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == \
            "LINEAGE_DEACTIVATION_CONFIRM_REQUIRED"
        assert r.json()["error"]["details"]["ratio"] == 1.0

    def test_empty_full_snapshot_fresh_scope_not_triggered(self, client):
        """fresh scope 无 active 边 → ratio=0，空扫描合法（V0.9.2 行为保持）。"""
        p, m1, m2 = _v09_two_models(client, "v10dc6")
        r = _v09_import(client, p["id"], "run-dc6-fs", [],
                        scan_mode="full_snapshot", scope_key="zone_new",
                        complete_scan=True)
        assert r.status_code == 201
        assert r.json()["deactivated"] == 0
        assert r.json()["planned_deactivate_ratio"] == 0.0

    def test_preview_warns_ratio_and_confirmation_required(self, client):
        p, m1, m2 = _v09_two_models(client, "v10dc7")
        pid = p["id"]
        edges = [_v09_edge(m1["model_key"], m2["model_key"],
                           source_ref=f"ref-dc7-{i}")
                 for i in range(1, 5)]
        r = _v09_import(client, pid, "run-dc7-seed", edges, scope_key="dag_x")
        assert r.status_code == 201
        body = {
            "source_system": "airflow", "run_id": "run-dc7-fs",
            "scan_mode": "full_snapshot", "scope_key": "dag_x",
            "complete_scan": True, "edges": edges[:1],
        }
        r = api(client, "POST", f"/api/projects/{pid}/lineage/preview",
                json=body)
        assert r.status_code == 200
        j = r.json()
        assert j["writes_performed"] is False
        assert j["planned_deactivate_ratio"] == 0.75
        assert j["confirmation_required"] is True
        assert j["active_existing"] == 4
        # preview 不写入：边仍 4 条 active，无 run 落库
        active = api(client, "GET",
                     f"/api/projects/{pid}/lineage/edges?active_only=true"
                     ).json()
        assert len(active) == 4
        runs = api(client, "GET",
                   f"/api/projects/{pid}/lineage/import-runs").json()
        assert all(x["run_id"] != "run-dc7-fs" for x in runs)
        # 带确认的 payload preview → confirmation_required=false
        confirmed = dict(body, confirm_deactivation=True)
        r = api(client, "POST", f"/api/projects/{pid}/lineage/preview",
                json=confirmed)
        assert r.json()["confirmation_required"] is False

    def test_incremental_mode_unaffected(self, client):
        """增量模式不看比例：大量新边 incremental 导入无需确认。"""
        p, m1, m2 = _v09_two_models(client, "v10dc8")
        pid = p["id"]
        edges = [_v09_edge(m1["model_key"], m2["model_key"],
                           source_ref=f"ref-dc8-{i}")
                 for i in range(1, 6)]
        r = _v09_import(client, pid, "run-dc8-seed", edges[:4],
                        scope_key="dag_x")
        assert r.status_code == 201
        r = _v09_import(client, pid, "run-dc8-inc", edges, scope_key="dag_x")
        assert r.status_code == 201
        j = r.json()
        assert j["updated"] == 4 and j["inserted"] == 1
        assert j["deactivated"] == 0
        assert j["planned_deactivate_ratio"] == 0.0
        assert j["confirmation_required"] is False

    def test_failed_run_retry_with_confirm_keeps_counters(self, client):
        """422 落 failed run 后带确认重试：attempt/failure 历史保留。"""
        p, m1, m2 = _v09_two_models(client, "v10dc9")
        pid = p["id"]
        edges = [_v09_edge(m1["model_key"], m2["model_key"],
                           source_ref=f"ref-dc9-{i}")
                 for i in range(1, 5)]
        r = _v09_import(client, pid, "run-dc9-seed", edges, scope_key="dag_x")
        assert r.status_code == 201
        r = _v09_import(client, pid, "run-dc9-fs", edges[:1],
                        scan_mode="full_snapshot", scope_key="dag_x",
                        complete_scan=True)
        assert r.status_code == 422  # 第一次：门槛拒绝
        r = _v09_import(client, pid, "run-dc9-fs", edges[:1],
                        scan_mode="full_snapshot", scope_key="dag_x",
                        complete_scan=True, confirm_deactivation=True)
        assert r.status_code == 201
        j = r.json()
        assert j["deactivated"] == 3
        assert j["deactivation_confirmed"] is True
        assert j["attempt_count"] == 2  # 历史保留
        assert j["failure_count"] == 1
        runs = api(client, "GET",
                   f"/api/projects/{pid}/lineage/import-runs").json()
        run = next(x for x in runs if x["run_id"] == "run-dc9-fs")
        assert run["status"] == "completed"
        assert run["attempt_count"] == 2 and run["failure_count"] == 1
        assert run["deactivation_confirmed"] is True

    def test_custom_threshold_env(self, client, monkeypatch):
        """阈值可配置（FULL_SNAPSHOT_DEACTIVATE_RATIO_THRESHOLD）。"""
        monkeypatch.setattr(
            settings, "full_snapshot_deactivate_ratio_threshold", 0.9)
        p, m1, m2 = _v09_two_models(client, "v10dc10")
        pid = p["id"]
        edges = [_v09_edge(m1["model_key"], m2["model_key"],
                           source_ref=f"ref-dc10-{i}")
                 for i in range(1, 5)]
        r = _v09_import(client, pid, "run-dc10-seed", edges,
                        scope_key="dag_x")
        assert r.status_code == 201
        # 0.75 < 0.9（调高后）→ 不再触发
        r = _v09_import(client, pid, "run-dc10-fs", edges[:1],
                        scan_mode="full_snapshot", scope_key="dag_x",
                        complete_scan=True)
        assert r.status_code == 201
        assert r.json()["deactivated"] == 3


def _v10_reset_metrics() -> None:
    """清零进程内业务指标计数器（测试确定性断言用）。"""
    m = metrics_mod
    m.auth_denied_by_reason.clear()
    m.model_published_by_layer.clear()
    m.graph_snapshot_created_by_type.clear()
    m.lineage_import_runs_by_status.clear()
    m.lineage_import_records_by_result.clear()
    m.lineage_import_conflicts_total = 0


def _v10_reference_health_groups(
    project_id: int, stale_after_minutes: int
) -> dict[tuple[str, str], dict]:
    """对拍基准：按 V1.0 前「全量加载 + 内存分组」旧算法独立复算。

    不经由 API/ORM 聚合路径，直接 sqlite3 全量读边与批次，逐组复算
    active/inactive/latest/stale/last_successful/last_failed/
    consecutive_failures，与 SQL 聚合版响应逐字段比较。
    """
    db_path = os.environ["DATABASE_URL"].removeprefix("sqlite:///")
    now = models_mod.utcnow()
    with sqlite3.connect(db_path) as conn:
        edge_rows = conn.execute(
            "SELECT source_system, scope_key, active, last_seen_at,"
            " observed_at FROM external_lineage_edges WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        run_rows = conn.execute(
            "SELECT source_system, scope_key, status, created_at"
            " FROM lineage_import_runs WHERE project_id = ?"
            " ORDER BY created_at DESC, id DESC",
            (project_id,),
        ).fetchall()

    def parse(value):
        if value is None:
            return None
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)

    groups: dict[tuple[str, str], dict] = {}

    def group_of(source_system, scope_key):
        return groups.setdefault((source_system, scope_key), {
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

    for source_system, scope_key, active, last_seen, observed in edge_rows:
        g = group_of(source_system, scope_key)
        if active:
            g["active_edges"] += 1
        else:
            g["inactive_edges"] += 1
        basis = parse(last_seen) or parse(observed)
        if basis is not None and (g["latest_observed_at"] is None
                                  or basis > g["latest_observed_at"]):
            g["latest_observed_at"] = basis
        if active and (basis is None
                       or (now - basis).total_seconds() / 60
                       > stale_after_minutes):
            g["stale_candidates"] += 1

    for source_system, scope_key, status, created_at in run_rows:
        g = group_of(source_system, scope_key)
        created = parse(created_at)
        if status == "completed":
            if g["last_successful_run_at"] is None:
                g["last_successful_run_at"] = created
        else:
            if g["last_failed_run_at"] is None:
                g["last_failed_run_at"] = created
            if g["last_successful_run_at"] is None or (
                created > g["last_successful_run_at"]
            ):
                g["consecutive_failures"] += 1

    for g in groups.values():
        g["latest_observed_at"] = iso_mod(g["latest_observed_at"])
        g["last_successful_run_at"] = iso_mod(g["last_successful_run_at"])
        g["last_failed_run_at"] = iso_mod(g["last_failed_run_at"])
    return groups


class TestV10Observability:
    """M6 可观测生产化：指标族、summary 增量、health SQL 聚合并拍。"""

    def test_metrics_business_families(self, client):
        _v10_reset_metrics()
        p, m1, m2 = _v09_two_models(client, "v10obs")
        pid = p["id"]
        publish_model(client, m1["id"], "V1")  # 发布 + publish 快照
        # 血缘导入：成功一次
        _v09_import(client, pid, "run-obs-ok",
                    [_v09_edge(m1["model_key"], m2["model_key"],
                               source_ref="ref-obs-ok")])
        # 血缘导入：失败一次（模型不存在 → 校验失败）
        _v09_import(client, pid, "run-obs-bad",
                    [_v09_edge("no_such_model", m2["model_key"],
                               source_ref="ref-obs-bad")])
        # run_id 冲突（同 run_id 不同 payload）
        payload = [_v09_edge(m1["model_key"], m2["model_key"],
                             source_ref="ref-obs-ok", confidence=42)]
        _v09_import(client, pid, "run-obs-ok", payload)

        r = api(client, "GET", "/metrics")
        assert r.status_code == 200
        text = r.text
        assert 'modeling_model_published_total{layer="DWD"} 1' in text
        assert 'modeling_graph_snapshot_created_total{snapshot_type="publish"} 1' \
            in text
        assert 'modeling_lineage_import_runs_total{status="completed"} 1' in text
        assert 'modeling_lineage_import_runs_total{status="failed"} 1' in text
        assert 'modeling_lineage_import_records_total{result="inserted"} 1' in text
        assert "modeling_lineage_import_conflicts_total 1" in text
        # 既有指标族保留（向后兼容）
        assert "http_requests_total" in text
        assert "process_uptime_seconds" in text
        # gauge 族抓取数据库实时状态（session 库含其他测试数据）：
        # 与 sqlite3 直连复算逐项相等
        db_path = os.environ["DATABASE_URL"].removeprefix("sqlite:///")
        with sqlite3.connect(db_path) as conn:
            snap_total = conn.execute(
                "SELECT COUNT(*) FROM project_graph_snapshots").fetchone()[0]
            node_sum, edge_sum = conn.execute(
                "SELECT COALESCE(SUM(node_count),0), COALESCE(SUM(edge_count),0)"
                " FROM project_graph_snapshots s JOIN ("
                "   SELECT project_id, MAX(snapshot_no) AS mx"
                "   FROM project_graph_snapshots GROUP BY project_id"
                " ) t ON s.project_id = t.project_id"
                " AND s.snapshot_no = t.mx").fetchone()
            active = conn.execute(
                "SELECT COUNT(*) FROM external_lineage_edges"
                " WHERE active = 1").fetchone()[0]
            inactive = conn.execute(
                "SELECT COUNT(*) FROM external_lineage_edges"
                " WHERE active = 0").fetchone()[0]
        assert f"modeling_graph_snapshots_current {snap_total}" in text
        assert f"modeling_graph_snapshot_nodes {node_sum}" in text
        assert f"modeling_graph_snapshot_edges {edge_sum}" in text
        assert f"modeling_lineage_edges_active {active}" in text
        assert f"modeling_lineage_edges_inactive {inactive}" in text

    def test_summary_incremental_fields(self, client):
        r = api(client, "GET", "/api/observability/summary")
        assert r.status_code == 200
        b = r.json()
        # 既有字段保留
        for key in ("uptime_seconds", "requests_total",
                    "requests_by_status_class", "errors_total",
                    "avg_duration_ms", "note"):
            assert key in b
        # V1.0 增量
        assert b["app_version"] == "1.2.0"
        assert b["head_revision"] == "7f0c38e854e5"
        assert b["auth"]["mode"] == "demo"
        assert isinstance(b["auth"]["denied_total"], int)
        lineage = b["lineage"]
        for key in ("import_runs_completed", "import_runs_failed",
                    "active_edges", "inactive_edges", "stale_candidates"):
            assert key in lineage
        assert lineage["import_runs_completed"] >= 1
        assert lineage["active_edges"] >= 1

    def test_health_sql_aggregation_matches_reference(self, client):
        """health SQL 聚合与旧实现（全量内存分组）对拍：逐字段相等。"""
        p, m1, m2 = _v09_two_models(client, "v10health")
        pid = p["id"]
        # airflow/default：一次成功 + 一次失败（consecutive=1）
        _v09_import(client, pid, "run-vh1",
                    [_v09_edge(m1["model_key"], m2["model_key"],
                               source_ref="ref-vh1")])
        _v09_import(client, pid, "run-vh2",
                    [_v09_edge("no_such", m2["model_key"],
                               source_ref="ref-vh2")])
        # dbt/prod：一次成功（3 天前观察 → stale 候选）
        _v09_import(client, pid, "run-vh3",
                    [_v09_edge(m1["model_key"], m2["model_key"],
                               source_ref="ref-vh3")],
                    source_system="dbt", scope_key="prod",
                    observed_at=(datetime.now(timezone.utc)
                                 - timedelta(days=3)).isoformat())
        edges = api(client, "GET",
                    f"/api/projects/{pid}/lineage/edges"
                    f"?source_system=dbt").json()
        _backdate_edge(edges[0]["id"], days=3)
        # flink/adhoc：一次成功 + full_snapshot 空批次停用（inactive 边）
        _v09_import(client, pid, "run-vh4",
                    [_v09_edge(m1["model_key"], m2["model_key"],
                               source_ref="ref-vh4")],
                    source_system="flink", scope_key="adhoc")
        _v09_import(client, pid, "run-vh5", [],
                    scan_mode="full_snapshot", scope_key="adhoc",
                    complete_scan=True)

        stale_after_minutes = 1440
        h = api(client, "GET",
                f"/api/projects/{pid}/lineage/health"
                f"?stale_after_minutes={stale_after_minutes}")
        assert h.status_code == 200
        body = h.json()
        reference = _v10_reference_health_groups(pid, stale_after_minutes)
        actual = {(g["source_system"], g["scope_key"]): g
                  for g in body["groups"]}
        assert set(actual) == set(reference)
        for key, expected in reference.items():
            got = actual[key]
            for field in ("active_edges", "inactive_edges",
                          "latest_observed_at", "stale_candidates",
                          "last_successful_run_at", "last_failed_run_at",
                          "consecutive_failures"):
                assert got[field] == expected[field], (
                    f"{key}.{field}: api={got[field]} ref={expected[field]}"
                )
        assert body["total_active_edges"] == sum(
            g["active_edges"] for g in reference.values())
        assert body["total_stale_candidates"] == sum(
            g["stale_candidates"] for g in reference.values())

    def test_auth_denied_counter_in_api_key_mode(self, client, monkeypatch):
        _v10_reset_metrics()
        admin_secret = _v10_bootstrap_admin(client, monkeypatch)
        # 无令牌访问 → 401 AUTH_REQUIRED，拒绝计数 +1
        r = client.get("/api/projects")
        assert r.status_code == 401
        # admin 读 /metrics：拒绝计数入指标族
        r = _v10_secret_request(client, "GET", "/metrics", admin_secret)
        assert r.status_code == 200
        assert 'modeling_auth_denied_total{reason="AUTH_REQUIRED"} 1' in r.text
        # 非 admin（viewer）访问 /metrics → 403（设计文档 3.3 授权矩阵）
        p = _v10_create_project(client, admin_secret, "PRJ_V10OBS_MT")
        viewer_secret = _v10_member_secret(
            client, admin_secret, "u:v10obsmt", p["id"], "viewer")
        r = _v10_secret_request(client, "GET", "/metrics", viewer_secret)
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "PROJECT_ROLE_INSUFFICIENT"
        r = _v10_secret_request(client, "GET", "/metrics", admin_secret)
        assert 'modeling_auth_denied_total{reason="AUTH_REQUIRED"} 1' in r.text
        # gauge 族（查库抓取）始终渲染；空 counter 族按 Prometheus 惯例省略
        assert "modeling_graph_snapshots_current" in r.text
        assert "modeling_lineage_edges_active" in r.text


# ---------------------------------------------------------------- V1.2：用户管理与 RBAC 防护


def _v12_user_id(client: TestClient, admin_secret: str,
                 user_key: str) -> int:
    """按 user_key 查 user_id（管理端点返回体）。"""
    r = _v10_secret_request(client, "GET", "/api/users", admin_secret)
    assert r.status_code == 200, r.text
    for u in r.json():
        if u["user_key"] == user_key:
            return u["user_id"]
    raise AssertionError(f"user {user_key} not found")


def _v12_create_user(client: TestClient, admin_secret: str, user_key: str,
                     display_name: str = "V12 用户",
                     is_admin: bool = False) -> dict:
    r = _v10_secret_request(client, "POST", "/api/users", admin_secret,
                            json={"user_key": user_key,
                                  "display_name": display_name,
                                  "is_admin": is_admin})
    assert r.status_code == 201, r.text
    return r.json()


class TestV12Users:
    """V1.2 用户管理（平台级）：CRUD、越权拒绝、能力查询。"""

    def test_users_list_requires_admin(self, client, monkeypatch):
        admin_secret = _v10_bootstrap_admin(client, monkeypatch)
        p = _v10_create_project(client, admin_secret, "PRJ_V12U_L")
        # admin：200 且含自身摘要
        r = _v10_secret_request(client, "GET", "/api/users", admin_secret)
        assert r.status_code == 200
        rows = r.json()
        me_row = next(u for u in rows if u["user_key"] == "u:v10admin")
        assert me_row["is_admin"] is True
        assert me_row["status"] == "active"
        assert me_row["project_count"] >= 1
        # 两级正交：项目 owner（非 admin）也不能进用户管理
        owner_secret = _v10_member_secret(
            client, admin_secret, "u:v12ul", p["id"], "owner")
        r = _v10_secret_request(client, "GET", "/api/users", owner_secret)
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "ADMIN_REQUIRED"
        r = _v10_secret_request(client, "POST", "/api/users", owner_secret,
                                json={"user_key": "u:v12x",
                                      "display_name": "越权"})
        assert r.status_code == 403
        # viewer 同样拒绝
        viewer_secret = _v10_member_secret(
            client, admin_secret, "u:v12ulv", p["id"], "viewer")
        r = _v10_secret_request(client, "GET", "/api/users", viewer_secret)
        assert r.status_code == 403

    def test_create_user_and_duplicate_409(self, client, monkeypatch):
        admin_secret = _v10_bootstrap_admin(client, monkeypatch)
        body = _v12_create_user(client, admin_secret, "u:v12new", "新用户")
        assert body["user_key"] == "u:v12new"
        assert body["kind"] == "human"
        assert body["is_admin"] is False
        assert body["status"] == "active"
        assert body["project_count"] == 0
        assert body["active_key_count"] == 0
        # 重复 user_key → 409 USER_KEY_EXISTS
        r = _v10_secret_request(client, "POST", "/api/users", admin_secret,
                                json={"user_key": "u:v12new",
                                      "display_name": "重复"})
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "USER_KEY_EXISTS"

    def test_me_capabilities(self, client, monkeypatch):
        admin_secret = _v10_bootstrap_admin(client, monkeypatch)
        p = _v10_create_project(client, admin_secret, "PRJ_V12ME")
        editor_secret = _v10_member_secret(
            client, admin_secret, "u:v12me", p["id"], "editor")
        # admin：平台全能力 + status 字段
        me = _v10_secret_request(client, "GET", "/api/auth/me",
                                 admin_secret).json()
        assert me["status"] == "active"
        assert me["capabilities"]["platform"] == [
            "user.manage", "api_key.provision", "metrics.view"]
        # editor：无平台能力；项目能力族 read+write
        me2 = _v10_secret_request(client, "GET", "/api/auth/me",
                                  editor_secret).json()
        assert me2["capabilities"]["platform"] == []
        proj_caps = {c["project_id"]: c
                     for c in me2["capabilities"]["projects"]}
        assert proj_caps[p["id"]]["role"] == "editor"
        assert proj_caps[p["id"]]["capabilities"] == [
            "model.read", "model.write"]

    def test_update_display_name_and_filter(self, client, monkeypatch):
        admin_secret = _v10_bootstrap_admin(client, monkeypatch)
        _v12_create_user(client, admin_secret, "u:v12filter", "过滤测试")
        uid = _v12_user_id(client, admin_secret, "u:v12filter")
        r = _v10_secret_request(client, "PUT", f"/api/users/{uid}",
                                admin_secret,
                                json={"display_name": "改名后"})
        assert r.status_code == 200
        assert r.json()["display_name"] == "改名后"
        # q 模糊匹配（user_key 与 display_name 均可）
        r = _v10_secret_request(client, "GET", "/api/users", admin_secret,
                                params={"q": "v12filter"})
        assert [u["user_key"] for u in r.json()] == ["u:v12filter"]
        r = _v10_secret_request(client, "GET", "/api/users", admin_secret,
                                params={"q": "改名后"})
        assert len(r.json()) == 1
        # status 过滤
        r = _v10_secret_request(client, "GET", "/api/users", admin_secret,
                                params={"status": "disabled"})
        assert r.json() == []
        # 未知 user → 404
        r = _v10_secret_request(client, "PUT", "/api/users/999999",
                                admin_secret, json={"display_name": "x"})
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "USER_NOT_FOUND"

    def test_disable_user_invalidates_keys(self, client, monkeypatch):
        """P6：禁用即时全量失效；重新启用后恢复（缓存双向清理）。"""
        admin_secret = _v10_bootstrap_admin(client, monkeypatch)
        _v12_create_user(client, admin_secret, "u:v12dis", "待禁用")
        secret = _v10_provision_user(client, admin_secret, "u:v12dis")
        r = _v10_secret_request(client, "GET", "/api/auth/me", secret)
        assert r.status_code == 200
        uid = _v12_user_id(client, admin_secret, "u:v12dis")
        r = _v10_secret_request(client, "PUT", f"/api/users/{uid}",
                                admin_secret, json={"status": "disabled"})
        assert r.status_code == 200
        # 旧令牌下一请求即被拒（不等 TTL）
        r = _v10_secret_request(client, "GET", "/api/auth/me", secret)
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "USER_DISABLED"
        # 禁用主体不可再加成员（P5）
        p = _v10_create_project(client, admin_secret, "PRJ_V12DIS")
        r = _v10_secret_request(
            client, "POST", f"/api/projects/{p['id']}/members",
            admin_secret, json={"user_key": "u:v12dis", "role": "viewer"})
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "USER_DISABLED"
        # 重新启用 → 恢复可用
        r = _v10_secret_request(client, "PUT", f"/api/users/{uid}",
                                admin_secret, json={"status": "active"})
        assert r.status_code == 200
        r = _v10_secret_request(client, "GET", "/api/auth/me", secret)
        assert r.status_code == 200

    def test_admin_change_invalidates_identity_cache(self, client, monkeypatch):
        """P6 扩展：is_admin 降级即时生效（旧令牌不保留 admin 权力）。"""
        admin_secret = _v10_bootstrap_admin(client, monkeypatch)
        _v12_create_user(client, admin_secret, "u:v12adm2", "副管理员",
                         is_admin=True)
        secret2 = _v10_provision_user(client, admin_secret, "u:v12adm2")
        r = _v10_secret_request(client, "GET", "/api/users", secret2)
        assert r.status_code == 200
        uid = _v12_user_id(client, admin_secret, "u:v12adm2")
        r = _v10_secret_request(client, "PUT", f"/api/users/{uid}",
                                admin_secret, json={"is_admin": False})
        assert r.status_code == 200
        r = _v10_secret_request(client, "GET", "/api/users", secret2)
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "ADMIN_REQUIRED"

    def test_last_admin_protected_409(self, client, monkeypatch):
        """P1：唯一 admin 降级/禁用均 409 ADMIN_LAST_ADMIN。"""
        admin_secret = _v10_bootstrap_admin(client, monkeypatch)
        uid = _v12_user_id(client, admin_secret, "u:v10admin")
        r = _v10_secret_request(client, "PUT", f"/api/users/{uid}",
                                admin_secret, json={"is_admin": False})
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "ADMIN_LAST_ADMIN"
        r = _v10_secret_request(client, "PUT", f"/api/users/{uid}",
                                admin_secret, json={"status": "disabled"})
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "ADMIN_LAST_ADMIN"
        # 平台仍可操作（未锁死）
        r = _v10_secret_request(client, "GET", "/api/users", admin_secret)
        assert r.status_code == 200

    def test_self_demote_and_disable_protected(self, client, monkeypatch):
        """P2/P3：双 admin 场景下自降级/自禁用 409（P1 不触发）。"""
        admin_secret = _v10_bootstrap_admin(client, monkeypatch)
        _v12_create_user(client, admin_secret, "u:v12adm3", "副管理员2",
                         is_admin=True)
        uid = _v12_user_id(client, admin_secret, "u:v10admin")
        r = _v10_secret_request(client, "PUT", f"/api/users/{uid}",
                                admin_secret, json={"is_admin": False})
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "ADMIN_SELF_DEMOTE"
        r = _v10_secret_request(client, "PUT", f"/api/users/{uid}",
                                admin_secret, json={"status": "disabled"})
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "ADMIN_SELF_DISABLE"

    def test_second_admin_can_demote_first(self, client, monkeypatch):
        """双 admin 互相可降级（P1 不触发、非自操作），且即时生效。"""
        admin_secret = _v10_bootstrap_admin(client, monkeypatch)
        _v12_create_user(client, admin_secret, "u:v12adm4", "副管理员3",
                         is_admin=True)
        secret2 = _v10_provision_user(client, admin_secret, "u:v12adm4")
        uid = _v12_user_id(client, secret2, "u:v10admin")
        r = _v10_secret_request(client, "PUT", f"/api/users/{uid}",
                                secret2, json={"is_admin": False})
        assert r.status_code == 200
        # 原 admin 即时失去平台能力
        r = _v10_secret_request(client, "GET", "/api/users", admin_secret)
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "ADMIN_REQUIRED"
        # 但项目旁路随之失效：不再是 admin 也无成员关系 → 403
        p = _v10_create_project(client, secret2, "PRJ_V12DEM")
        r = _v10_secret_request(
            client, "GET", f"/api/projects/{p['id']}/subject-areas",
            admin_secret)
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "PROJECT_ACCESS_DENIED"

    def test_update_empty_body_422(self, client, monkeypatch):
        admin_secret = _v10_bootstrap_admin(client, monkeypatch)
        uid = _v12_create_user(client, admin_secret, "u:v12empty")["user_id"]
        r = _v10_secret_request(client, "PUT", f"/api/users/{uid}",
                                admin_secret, json={})
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "USER_UPDATE_EMPTY"


class TestV12MemberGuard:
    """V1.2 P4：成员 upsert 降级唯一 owner 保护（与 DELETE 路径对齐）。"""

    def test_upsert_last_owner_protected_409(self, client, monkeypatch):
        admin_secret = _v10_bootstrap_admin(client, monkeypatch)
        # 普通成员自建项目 → 唯一 owner（创建者自动 owner）
        member_secret = _v10_provision_user(client, admin_secret, "u:v12p4")
        p = _v10_create_project(client, member_secret, "PRJ_V12P4")
        members = _v10_secret_request(
            client, "GET", f"/api/projects/{p['id']}/members",
            admin_secret).json()
        owners = [m for m in members if m["role"] == "owner"]
        assert len(owners) == 1 and owners[0]["user_key"] == "u:v12p4"
        # admin upsert 把唯一 owner 降级 → 409（调用方是 admin 也不豁免）
        r = _v10_secret_request(
            client, "POST", f"/api/projects/{p['id']}/members",
            admin_secret, json={"user_key": "u:v12p4", "role": "viewer"})
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "MEMBER_LAST_OWNER"
        # 角色未被改动
        members = _v10_secret_request(
            client, "GET", f"/api/projects/{p['id']}/members",
            admin_secret).json()
        assert members[0]["role"] == "owner"
        # 先提升第二个 owner，再降级第一个 → 放行
        _v10_member_secret(client, admin_secret, "u:v12p4b", p["id"],
                           "owner")
        r = _v10_secret_request(
            client, "POST", f"/api/projects/{p['id']}/members",
            admin_secret, json={"user_key": "u:v12p4", "role": "viewer"})
        assert r.status_code == 200
        assert r.json()["role"] == "viewer"
