"""V0.9 冒烟：发布快照 / 视图 / diff / path / 血缘导入族。"""
import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_TMP = tempfile.mkdtemp(prefix="dmc_v09_smoke_")
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/platform.db"
os.environ["AUTO_MIGRATE"] = "true"
os.environ["SEED_DEMO_DATA"] = "false"
os.environ["LOG_LEVEL"] = "WARNING"
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient
from backend.app.main import app

failures = []


def check(name, cond, extra=""):
    status = "OK " if cond else "FAIL"
    print(f"[{status}] {name} {extra if not cond else ''}")
    if not cond:
        failures.append(name)


with TestClient(app) as c:
    h = {"X-Actor": "smoke.bot"}

    def api(m, url, **kw):
        kw.setdefault("headers", h)
        return c.request(m, url, **kw)

    # --- 基础：项目 + 两个模型 ---
    p = api("POST", "/api/projects", json={
        "project_key": "PRJ_V09", "name": "V0.9 冒烟"}).json()
    pid = p["id"]

    def mk_model(key, fields):
        return api("POST", f"/api/projects/{pid}/models", json={
            "model_key": key, "name": key, "layer": "DWD",
            "fields": fields}).json()

    m1 = mk_model("ods_a", [{"field_code": "id"},
                            {"field_code": "name"}])
    m2 = mk_model("dwd_b", [{"field_code": "id"},
                            {"field_code": "total"}])

    # --- 关系（发布前建立）+ 两次发布 → 快照 A/B ---
    def publish(mid, comment="v1"):
        api("POST", f"/api/models/{mid}/submit-review")
        api("POST", f"/api/models/{mid}/approve", json={"comment": "ok"})
        r = api("POST", f"/api/models/{mid}/publish", json={"comment": comment})
        assert r.status_code == 200, r.text
        return r.json()

    api("POST", f"/api/models/{m1['id']}/relationships", json={
        "target_model_id": m2["id"], "relationship_type": "depends_on"})
    pub1 = publish(m1["id"])
    check("发布返回 graph_snapshot_no", "graph_snapshot_no" in pub1, pub1)
    check("发布返回 graph_hash(64)", len(pub1.get("graph_hash", "")) == 64)

    # --- 快照列表 ---
    snaps = api("GET", f"/api/projects/{pid}/model-graph/snapshots").json()
    check("快照列表 1 条", len(snaps) == 1, snaps)
    check("快照类型 publish", snaps[0]["snapshot_type"] == "publish")
    check("快照节点数 1", snaps[0]["node_count"] == 1)
    check("快照 omitted_edge_count=1（目标未发布）",
          snaps[0]["omitted_edge_count"] == 1, snaps[0])

    pub2 = publish(m2["id"])
    check("第二次发布 snapshot_no=2", pub2["graph_snapshot_no"] == 2, pub2)

    # --- published_latest 视图 ---
    g = api("GET", f"/api/projects/{pid}/model-graph?view=published_latest").json()
    check("published_latest 节点 2", len(g["nodes"]) == 2, g)
    check("published_latest 边 1", len(g["edges"]) == 1, g)
    check("edge_key 格式", g["edges"][0]["edge_key"] ==
          f"design:{m1['id']}:{m2['id']}:depends_on", g["edges"][0])

    # --- snapshot 视图 ---
    g2 = api("GET", f"/api/projects/{pid}/model-graph?view=snapshot&snapshot_no=1").json()
    check("snapshot#1 节点 1", len(g2["nodes"]) == 1, g2)
    r = api("GET", f"/api/projects/{pid}/model-graph?view=snapshot")
    check("snapshot 缺 snapshot_no → 422 GRAPH_SNAPSHOT_REQUIRED",
          r.status_code == 422 and r.json()["error"]["code"] == "GRAPH_SNAPSHOT_REQUIRED")
    r = api("GET", f"/api/projects/{pid}/model-graph?view=snapshot&snapshot_no=99")
    check("snapshot 不存在 → 404 GRAPH_SNAPSHOT_NOT_FOUND",
          r.status_code == 404 and r.json()["error"]["code"] == "GRAPH_SNAPSHOT_NOT_FOUND")

    # --- diff ---
    d = api("GET", f"/api/projects/{pid}/model-graph/diff?from_snapshot_no=1&to_snapshot_no=2").json()
    check("diff 节点新增 1", len(d["nodes"]["added"]) == 1, d)
    check("diff 边新增 1", len(d["edges"]["added"]) == 1, d)

    # --- path：三方向 ---
    r = api("GET", f"/api/projects/{pid}/model-graph/path?source_model_id={m1['id']}"
            f"&target_model_id={m2['id']}&direction=downstream&view=published_latest")
    check("path downstream found", r.json().get("found") is True, r.text)
    r = api("GET", f"/api/projects/{pid}/model-graph/path?source_model_id={m2['id']}"
            f"&target_model_id={m1['id']}&direction=upstream&view=published_latest")
    j = r.json()
    check("path upstream found + reverse", j.get("found") is True
          and j["edges"][0]["traversal_direction"] == "reverse", j)
    r = api("GET", f"/api/projects/{pid}/model-graph/path?source_model_id={m1['id']}"
            f"&target_model_id={m2['id']}&direction=sideways")
    check("direction 非法 → 422 GRAPH_DIRECTION_INVALID",
          r.status_code == 422 and r.json()["error"]["code"] == "GRAPH_DIRECTION_INVALID")
    r = api("GET", f"/api/projects/{pid}/model-graph/path?source_model_id={m1['id']}"
            f"&target_model_id={m2['id']}&direction=downstream&max_depth=60")
    check("max_depth 越界 → 422", r.status_code == 422)

    # --- 血缘导入：incremental ---
    payload = {
        "source_system": "airflow", "run_id": "run-001",
        "scan_mode": "incremental", "scope_key": "default",
        "edges": [{
            "source_ref": "ref-1", "job_key": "dag_a.t1",
            "source_model_key": "ods_a", "target_model_key": "dwd_b",
            "lineage_type": "etl_data_flow", "confidence": 90,
        }],
    }
    r = api("POST", f"/api/projects/{pid}/lineage/import", json=payload)
    j = r.json()
    check("导入 201 inserted=1", r.status_code == 201 and j["inserted"] == 1, j)

    # 重放
    r = api("POST", f"/api/projects/{pid}/lineage/import", json=payload)
    j = r.json()
    check("重放 200 replayed=true", r.status_code == 200 and j["replayed"] is True, j)

    # 同 run_id 不同 payload → 409
    payload_diff = json.loads(json.dumps(payload))
    payload_diff["edges"][0]["confidence"] = 50
    r = api("POST", f"/api/projects/{pid}/lineage/import", json=payload_diff)
    check("run_id 冲突 → 409 LINEAGE_RUN_ID_REUSED",
          r.status_code == 409 and r.json()["error"]["code"] == "LINEAGE_RUN_ID_REUSED", r.text)

    # 校验失败 → 422 且不写边
    payload_bad = json.loads(json.dumps(payload))
    payload_bad["run_id"] = "run-002"
    payload_bad["edges"][0]["source_model_key"] = "no_such_model"
    r = api("POST", f"/api/projects/{pid}/lineage/import", json=payload_bad)
    check("模型不存在 → 422 LINEAGE_IMPORT_VALIDATION_FAILED",
          r.status_code == 422 and r.json()["error"]["code"] == "LINEAGE_IMPORT_VALIDATION_FAILED")
    edges = api("GET", f"/api/projects/{pid}/lineage/edges").json()
    check("失败批次未写边（仍 1 条）", len(edges) == 1, edges)

    # 失败批次可查
    runs = api("GET", f"/api/projects/{pid}/lineage/import-runs").json()
    check("import-runs 含 failed 记录",
          any(r["status"] == "failed" and r["failure_count"] == 1 for r in runs), runs)

    # --- full_snapshot 停用（必须非 default scope）---
    fs = {
        "source_system": "airflow", "run_id": "run-003",
        "scan_mode": "full_snapshot", "scope_key": "dag_a",
        "complete_scan": True, "edges": [],
    }
    r = api("POST", f"/api/projects/{pid}/lineage/import", json=fs)
    j = r.json()
    check("full_snapshot 空批次 201 deactivated=0（本 scope 无边）",
          r.status_code == 201 and j["deactivated"] == 0, j)
    # full_snapshot 拒绝 default scope（避免误停用默认范围）
    fs_bad = json.loads(json.dumps(fs))
    fs_bad["run_id"] = "run-003b"
    fs_bad["scope_key"] = "default"
    r = api("POST", f"/api/projects/{pid}/lineage/import", json=fs_bad)
    check("full_snapshot default scope → 422 FULL_SNAPSHOT_SCOPE_REQUIRED",
          r.status_code == 422 and any(
              e["code"] == "FULL_SNAPSHOT_SCOPE_REQUIRED"
              for e in r.json()["error"]["details"]["errors"]), r.text)
    # dag_a scope 先导入一条边，再 full_snapshot 空批次 → 停用
    fs_edge = {
        "source_system": "airflow", "run_id": "run-005",
        "scan_mode": "incremental", "scope_key": "dag_a",
        "edges": [{
            "source_ref": "ref-2", "job_key": "dag_a.t2",
            "source_model_key": "ods_a", "target_model_key": "dwd_b",
        }],
    }
    r = api("POST", f"/api/projects/{pid}/lineage/import", json=fs_edge)
    check("dag_a scope 增量导入 inserted=1", r.status_code == 201
          and r.json()["inserted"] == 1, r.text)
    fs2 = {
        "source_system": "airflow", "run_id": "run-004",
        "scan_mode": "full_snapshot", "scope_key": "dag_a",
        "complete_scan": True, "edges": [],
    }
    # V1.0 M5：dag_a 现有 1 条 active 边，空扫描计划停用比例 1/1=1.0
    # 超过阈值（默认 0.5），必须先 preview 读 confirmation_required，
    # 再带 confirm_deactivation=true 提交（升级指南 8.3 两步式）。
    pv = api("POST", f"/api/projects/{pid}/lineage/preview", json=fs2)
    check("preview 预告 confirmation_required=true",
          pv.status_code == 200 and pv.json()["confirmation_required"] is True
          and pv.json()["planned_deactivate_ratio"] == 1.0, pv.text)
    r = api("POST", f"/api/projects/{pid}/lineage/import", json=fs2)
    check("full_snapshot 空批次无确认 → 422 LINEAGE_DEACTIVATION_CONFIRM_REQUIRED",
          r.status_code == 422
          and r.json()["error"]["code"] == "LINEAGE_DEACTIVATION_CONFIRM_REQUIRED", r.text)
    fs_confirmed = dict(fs2, confirm_deactivation=True)
    r = api("POST", f"/api/projects/{pid}/lineage/import", json=fs_confirmed)
    j = r.json()
    check("full_snapshot 空批次带确认停用 dag_a 旧边 deactivated=1",
          r.status_code == 201 and j["deactivated"] == 1, j)
    runs = api("GET", f"/api/projects/{pid}/lineage/import-runs").json()
    confirmed_run = next((x for x in runs if x["run_id"] == "run-004"), None)
    check("run-004 deactivation_confirmed=true",
          confirmed_run is not None
          and confirmed_run.get("deactivation_confirmed") is True, confirmed_run)
    edges_active = api("GET", f"/api/projects/{pid}/lineage/edges?active_only=true").json()
    edges_all = api("GET", f"/api/projects/{pid}/lineage/edges?active_only=false").json()
    check("active_only 过滤生效（default 仍 active，dag_a 已停用）",
          len(edges_active) == 1 and edges_active[0]["scope_key"] == "default"
          and len(edges_all) == 2,
          (edges_active, edges_all))
    check("edges 响应头 X-Total-Count",
          api("GET", f"/api/projects/{pid}/lineage/edges").headers.get("X-Total-Count") == "1")

    # --- lineage 视图 ---
    g3 = api("GET", f"/api/projects/{pid}/model-graph?view=lineage").json()
    check("lineage 视图含设计边 1", len(g3["edges"]) == 1, g3)
    check("lineage 视图 external_edges=1（仅 active）",
          len(g3.get("external_edges", [])) == 1
          and g3["external_edges"][0]["scope_key"] == "default", g3)

    # --- preview / health / stale-candidates ---
    r = api("POST", f"/api/projects/{pid}/lineage/preview", json=payload)
    j = r.json()
    check("preview writes_performed=false + updated=1",
          j["writes_performed"] is False and j["updated"] == 1, j)
    hh = api("GET", f"/api/projects/{pid}/lineage/health").json()
    check("health groups 非空", len(hh["groups"]) >= 1, hh)
    sc = api("GET", f"/api/projects/{pid}/lineage/stale-candidates?stale_after_minutes=1").json()
    check("stale-candidates 只读列表", isinstance(sc, list))

    # --- deactivate ---
    r = api("POST", f"/api/projects/{pid}/lineage/edges/{edges_all[0]['id']}/deactivate")
    check("deactivate 幂等返回 active=false", r.status_code == 200 and r.json()["active"] is False)
    r = api("POST", f"/api/projects/{pid}/lineage/edges/99999/deactivate")
    check("deactivate 不存在 → 404 EXTERNAL_LINEAGE_EDGE_NOT_FOUND",
          r.status_code == 404 and r.json()["error"]["code"] == "EXTERNAL_LINEAGE_EDGE_NOT_FOUND")

print()
if failures:
    print("FAILED:", failures)
    sys.exit(1)
print("ALL V0.9 SMOKE CHECKS PASSED")
