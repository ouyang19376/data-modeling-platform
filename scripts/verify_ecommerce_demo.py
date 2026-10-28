#!/usr/bin/env python3
"""电商演示数据 API 层验收脚本（任务 #52）。

对 PRJ_ECOM 种子数据逐项验证平台可演示功能：
1. 项目/主题域/需求/模型计数与状态分布
2. 图谱四视图（current/published_latest/snapshot/lineage）
3. 最短路径（下游/上游）
4. 图谱快照 diff
5. 模型版本 diff（V2 再发布模型）
6. 血缘 health / import-runs（含 failed 批次）/ edges 停用列表
7. 影响分析
8. 规范校验（问题草稿应有阻断错误、健康模型应通过）
9. DDL 预览（三引擎，含表级约束与索引）
"""
from __future__ import annotations

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8000"
HEADERS = {"Content-Type": "application/json", "X-Actor": "ecom.verifier"}

PASS: list[str] = []
FAIL: list[str] = []


def call(method: str, path: str, body=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]
    except Exception as e:  # noqa: BLE001
        return 0, str(e)[:200]


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    mark = "✓" if cond else "✗"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    # ---------- 0. 定位项目 ----------
    st, projects = call("GET", "/api/projects")
    proj = next((p for p in projects if p["project_key"] == "PRJ_ECOM"), None)
    if not proj:
        print("未找到 PRJ_ECOM 项目")
        return 1
    pid = proj["id"]
    print(f"== 项目 #{pid} {proj['name']} ==")

    # ---------- 1. 基础计数 ----------
    st, models = call("GET", f"/api/projects/{pid}/models")
    check("模型列表 114", st == 200 and len(models) == 114, f"实际 {len(models)}")
    dist: dict[str, int] = {}
    for m in models:
        dist[m["status"]] = dist.get(m["status"], 0) + 1
    check("状态分布 75/17/13/9",
          dist == {"published": 75, "approved": 17, "in_review": 13, "draft": 9},
          str(dist))
    layer_dist: dict[str, int] = {}
    for m in models:
        layer_dist[m["layer"]] = layer_dist.get(m["layer"], 0) + 1
    check("分层分布 28/32/14/22/18",
          layer_dist == {"ODS": 28, "DWD": 32, "DIM": 14, "DWS": 22, "ADS": 18},
          str(layer_dist))
    mids = {m["model_key"]: m["id"] for m in models}

    st, reqs = call("GET", f"/api/projects/{pid}/requirements")
    check("需求 26", st == 200 and len(reqs) == 26, f"实际 {len(reqs)}")
    st, areas = call("GET", f"/api/projects/{pid}/subject-areas")
    check("主题域 8", st == 200 and len(areas) == 8, f"实际 {len(areas)}")

    # ---------- 2. 图谱四视图 ----------
    st, g_cur = call("GET", f"/api/projects/{pid}/model-graph?view=current")
    check("current 视图 114 节点/159 边",
          st == 200 and len(g_cur["nodes"]) == 114 and len(g_cur["edges"]) == 159,
          f"节点 {len(g_cur.get('nodes', []))} 边 {len(g_cur.get('edges', []))}")
    st, g_pub = call("GET", f"/api/projects/{pid}/model-graph?view=published_latest")
    check("published_latest 视图可读",
          st == 200 and len(g_pub.get("nodes", [])) > 0,
          f"节点 {len(g_pub.get('nodes', []))}")
    st, g_lin = call("GET", f"/api/projects/{pid}/model-graph?view=lineage")
    ext = g_lin.get("external_edges", [])
    check("lineage 视图含 79 条外部边",
          st == 200 and len(ext) == 79, f"外部边 {len(ext)}")
    st, snaps = call("GET", f"/api/projects/{pid}/model-graph/snapshots")
    items = snaps if isinstance(snaps, list) else snaps.get("items", [])
    check("图谱快照 82 个", st == 200 and len(items) == 82, f"实际 {len(items)}")
    snap_no = items[-1]["snapshot_no"] if items else 1
    st, g_snap = call("GET", f"/api/projects/{pid}/model-graph?view=snapshot&snapshot_no={snap_no}")
    check(f"snapshot 视图 #{snap_no} 可读",
          st == 200 and len(g_snap.get("nodes", [])) > 0,
          f"节点 {len(g_snap.get('nodes', []))}")

    # ---------- 3. 最短路径 ----------
    # 边的方向语义：ADS→DWS→DWD→ODS（报表指回其数据来源），
    # 下游遍历沿该方向；lineage 视图额外打通外部血缘边。
    st, path = call("GET", f"/api/projects/{pid}/model-graph/path"
                          f"?source_model_id={mids['ads_gmv_daily']}"
                          f"&target_model_id={mids['dim_category']}&direction=downstream")
    nodes = path.get("nodes", []) if isinstance(path, dict) else []
    check("最短路径 ads_gmv_daily→dim_category（下游 6 跳）",
          st == 200 and path.get("found") and len(nodes) >= 5,
          f"{path.get('distance')} 跳: " + " → ".join(
              n.get("model_key", "?") for n in nodes[:8]))
    st, path_up = call("GET", f"/api/projects/{pid}/model-graph/path"
                              f"?source_model_id={mids['dim_category']}"
                              f"&target_model_id={mids['ads_gmv_daily']}&direction=upstream")
    nodes_up = path_up.get("nodes", []) if isinstance(path_up, dict) else []
    check("最短路径 dim_category→ads_gmv_daily（上游反向）",
          st == 200 and path_up.get("found") and len(nodes_up) >= 5,
          f"{path_up.get('distance')} 跳")
    st, path_lin = call("GET", f"/api/projects/{pid}/model-graph/path"
                               f"?source_model_id={mids['ods_order_raw']}"
                               f"&target_model_id={mids['ads_gmv_daily']}"
                               f"&direction=downstream&view=lineage")
    nodes_lin = path_lin.get("nodes", []) if isinstance(path_lin, dict) else []
    edges_lin = path_lin.get("edges", []) if isinstance(path_lin, dict) else []
    has_external = any(str(e.get("edge_key", "")).startswith("external:")
                       for e in edges_lin)
    check("lineage 视图路径 ods_order_raw→ads_gmv_daily 跨外部血缘边",
          st == 200 and path_lin.get("found") and len(nodes_lin) >= 3 and has_external,
          f"{path_lin.get('distance')} 跳（含外部边）")

    # ---------- 4. 快照 diff（列表倒序，最新在前） ----------
    new_no = items[0]["snapshot_no"]
    old_no = items[1]["snapshot_no"]
    st, diff = call("GET", f"/api/projects/{pid}/model-graph/diff"
                           f"?from_snapshot_no={old_no}&to_snapshot_no={new_no}")
    check(f"图谱快照 diff #{old_no}→#{new_no} 可读",
          st == 200 and "nodes" in diff and "edges" in diff,
          json.dumps(diff.get("nodes", {}), ensure_ascii=False)[:100])

    # ---------- 5. 版本 diff（V2 模型） ----------
    st, versions = call("GET", f"/api/models/{mids['dwd_order_detail']}/versions")
    vlist = versions if isinstance(versions, list) else versions.get("items", [])
    check("dwd_order_detail 有 2 个版本", len(vlist) == 2, f"版本 {[v.get('version_no') for v in vlist]}")
    if len(vlist) == 2:
        st, vdiff = call("GET", f"/api/models/{mids['dwd_order_detail']}/versions/diff"
                                f"?from_version=1&to_version=2")
        added = (vdiff.get("fields") or {}).get("added", []) if isinstance(vdiff, dict) else []
        modified = (vdiff.get("fields") or {}).get("modified", []) if isinstance(vdiff, dict) else []
        check("版本 diff 检出新增字段 buyer_remark",
              st == 200 and any(f.get("field_code") == "buyer_remark" for f in added),
              f"新增 {[f.get('field_code') for f in added]}")
        check("版本 diff 检出修改字段 order_status",
              any(f.get("field_code") == "order_status" for f in modified),
              f"修改 {[f.get('field_code') for f in modified]}")

    # ---------- 6. 血缘 ----------
    st, health = call("GET", f"/api/projects/{pid}/lineage/health")
    groups = health.get("groups", []) if isinstance(health, dict) else []
    check("血缘 health 含分组统计", st == 200 and len(groups) >= 4,
          "; ".join(f"{g.get('source_system')}/{g.get('scope_key')}"
                    f" 活跃{g.get('active_edges')}" for g in groups[:6]))
    st, runs = call("GET", f"/api/projects/{pid}/lineage/import-runs")
    ritems = runs if isinstance(runs, list) else runs.get("items", [])
    statuses = [r.get("status") for r in ritems]
    check("导入批次 6 个且含 1 个 failed",
          len(ritems) == 6 and "failed" in statuses, str(statuses))
    failed = next((r for r in ritems if r.get("status") == "failed"), None)
    if failed:
        check("failed 批次引用 ods_missing_table",
              "ods_missing_table" in json.dumps(failed, ensure_ascii=False)[:2000])
    st, edges = call("GET", f"/api/projects/{pid}/lineage/edges?limit=200")
    eitems = edges if isinstance(edges, list) else edges.get("items", [])
    ltypes = {e.get("lineage_type") for e in eitems}
    check("血缘边覆盖 5 种类型",
          {"etl_data_flow", "scheduler_dependency", "bi_consumption",
           "dbt_dependency", "other"} <= ltypes, str(sorted(ltypes)))

    # ---------- 7. 影响分析 ----------
    # 已发布模型无待决变更 → 影响为空（正确语义）；变更中草稿有模型级变更可传播。
    st, impact_pub = call("GET", f"/api/models/{mids['dwd_order_detail']}/impact")
    check("已发布模型无待决变更时影响为空",
          st == 200 and impact_pub.get("analysis_mode") == "draft_vs_latest_published"
          and impact_pub.get("impacted_models") == [],
          f"mode={impact_pub.get('analysis_mode')}")
    st, impact = call("GET", f"/api/models/{mids['dwd_ticket_detail']}/impact")
    down = impact.get("impacted_models", []) if isinstance(impact, dict) else []
    check("变更中草稿 dwd_ticket_detail 影响分析下游 ≥10",
          st == 200 and len(down) >= 10,
          f"下游 {len(down)} 个: " + ", ".join(
              m.get("model_key", "?") for m in down[:8]))

    # ---------- 8. 规范校验 ----------
    st, bad = call("POST", f"/api/models/{mids['dwd_cart_detail']}/validate")
    issues = bad.get("issues", []) if isinstance(bad, dict) else []
    blockers = [i for i in issues if i.get("severity") == "error"]
    check("问题草稿 dwd_cart_detail 报阻断错误", len(blockers) >= 1,
          f"{len(blockers)} 个阻断: " + "; ".join(
              i.get("rule_id", "?") for i in blockers[:4]))
    st, good = call("POST", f"/api/models/{mids['dwd_order_detail']}/validate")
    gissues = good.get("issues", []) if isinstance(good, dict) else []
    gblock = [i for i in gissues if i.get("severity") == "error"]
    check("健康模型 dwd_order_detail 无阻断错误", len(gblock) == 0,
          f"issues {len(gissues)}")

    # ---------- 9. DDL 预览（含引擎矩阵拒绝语义） ----------
    for engine in ("postgresql", "hive"):
        st, ddl = call("POST", f"/api/models/{mids['dwd_order_detail']}/ddl",
                       {"target_engine": engine})
        sql = ddl.get("ddl", "") if isinstance(ddl, dict) else ""
        check(f"DDL 预览 {engine} 含约束与索引",
              st == 200 and "uq_order_no" in sql and "idx_order_dt" in sql,
              f"{len(sql)} 字符")
    st, ddl_my = call("POST", f"/api/models/{mids['dwd_inventory_detail']}/ddl",
                      {"target_engine": "mysql"})
    sql_my = ddl_my.get("ddl", "") if isinstance(ddl_my, dict) else ""
    check("DDL 预览 mysql（mysql 原生模型）含索引",
          st == 200 and "idx_inv_sku" in sql_my, f"{len(sql_my)} 字符")
    st, ddl_rej = call("POST", f"/api/models/{mids['dwd_order_detail']}/ddl",
                       {"target_engine": "mysql"})
    check("引擎矩阵：PG 模型的 hash 索引生成 MySQL DDL 被拒",
          st == 422 and "hash" in str(ddl_rej), str(ddl_rej)[:100])

    # ---------- 汇总 ----------
    print(f"\n===== 验收：通过 {len(PASS)} / 失败 {len(FAIL)} =====")
    if FAIL:
        for f in FAIL:
            print("  - " + f)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
