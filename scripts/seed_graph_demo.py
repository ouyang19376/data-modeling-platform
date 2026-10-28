"""图谱引擎验证前的演示数据扩充（幂等）：模型 → 字段 → 关系 → 外部血缘。"""
import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
HEADERS = {"Content-Type": "application/json", "X-Actor": "demo.user"}


def call(method, path, body=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers=HEADERS,
        method=method,
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        raise RuntimeError(f"{method} {path} -> {e.code}: {detail[:200]}") from e


def f(code, name, ltype="VARCHAR", pk=False, **kw):
    d = {"field_code": code, "field_name": name, "logical_type": ltype,
         "nullable": not pk, "is_primary_key": pk, "is_unique": pk}
    d.update(kw)
    return d


MODELS = [
    ("ods_order", "订单原始表", "ODS", "TABLE", "postgresql"),
    ("ods_customer", "客户原始表", "ODS", "TABLE", "postgresql"),
    ("dim_region", "地区维表", "DIM", "DIMENSION", "postgresql"),
    ("dim_product", "商品维表", "DIM", "DIMENSION", "postgresql"),
    ("dws_order_summary", "订单周期汇总", "DWS", "TABLE", "hive"),
    ("ads_retail_report", "零售经营报表", "ADS", "VIEW", "postgresql"),
]

FIELDS = {
    "ods_order": [f("order_id", "订单ID", "BIGINT", pk=True), f("customer_id", "客户ID", "BIGINT"),
                  f("contract_id", "合同ID", "BIGINT"), f("order_amt", "订单金额", "DECIMAL"),
                  f("order_dt", "订单日期", "DATE")],
    "ods_customer": [f("customer_id", "客户ID", "BIGINT", pk=True), f("customer_name", "客户名称"),
                     f("region_id", "地区ID", "BIGINT")],
    "dim_region": [f("region_id", "地区ID", "BIGINT", pk=True), f("region_name", "地区名称"),
                   f("parent_region_id", "上级地区", "BIGINT")],
    "dim_product": [f("product_id", "商品ID", "BIGINT", pk=True), f("product_name", "商品名称"),
                    f("category", "商品类目")],
    "dim_customer": [f("customer_id", "客户ID", "BIGINT", pk=True), f("customer_name", "客户名称"),
                     f("customer_level", "客户等级"), f("region_id", "地区ID", "BIGINT")],
    "dws_order_summary": [f("order_id", "订单ID", "BIGINT"), f("contract_id", "合同ID", "BIGINT"),
                          f("customer_id", "客户ID", "BIGINT"), f("product_id", "商品ID", "BIGINT"),
                          f("summary_amt", "汇总金额", "DECIMAL")],
    "ads_retail_report": [f("report_id", "报表ID", "BIGINT", pk=True), f("order_id", "订单ID", "BIGINT"),
                          f("report_amt", "报表金额", "DECIMAL")],
}

RELS = [
    ("ods_order", "dwd_contract", "depends_on", ["contract_id"], ["contract_id"], "订单流水归集合同口径"),
    ("ods_customer", "dim_customer", "depends_on", ["customer_id"], ["customer_id"], "客户档案清洗"),
    ("ods_order", "ods_customer", "references", ["customer_id"], ["customer_id"], "同层引用：订单关联客户"),
    ("dim_region", "dim_customer", "joins_to", ["region_id"], ["region_id"], "同层上行：地区核对客户归属"),
    ("dim_product", "dws_order_summary", "joins_to", ["product_id"], ["product_id"], "汇总关联商品维度"),
    ("dwd_contract", "dws_order_summary", "derived_from", ["contract_id"], ["contract_id"], "合同明细派生汇总"),
    ("dim_customer", "dws_order_summary", "joins_to", ["customer_id"], ["customer_id"], "汇总关联客户维度"),
    ("dws_order_summary", "ads_retail_report", "references", ["order_id"], ["order_id"], "报表引用汇总结果"),
    ("dws_order_summary", "dwd_contract", "joins_to", ["contract_id"], ["contract_id"], "跨层回环：汇总反核对合同"),
]

print("== 创建模型 ==")
for m in MODELS:
    try:
        r = call("POST", "/api/projects/1/models", {
            "model_key": m[0], "name": m[1], "description": f"{m[1]}（图谱引擎演示数据）",
            "layer": m[2], "model_type": m[3], "target_engine": m[4],
            "subject_area_id": None, "fields": [],
        })
        print(f"  新建 #{r['id']} {r['model_key']}")
    except RuntimeError as e:
        if "409" in str(e):
            print(f"  已存在: {m[0]}")
        else:
            raise

all_models = {m["model_key"]: m for m in call("GET", "/api/projects/1/models")}

print("== 保存字段 ==")
for key, fields in FIELDS.items():
    m = all_models[key]
    if m["status"] == "published":
        print(f"  跳过（已发布）: {key}")
        continue
    body = {
        "model_key": m["model_key"], "name": m["name"], "description": m["description"],
        "layer": m["layer"], "model_type": m["model_type"], "target_engine": m["target_engine"],
        "subject_area_id": m.get("subject_area_id"), "requirement_ids": [],
        "fields": fields, "expected_revision": m["edit_revision"],
    }
    try:
        call("PUT", f"/api/models/{m['id']}", body)
        print(f"  {key}: {len(fields)} 字段")
    except RuntimeError as e:
        print(f"  {key} 失败: {str(e)[:160]}")

# 字段保存后 edit_revision 变化，重新拉取
all_models = {m["model_key"]: m for m in call("GET", "/api/projects/1/models")}

print("== 创建关系 ==")
for src, tgt, rtype, sf, tf, desc in RELS:
    try:
        call("POST", f"/api/models/{all_models[src]['id']}/relationships", {
            "target_model_id": all_models[tgt]["id"], "relationship_type": rtype,
            "cardinality": "unspecified", "source_fields": sf, "target_fields": tf,
            "description": desc,
        })
        print(f"  {src} -> {tgt} ({rtype})")
    except RuntimeError as e:
        if "409" in str(e):
            print(f"  已存在: {src} -> {tgt}")
        else:
            print(f"  失败 {src} -> {tgt}: {str(e)[:160]}")

print("== 导入外部血缘 ==")
try:
    r = call("POST", "/api/projects/1/lineage/import", {
        "source_system": "airflow", "run_id": "graph-demo-001", "scan_mode": "incremental",
        "scope_key": "etl-daily", "complete_scan": False,
        "edges": [
            {"source_ref": "airflow://ods_order_load", "job_key": "dag_daily.ods_order_load",
             "source_model_key": "ods_order", "target_model_key": "dwd_contract",
             "lineage_type": "etl_data_flow", "transformation_type": "sql", "confidence": 95,
             "metadata": {"dag_id": "dag_daily", "task_id": "ods_order_load"}},
            {"source_ref": "airflow://dim_customer_sync", "job_key": "dag_daily.dim_customer_sync",
             "source_model_key": "ods_customer", "target_model_key": "dim_customer",
             "lineage_type": "etl_data_flow", "transformation_type": "sql", "confidence": 88,
             "metadata": {"dag_id": "dag_daily", "task_id": "dim_customer_sync"}},
            {"source_ref": "airflow://ads_report_build", "job_key": "dag_daily.ads_report_build",
             "source_model_key": "dws_order_summary", "target_model_key": "ads_retail_report",
             "lineage_type": "scheduler_dependency", "transformation_type": "sql", "confidence": 70,
             "metadata": {"dag_id": "dag_daily", "task_id": "ads_report_build"}},
        ],
    })
    print("  导入结果:", {k: r[k] for k in ("received", "inserted", "updated", "deactivated", "replayed")})
except RuntimeError as e:
    print("  导入跳过:", str(e)[:160])

g = call("GET", "/api/projects/1/model-graph?view=current")
print(f"== current 视图：节点 {len(g['nodes'])} / 设计边 {len(g['edges'])} ==")
gl = call("GET", "/api/projects/1/model-graph?view=lineage")
print(f"== lineage 视图：外部边 {len(gl.get('external_edges', []))} ==")
