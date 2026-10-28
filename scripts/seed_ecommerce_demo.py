#!/usr/bin/env python3
"""电商行业数仓演示数据生成器（PRJ_ECOM 电商数据中台）。

在干净库上通过 REST API 生成一整套电商数仓演示数据，覆盖平台当前版本
可演示的全部功能范围：

- 项目 / 8 个主题域 / 26 个业务需求（draft/in_analysis/modeled/accepted/rejected）
- 114 个模型，按数仓分层设计：ODS(28) / DWD(32) / DIM(14) / DWS(22) / ADS(18)，
  状态分布 published(75) / approved(17) / in_review(13) / draft(9，其中 6 个
  "问题草稿"含阻断级校验错误用于演示规范校验器，1 个"变更中"草稿用于演示
  影响分析)
- 字段 / 表级约束（主键/唯一/CHECK/外键）/ 索引（btree/hash/gin，遵守引擎矩阵）
- 159 条模型关系：4 种关系类型 × 5 种基数全覆盖，跨层 ODS→DWD→DWS→ADS
  + 同层 + 跨层回环 + 桥接多对多
- 完整生命周期：校验 → 提交评审 → 评审通过 → 发布（版本 + 图谱快照同事务），
  6 个模型走二次变更（start-change → 改结构 → 再发布）形成多版本 diff
- 外部血缘：airflow（etl_data_flow/scheduler_dependency）、superset（bi_consumption）、
  manual-registry（other）、dbt manifest（dbt_dependency）+ 一个故意失败的批次
  （演示 import-runs 失败态与 attempt/failure 计数）

用法：
  1. 重置库（或对空库）并以 SEED_DEMO_DATA=false 启动服务
  2. python scripts/seed_ecommerce_demo.py
  3. 浏览器打开 http://127.0.0.1:8000/ 选择项目「电商数据中台」

设计要点：全部走 API（而非直接写库），保证状态机、版本快照、图谱快照、
事件与审计的一致；约束/索引只能在草稿态创建，故生命周期在结构对象之后执行。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
HEADERS = {"Content-Type": "application/json", "X-Actor": "ecom.architect"}

STATS = {"api": 0, "skip": 0, "fail": 0}
FAILURES: list[str] = []


def call(method: str, path: str, body=None):
    """发起 API 调用，返回 (ok, data)。"""
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
        headers=HEADERS,
        method=method,
    )
    STATS["api"] += 1
    try:
        with urllib.request.urlopen(req) as resp:
            return True, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        try:
            detail = json.loads(detail).get("error", {}).get("code", detail[:120])
        except Exception:
            detail = detail[:120]
        return False, f"{e.code} {detail}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:120]


def must(method: str, path: str, body=None, what: str = ""):
    ok, data = call(method, path, body)
    if not ok:
        STATS["fail"] += 1
        FAILURES.append(f"{what or path}: {data}")
        print(f"  [失败] {what or path} -> {data}")
    return data if ok else None


def soft(method: str, path: str, body=None, what: str = ""):
    """容忍失败（幂等重跑/预期失败场景），返回数据或 None。"""
    ok, data = call(method, path, body)
    if not ok:
        STATS["skip"] += 1
        print(f"  [跳过] {what or path} -> {data}")
        return None
    return data


# ---------------------------------------------------------------- 字段构造助手

def F(code, name, t="VARCHAR", length=None, precision=None, scale=None,
      nullable=True, pk=False, unique=False, default=None, desc="",
      sens="internal"):
    d = {"field_code": code, "field_name": name, "logical_type": t,
         "nullable": nullable, "is_primary_key": pk, "is_unique": unique,
         "description": desc or name, "sensitivity": sens}
    if length is not None:
        d["length"] = length
    if precision is not None:
        d["precision"] = precision
    if scale is not None:
        d["scale"] = scale
    if default is not None:
        d["default_value"] = default
    return d


def PKC(code, name, desc=""):
    """BIGINT 字段级主键。"""
    return F(code, name, "BIGINT", nullable=False, pk=True, desc=desc or f"{name}，业务主键")


def BKC(code, name, **kw):
    return F(code, name, "BIGINT", **kw)


def VC(code, name, n=64, **kw):
    return F(code, name, "VARCHAR", length=n, **kw)


def AMT(code, name, p=18, s=2, **kw):
    return F(code, name, "DECIMAL", precision=p, scale=s, **kw)


def QTY(code, name, **kw):
    return F(code, name, "INTEGER", **kw)


def DT(code, name, **kw):
    return F(code, name, "DATE", **kw)


def TS(code, name, **kw):
    return F(code, name, "TIMESTAMP", **kw)


def FLAG(code, name, **kw):
    return F(code, name, "BOOLEAN", **kw)


def ETLCOLS():
    """DWD/DWS/ADS 标准加工审计列。"""
    return [TS("etl_time", "ETL 加工时间"), TS("update_time", "记录更新时间")]


# ---------------------------------------------------------------- 主题域

AREAS = {
    "TRADE": ("交易域", "订单、支付、退款、购物车等交易主链路。"),
    "USER": ("用户域", "用户注册、会员、地址、设备与登录行为。"),
    "PRODUCT": ("商品域", "商品、SKU、类目、品牌、价格与库存。"),
    "MARKETING": ("营销域", "优惠券、活动、促销等营销工具与效果。"),
    "TRAFFIC": ("流量域", "页面浏览、点击、搜索等前端埋点流量。"),
    "LOGISTICS": ("物流履约域", "发货、仓库、物流轨迹与履约时效。"),
    "SERVICE": ("服务域", "客服工单、售后、评价与服务质量。"),
    "FINANCE": ("财务域", "结算、发票与资金对账。"),
}

# ---------------------------------------------------------------- 业务需求

REQS = [
    # (key, title, domain, priority, status, desc, acceptance, [model_keys])
    ("REQ_ECOM_001", "电商GMV日报", "销售经营", "P1", "modeled",
     "按日/店铺/渠道统计GMV、支付金额与退款金额，支撑经营日报。",
     "T+1 更新；GMV 口径含未支付订单；支持按店铺下钻。",
     ["ads_gmv_daily", "dws_shop_gmv_1d", "dwd_order_detail"]),
    ("REQ_ECOM_002", "用户下单转化漏斗", "流量运营", "P0", "modeled",
     "从访问到详情页到加购到下单到支付的漏斗转化分析。",
     "漏斗各步可下钻到渠道；转化率保留 6 位小数。",
     ["ads_order_conv_funnel", "dws_traffic_conv_1d"]),
    ("REQ_ECOM_003", "优惠券核销ROI分析", "营销", "P1", "in_analysis",
     "统计券发放-核销-带动GMV，计算投入产出比。",
     "核销金额与订单优惠金额双口径；ROI 保留 4 位小数。",
     ["ads_coupon_roi", "dws_coupon_use_1d"]),
    ("REQ_ECOM_004", "商品畅销排行", "商品运营", "P2", "modeled",
     "按 SKU/商品/类目维度输出销售件数与金额排行。",
     "支持日/周粒度；剔除退款件数。",
     ["ads_product_rank", "dws_product_sales_1d", "dws_sku_sales_1d"]),
    ("REQ_ECOM_005", "物流时效监控", "供应链", "P1", "modeled",
     "监控发货及时率、平均履约时长，按仓维度排名。",
     "及时率 = 按时长达标发货单 / 总发货单；T+1。",
     ["ads_logistics_ontime", "dws_logistics_perf_1d"]),
    ("REQ_ECOM_006", "客服满意度分析", "客户服务", "P2", "in_analysis",
     "统计工单量、解决率与满意度评分。",
     "评分口径 1-5 分；支持按工单类型下钻。",
     ["ads_service_satisfaction", "dws_service_quality_1d"]),
    ("REQ_ECOM_007", "会员留存分析", "用户运营", "P1", "modeled",
     "统计新增/活跃/流失会员与 1/7/30 日留存。",
     "留存按注册日期 Cohorts 计算。",
     ["ads_member_retention", "dws_member_growth_1d"]),
    ("REQ_ECOM_008", "店铺结算对账", "财务", "P0", "accepted",
     "按日汇总店铺结算金额、佣金与结算单量。",
     "结算金额与佣金分列；支持按店铺对账。",
     ["ads_finance_settlement", "dws_settlement_1d"]),
    ("REQ_ECOM_009", "退款原因分析", "售后服务", "P2", "draft",
     "统计退款原因分布、退款金额与退款率。",
     "退款率 = 退款金额 / 支付金额；待与业务确认口径。",
     ["ads_refund_reason", "dws_refund_1d"]),
    ("REQ_ECOM_010", "库存预警", "供应链", "P1", "in_analysis",
     "按 SKU 监控可用库存与安全库存，输出预警分级。",
     "预警分级：严重/警告/正常；需与商品运营确认安全库存算法。",
     ["ads_inventory_alert", "dws_warehouse_stock_1d", "dwd_inventory_detail"]),
    ("REQ_ECOM_011", "搜索热词分析", "流量运营", "P3", "draft",
     "统计搜索词热度、点击率与无结果率。",
     "热度分算法待定；先落明细与日汇总。",
     ["ads_search_hot_word", "dws_search_word_1d"]),
    ("REQ_ECOM_012", "用户增长看板", "用户运营", "P1", "modeled",
     "注册、活跃、新增付费用户与增长率。",
     "活跃 = 当日有登录；T+1。",
     ["ads_user_growth", "dws_user_active_1d"]),
    ("REQ_ECOM_013", "高价值用户画像", "用户运营", "P2", "in_analysis",
     "基于 RFM 与 LTV 输出高价值用户分层与偏好类目。",
     "RFM 分箱规则待与运营确认。",
     ["ads_user_rfm", "ads_vip_profile"]),
    ("REQ_ECOM_014", "类目GMV结构", "商品运营", "P2", "modeled",
     "按类目统计 GMV 与销售件数，输出结构占比。",
     "类目取一级类目；支持日粒度。",
     ["ads_category_gmv", "dws_category_sales_1d"]),
    ("REQ_ECOM_015", "活动效果评估", "营销", "P1", "in_analysis",
     "活动曝光-点击-下单-GMV 全链路效果与 ROI。",
     "活动口径需与营销中台对齐。",
     ["ads_campaign_roi", "dws_campaign_effect_1d"]),
    ("REQ_ECOM_016", "流量来源日报", "流量运营", "P2", "modeled",
     "按渠道统计 UV、订单与 GMV、转化率。",
     "渠道取渠道字典一级渠道。",
     ["ads_traffic_source_daily", "dws_traffic_source_1d"]),
    ("REQ_ECOM_017", "订单主数据治理", "交易", "P0", "accepted",
     "订单与订单商品全生命周期状态治理。",
     "订单号全局唯一；状态机与交易系统一致。",
     ["dwd_order_detail", "dwd_order_item", "dwd_order_pay_bridge"]),
    ("REQ_ECOM_018", "用户主数据治理", "用户", "P0", "accepted",
     "用户档案、会员与设备主数据。",
     "用户名唯一；设备按首次可见归档。",
     ["dim_user", "dwd_user_detail", "dwd_user_device"]),
    ("REQ_ECOM_019", "商品主数据治理", "商品", "P0", "accepted",
     "商品、SKU、类目、品牌主数据与桥接。",
     "类目支持多级；商品-类目多对多桥接。",
     ["dim_product", "dim_sku", "dim_category", "dim_brand", "dwd_product_category_bridge"]),
    ("REQ_ECOM_020", "支付对账", "交易", "P0", "in_analysis",
     "支付流水与支付渠道核对。",
     "支付单号唯一；渠道口径待与支付中台确认。",
     ["dwd_pay_detail", "dim_payment_method"]),
    ("REQ_ECOM_021", "购物车转化分析", "交易", "P3", "draft",
     "加购到下单的转化率与购物车金额。",
     "转化率口径待定；先落汇总。",
     ["dws_cart_conv_1d", "dwd_cart_detail"]),
    ("REQ_ECOM_022", "促销规则管理", "营销", "P2", "rejected",
     "统一管理促销类型与规则配置。",
     "已评估：规则复杂度高，V1.2 暂缓，转入配置中心项目。",
     ["dwd_promo_detail"]),
    ("REQ_ECOM_023", "评价数据沉淀", "客户服务", "P3", "draft",
     "商品评分与评价内容沉淀，支撑商品质量分析。",
     "评分 1-5 分；内容脱敏后入库。",
     ["dwd_review_detail"]),
    ("REQ_ECOM_024", "发票开具跟踪", "财务", "P2", "draft",
     "发票申请-开具-红冲全流程跟踪。",
     "按订单维度；金额与订单支付金额核对。",
     ["dwd_invoice_detail", "dws_invoice_1d"]),
    ("REQ_ECOM_025", "售后流程数字化", "客户服务", "P1", "in_analysis",
     "售后单与退款单联动，闭环售后流程。",
     "售后单状态机待与客服系统对齐。",
     ["dwd_after_sale_detail"]),
    ("REQ_ECOM_026", "渠道字典统一", "流量运营", "P3", "accepted",
     "统一渠道层级字典，供各流量报表复用。",
     "渠道编码唯一；支持多级。",
     ["dim_channel"]),
]

# ---------------------------------------------------------------- 模型规格
# 字段：key / 名称 / 分层 / 类型 / 引擎 / 主题域 / 描述 / 状态 / 关联需求 /
#       字段 / 表级约束 / 索引 / 关系（内联）/ 二次变更（v2）

MODELS: list[dict] = [

    # ==================== ODS 贴源层（28） ====================
    dict(key="ods_order_raw", name="订单原始流水", layer="ODS", mtype="TABLE", engine="postgresql",
         area="TRADE", status="published", reqs=["REQ_ECOM_017"],
         desc="交易系统订单表 T+1 全量快照，未做清洗。",
         fields=[PKC("order_id", "订单ID"), VC("order_no", "订单业务号", 64, unique=True),
                 BKC("user_id", "用户ID"), BKC("shop_id", "店铺ID"),
                 VC("order_status", "订单状态", 20), AMT("order_amt", "订单金额"),
                 AMT("pay_amt", "支付金额"), AMT("discount_amt", "优惠金额"),
                 AMT("freight_amt", "运费金额"), DT("order_dt", "下单日期"),
                 TS("created_time", "下单时间")],
         links=[("dwd_order_detail", "depends_on", ["order_id"], ["order_id"], "one_to_many",
                 "订单流水清洗进入订单明细")]),
    dict(key="ods_order_item_raw", name="订单商品原始流水", layer="ODS", mtype="TABLE", engine="postgresql",
         area="TRADE", status="published", reqs=["REQ_ECOM_017"],
         desc="交易系统订单商品表快照。",
         fields=[PKC("item_id", "订单商品行ID"), BKC("order_id", "订单ID"),
                 BKC("sku_id", "SKU ID"), VC("item_name", "商品名称", 200),
                 AMT("sale_price", "成交单价"), QTY("quantity", "购买数量"),
                 AMT("item_amt", "行金额"), TS("created_time", "创建时间")],
         links=[("dwd_order_item", "depends_on", ["order_id"], ["order_id"], "one_to_many",
                 "订单商品流水清洗进入订单商品明细")]),
    dict(key="ods_pay_raw", name="支付流水原始表", layer="ODS", mtype="TABLE", engine="postgresql",
         area="TRADE", status="published", reqs=["REQ_ECOM_020"],
         desc="支付渠道回调流水快照。",
         fields=[PKC("pay_id", "支付ID"), BKC("order_id", "订单ID"),
                 VC("pay_no", "支付单号", 64, unique=True), VC("pay_channel", "支付渠道", 20),
                 VC("pay_status", "支付状态", 20), AMT("pay_amt", "支付金额"),
                 TS("pay_time", "支付时间")],
         links=[("dwd_pay_detail", "depends_on", ["pay_id"], ["pay_id"], "one_to_one",
                 "支付流水清洗进入支付明细"),
                ("dwd_order_detail", "references", ["order_id"], ["order_id"], "many_to_one",
                 "支付流水回指订单明细")]),
    dict(key="ods_refund_raw", name="退款流水原始表", layer="ODS", mtype="TABLE", engine="postgresql",
         area="TRADE", status="published", reqs=["REQ_ECOM_009"],
         desc="退款系统流水快照。",
         fields=[PKC("refund_id", "退款ID"), BKC("order_id", "订单ID"), BKC("pay_id", "支付ID"),
                 VC("refund_no", "退款单号", 64, unique=True), AMT("refund_amt", "退款金额"),
                 VC("refund_reason", "退款原因", 200), VC("refund_status", "退款状态", 20),
                 TS("apply_time", "申请时间")],
         links=[("dwd_refund_detail", "depends_on", ["refund_id"], ["refund_id"], "one_to_one",
                 "退款流水清洗进入退款明细")]),
    dict(key="ods_cart_raw", name="购物车原始表", layer="ODS", mtype="TABLE", engine="postgresql",
         area="TRADE", status="approved", reqs=["REQ_ECOM_021"],
         desc="购物车加购行为快照。",
         fields=[PKC("cart_id", "购物车记录ID"), BKC("user_id", "用户ID"),
                 BKC("sku_id", "SKU ID"), QTY("quantity", "加购数量"),
                 TS("added_time", "加购时间")],
         links=[("dwd_cart_detail", "depends_on", ["cart_id"], ["cart_id"], "one_to_one",
                 "购物车流水清洗进入购物车明细")]),
    dict(key="ods_user_raw", name="用户注册原始表", layer="ODS", mtype="TABLE", engine="postgresql",
         area="USER", status="published", reqs=["REQ_ECOM_018"],
         desc="用户中心注册信息快照。",
         fields=[PKC("user_id", "用户ID"), VC("user_name", "用户名", 64, unique=True),
                 TS("register_time", "注册时间"), VC("register_channel", "注册渠道", 20),
                 VC("user_status", "用户状态", 20)],
         links=[("dwd_user_detail", "depends_on", ["user_id"], ["user_id"], "one_to_one",
                 "用户注册信息清洗进入用户明细"),
                ("dim_user", "derived_from", ["user_id"], ["user_id"], "one_to_one",
                 "用户注册信息入用户维表")]),
    dict(key="ods_member_raw", name="会员原始表", layer="ODS", mtype="TABLE", engine="postgresql",
         area="USER", status="published", reqs=["REQ_ECOM_007"],
         desc="会员体系会员信息快照。",
         fields=[PKC("member_id", "会员ID"), BKC("user_id", "用户ID"),
                 VC("member_level", "会员等级", 20), QTY("member_points", "会员积分"),
                 TS("join_time", "入会时间")],
         links=[("dwd_member_detail", "depends_on", ["member_id"], ["member_id"], "one_to_one",
                 "会员信息清洗进入会员明细"),
                ("dim_member", "derived_from", ["member_id"], ["member_id"], "one_to_one",
                 "会员信息入会员维表")]),
    dict(key="ods_address_raw", name="地址原始表", layer="ODS", mtype="TABLE", engine="postgresql",
         area="USER", status="published", reqs=["REQ_ECOM_018"],
         desc="用户收货地址快照。",
         fields=[PKC("address_id", "地址ID"), BKC("user_id", "用户ID"),
                 VC("province", "省份", 64), VC("city", "城市", 64),
                 VC("district", "区县", 64), VC("detail_addr", "详细地址", 200),
                 FLAG("is_default", "是否默认地址")],
         links=[("dwd_address_detail", "depends_on", ["address_id"], ["address_id"], "one_to_one",
                 "地址清洗进入地址明细")]),
    dict(key="ods_user_login_raw", name="登录日志原始表", layer="ODS", mtype="TABLE", engine="hive",
         area="USER", status="published", reqs=["REQ_ECOM_012"],
         desc="登录埋点日志（Hive 分区表）。",
         fields=[PKC("login_id", "登录记录ID"), BKC("user_id", "用户ID"),
                 TS("login_time", "登录时间"), VC("login_channel", "登录渠道", 20),
                 VC("device_id", "设备ID", 64), VC("login_result", "登录结果", 20)],
         links=[("dwd_login_log", "depends_on", ["login_id"], ["login_id"], "one_to_one",
                 "登录日志清洗进入登录日志明细")]),
    dict(key="ods_product_raw", name="商品原始表", layer="ODS", mtype="TABLE", engine="postgresql",
         area="PRODUCT", status="published", reqs=["REQ_ECOM_019"],
         desc="商品中心商品信息快照。",
         fields=[PKC("product_id", "商品ID"), VC("product_name", "商品名称", 200),
                 BKC("category_id", "类目ID"), BKC("brand_id", "品牌ID"),
                 BKC("shop_id", "店铺ID"), VC("product_status", "商品状态", 20)],
         links=[("dwd_product_detail", "depends_on", ["product_id"], ["product_id"], "one_to_one",
                 "商品信息清洗进入商品明细"),
                ("dim_product", "derived_from", ["product_id"], ["product_id"], "one_to_one",
                 "商品信息入商品维表")]),
    dict(key="ods_sku_raw", name="SKU原始表", layer="ODS", mtype="TABLE", engine="postgresql",
         area="PRODUCT", status="published", reqs=["REQ_ECOM_019"],
         desc="商品中心 SKU 信息快照。",
         fields=[PKC("sku_id", "SKU ID"), BKC("product_id", "商品ID"),
                 VC("sku_name", "SKU 名称", 200), VC("sku_props", "销售属性", 500),
                 VC("sku_status", "SKU 状态", 20)],
         links=[("dwd_sku_detail", "depends_on", ["sku_id"], ["sku_id"], "one_to_one",
                 "SKU 信息清洗进入 SKU 明细"),
                ("dim_sku", "derived_from", ["sku_id"], ["sku_id"], "one_to_one",
                 "SKU 信息入 SKU 维表")]),
    dict(key="ods_category_raw", name="类目原始表", layer="ODS", mtype="TABLE", engine="postgresql",
         area="PRODUCT", status="published", reqs=["REQ_ECOM_019"],
         desc="商品类目快照（含层级）。",
         fields=[PKC("category_id", "类目ID"), VC("category_name", "类目名称", 64),
                 BKC("parent_id", "父类目ID"), QTY("category_level", "类目层级")],
         links=[("dim_category", "derived_from", ["category_id"], ["category_id"], "one_to_one",
                 "类目入类目维表"),
                ("dwd_product_category_bridge", "depends_on", ["category_id"], ["category_id"],
                 "one_to_many", "类目参与商品-类目桥接")]),
    dict(key="ods_brand_raw", name="品牌原始表", layer="ODS", mtype="TABLE", engine="postgresql",
         area="PRODUCT", status="published", reqs=["REQ_ECOM_019"],
         desc="品牌信息快照。",
         fields=[PKC("brand_id", "品牌ID"), VC("brand_name", "品牌名称", 64),
                 VC("brand_country", "品牌国家", 64)],
         links=[("dim_brand", "derived_from", ["brand_id"], ["brand_id"], "one_to_one",
                 "品牌入品牌维表")]),
    dict(key="ods_inventory_raw", name="库存原始表", layer="ODS", mtype="TABLE", engine="mysql",
         area="PRODUCT", status="published", reqs=["REQ_ECOM_010"],
         desc="库存中心库存快照（MySQL）。",
         fields=[PKC("inv_id", "库存记录ID"), BKC("sku_id", "SKU ID"),
                 BKC("warehouse_id", "仓库ID"), QTY("stock_qty", "可用库存"),
                 QTY("locked_qty", "锁定库存"), DT("stat_date", "统计日期")],
         links=[("dwd_inventory_detail", "depends_on", ["inv_id"], ["inv_id"], "one_to_one",
                 "库存清洗进入库存明细")]),
    dict(key="ods_price_raw", name="价格原始表", layer="ODS", mtype="TABLE", engine="mysql",
         area="PRODUCT", status="in_review", reqs=["REQ_ECOM_010"],
         desc="价格体系售价快照（MySQL）。",
         fields=[PKC("price_id", "价格记录ID"), BKC("sku_id", "SKU ID"),
                 AMT("sale_price", "售价"), AMT("market_price", "市场价"),
                 DT("effective_dt", "生效日期"), DT("expire_dt", "失效日期")],
         links=[("dwd_price_detail", "depends_on", ["price_id"], ["price_id"], "one_to_one",
                 "价格清洗进入价格明细")]),
    dict(key="ods_coupon_raw", name="优惠券原始表", layer="ODS", mtype="TABLE", engine="postgresql",
         area="MARKETING", status="published", reqs=["REQ_ECOM_003"],
         desc="营销平台券模板快照。",
         fields=[PKC("coupon_id", "优惠券ID"), VC("coupon_name", "券名称", 128),
                 VC("coupon_type", "券类型", 20), VC("discount_type", "优惠方式", 20),
                 AMT("discount_value", "优惠面额"), AMT("min_amount", "使用门槛"),
                 QTY("valid_days", "有效天数")],
         links=[("dwd_coupon_detail", "depends_on", ["coupon_id"], ["coupon_id"], "one_to_one",
                 "券模板清洗进入优惠券明细"),
                ("dim_coupon", "derived_from", ["coupon_id"], ["coupon_id"], "one_to_one",
                 "券模板入优惠券维表")]),
    dict(key="ods_coupon_grant_raw", name="优惠券发放原始表", layer="ODS", mtype="TABLE", engine="postgresql",
         area="MARKETING", status="published", reqs=["REQ_ECOM_003"],
         desc="券发放记录快照。",
         fields=[PKC("grant_id", "发放记录ID"), BKC("coupon_id", "优惠券ID"),
                 BKC("user_id", "用户ID"), TS("grant_time", "发放时间"),
                 VC("grant_channel", "发放渠道", 20)],
         links=[("dwd_coupon_grant_detail", "depends_on", ["grant_id"], ["grant_id"], "one_to_one",
                 "发放记录清洗进入发放明细")]),
    dict(key="ods_campaign_raw", name="活动原始表", layer="ODS", mtype="TABLE", engine="postgresql",
         area="MARKETING", status="published", reqs=["REQ_ECOM_015"],
         desc="营销活动快照。",
         fields=[PKC("campaign_id", "活动ID"), VC("campaign_name", "活动名称", 128),
                 VC("campaign_type", "活动类型", 20), TS("start_time", "开始时间"),
                 TS("end_time", "结束时间"), AMT("budget_amt", "活动预算")],
         links=[("dwd_campaign_detail", "depends_on", ["campaign_id"], ["campaign_id"], "one_to_one",
                 "活动清洗进入活动明细"),
                ("dim_campaign", "derived_from", ["campaign_id"], ["campaign_id"], "one_to_one",
                 "活动入活动维表")]),
    dict(key="ods_promo_raw", name="促销原始表", layer="ODS", mtype="TABLE", engine="hive",
         area="MARKETING", status="in_review", reqs=["REQ_ECOM_022"],
         desc="促销配置快照（Hive）。",
         fields=[PKC("promo_id", "促销ID"), VC("promo_name", "促销名称", 128),
                 VC("promo_type", "促销类型", 20), VC("promo_rule", "促销规则", 2000),
                 DT("start_dt", "开始日期"), DT("end_dt", "结束日期")],
         links=[("dwd_promo_detail", "depends_on", ["promo_id"], ["promo_id"], "one_to_one",
                 "促销清洗进入促销明细")]),
    dict(key="ods_page_view_raw", name="页面浏览原始表", layer="ODS", mtype="TABLE", engine="hive",
         area="TRAFFIC", status="published", reqs=["REQ_ECOM_002"],
         desc="前端页面浏览埋点（Hive）。",
         fields=[PKC("pv_id", "浏览记录ID"), VC("session_id", "会话ID", 64),
                 BKC("user_id", "用户ID"), VC("page_url", "页面URL", 500),
                 VC("refer_url", "来源URL", 500), TS("view_time", "浏览时间")],
         links=[("dwd_page_view_detail", "depends_on", ["pv_id"], ["pv_id"], "one_to_one",
                 "浏览日志清洗进入浏览明细")]),
    dict(key="ods_click_raw", name="点击流原始表", layer="ODS", mtype="TABLE", engine="hive",
         area="TRAFFIC", status="published", reqs=["REQ_ECOM_002"],
         desc="前端点击埋点（Hive）。",
         fields=[PKC("click_id", "点击记录ID"), VC("session_id", "会话ID", 64),
                 BKC("user_id", "用户ID"), VC("element_id", "元素ID", 128),
                 VC("element_type", "元素类型", 32), TS("click_time", "点击时间")],
         links=[("dwd_click_detail", "depends_on", ["click_id"], ["click_id"], "one_to_one",
                 "点击日志清洗进入点击明细")]),
    dict(key="ods_search_raw", name="搜索日志原始表", layer="ODS", mtype="TABLE", engine="hive",
         area="TRAFFIC", status="published", reqs=["REQ_ECOM_011"],
         desc="搜索埋点日志（Hive）。",
         fields=[PKC("search_id", "搜索记录ID"), BKC("user_id", "用户ID"),
                 VC("keyword", "搜索词", 200), QTY("result_count", "结果数"),
                 TS("search_time", "搜索时间")],
         links=[("dwd_search_detail", "depends_on", ["search_id"], ["search_id"], "one_to_one",
                 "搜索日志清洗进入搜索明细")]),
    dict(key="ods_shipment_raw", name="发货单原始表", layer="ODS", mtype="TABLE", engine="postgresql",
         area="LOGISTICS", status="published", reqs=["REQ_ECOM_005"],
         desc="履约系统发货单快照。",
         fields=[PKC("shipment_id", "发货单ID"), BKC("order_id", "订单ID"),
                 BKC("warehouse_id", "仓库ID"), VC("logistics_no", "物流单号", 64, unique=True),
                 TS("ship_time", "发货时间"), VC("ship_status", "发货状态", 20)],
         links=[("dwd_shipment_detail", "depends_on", ["shipment_id"], ["shipment_id"], "one_to_one",
                 "发货单清洗进入发货明细")]),
    dict(key="ods_warehouse_raw", name="仓库原始表", layer="ODS", mtype="TABLE", engine="postgresql",
         area="LOGISTICS", status="published", reqs=["REQ_ECOM_010"],
         desc="仓库主数据快照。",
         fields=[PKC("warehouse_id", "仓库ID"), VC("warehouse_name", "仓库名称", 128),
                 VC("province", "省份", 64), VC("city", "城市", 64),
                 VC("warehouse_type", "仓库类型", 20)],
         links=[("dim_warehouse", "derived_from", ["warehouse_id"], ["warehouse_id"], "one_to_one",
                 "仓库入仓库维表"),
                ("dwd_warehouse_detail", "depends_on", ["warehouse_id"], ["warehouse_id"], "one_to_one",
                 "仓库清洗进入仓库明细")]),
    dict(key="ods_ticket_raw", name="客服工单原始表", layer="ODS", mtype="TABLE", engine="postgresql",
         area="SERVICE", status="published", reqs=["REQ_ECOM_006"],
         desc="客服系统工单快照。",
         fields=[PKC("ticket_id", "工单ID"), BKC("order_id", "订单ID"),
                 BKC("user_id", "用户ID"), VC("ticket_type", "工单类型", 20),
                 VC("ticket_status", "工单状态", 20), TS("create_time", "创建时间")],
         links=[("dwd_ticket_detail", "depends_on", ["ticket_id"], ["ticket_id"], "one_to_one",
                 "工单清洗进入工单明细")]),
    dict(key="ods_review_raw", name="评价原始表", layer="ODS", mtype="TABLE", engine="hive",
         area="SERVICE", status="in_review", reqs=["REQ_ECOM_023"],
         desc="商品评价埋点（Hive）。",
         fields=[PKC("review_id", "评价ID"), BKC("order_id", "订单ID"),
                 BKC("sku_id", "SKU ID"), BKC("user_id", "用户ID"),
                 QTY("score", "评分"), VC("content", "评价内容", 2000),
                 TS("review_time", "评价时间")],
         links=[("dwd_review_detail", "depends_on", ["review_id"], ["review_id"], "one_to_one",
                 "评价清洗进入评价明细")]),
    dict(key="ods_settlement_raw", name="结算原始表", layer="ODS", mtype="TABLE", engine="postgresql",
         area="FINANCE", status="published", reqs=["REQ_ECOM_008"],
         desc="财务结算单快照。",
         fields=[PKC("settle_id", "结算单ID"), BKC("shop_id", "店铺ID"),
                 AMT("settle_amt", "结算金额"), DT("settle_dt", "结算日期"),
                 VC("settle_status", "结算状态", 20)],
         links=[("dwd_settlement_detail", "depends_on", ["settle_id"], ["settle_id"], "one_to_one",
                 "结算清洗进入结算明细")]),
    dict(key="ods_invoice_raw", name="发票原始表", layer="ODS", mtype="TABLE", engine="postgresql",
         area="FINANCE", status="approved", reqs=["REQ_ECOM_024"],
         desc="发票系统快照。",
         fields=[PKC("invoice_id", "发票ID"), BKC("order_id", "订单ID"),
                 AMT("invoice_amt", "发票金额"), VC("invoice_type", "发票类型", 20),
                 VC("invoice_status", "发票状态", 20), TS("issue_time", "开具时间")],
         links=[("dwd_invoice_detail", "depends_on", ["invoice_id"], ["invoice_id"], "one_to_one",
                 "发票清洗进入发票明细")]),

    # ==================== DWD 明细层（32） ====================
    dict(key="dwd_order_detail", name="订单明细", layer="DWD", mtype="FACT", engine="postgresql",
         area="TRADE", status="published", reqs=["REQ_ECOM_001", "REQ_ECOM_017"],
         desc="每行代表一个订单的当前明细（清洗后）。",
         fields=[PKC("order_id", "订单ID"), VC("order_no", "订单业务号", 64, unique=True),
                 BKC("user_id", "用户ID"), BKC("shop_id", "店铺ID"),
                 VC("order_status", "订单状态", 20), AMT("order_amt", "订单金额"),
                 AMT("pay_amt", "支付金额"), AMT("discount_amt", "优惠金额"),
                 AMT("freight_amt", "运费金额"), DT("order_dt", "下单日期"),
                 TS("created_time", "下单时间")] + ETLCOLS(),
         constraints=[
             {"name": "uq_order_no", "type": "unique", "fields": ["order_no"]},
             {"name": "ck_order_amt", "type": "check", "expression": "order_amt >= 0"},
             {"name": "fk_order_user", "type": "foreign_key", "fields": ["user_id"],
              "ref_model": "dim_user", "ref_fields": ["user_id"], "on_delete": "CASCADE"},
         ],
         indexes=[
             {"name": "idx_order_dt", "type": "btree", "fields": ["order_dt"]},
             {"name": "idx_order_user", "type": "btree", "fields": ["user_id"]},
             {"name": "idx_order_status", "type": "hash", "fields": ["order_status"]},
         ],
         links=[("dwd_order_item", "references", ["order_id"], ["order_id"], "one_to_many",
                 "订单明细关联订单商品明细"),
                ("dim_user", "joins_to", ["user_id"], ["user_id"], "many_to_one",
                 "订单关联用户维表"),
                ("dim_shop", "joins_to", ["shop_id"], ["shop_id"], "many_to_one",
                 "订单关联店铺维表"),
                ("dwd_pay_detail", "references", ["order_id"], ["order_id"], "one_to_many",
                 "订单明细关联支付明细"),
                ("dws_user_order_1d", "derived_from", ["user_id"], ["user_id"], "many_to_one",
                 "订单明细派生用户订单日汇总"),
                ("dws_shop_gmv_1d", "derived_from", ["shop_id"], ["shop_id"], "many_to_one",
                 "订单明细派生店铺GMV日汇总"),
                ("dwd_order_coupon_bridge", "references", ["order_id"], ["order_id"], "one_to_many",
                 "订单明细关联订单优惠券桥接"),
                ("dwd_pay_detail", "joins_to", ["order_id"], ["order_id"], "many_to_many",
                 "订单与支付经桥接表多对多（合并支付/多次支付）")],
         v2={"add": [VC("buyer_remark", "买家备注", 500)],
             "note": "V2：新增买家备注字段，订单状态长度 20→32",
             "modify": {"order_status": {"length": 32}}}),
    dict(key="dwd_order_item", name="订单商品明细", layer="DWD", mtype="FACT", engine="postgresql",
         area="TRADE", status="published", reqs=["REQ_ECOM_017"],
         desc="每行代表订单中的一个 SKU 行。",
         fields=[BKC("order_id", "订单ID"), BKC("sku_id", "SKU ID"),
                 VC("item_name", "商品名称", 200), AMT("sale_price", "成交单价"),
                 QTY("quantity", "购买数量"), AMT("item_amt", "行金额")] + ETLCOLS(),
         constraints=[
             {"name": "pk_order_item", "type": "primary_key", "fields": ["order_id", "sku_id"]},
             {"name": "fk_item_order", "type": "foreign_key", "fields": ["order_id"],
              "ref_model": "dwd_order_detail", "ref_fields": ["order_id"], "on_delete": "RESTRICT"},
             {"name": "fk_item_sku", "type": "foreign_key", "fields": ["sku_id"],
              "ref_model": "dim_sku", "ref_fields": ["sku_id"], "on_delete": "RESTRICT"},
         ],
         indexes=[{"name": "idx_item_sku", "type": "btree", "fields": ["sku_id"]}],
         links=[("dim_sku", "joins_to", ["sku_id"], ["sku_id"], "many_to_one",
                 "订单商品关联 SKU 维表"),
                ("dws_sku_sales_1d", "derived_from", ["sku_id"], ["sku_id"], "one_to_many",
                 "订单商品派生 SKU 销售日汇总")]),
    dict(key="dwd_pay_detail", name="支付明细", layer="DWD", mtype="TABLE", engine="postgresql",
         area="TRADE", status="published", reqs=["REQ_ECOM_020"],
         desc="每行代表一笔支付（清洗后）。",
         fields=[PKC("pay_id", "支付ID"), BKC("order_id", "订单ID"),
                 BKC("user_id", "用户ID"),
                 VC("pay_no", "支付单号", 64, unique=True), VC("pay_channel", "支付渠道", 20),
                 VC("pay_status", "支付状态", 20), AMT("pay_amt", "支付金额"),
                 TS("pay_time", "支付时间")] + ETLCOLS(),
         constraints=[
             {"name": "uq_pay_no", "type": "unique", "fields": ["pay_no"]},
             {"name": "ck_pay_amt", "type": "check", "expression": "pay_amt >= 0"},
             {"name": "fk_pay_order", "type": "foreign_key", "fields": ["order_id"],
              "ref_model": "dwd_order_detail", "ref_fields": ["order_id"], "on_delete": "CASCADE"},
         ],
         indexes=[
             {"name": "idx_pay_order", "type": "btree", "fields": ["order_id"]},
             {"name": "idx_pay_channel", "type": "hash", "fields": ["pay_channel"]},
         ],
         links=[("dim_payment_method", "joins_to", ["pay_channel"], ["pay_method_code"], "many_to_one",
                 "支付明细关联支付方式维表"),
                ("dws_user_pay_1d", "derived_from", ["user_id"], ["user_id"], "many_to_one",
                 "支付明细派生用户支付日汇总")]),
    dict(key="dwd_refund_detail", name="退款明细", layer="DWD", mtype="TABLE", engine="postgresql",
         area="TRADE", status="approved", reqs=["REQ_ECOM_009"],
         desc="每行代表一笔退款申请（清洗后）。",
         fields=[PKC("refund_id", "退款ID"), BKC("order_id", "订单ID"), BKC("pay_id", "支付ID"),
                 VC("refund_no", "退款单号", 64, unique=True), AMT("refund_amt", "退款金额"),
                 VC("refund_reason", "退款原因", 200), VC("refund_status", "退款状态", 20),
                 TS("apply_time", "申请时间")] + ETLCOLS(),
         constraints=[
             {"name": "uq_refund_no", "type": "unique", "fields": ["refund_no"]},
             {"name": "ck_refund_amt", "type": "check", "expression": "refund_amt > 0"},
             {"name": "fk_refund_order", "type": "foreign_key", "fields": ["order_id"],
              "ref_model": "dwd_order_detail", "ref_fields": ["order_id"], "on_delete": "RESTRICT"},
         ],
         indexes=[{"name": "idx_refund_order", "type": "btree", "fields": ["order_id"]}],
         links=[("dwd_order_detail", "joins_to", ["order_id"], ["order_id"], "many_to_one",
                 "退款明细关联订单明细"),
                ("dws_refund_1d", "derived_from", ["refund_reason"], ["refund_reason"], "many_to_one",
                 "退款明细派生退款日汇总")]),
    dict(key="dwd_cart_detail", name="购物车明细", layer="DWD", mtype="TABLE", engine="postgresql",
         area="TRADE", status="draft", reqs=["REQ_ECOM_021"], problem=True,
         desc="购物车加购明细（问题草稿：cart_id 缺 VARCHAR 长度、qty 主键可空）。",
         fields=[F("cart_id", "购物车记录ID", "VARCHAR"), BKC("user_id", "用户ID"),
                 BKC("sku_id", "SKU ID"),
                 F("qty", "加购数量", "INTEGER", pk=True, nullable=True),
                 TS("added_time", "加购时间")] + ETLCOLS(),
         links=[("dim_user", "joins_to", ["user_id"], ["user_id"], "many_to_one",
                 "购物车关联用户维表"),
                ("dim_sku", "joins_to", ["sku_id"], ["sku_id"], "many_to_one",
                 "购物车关联 SKU 维表")]),
    dict(key="dwd_user_detail", name="用户明细", layer="DWD", mtype="TABLE", engine="postgresql",
         area="USER", status="published", reqs=["REQ_ECOM_018"],
         desc="每行代表一个用户的当前档案（清洗后）。",
         fields=[PKC("user_id", "用户ID"), VC("user_name", "用户名", 64, unique=True),
                 TS("register_time", "注册时间"), VC("register_channel", "注册渠道", 20),
                 VC("user_status", "用户状态", 20)] + ETLCOLS(),
         constraints=[{"name": "uq_user_name", "type": "unique", "fields": ["user_name"]}],
         indexes=[{"name": "idx_user_status", "type": "btree", "fields": ["user_status"]}],
         links=[("dim_user", "joins_to", ["user_id"], ["user_id"], "many_to_one",
                 "用户明细与用户维表同源核对")]),
    dict(key="dwd_member_detail", name="会员明细", layer="DWD", mtype="TABLE", engine="postgresql",
         area="USER", status="published", reqs=["REQ_ECOM_007"],
         desc="每行代表一个会员的当前状态。",
         fields=[PKC("member_id", "会员ID"), BKC("user_id", "用户ID"),
                 VC("member_level", "会员等级", 20), QTY("member_points", "会员积分"),
                 TS("join_time", "入会时间")] + ETLCOLS(),
         links=[("dim_member", "joins_to", ["member_id"], ["member_id"], "many_to_one",
                 "会员明细关联会员维表"),
                ("dim_user", "joins_to", ["user_id"], ["user_id"], "many_to_one",
                 "会员明细关联用户维表")]),
    dict(key="dwd_address_detail", name="地址明细", layer="DWD", mtype="TABLE", engine="postgresql",
         area="USER", status="approved", reqs=["REQ_ECOM_018"],
         desc="收货地址明细（清洗后）。",
         fields=[PKC("address_id", "地址ID"), BKC("user_id", "用户ID"),
                 VC("province", "省份", 64), VC("city", "城市", 64),
                 VC("district", "区县", 64), VC("detail_addr", "详细地址", 200),
                 FLAG("is_default", "是否默认地址")] + ETLCOLS(),
         links=[("dim_region", "joins_to", ["province"], ["region_name"], "many_to_one",
                 "地址省份核对地区维表"),
                ("dim_user", "joins_to", ["user_id"], ["user_id"], "many_to_one",
                 "地址关联用户维表")]),
    dict(key="dwd_login_log", name="登录日志", layer="DWD", mtype="TABLE", engine="hive",
         area="USER", status="published", reqs=["REQ_ECOM_012"],
         desc="登录日志明细（Hive）。",
         fields=[PKC("login_id", "登录记录ID"), BKC("user_id", "用户ID"),
                 TS("login_time", "登录时间"), VC("login_channel", "登录渠道", 20),
                 VC("device_id", "设备ID", 64), VC("login_result", "登录结果", 20)] + ETLCOLS(),
         indexes=[{"name": "idx_login_user", "type": "btree", "fields": ["user_id"]}],
         links=[("dim_user", "joins_to", ["user_id"], ["user_id"], "many_to_one",
                 "登录日志关联用户维表"),
                ("dim_channel", "joins_to", ["login_channel"], ["channel_code"], "unspecified",
                 "登录渠道与流量渠道口径待统一，基数未声明"),
                ("dws_user_active_1d", "derived_from", ["user_id"], ["user_id"], "many_to_one",
                 "登录日志派生用户活跃日汇总")]),
    dict(key="dwd_product_detail", name="商品明细", layer="DWD", mtype="TABLE", engine="postgresql",
         area="PRODUCT", status="published", reqs=["REQ_ECOM_019"],
         desc="商品明细（清洗后）。",
         fields=[PKC("product_id", "商品ID"), VC("product_name", "商品名称", 200),
                 BKC("category_id", "类目ID"), BKC("brand_id", "品牌ID"),
                 BKC("shop_id", "店铺ID"), VC("product_status", "商品状态", 20)] + ETLCOLS(),
         links=[("dim_product", "joins_to", ["product_id"], ["product_id"], "many_to_one",
                 "商品明细关联商品维表"),
                ("dim_category", "joins_to", ["category_id"], ["category_id"], "many_to_one",
                 "商品明细关联类目维表"),
                ("dim_brand", "joins_to", ["brand_id"], ["brand_id"], "many_to_one",
                 "商品明细关联品牌维表"),
                ("dim_shop", "joins_to", ["shop_id"], ["shop_id"], "many_to_one",
                 "商品明细关联店铺维表")],
         v2={"add": [VC("shelf_status", "上下架状态", 20)],
             "note": "V2：新增上下架状态字段",
             "modify": {"product_status": {"length": 32}}}),
    dict(key="dwd_sku_detail", name="SKU明细", layer="DWD", mtype="TABLE", engine="postgresql",
         area="PRODUCT", status="published", reqs=["REQ_ECOM_019"],
         desc="SKU 明细（清洗后）。",
         fields=[PKC("sku_id", "SKU ID"), BKC("product_id", "商品ID"),
                 VC("sku_name", "SKU 名称", 200), VC("sku_props", "销售属性", 500),
                 VC("sku_status", "SKU 状态", 20)] + ETLCOLS(),
         links=[("dim_sku", "joins_to", ["sku_id"], ["sku_id"], "many_to_one",
                 "SKU 明细关联 SKU 维表"),
                ("dwd_product_detail", "references", ["product_id"], ["product_id"], "many_to_one",
                 "SKU 明细回指商品明细")]),
    dict(key="dwd_inventory_detail", name="库存明细", layer="DWD", mtype="TABLE", engine="mysql",
         area="PRODUCT", status="published", reqs=["REQ_ECOM_010"],
         desc="仓维度库存明细（MySQL）。",
         fields=[PKC("inv_id", "库存记录ID"), BKC("sku_id", "SKU ID"),
                 BKC("warehouse_id", "仓库ID"), QTY("stock_qty", "可用库存"),
                 QTY("locked_qty", "锁定库存"), DT("stat_date", "统计日期")] + ETLCOLS(),
         constraints=[{"name": "ck_stock", "type": "check", "expression": "stock_qty >= 0"}],
         indexes=[{"name": "idx_inv_sku", "type": "btree", "fields": ["sku_id"]}],
         links=[("dim_sku", "joins_to", ["sku_id"], ["sku_id"], "many_to_one",
                 "库存明细关联 SKU 维表"),
                ("dim_warehouse", "joins_to", ["warehouse_id"], ["warehouse_id"], "many_to_one",
                 "库存明细关联仓库维表"),
                ("dws_warehouse_stock_1d", "derived_from", ["warehouse_id"], ["warehouse_id"], "many_to_one",
                 "库存明细派生仓库库存日汇总")]),
    dict(key="dwd_price_detail", name="价格明细", layer="DWD", mtype="TABLE", engine="mysql",
         area="PRODUCT", status="in_review", reqs=["REQ_ECOM_010"],
         desc="SKU 售价区间明细（MySQL）。",
         fields=[PKC("price_id", "价格记录ID"), BKC("sku_id", "SKU ID"),
                 AMT("sale_price", "售价"), AMT("market_price", "市场价"),
                 DT("effective_dt", "生效日期"), DT("expire_dt", "失效日期")] + ETLCOLS(),
         constraints=[{"name": "ck_price", "type": "check", "expression": "sale_price > 0"}],
         links=[("dim_sku", "joins_to", ["sku_id"], ["sku_id"], "many_to_one",
                 "价格明细关联 SKU 维表")]),
    dict(key="dwd_coupon_detail", name="优惠券明细", layer="DWD", mtype="TABLE", engine="postgresql",
         area="MARKETING", status="published", reqs=["REQ_ECOM_003"],
         desc="券模板明细（清洗后）。",
         fields=[PKC("coupon_id", "优惠券ID"), VC("coupon_name", "券名称", 128),
                 VC("coupon_type", "券类型", 20), VC("discount_type", "优惠方式", 20),
                 AMT("discount_value", "优惠面额"), AMT("min_amount", "使用门槛"),
                 QTY("valid_days", "有效天数")] + ETLCOLS(),
         constraints=[{"name": "ck_discount", "type": "check", "expression": "discount_value > 0"}],
         links=[("dim_coupon", "joins_to", ["coupon_id"], ["coupon_id"], "many_to_one",
                 "优惠券明细关联优惠券维表")]),
    dict(key="dwd_coupon_grant_detail", name="优惠券发放明细", layer="DWD", mtype="TABLE", engine="postgresql",
         area="MARKETING", status="published", reqs=["REQ_ECOM_003"],
         desc="每行代表一次券发放（清洗后）。",
         fields=[BKC("grant_id", "发放记录ID"), BKC("coupon_id", "优惠券ID"),
                 BKC("user_id", "用户ID"), TS("grant_time", "发放时间"),
                 VC("grant_channel", "发放渠道", 20), VC("use_status", "核销状态", 20)] + ETLCOLS(),
         constraints=[
             {"name": "pk_grant", "type": "primary_key", "fields": ["grant_id", "coupon_id"]},
             {"name": "fk_grant_user", "type": "foreign_key", "fields": ["user_id"],
              "ref_model": "dim_user", "ref_fields": ["user_id"], "on_delete": "CASCADE"},
             {"name": "fk_grant_coupon", "type": "foreign_key", "fields": ["coupon_id"],
              "ref_model": "dim_coupon", "ref_fields": ["coupon_id"], "on_delete": "RESTRICT"},
         ],
         indexes=[{"name": "idx_grant_user", "type": "btree", "fields": ["user_id"]}],
         links=[("dim_coupon", "joins_to", ["coupon_id"], ["coupon_id"], "many_to_one",
                 "发放明细关联优惠券维表"),
                ("dim_user", "joins_to", ["user_id"], ["user_id"], "many_to_one",
                 "发放明细关联用户维表"),
                ("dws_coupon_use_1d", "derived_from", ["coupon_id"], ["coupon_id"], "many_to_one",
                 "发放明细派生优惠券核销日汇总")]),
    dict(key="dwd_campaign_detail", name="活动明细", layer="DWD", mtype="TABLE", engine="postgresql",
         area="MARKETING", status="published", reqs=["REQ_ECOM_015"],
         desc="营销活动明细（清洗后）。",
         fields=[PKC("campaign_id", "活动ID"), VC("campaign_name", "活动名称", 128),
                 VC("campaign_type", "活动类型", 20), TS("start_time", "开始时间"),
                 TS("end_time", "结束时间"), AMT("budget_amt", "活动预算")] + ETLCOLS(),
         links=[("dim_campaign", "joins_to", ["campaign_id"], ["campaign_id"], "many_to_one",
                 "活动明细关联活动维表")]),
    dict(key="dwd_promo_detail", name="促销明细", layer="DWD", mtype="TABLE", engine="hive",
         area="MARKETING", status="draft", reqs=["REQ_ECOM_022"], problem=True,
         desc="促销明细（问题草稿：promo_rule 缺 VARCHAR 长度）。",
         fields=[PKC("promo_id", "促销ID"), VC("promo_name", "促销名称", 128),
                 VC("promo_type", "促销类型", 20),
                 F("promo_rule", "促销规则", "VARCHAR"),
                 DT("start_dt", "开始日期"), DT("end_dt", "结束日期")] + ETLCOLS(),
         links=[("dim_campaign", "joins_to", ["promo_type"], ["campaign_type"], "many_to_one",
                 "促销类型核对活动维表活动类型")]),
    dict(key="dwd_page_view_detail", name="页面浏览明细", layer="DWD", mtype="TABLE", engine="hive",
         area="TRAFFIC", status="published", reqs=["REQ_ECOM_002"],
         desc="页面浏览明细（Hive，按会话+事件时间唯一）。",
         fields=[BKC("pv_id", "浏览记录ID"), VC("session_id", "会话ID", 64),
                 TS("event_time", "事件时间"),
                 BKC("user_id", "用户ID"), BKC("channel_id", "渠道ID"),
                 VC("page_url", "页面URL", 500),
                 VC("refer_url", "来源URL", 500), VC("device_type", "设备类型", 20)] + ETLCOLS(),
         constraints=[{"name": "pk_pv", "type": "primary_key", "fields": ["session_id", "event_time"]}],
         indexes=[{"name": "idx_pv_session", "type": "btree", "fields": ["session_id"]}],
         links=[("dim_user", "joins_to", ["user_id"], ["user_id"], "many_to_one",
                 "浏览明细关联用户维表"),
                ("dim_channel", "joins_to", ["channel_id"], ["channel_id"], "many_to_one",
                 "浏览明细关联渠道维表"),
                ("dws_traffic_conv_1d", "derived_from", ["channel_id"], ["channel_id"], "many_to_one",
                 "浏览明细派生流量转化日汇总")]),
    dict(key="dwd_click_detail", name="点击明细", layer="DWD", mtype="TABLE", engine="hive",
         area="TRAFFIC", status="approved", reqs=["REQ_ECOM_002"],
         desc="前端点击明细（Hive）。",
         fields=[PKC("click_id", "点击记录ID"), VC("session_id", "会话ID", 64),
                 BKC("user_id", "用户ID"), VC("element_id", "元素ID", 128),
                 VC("element_type", "元素类型", 32), TS("click_time", "点击时间")] + ETLCOLS(),
         links=[("dwd_page_view_detail", "references", ["session_id"], ["session_id"], "many_to_one",
                 "点击归属会话")]),
    dict(key="dwd_search_detail", name="搜索明细", layer="DWD", mtype="TABLE", engine="hive",
         area="TRAFFIC", status="published", reqs=["REQ_ECOM_011"],
         desc="搜索行为明细（Hive）。",
         fields=[PKC("search_id", "搜索记录ID"), BKC("user_id", "用户ID"),
                 VC("keyword", "搜索词", 200), QTY("result_count", "结果数"),
                 TS("search_time", "搜索时间")] + ETLCOLS(),
         links=[("dim_user", "joins_to", ["user_id"], ["user_id"], "many_to_one",
                 "搜索明细关联用户维表"),
                ("dws_search_word_1d", "derived_from", ["keyword"], ["keyword"], "many_to_one",
                 "搜索明细派生搜索词日汇总")]),
    dict(key="dwd_shipment_detail", name="发货明细", layer="DWD", mtype="TABLE", engine="postgresql",
         area="LOGISTICS", status="published", reqs=["REQ_ECOM_005"],
         desc="发货单明细（清洗后）。",
         fields=[PKC("shipment_id", "发货单ID"), BKC("order_id", "订单ID"),
                 BKC("warehouse_id", "仓库ID"), VC("logistics_no", "物流单号", 64, unique=True),
                 TS("ship_time", "发货时间"), VC("ship_status", "发货状态", 20)] + ETLCOLS(),
         constraints=[
             {"name": "uq_logistics_no", "type": "unique", "fields": ["logistics_no"]},
             {"name": "fk_ship_order", "type": "foreign_key", "fields": ["order_id"],
              "ref_model": "dwd_order_detail", "ref_fields": ["order_id"], "on_delete": "RESTRICT"},
             {"name": "fk_ship_wh", "type": "foreign_key", "fields": ["warehouse_id"],
              "ref_model": "dim_warehouse", "ref_fields": ["warehouse_id"], "on_delete": "RESTRICT"},
         ],
         indexes=[{"name": "idx_ship_order", "type": "btree", "fields": ["order_id"]}],
         links=[("dwd_order_detail", "joins_to", ["order_id"], ["order_id"], "many_to_one",
                 "发货明细关联订单明细"),
                ("dim_warehouse", "joins_to", ["warehouse_id"], ["warehouse_id"], "many_to_one",
                 "发货明细关联仓库维表"),
                ("dws_logistics_perf_1d", "derived_from", ["warehouse_id"], ["warehouse_id"], "many_to_one",
                 "发货明细派生物流履约日汇总")]),
    dict(key="dwd_ticket_detail", name="工单明细", layer="DWD", mtype="TABLE", engine="postgresql",
         area="SERVICE", status="published", reqs=["REQ_ECOM_006"],
         desc="客服工单明细（清洗后）。",
         fields=[PKC("ticket_id", "工单ID"), BKC("order_id", "订单ID"),
                 BKC("user_id", "用户ID"), VC("ticket_type", "工单类型", 20),
                 VC("ticket_status", "工单状态", 20), QTY("score", "满意度评分"),
                 TS("create_time", "创建时间"), TS("close_time", "关闭时间")] + ETLCOLS(),
         constraints=[{"name": "ck_score", "type": "check",
                       "expression": "score >= 1 AND score <= 5"}],
         indexes=[{"name": "idx_ticket_user", "type": "btree", "fields": ["user_id"]}],
         links=[("dim_user", "joins_to", ["user_id"], ["user_id"], "many_to_one",
                 "工单关联用户维表"),
                ("dwd_order_detail", "joins_to", ["order_id"], ["order_id"], "many_to_one",
                 "工单关联订单明细"),
                ("dws_service_quality_1d", "derived_from", ["ticket_type"], ["ticket_type"], "many_to_one",
                 "工单派生服务质量日汇总")]),
    dict(key="dwd_review_detail", name="评价明细", layer="DWD", mtype="TABLE", engine="hive",
         area="SERVICE", status="draft", reqs=["REQ_ECOM_023"], problem=True,
         desc="商品评价明细（问题草稿：created_at 默认值 now() 不在安全白名单）。",
         fields=[PKC("review_id", "评价ID"), BKC("order_id", "订单ID"),
                 BKC("sku_id", "SKU ID"), BKC("user_id", "用户ID"),
                 QTY("score", "评分"), VC("content", "评价内容", 2000),
                 TS("review_time", "评价时间"),
                 F("created_at", "记录创建时间", "TIMESTAMP", default="now()")] + ETLCOLS(),
         indexes=[{"name": "idx_review_content", "type": "gin", "fields": ["content"]}],
         links=[("dim_user", "joins_to", ["user_id"], ["user_id"], "many_to_one",
                 "评价关联用户维表"),
                ("dim_sku", "joins_to", ["sku_id"], ["sku_id"], "many_to_one",
                 "评价关联 SKU 维表")]),
    dict(key="dwd_settlement_detail", name="结算明细", layer="DWD", mtype="TABLE", engine="postgresql",
         area="FINANCE", status="published", reqs=["REQ_ECOM_008"],
         desc="店铺结算单明细（清洗后）。",
         fields=[PKC("settle_id", "结算单ID"), BKC("shop_id", "店铺ID"),
                 AMT("settle_amt", "结算金额"), DT("settle_dt", "结算日期"),
                 VC("settle_status", "结算状态", 20)] + ETLCOLS(),
         constraints=[{"name": "ck_settle", "type": "check", "expression": "settle_amt >= 0"}],
         links=[("dim_shop", "joins_to", ["shop_id"], ["shop_id"], "many_to_one",
                 "结算明细关联店铺维表"),
                ("dws_settlement_1d", "derived_from", ["shop_id"], ["shop_id"], "many_to_one",
                 "结算明细派生结算日汇总")]),
    dict(key="dwd_invoice_detail", name="发票明细", layer="DWD", mtype="TABLE", engine="postgresql",
         area="FINANCE", status="approved", reqs=["REQ_ECOM_024"],
         desc="发票明细（清洗后）。",
         fields=[PKC("invoice_id", "发票ID"), BKC("order_id", "订单ID"),
                 AMT("invoice_amt", "发票金额"), VC("invoice_type", "发票类型", 20),
                 VC("invoice_status", "发票状态", 20), TS("issue_time", "开具时间")] + ETLCOLS(),
         links=[("dwd_order_detail", "joins_to", ["order_id"], ["order_id"], "many_to_one",
                 "发票明细关联订单明细"),
                ("dws_invoice_1d", "derived_from", ["invoice_type"], ["invoice_type"], "many_to_one",
                 "发票明细派生发票日汇总")]),
    dict(key="dwd_order_pay_bridge", name="订单支付桥接", layer="DWD", mtype="TABLE", engine="postgresql",
         area="TRADE", status="published", reqs=["REQ_ECOM_017"],
         desc="订单与支付的多对多桥接（合并支付/多次支付）。",
         fields=[BKC("order_id", "订单ID"), BKC("pay_id", "支付ID"),
                 AMT("match_amt", "匹配金额"), TS("match_time", "匹配时间")] + ETLCOLS(),
         constraints=[
             {"name": "pk_op_bridge", "type": "primary_key", "fields": ["order_id", "pay_id"]},
             {"name": "fk_bp_order", "type": "foreign_key", "fields": ["order_id"],
              "ref_model": "dwd_order_detail", "ref_fields": ["order_id"], "on_delete": "CASCADE"},
             {"name": "fk_bp_pay", "type": "foreign_key", "fields": ["pay_id"],
              "ref_model": "dwd_pay_detail", "ref_fields": ["pay_id"], "on_delete": "CASCADE"},
         ],
         links=[("dwd_order_detail", "joins_to", ["order_id"], ["order_id"], "many_to_one",
                 "桥接关联订单明细"),
                ("dwd_pay_detail", "joins_to", ["pay_id"], ["pay_id"], "many_to_one",
                 "桥接关联支付明细")]),
    dict(key="dwd_order_coupon_bridge", name="订单优惠券桥接", layer="DWD", mtype="TABLE", engine="postgresql",
         area="TRADE", status="approved", reqs=["REQ_ECOM_017"],
         desc="订单与券发放记录的多对多桥接。",
         fields=[BKC("order_id", "订单ID"), BKC("grant_id", "发放记录ID"),
                 AMT("coupon_amt", "优惠金额")] + ETLCOLS(),
         constraints=[
             {"name": "pk_oc_bridge", "type": "primary_key", "fields": ["order_id", "grant_id"]},
             {"name": "fk_oc_order", "type": "foreign_key", "fields": ["order_id"],
              "ref_model": "dwd_order_detail", "ref_fields": ["order_id"], "on_delete": "CASCADE"},
         ],
         links=[("dwd_order_detail", "joins_to", ["order_id"], ["order_id"], "many_to_one",
                 "桥接关联订单明细"),
                ("dwd_coupon_grant_detail", "joins_to", ["grant_id"], ["grant_id"], "many_to_one",
                 "桥接关联券发放明细")]),
    dict(key="dwd_user_device", name="用户设备", layer="DWD", mtype="TABLE", engine="postgresql",
         area="USER", status="in_review", reqs=["REQ_ECOM_018"],
         desc="用户设备档案（清洗后）。",
         fields=[PKC("device_id", "设备ID"), BKC("user_id", "用户ID"),
                 VC("device_type", "设备类型", 20), VC("os_type", "操作系统", 20),
                 VC("app_version", "应用版本", 20), TS("first_seen", "首次可见时间")] + ETLCOLS(),
         links=[("dim_user", "joins_to", ["user_id"], ["user_id"], "many_to_one",
                 "设备关联用户维表")]),
    dict(key="dwd_product_category_bridge", name="商品类目桥接", layer="DWD", mtype="TABLE", engine="postgresql",
         area="PRODUCT", status="published", reqs=["REQ_ECOM_019"],
         desc="商品与类目的多对多桥接（支持主辅类目）。",
         fields=[BKC("product_id", "商品ID"), BKC("category_id", "类目ID"),
                 FLAG("is_primary", "是否主类目")] + ETLCOLS(),
         constraints=[
             {"name": "pk_pc_bridge", "type": "primary_key", "fields": ["product_id", "category_id"]},
             {"name": "fk_pc_product", "type": "foreign_key", "fields": ["product_id"],
              "ref_model": "dim_product", "ref_fields": ["product_id"], "on_delete": "CASCADE"},
             {"name": "fk_pc_category", "type": "foreign_key", "fields": ["category_id"],
              "ref_model": "dim_category", "ref_fields": ["category_id"], "on_delete": "CASCADE"},
         ],
         links=[("dim_product", "joins_to", ["product_id"], ["product_id"], "many_to_one",
                 "桥接关联商品维表"),
                ("dim_category", "joins_to", ["category_id"], ["category_id"], "many_to_one",
                 "桥接关联类目维表")]),
    dict(key="dwd_warehouse_detail", name="仓库明细", layer="DWD", mtype="TABLE", engine="postgresql",
         area="LOGISTICS", status="published", reqs=["REQ_ECOM_010"],
         desc="仓库明细（清洗后）。",
         fields=[PKC("warehouse_id", "仓库ID"), VC("warehouse_name", "仓库名称", 128),
                 VC("province", "省份", 64), VC("city", "城市", 64),
                 VC("warehouse_type", "仓库类型", 20)] + ETLCOLS(),
         links=[("dim_warehouse", "joins_to", ["warehouse_id"], ["warehouse_id"], "many_to_one",
                 "仓库明细关联仓库维表")]),
    dict(key="dwd_logistics_track", name="物流轨迹", layer="DWD", mtype="TABLE", engine="hive",
         area="LOGISTICS", status="draft", reqs=["REQ_ECOM_005"], problem=True,
         desc="物流轨迹节点（问题草稿：track_id 主键声明可空）。",
         fields=[F("track_id", "轨迹节点ID", "BIGINT", pk=True, nullable=True),
                 BKC("shipment_id", "发货单ID"), VC("node_desc", "节点描述", 200),
                 TS("node_time", "节点时间")] + ETLCOLS(),
         links=[("dwd_shipment_detail", "joins_to", ["shipment_id"], ["shipment_id"], "many_to_one",
                 "轨迹关联发货明细")]),
    dict(key="dwd_after_sale_detail", name="售后单明细", layer="DWD", mtype="TABLE", engine="postgresql",
         area="SERVICE", status="in_review", reqs=["REQ_ECOM_025"],
         desc="售后单明细（清洗后）。",
         fields=[PKC("after_sale_id", "售后单ID"), BKC("order_id", "订单ID"),
                 VC("after_sale_type", "售后类型", 20), VC("after_sale_status", "售后状态", 20),
                 AMT("apply_amt", "申请金额"), TS("create_time", "创建时间")] + ETLCOLS(),
         constraints=[{"name": "ck_as_amt", "type": "check", "expression": "apply_amt > 0"}],
         links=[("dwd_order_detail", "joins_to", ["order_id"], ["order_id"], "many_to_one",
                 "售后单关联订单明细"),
                ("dwd_refund_detail", "references", ["order_id"], ["order_id"], "one_to_many",
                 "售后单关联退款明细")]),

    # ==================== DIM 维度层（14） ====================
    dict(key="dim_user", name="用户维表", layer="DIM", mtype="DIMENSION", engine="postgresql",
         area="USER", status="published", reqs=["REQ_ECOM_018"],
         desc="用户维度（拉链表，SCD2）。",
         fields=[PKC("user_id", "用户ID"), VC("user_name", "用户名", 64, unique=True),
                 VC("user_level", "用户等级", 20), DT("register_dt", "注册日期"),
                 BKC("region_id", "地区ID"), VC("user_status", "用户状态", 20),
                 DT("start_dt", "生效日期"), DT("end_dt", "失效日期"),
                 FLAG("is_current", "是否当前有效")],
         constraints=[{"name": "uq_dim_user_name", "type": "unique", "fields": ["user_name"]}],
         indexes=[{"name": "idx_dim_user_level", "type": "btree", "fields": ["user_level"]}],
         links=[("dim_region", "joins_to", ["region_id"], ["region_id"], "many_to_one",
                 "用户维表关联地区维表")]),
    dict(key="dim_member", name="会员维表", layer="DIM", mtype="DIMENSION", engine="postgresql",
         area="USER", status="published", reqs=["REQ_ECOM_007"],
         desc="会员维度（SCD2）。",
         fields=[PKC("member_id", "会员ID"), BKC("user_id", "用户ID"),
                 VC("member_level", "会员等级", 20), QTY("member_points", "会员积分"),
                 DT("join_dt", "入会日期"), DT("start_dt", "生效日期"),
                 DT("end_dt", "失效日期"), FLAG("is_current", "是否当前有效")],
         links=[("dim_user", "joins_to", ["user_id"], ["user_id"], "many_to_one",
                 "会员维表关联用户维表")]),
    dict(key="dim_product", name="商品维表", layer="DIM", mtype="DIMENSION", engine="postgresql",
         area="PRODUCT", status="published", reqs=["REQ_ECOM_019"],
         desc="商品维度（SCD2）。",
         fields=[PKC("product_id", "商品ID"), VC("product_name", "商品名称", 200),
                 BKC("category_id", "类目ID"), BKC("brand_id", "品牌ID"),
                 BKC("shop_id", "店铺ID"), VC("product_status", "商品状态", 20),
                 DT("start_dt", "生效日期"), DT("end_dt", "失效日期"),
                 FLAG("is_current", "是否当前有效")],
         links=[("dim_category", "joins_to", ["category_id"], ["category_id"], "many_to_one",
                 "商品维表关联类目维表"),
                ("dim_brand", "joins_to", ["brand_id"], ["brand_id"], "many_to_one",
                 "商品维表关联品牌维表"),
                ("dim_shop", "joins_to", ["shop_id"], ["shop_id"], "many_to_one",
                 "商品维表关联店铺维表")]),
    dict(key="dim_sku", name="SKU维表", layer="DIM", mtype="DIMENSION", engine="postgresql",
         area="PRODUCT", status="published", reqs=["REQ_ECOM_019"],
         desc="SKU 维度（SCD2）。",
         fields=[PKC("sku_id", "SKU ID"), BKC("product_id", "商品ID"),
                 VC("sku_name", "SKU 名称", 200), VC("sku_props", "销售属性", 500),
                 VC("sku_status", "SKU 状态", 20), DT("start_dt", "生效日期"),
                 DT("end_dt", "失效日期"), FLAG("is_current", "是否当前有效")],
         links=[("dim_product", "joins_to", ["product_id"], ["product_id"], "many_to_one",
                 "SKU 维表关联商品维表")]),
    dict(key="dim_category", name="类目维表", layer="DIM", mtype="DIMENSION", engine="postgresql",
         area="PRODUCT", status="published", reqs=["REQ_ECOM_019"],
         desc="商品类目维度（多级，自引用）。",
         fields=[PKC("category_id", "类目ID"), VC("category_name", "类目名称", 64),
                 BKC("parent_id", "父类目ID"), QTY("category_level", "类目层级"),
                 VC("category_path", "类目路径", 500)],
         constraints=[{"name": "uq_category_path", "type": "unique", "fields": ["category_path"]}],
         indexes=[{"name": "idx_cat_parent", "type": "btree", "fields": ["parent_id"]}]),
    dict(key="dim_brand", name="品牌维表", layer="DIM", mtype="DIMENSION", engine="postgresql",
         area="PRODUCT", status="approved", reqs=["REQ_ECOM_019"],
         desc="品牌维度。",
         fields=[PKC("brand_id", "品牌ID"), VC("brand_name", "品牌名称", 64),
                 VC("brand_country", "品牌国家", 64), VC("brand_level", "品牌档次", 20)]),
    dict(key="dim_region", name="地区维表", layer="DIM", mtype="DIMENSION", engine="postgresql",
         area="USER", status="published", reqs=["REQ_ECOM_018"],
         desc="行政区划维度（多级，自引用）。",
         fields=[PKC("region_id", "地区ID"), VC("region_code", "地区编码", 32, unique=True),
                 VC("region_name", "地区名称", 64), BKC("parent_id", "父地区ID"),
                 QTY("region_level", "地区层级")],
         constraints=[{"name": "uq_region_code", "type": "unique", "fields": ["region_code"]},
                     {"name": "uq_region_path", "type": "unique", "fields": ["region_name", "parent_id"]}],
         indexes=[{"name": "idx_region_parent", "type": "btree", "fields": ["parent_id"]}]),
    dict(key="dim_channel", name="渠道维表", layer="DIM", mtype="DIMENSION", engine="postgresql",
         area="TRAFFIC", status="published", reqs=["REQ_ECOM_026"],
         desc="流量渠道维度（多级，自引用）。",
         fields=[PKC("channel_id", "渠道ID"), VC("channel_code", "渠道编码", 32, unique=True),
                 VC("channel_name", "渠道名称", 64), VC("channel_type", "渠道类型", 20),
                 BKC("parent_id", "父渠道ID")],
         constraints=[{"name": "uq_channel_code", "type": "unique", "fields": ["channel_code"]}],
         indexes=[{"name": "idx_channel_parent", "type": "btree", "fields": ["parent_id"]},
                  {"name": "idx_channel_type", "type": "hash", "fields": ["channel_type"]}]),
    dict(key="dim_date", name="日期维表", layer="DIM", mtype="DIMENSION", engine="postgresql",
         area="TRADE", status="published", reqs=["REQ_ECOM_001"],
         desc="日期维度（自然日属性）。",
         fields=[PKC("date_id", "日期ID"), DT("dt", "日期", unique=True),
                 QTY("year_no", "年"), QTY("quarter_no", "季度"), QTY("month_no", "月"),
                 QTY("week_no", "周"), QTY("day_of_week", "星期"),
                 FLAG("is_weekend", "是否周末"), FLAG("is_holiday", "是否节假日")],
         constraints=[{"name": "uq_dim_date", "type": "unique", "fields": ["dt"]}],
         indexes=[{"name": "idx_dim_date_year", "type": "btree", "fields": ["year_no"]}]),
    dict(key="dim_coupon", name="优惠券维表", layer="DIM", mtype="DIMENSION", engine="postgresql",
         area="MARKETING", status="approved", reqs=["REQ_ECOM_003"],
         desc="优惠券维度。",
         fields=[PKC("coupon_id", "优惠券ID"), VC("coupon_name", "券名称", 128),
                 VC("coupon_type", "券类型", 20), VC("discount_type", "优惠方式", 20),
                 DT("start_dt", "生效日期"), DT("end_dt", "失效日期")]),
    dict(key="dim_campaign", name="活动维表", layer="DIM", mtype="DIMENSION", engine="postgresql",
         area="MARKETING", status="published", reqs=["REQ_ECOM_015"],
         desc="营销活动维度。",
         fields=[PKC("campaign_id", "活动ID"), VC("campaign_name", "活动名称", 128),
                 VC("campaign_type", "活动类型", 20), DT("start_dt", "开始日期"),
                 DT("end_dt", "结束日期"), AMT("budget_amt", "活动预算")]),
    dict(key="dim_warehouse", name="仓库维表", layer="DIM", mtype="DIMENSION", engine="postgresql",
         area="LOGISTICS", status="published", reqs=["REQ_ECOM_010"],
         desc="仓库维度。",
         fields=[PKC("warehouse_id", "仓库ID"), VC("warehouse_name", "仓库名称", 128),
                 VC("province", "省份", 64), VC("city", "城市", 64),
                 VC("warehouse_type", "仓库类型", 20)]),
    dict(key="dim_payment_method", name="支付方式维表", layer="DIM", mtype="DIMENSION", engine="postgresql",
         area="TRADE", status="in_review", reqs=["REQ_ECOM_020"],
         desc="支付方式维度（渠道编码为主键）。",
         fields=[F("pay_method_code", "支付方式编码", "VARCHAR", length=32, nullable=False, pk=True),
                 VC("pay_method_name", "支付方式名称", 64),
                 VC("pay_channel_type", "渠道类型", 20), FLAG("is_online", "是否在线支付")]),
    dict(key="dim_shop", name="店铺维表", layer="DIM", mtype="DIMENSION", engine="postgresql",
         area="TRADE", status="published", reqs=["REQ_ECOM_008"],
         desc="店铺维度。",
         fields=[PKC("shop_id", "店铺ID"), VC("shop_name", "店铺名称", 128),
                 VC("shop_type", "店铺类型", 20), VC("shop_level", "店铺等级", 20),
                 VC("province", "省份", 64), VC("city", "城市", 64)]),

    # ==================== DWS 汇总层（22） ====================
    dict(key="dws_user_order_1d", name="用户订单日汇总", layer="DWS", mtype="TABLE", engine="postgresql",
         area="USER", status="published", reqs=["REQ_ECOM_001"],
         desc="用户粒度的订单/支付/退款日汇总。",
         fields=[DT("stat_date", "统计日期"), BKC("user_id", "用户ID"),
                 QTY("order_cnt", "订单数"), AMT("order_amt", "订单金额"),
                 AMT("pay_amt", "支付金额"), AMT("refund_amt", "退款金额")] + ETLCOLS(),
         constraints=[{"name": "pk_uo", "type": "primary_key", "fields": ["stat_date", "user_id"]}],
         indexes=[{"name": "idx_uo_user", "type": "btree", "fields": ["user_id"]}],
         links=[("dim_user", "joins_to", ["user_id"], ["user_id"], "many_to_one",
                 "汇总关联用户维表"),
                ("dim_date", "joins_to", ["stat_date"], ["dt"], "many_to_one",
                 "汇总日期核对日期维表")],
         v2={"add": [QTY("order_item_cnt", "下单商品件数")],
             "note": "V2：补充下单商品件数指标"}),
    dict(key="dws_user_pay_1d", name="用户支付日汇总", layer="DWS", mtype="TABLE", engine="postgresql",
         area="USER", status="published", reqs=["REQ_ECOM_020"],
         desc="用户粒度的支付日汇总。",
         fields=[DT("stat_date", "统计日期"), BKC("user_id", "用户ID"),
                 QTY("pay_cnt", "支付笔数"), AMT("pay_amt", "支付金额"),
                 VC("pay_channel", "主支付渠道", 20)] + ETLCOLS(),
         constraints=[{"name": "pk_up", "type": "primary_key", "fields": ["stat_date", "user_id"]}],
         links=[("dim_user", "joins_to", ["user_id"], ["user_id"], "many_to_one",
                 "汇总关联用户维表"),
                ("dim_date", "joins_to", ["stat_date"], ["dt"], "many_to_one",
                 "汇总日期核对日期维表")]),
    dict(key="dws_shop_gmv_1d", name="店铺GMV日汇总", layer="DWS", mtype="TABLE", engine="postgresql",
         area="TRADE", status="published", reqs=["REQ_ECOM_001"],
         desc="店铺粒度的 GMV 日汇总。",
         fields=[DT("stat_date", "统计日期"), BKC("shop_id", "店铺ID"),
                 AMT("gmv_amt", "GMV金额"), QTY("order_cnt", "订单数"),
                 AMT("pay_amt", "支付金额"), AMT("refund_amt", "退款金额")] + ETLCOLS(),
         constraints=[{"name": "pk_sg", "type": "primary_key", "fields": ["stat_date", "shop_id"]}],
         links=[("dim_shop", "joins_to", ["shop_id"], ["shop_id"], "many_to_one",
                 "汇总关联店铺维表"),
                ("dwd_order_detail", "joins_to", ["shop_id"], ["shop_id"], "many_to_one",
                 "跨层回环：汇总反核对订单明细")],
         v2={"add": [AMT("gmv_amt_excl_refund", "剔除退款GMV")],
             "note": "V2：补充剔除退款 GMV 指标"}),
    dict(key="dws_sku_sales_1d", name="SKU销售日汇总", layer="DWS", mtype="TABLE", engine="postgresql",
         area="PRODUCT", status="published", reqs=["REQ_ECOM_004"],
         desc="SKU 粒度销售日汇总。",
         fields=[DT("stat_date", "统计日期"), BKC("sku_id", "SKU ID"),
                 QTY("sale_qty", "销售件数"), AMT("sale_amt", "销售金额"),
                 QTY("refund_qty", "退款件数")] + ETLCOLS(),
         constraints=[{"name": "pk_ss", "type": "primary_key", "fields": ["stat_date", "sku_id"]}],
         links=[("dim_sku", "joins_to", ["sku_id"], ["sku_id"], "many_to_one",
                 "汇总关联 SKU 维表"),
                ("dim_date", "joins_to", ["stat_date"], ["dt"], "many_to_one",
                 "汇总日期核对日期维表")]),
    dict(key="dws_product_sales_1d", name="商品销售日汇总", layer="DWS", mtype="TABLE", engine="postgresql",
         area="PRODUCT", status="published", reqs=["REQ_ECOM_004"],
         desc="商品粒度销售日汇总。",
         fields=[DT("stat_date", "统计日期"), BKC("product_id", "商品ID"),
                 QTY("sale_qty", "销售件数"), AMT("sale_amt", "销售金额"),
                 QTY("pv_cnt", "商品页浏览数")] + ETLCOLS(),
         constraints=[{"name": "pk_ps", "type": "primary_key", "fields": ["stat_date", "product_id"]}],
         links=[("dim_product", "joins_to", ["product_id"], ["product_id"], "many_to_one",
                 "汇总关联商品维表"),
                ("dim_date", "joins_to", ["stat_date"], ["dt"], "many_to_one",
                 "汇总日期核对日期维表")]),
    dict(key="dws_category_sales_1d", name="类目销售日汇总", layer="DWS", mtype="TABLE", engine="hive",
         area="PRODUCT", status="approved", reqs=["REQ_ECOM_014"],
         desc="类目粒度销售日汇总（Hive）。",
         fields=[DT("stat_date", "统计日期"), BKC("category_id", "类目ID"),
                 AMT("sale_amt", "销售金额"), QTY("sale_qty", "销售件数")] + ETLCOLS(),
         constraints=[{"name": "pk_cs", "type": "primary_key", "fields": ["stat_date", "category_id"]}],
         links=[("dim_category", "joins_to", ["category_id"], ["category_id"], "many_to_one",
                 "汇总关联类目维表")]),
    dict(key="dws_coupon_use_1d", name="优惠券核销日汇总", layer="DWS", mtype="TABLE", engine="postgresql",
         area="MARKETING", status="published", reqs=["REQ_ECOM_003"],
         desc="券粒度的发放/核销日汇总。",
         fields=[DT("stat_date", "统计日期"), BKC("coupon_id", "优惠券ID"),
                 QTY("grant_cnt", "发放张数"), QTY("use_cnt", "核销张数"),
                 AMT("use_amt", "核销金额")] + ETLCOLS(),
         constraints=[{"name": "pk_cu", "type": "primary_key", "fields": ["stat_date", "coupon_id"]}],
         links=[("dim_coupon", "joins_to", ["coupon_id"], ["coupon_id"], "many_to_one",
                 "汇总关联优惠券维表")]),
    dict(key="dws_campaign_effect_1d", name="活动效果日汇总", layer="DWS", mtype="TABLE", engine="hive",
         area="MARKETING", status="in_review", reqs=["REQ_ECOM_015"],
         desc="活动粒度的曝光/点击/转化日汇总（Hive）。",
         fields=[DT("stat_date", "统计日期"), BKC("campaign_id", "活动ID"),
                 QTY("exposure_cnt", "曝光次数"), QTY("click_cnt", "点击次数"),
                 QTY("order_cnt", "订单数"), AMT("gmv_amt", "带动GMV")] + ETLCOLS(),
         constraints=[{"name": "pk_ce", "type": "primary_key", "fields": ["stat_date", "campaign_id"]}],
         links=[("dim_campaign", "joins_to", ["campaign_id"], ["campaign_id"], "many_to_one",
                 "汇总关联活动维表")]),
    dict(key="dws_traffic_conv_1d", name="流量转化日汇总", layer="DWS", mtype="TABLE", engine="hive",
         area="TRAFFIC", status="published", reqs=["REQ_ECOM_002"],
         desc="渠道粒度的流量转化日汇总（Hive）。",
         fields=[DT("stat_date", "统计日期"), BKC("channel_id", "渠道ID"),
                 QTY("pv_cnt", "浏览数"), QTY("uv_cnt", "访客数"),
                 QTY("order_cnt", "订单数"), F("conv_rate", "转化率", "DECIMAL",
                                              precision=10, scale=6)] + ETLCOLS(),
         constraints=[{"name": "pk_tc", "type": "primary_key", "fields": ["stat_date", "channel_id"]}],
         links=[("dim_channel", "joins_to", ["channel_id"], ["channel_id"], "many_to_one",
                 "汇总关联渠道维表"),
                ("dim_date", "joins_to", ["stat_date"], ["dt"], "many_to_one",
                 "汇总日期核对日期维表")],
         v2={"add": [QTY("cart_cnt", "加购次数")],
             "note": "V2：漏斗补充加购步骤"}),
    dict(key="dws_traffic_source_1d", name="流量来源日汇总", layer="DWS", mtype="TABLE", engine="hive",
         area="TRAFFIC", status="published", reqs=["REQ_ECOM_016"],
         desc="渠道粒度的来源质量日汇总（Hive）。",
         fields=[DT("stat_date", "统计日期"), BKC("channel_id", "渠道ID"),
                 QTY("visit_cnt", "访问次数"), QTY("new_uv_cnt", "新访客数"),
                 F("bounce_rate", "跳出率", "DECIMAL", precision=10, scale=6)] + ETLCOLS(),
         constraints=[{"name": "pk_ts", "type": "primary_key", "fields": ["stat_date", "channel_id"]}],
         links=[("dim_channel", "joins_to", ["channel_id"], ["channel_id"], "many_to_one",
                 "汇总关联渠道维表")]),
    dict(key="dws_search_word_1d", name="搜索词日汇总", layer="DWS", mtype="TABLE", engine="hive",
         area="TRAFFIC", status="draft", reqs=["REQ_ECOM_011"], problem=True,
         desc="搜索词日汇总（问题草稿：ctr 缺 DECIMAL 精度）。",
         fields=[DT("stat_date", "统计日期"), VC("keyword", "搜索词", 200),
                 QTY("search_cnt", "搜索次数"), QTY("click_cnt", "点击次数"),
                 F("ctr", "点击率", "DECIMAL")] + ETLCOLS(),
         constraints=[{"name": "pk_sw", "type": "primary_key", "fields": ["stat_date", "keyword"]}],
         links=[("dim_date", "joins_to", ["stat_date"], ["dt"], "many_to_one",
                 "汇总日期核对日期维表")]),
    dict(key="dws_logistics_perf_1d", name="物流履约日汇总", layer="DWS", mtype="TABLE", engine="postgresql",
         area="LOGISTICS", status="published", reqs=["REQ_ECOM_005"],
         desc="仓库粒度的履约时效日汇总。",
         fields=[DT("stat_date", "统计日期"), BKC("warehouse_id", "仓库ID"),
                 QTY("ship_cnt", "发货单数"), QTY("ontime_cnt", "及时发货单数"),
                 F("avg_hours", "平均履约时长(小时)", "DECIMAL", precision=10, scale=2)] + ETLCOLS(),
         constraints=[{"name": "pk_lp", "type": "primary_key", "fields": ["stat_date", "warehouse_id"]}],
         links=[("dim_warehouse", "joins_to", ["warehouse_id"], ["warehouse_id"], "many_to_one",
                 "汇总关联仓库维表")]),
    dict(key="dws_warehouse_stock_1d", name="仓库库存日汇总", layer="DWS", mtype="TABLE", engine="mysql",
         area="LOGISTICS", status="approved", reqs=["REQ_ECOM_010"],
         desc="仓库粒度的库存日汇总（MySQL）。",
         fields=[DT("stat_date", "统计日期"), BKC("warehouse_id", "仓库ID"),
                 QTY("sku_cnt", "SKU 数"), QTY("stock_qty", "库存件数"),
                 AMT("stock_amt", "库存金额")] + ETLCOLS(),
         constraints=[{"name": "pk_ws", "type": "primary_key", "fields": ["stat_date", "warehouse_id"]}],
         links=[("dim_warehouse", "joins_to", ["warehouse_id"], ["warehouse_id"], "many_to_one",
                 "汇总关联仓库维表")]),
    dict(key="dws_service_quality_1d", name="服务质量日汇总", layer="DWS", mtype="TABLE", engine="postgresql",
         area="SERVICE", status="published", reqs=["REQ_ECOM_006"],
         desc="工单类型的服务质量日汇总。",
         fields=[DT("stat_date", "统计日期"), VC("ticket_type", "工单类型", 20),
                 QTY("ticket_cnt", "工单量"), QTY("close_cnt", "关闭量"),
                 F("avg_score", "平均满意度", "DECIMAL", precision=10, scale=2)] + ETLCOLS(),
         constraints=[{"name": "pk_sq", "type": "primary_key", "fields": ["stat_date", "ticket_type"]}],
         links=[("dim_date", "joins_to", ["stat_date"], ["dt"], "many_to_one",
                 "汇总日期核对日期维表")]),
    dict(key="dws_member_growth_1d", name="会员增长日汇总", layer="DWS", mtype="TABLE", engine="postgresql",
         area="USER", status="published", reqs=["REQ_ECOM_007"],
         desc="会员等级的增减日汇总。",
         fields=[DT("stat_date", "统计日期"), VC("member_level", "会员等级", 20),
                 QTY("new_cnt", "新增会员数"), QTY("active_cnt", "活跃会员数"),
                 QTY("churn_cnt", "流失会员数")] + ETLCOLS(),
         constraints=[{"name": "pk_mg", "type": "primary_key", "fields": ["stat_date", "member_level"]}],
         links=[("dim_date", "joins_to", ["stat_date"], ["dt"], "many_to_one",
                 "汇总日期核对日期维表")]),
    dict(key="dws_refund_1d", name="退款日汇总", layer="DWS", mtype="TABLE", engine="postgresql",
         area="TRADE", status="in_review", reqs=["REQ_ECOM_009"],
         desc="退款原因的退款日汇总。",
         fields=[DT("stat_date", "统计日期"), VC("refund_reason", "退款原因", 200),
                 QTY("refund_cnt", "退款笔数"), AMT("refund_amt", "退款金额")] + ETLCOLS(),
         constraints=[{"name": "pk_rf", "type": "primary_key", "fields": ["stat_date", "refund_reason"]}],
         links=[("dim_date", "joins_to", ["stat_date"], ["dt"], "many_to_one",
                 "汇总日期核对日期维表")]),
    dict(key="dws_settlement_1d", name="结算日汇总", layer="DWS", mtype="TABLE", engine="postgresql",
         area="FINANCE", status="published", reqs=["REQ_ECOM_008"],
         desc="店铺粒度的结算日汇总。",
         fields=[DT("stat_date", "统计日期"), BKC("shop_id", "店铺ID"),
                 QTY("settle_cnt", "结算单数"), AMT("settle_amt", "结算金额"),
                 AMT("commission_amt", "佣金金额")] + ETLCOLS(),
         constraints=[{"name": "pk_st", "type": "primary_key", "fields": ["stat_date", "shop_id"]}],
         links=[("dim_shop", "joins_to", ["shop_id"], ["shop_id"], "many_to_one",
                 "汇总关联店铺维表")]),
    dict(key="dws_user_active_1d", name="用户活跃日汇总", layer="DWS", mtype="TABLE", engine="hive",
         area="USER", status="approved", reqs=["REQ_ECOM_012"],
         desc="用户粒度的活跃日汇总（Hive）。",
         fields=[DT("stat_date", "统计日期"), BKC("user_id", "用户ID"),
                 QTY("login_cnt", "登录次数"), QTY("pv_cnt", "浏览数"),
                 QTY("duration_sec", "停留秒数")] + ETLCOLS(),
         constraints=[{"name": "pk_ua", "type": "primary_key", "fields": ["stat_date", "user_id"]}],
         links=[("dim_user", "joins_to", ["user_id"], ["user_id"], "many_to_one",
                 "汇总关联用户维表")]),
    dict(key="dws_cart_conv_1d", name="购物车转化日汇总", layer="DWS", mtype="TABLE", engine="postgresql",
         area="TRADE", status="draft", reqs=["REQ_ECOM_021"],
         desc="用户粒度的购物车转化日汇总。",
         fields=[DT("stat_date", "统计日期"), BKC("user_id", "用户ID"),
                 QTY("cart_cnt", "加购次数"), QTY("cart_order_cnt", "加购转化订单数"),
                 AMT("cart_amt", "加购金额")] + ETLCOLS(),
         constraints=[{"name": "pk_cc", "type": "primary_key", "fields": ["stat_date", "user_id"]}],
         links=[("dim_user", "joins_to", ["user_id"], ["user_id"], "many_to_one",
                 "汇总关联用户维表")]),
    dict(key="dws_promo_effect_1d", name="促销效果日汇总", layer="DWS", mtype="TABLE", engine="hive",
         area="MARKETING", status="in_review", reqs=["REQ_ECOM_022"],
         desc="促销粒度的效果日汇总（Hive）。",
         fields=[DT("stat_date", "统计日期"), BKC("promo_id", "促销ID"),
                 QTY("order_cnt", "订单数"), AMT("gmv_amt", "带动GMV"),
                 AMT("discount_amt", "优惠金额")] + ETLCOLS(),
         constraints=[{"name": "pk_pe", "type": "primary_key", "fields": ["stat_date", "promo_id"]}],
         links=[("dim_date", "joins_to", ["stat_date"], ["dt"], "many_to_one",
                 "汇总日期核对日期维表")]),
    dict(key="dws_invoice_1d", name="发票日汇总", layer="DWS", mtype="TABLE", engine="postgresql",
         area="FINANCE", status="approved", reqs=["REQ_ECOM_024"],
         desc="发票类型的开具日汇总。",
         fields=[DT("stat_date", "统计日期"), VC("invoice_type", "发票类型", 20),
                 QTY("invoice_cnt", "发票张数"), AMT("invoice_amt", "发票金额")] + ETLCOLS(),
         constraints=[{"name": "pk_iv", "type": "primary_key", "fields": ["stat_date", "invoice_type"]}],
         links=[("dim_date", "joins_to", ["stat_date"], ["dt"], "many_to_one",
                 "汇总日期核对日期维表")]),
    dict(key="dws_ticket_1d", name="工单日汇总", layer="DWS", mtype="TABLE", engine="postgresql",
         area="SERVICE", status="published", reqs=["REQ_ECOM_006"],
         desc="工单类型的工单日汇总。",
         fields=[DT("stat_date", "统计日期"), VC("ticket_type", "工单类型", 20),
                 QTY("ticket_cnt", "工单量"),
                 F("avg_score", "平均满意度", "DECIMAL", precision=10, scale=2)] + ETLCOLS(),
         constraints=[{"name": "pk_tk", "type": "primary_key", "fields": ["stat_date", "ticket_type"]}],
         links=[("dim_date", "joins_to", ["stat_date"], ["dt"], "many_to_one",
                 "汇总日期核对日期维表")]),

    # ==================== ADS 应用层（18） ====================
    dict(key="ads_gmv_daily", name="GMV日报", layer="ADS", mtype="VIEW", engine="postgresql",
         area="TRADE", status="published", reqs=["REQ_ECOM_001"],
         desc="经营日报：GMV/支付/退款/订单量。",
         fields=[DT("stat_date", "统计日期"), AMT("gmv_amt", "GMV金额"),
                 AMT("pay_amt", "支付金额"), AMT("refund_amt", "退款金额"),
                 QTY("order_cnt", "订单量"), QTY("pay_user_cnt", "支付用户数")] + ETLCOLS(),
         indexes=[{"name": "idx_ads_gmv", "type": "btree", "fields": ["stat_date"]}],
         links=[("dws_shop_gmv_1d", "derived_from", ["stat_date"], ["stat_date"], "many_to_one",
                 "日报引用店铺GMV日汇总")],
         v2={"add": [AMT("gmv_amt_yoy", "同比GMV")],
             "note": "V2：补充同比 GMV"}),
    dict(key="ads_order_conv_funnel", name="下单转化漏斗", layer="ADS", mtype="VIEW", engine="hive",
         area="TRAFFIC", status="in_review", reqs=["REQ_ECOM_002"],
         desc="访问→详情→加购→下单→支付漏斗。",
         fields=[DT("stat_date", "统计日期"), QTY("pv_cnt", "访问数"),
                 QTY("detail_pv_cnt", "详情页浏览数"), QTY("cart_cnt", "加购数"),
                 QTY("order_cnt", "下单数"), QTY("pay_cnt", "支付数")] + ETLCOLS(),
         links=[("dws_traffic_conv_1d", "derived_from", ["stat_date"], ["stat_date"], "many_to_one",
                 "漏斗引用流量转化日汇总")]),
    dict(key="ads_user_rfm", name="用户RFM分层", layer="ADS", mtype="VIEW", engine="postgresql",
         area="USER", status="approved", reqs=["REQ_ECOM_013"],
         desc="用户 RFM 评分与分层。",
         fields=[PKC("user_id", "用户ID"), QTY("recency_days", "最近购买距今天数"),
                 QTY("frequency_cnt", "购买频次"), AMT("monetary_amt", "购买金额"),
                 VC("rfm_score", "RFM评分", 8), VC("rfm_group", "RFM分层", 20),
                 DT("stat_date", "统计日期")],
         indexes=[{"name": "idx_rfm_group", "type": "btree", "fields": ["rfm_group"]}],
         links=[("dws_user_order_1d", "derived_from", ["user_id"], ["user_id"], "many_to_one",
                 "RFM 引用用户订单日汇总")]),
    dict(key="ads_coupon_roi", name="优惠券ROI", layer="ADS", mtype="VIEW", engine="postgresql",
         area="MARKETING", status="published", reqs=["REQ_ECOM_003"],
         desc="券粒度的核销与 ROI。",
         fields=[DT("stat_date", "统计日期"), BKC("coupon_id", "优惠券ID"),
                 QTY("grant_cnt", "发放张数"), QTY("use_cnt", "核销张数"),
                 AMT("gmv_amt", "带动GMV"), AMT("cost_amt", "券成本"),
                 F("roi", "投入产出比", "DECIMAL", precision=10, scale=4)] + ETLCOLS(),
         constraints=[{"name": "pk_roi", "type": "primary_key", "fields": ["stat_date", "coupon_id"]}],
         links=[("dws_coupon_use_1d", "derived_from", ["coupon_id"], ["coupon_id"], "many_to_one",
                 "ROI 引用优惠券核销日汇总")]),
    dict(key="ads_product_rank", name="商品畅销排行", layer="ADS", mtype="VIEW", engine="postgresql",
         area="PRODUCT", status="published", reqs=["REQ_ECOM_004"],
         desc="商品销售排行 TopN。",
         fields=[DT("stat_date", "统计日期"), QTY("rank_no", "排名"),
                 BKC("product_id", "商品ID"), VC("product_name", "商品名称", 200),
                 QTY("sale_qty", "销售件数"), AMT("sale_amt", "销售金额")] + ETLCOLS(),
         constraints=[{"name": "pk_pr", "type": "primary_key", "fields": ["stat_date", "rank_no"]}],
         links=[("dws_product_sales_1d", "derived_from", ["product_id"], ["product_id"], "many_to_one",
                 "排行引用商品销售日汇总")]),
    dict(key="ads_traffic_source_daily", name="流量来源日报", layer="ADS", mtype="VIEW", engine="hive",
         area="TRAFFIC", status="approved", reqs=["REQ_ECOM_016"],
         desc="渠道粒度的流量与转化日报。",
         fields=[DT("stat_date", "统计日期"), BKC("channel_id", "渠道ID"),
                 QTY("uv_cnt", "访客数"), QTY("order_cnt", "订单数"),
                 AMT("gmv_amt", "GMV金额"),
                 F("conv_rate", "转化率", "DECIMAL", precision=10, scale=6)] + ETLCOLS(),
         constraints=[{"name": "pk_tsd", "type": "primary_key", "fields": ["stat_date", "channel_id"]}],
         links=[("dws_traffic_source_1d", "derived_from", ["channel_id"], ["channel_id"], "many_to_one",
                 "日报引用流量来源日汇总")]),
    dict(key="ads_logistics_ontime", name="物流时效报表", layer="ADS", mtype="VIEW", engine="postgresql",
         area="LOGISTICS", status="published", reqs=["REQ_ECOM_005"],
         desc="发货及时率与平均时长报表。",
         fields=[DT("stat_date", "统计日期"), QTY("ship_cnt", "发货单数"),
                 F("ontime_rate", "及时率", "DECIMAL", precision=10, scale=6),
                 F("avg_hours", "平均时长(小时)", "DECIMAL", precision=10, scale=2)] + ETLCOLS(),
         links=[("dws_logistics_perf_1d", "derived_from", ["stat_date"], ["stat_date"], "many_to_one",
                 "报表引用物流履约日汇总")]),
    dict(key="ads_service_satisfaction", name="客服满意度报表", layer="ADS", mtype="VIEW", engine="postgresql",
         area="SERVICE", status="published", reqs=["REQ_ECOM_006"],
         desc="工单满意度报表。",
         fields=[DT("stat_date", "统计日期"), QTY("ticket_cnt", "工单量"),
                 F("satisfaction_rate", "满意率", "DECIMAL", precision=10, scale=6),
                 F("avg_score", "平均评分", "DECIMAL", precision=10, scale=2)] + ETLCOLS(),
         links=[("dws_service_quality_1d", "derived_from", ["stat_date"], ["stat_date"], "many_to_one",
                 "报表引用服务质量日汇总")]),
    dict(key="ads_member_retention", name="会员留存报表", layer="ADS", mtype="VIEW", engine="hive",
         area="USER", status="in_review", reqs=["REQ_ECOM_007"],
         desc="会员 1/7/30 日留存报表。",
         fields=[DT("stat_date", "统计日期"), VC("member_level", "会员等级", 20),
                 QTY("register_cnt", "注册数"), QTY("retain_1d_cnt", "次日留存"),
                 QTY("retain_7d_cnt", "7日留存"), QTY("retain_30d_cnt", "30日留存")] + ETLCOLS(),
         constraints=[{"name": "pk_mr", "type": "primary_key", "fields": ["stat_date", "member_level"]}],
         links=[("dws_member_growth_1d", "derived_from", ["member_level"], ["member_level"], "many_to_one",
                 "留存报表引用会员增长日汇总")]),
    dict(key="ads_finance_settlement", name="财务结算报表", layer="ADS", mtype="VIEW", engine="postgresql",
         area="FINANCE", status="published", reqs=["REQ_ECOM_008"],
         desc="店铺结算与佣金报表。",
         fields=[DT("stat_date", "统计日期"), AMT("settle_amt", "结算金额"),
                 AMT("commission_amt", "佣金金额"), QTY("shop_cnt", "结算店铺数")] + ETLCOLS(),
         links=[("dws_settlement_1d", "derived_from", ["stat_date"], ["stat_date"], "many_to_one",
                 "报表引用结算日汇总")]),
    dict(key="ads_shop_rank", name="店铺排行", layer="ADS", mtype="VIEW", engine="postgresql",
         area="TRADE", status="approved", reqs=["REQ_ECOM_001"],
         desc="店铺 GMV 排行。",
         fields=[DT("stat_date", "统计日期"), QTY("rank_no", "排名"),
                 BKC("shop_id", "店铺ID"), VC("shop_name", "店铺名称", 128),
                 AMT("gmv_amt", "GMV金额"), QTY("order_cnt", "订单量")] + ETLCOLS(),
         constraints=[{"name": "pk_sr", "type": "primary_key", "fields": ["stat_date", "rank_no"]}],
         links=[("dws_shop_gmv_1d", "derived_from", ["shop_id"], ["shop_id"], "many_to_one",
                 "排行引用店铺GMV日汇总")]),
    dict(key="ads_category_gmv", name="类目GMV报表", layer="ADS", mtype="VIEW", engine="hive",
         area="PRODUCT", status="published", reqs=["REQ_ECOM_014"],
         desc="类目 GMV 结构报表。",
         fields=[DT("stat_date", "统计日期"), BKC("category_id", "类目ID"),
                 AMT("gmv_amt", "GMV金额"), QTY("sale_qty", "销售件数")] + ETLCOLS(),
         constraints=[{"name": "pk_cg", "type": "primary_key", "fields": ["stat_date", "category_id"]}],
         links=[("dws_category_sales_1d", "derived_from", ["category_id"], ["category_id"], "many_to_one",
                 "报表引用类目销售日汇总")]),
    dict(key="ads_campaign_roi", name="活动ROI报表", layer="ADS", mtype="VIEW", engine="postgresql",
         area="MARKETING", status="draft", reqs=["REQ_ECOM_015"],
         desc="活动投入产出报表（草稿，待效果口径确认）。",
         fields=[DT("stat_date", "统计日期"), BKC("campaign_id", "活动ID"),
                 AMT("gmv_amt", "带动GMV"), AMT("cost_amt", "活动成本"),
                 F("roi", "投入产出比", "DECIMAL", precision=10, scale=4)] + ETLCOLS(),
         constraints=[{"name": "pk_cr", "type": "primary_key", "fields": ["stat_date", "campaign_id"]}],
         links=[("dws_campaign_effect_1d", "derived_from", ["campaign_id"], ["campaign_id"], "many_to_one",
                 "报表引用活动效果日汇总")]),
    dict(key="ads_refund_reason", name="退款原因分析", layer="ADS", mtype="VIEW", engine="postgresql",
         area="TRADE", status="published", reqs=["REQ_ECOM_009"],
         desc="退款原因分布与退款率。",
         fields=[DT("stat_date", "统计日期"), VC("refund_reason", "退款原因", 200),
                 QTY("refund_cnt", "退款笔数"), AMT("refund_amt", "退款金额"),
                 F("refund_rate", "退款率", "DECIMAL", precision=10, scale=6)] + ETLCOLS(),
         constraints=[{"name": "pk_rr", "type": "primary_key", "fields": ["stat_date", "refund_reason"]}],
         links=[("dws_refund_1d", "derived_from", ["refund_reason"], ["refund_reason"], "many_to_one",
                 "分析引用退款日汇总")]),
    dict(key="ads_inventory_alert", name="库存预警", layer="ADS", mtype="VIEW", engine="mysql",
         area="PRODUCT", status="in_review", reqs=["REQ_ECOM_010"],
         desc="SKU 库存预警分级（MySQL）。",
         fields=[DT("stat_date", "统计日期"), BKC("sku_id", "SKU ID"),
                 QTY("stock_qty", "可用库存"), QTY("safe_qty", "安全库存"),
                 VC("alert_level", "预警等级", 20)] + ETLCOLS(),
         constraints=[{"name": "pk_ia", "type": "primary_key", "fields": ["stat_date", "sku_id"]}],
         links=[("dwd_inventory_detail", "derived_from", ["sku_id"], ["sku_id"], "many_to_one",
                 "预警基于库存明细（仓粒度下钻）")]),
    dict(key="ads_search_hot_word", name="搜索热词", layer="ADS", mtype="VIEW", engine="hive",
         area="TRAFFIC", status="draft", reqs=["REQ_ECOM_011"], problem=True,
         desc="搜索热词榜（问题草稿：hot_score 小数位大于总精度）。",
         fields=[DT("stat_date", "统计日期"), VC("keyword", "搜索词", 200),
                 QTY("search_cnt", "搜索次数"),
                 F("hot_score", "热度分", "DECIMAL", precision=5, scale=8)] + ETLCOLS(),
         constraints=[{"name": "pk_shw", "type": "primary_key", "fields": ["stat_date", "keyword"]}],
         links=[("dws_search_word_1d", "derived_from", ["keyword"], ["keyword"], "many_to_one",
                 "热词引用搜索词日汇总")]),
    dict(key="ads_user_growth", name="用户增长报表", layer="ADS", mtype="VIEW", engine="postgresql",
         area="USER", status="published", reqs=["REQ_ECOM_012"],
         desc="注册/活跃/付费用户增长报表。",
         fields=[DT("stat_date", "统计日期"), QTY("register_cnt", "注册数"),
                 QTY("active_cnt", "活跃数"), QTY("new_pay_cnt", "新增付费用户"),
                 F("growth_rate", "增长率", "DECIMAL", precision=10, scale=6)] + ETLCOLS(),
         links=[("dws_member_growth_1d", "derived_from", ["stat_date"], ["stat_date"], "many_to_one",
                 "报表引用会员增长日汇总")]),
    dict(key="ads_vip_profile", name="高价值用户画像", layer="ADS", mtype="VIEW", engine="postgresql",
         area="USER", status="approved", reqs=["REQ_ECOM_013"],
         desc="高价值用户分层与偏好。",
         fields=[PKC("user_id", "用户ID"), VC("vip_level", "价值分层", 20),
                 AMT("ltv_amt", "生命周期价值"), DT("last_order_dt", "最近订单日期"),
                 VC("fav_category", "偏好类目", 64), DT("stat_date", "统计日期")],
         indexes=[{"name": "idx_vip_level", "type": "btree", "fields": ["vip_level"]}],
         links=[("dim_user", "joins_to", ["user_id"], ["user_id"], "many_to_one",
                 "画像关联用户维表"),
                ("dws_user_order_1d", "derived_from", ["user_id"], ["user_id"], "many_to_one",
                 "画像引用用户订单日汇总")]),
]

# ---------------------------------------------------------------- 外部血缘

def _edge(src, tgt, ltype="etl_data_flow", ttype="sql", conf=95, job="", meta=None):
    d = {"source_ref": f"airflow://{src}=>{tgt}", "source_model_key": src,
         "target_model_key": tgt, "lineage_type": ltype,
         "transformation_type": ttype, "confidence": conf}
    if job:
        d["job_key"] = job
    if meta:
        d["metadata"] = meta
    return d


# airflow ETL 数据流（ODS→DWD 主清洗链路）
AIRFLOW_ETL_EDGES = [
    _edge("ods_order_raw", "dwd_order_detail", conf=98, job="dag_daily.ods_order_clean",
          meta={"dag_id": "dag_daily", "task_id": "ods_order_clean"}),
    _edge("ods_order_item_raw", "dwd_order_item", conf=98, job="dag_daily.ods_order_item_clean",
          meta={"dag_id": "dag_daily", "task_id": "ods_order_item_clean"}),
    _edge("ods_pay_raw", "dwd_pay_detail", conf=97, job="dag_daily.ods_pay_clean",
          meta={"dag_id": "dag_daily", "task_id": "ods_pay_clean"}),
    _edge("ods_refund_raw", "dwd_refund_detail", conf=97, job="dag_daily.ods_refund_clean",
          meta={"dag_id": "dag_daily", "task_id": "ods_refund_clean"}),
    _edge("ods_cart_raw", "dwd_cart_detail", conf=90, job="dag_daily.ods_cart_clean",
          meta={"dag_id": "dag_daily", "task_id": "ods_cart_clean"}),
    _edge("ods_user_raw", "dwd_user_detail", conf=99, job="dag_daily.ods_user_clean",
          meta={"dag_id": "dag_daily", "task_id": "ods_user_clean"}),
    _edge("ods_member_raw", "dwd_member_detail", conf=99, job="dag_daily.ods_member_clean",
          meta={"dag_id": "dag_daily", "task_id": "ods_member_clean"}),
    _edge("ods_address_raw", "dwd_address_detail", conf=96, job="dag_daily.ods_address_clean",
          meta={"dag_id": "dag_daily", "task_id": "ods_address_clean"}),
    _edge("ods_user_login_raw", "dwd_login_log", ttype="filter", conf=88,
          job="dag_daily.ods_login_clean", meta={"dag_id": "dag_daily", "task_id": "ods_login_clean"}),
    _edge("ods_product_raw", "dwd_product_detail", conf=99, job="dag_daily.ods_product_clean",
          meta={"dag_id": "dag_daily", "task_id": "ods_product_clean"}),
    _edge("ods_sku_raw", "dwd_sku_detail", conf=99, job="dag_daily.ods_sku_clean",
          meta={"dag_id": "dag_daily", "task_id": "ods_sku_clean"}),
    _edge("ods_inventory_raw", "dwd_inventory_detail", ttype="copy", conf=95,
          job="dag_daily.ods_inventory_sync", meta={"dag_id": "dag_daily", "task_id": "ods_inventory_sync"}),
    _edge("ods_price_raw", "dwd_price_detail", ttype="copy", conf=93,
          job="dag_daily.ods_price_sync", meta={"dag_id": "dag_daily", "task_id": "ods_price_sync"}),
    _edge("ods_coupon_raw", "dwd_coupon_detail", conf=98, job="dag_daily.ods_coupon_clean",
          meta={"dag_id": "dag_daily", "task_id": "ods_coupon_clean"}),
    _edge("ods_coupon_grant_raw", "dwd_coupon_grant_detail", conf=98,
          job="dag_daily.ods_grant_clean", meta={"dag_id": "dag_daily", "task_id": "ods_grant_clean"}),
    _edge("ods_campaign_raw", "dwd_campaign_detail", conf=98, job="dag_daily.ods_campaign_clean",
          meta={"dag_id": "dag_daily", "task_id": "ods_campaign_clean"}),
    _edge("ods_page_view_raw", "dwd_page_view_detail", ttype="filter", conf=90,
          job="dag_daily.ods_pv_clean", meta={"dag_id": "dag_daily", "task_id": "ods_pv_clean"}),
    _edge("ods_click_raw", "dwd_click_detail", ttype="filter", conf=90,
          job="dag_daily.ods_click_clean", meta={"dag_id": "dag_daily", "task_id": "ods_click_clean"}),
    _edge("ods_search_raw", "dwd_search_detail", ttype="filter", conf=88,
          job="dag_daily.ods_search_clean", meta={"dag_id": "dag_daily", "task_id": "ods_search_clean"}),
    _edge("ods_shipment_raw", "dwd_shipment_detail", conf=97, job="dag_daily.ods_shipment_clean",
          meta={"dag_id": "dag_daily", "task_id": "ods_shipment_clean"}),
    _edge("ods_warehouse_raw", "dwd_warehouse_detail", conf=99, job="dag_daily.ods_warehouse_clean",
          meta={"dag_id": "dag_daily", "task_id": "ods_warehouse_clean"}),
    _edge("ods_ticket_raw", "dwd_ticket_detail", conf=97, job="dag_daily.ods_ticket_clean",
          meta={"dag_id": "dag_daily", "task_id": "ods_ticket_clean"}),
    _edge("ods_review_raw", "dwd_review_detail", ttype="filter", conf=85,
          job="dag_daily.ods_review_clean", meta={"dag_id": "dag_daily", "task_id": "ods_review_clean"}),
    _edge("ods_settlement_raw", "dwd_settlement_detail", conf=99, job="dag_daily.ods_settlement_clean",
          meta={"dag_id": "dag_daily", "task_id": "ods_settlement_clean"}),
    _edge("ods_invoice_raw", "dwd_invoice_detail", conf=98, job="dag_daily.ods_invoice_clean",
          meta={"dag_id": "dag_daily", "task_id": "ods_invoice_clean"}),
]

# airflow 调度依赖（DWD/DIM→DWS，DAG 先后步）
AIRFLOW_SCHED_EDGES = [
    _edge("dwd_order_detail", "dws_user_order_1d", "scheduler_dependency", "aggregate", 85,
          "dag_daily.dws_user_order", {"dag_id": "dag_daily", "task_id": "dws_user_order"}),
    _edge("dwd_pay_detail", "dws_user_pay_1d", "scheduler_dependency", "aggregate", 85,
          "dag_daily.dws_user_pay", {"dag_id": "dag_daily", "task_id": "dws_user_pay"}),
    _edge("dwd_order_detail", "dws_shop_gmv_1d", "scheduler_dependency", "aggregate", 88,
          "dag_daily.dws_shop_gmv", {"dag_id": "dag_daily", "task_id": "dws_shop_gmv"}),
    _edge("dwd_order_item", "dws_sku_sales_1d", "scheduler_dependency", "aggregate", 88,
          "dag_daily.dws_sku_sales", {"dag_id": "dag_daily", "task_id": "dws_sku_sales"}),
    _edge("dwd_product_detail", "dws_product_sales_1d", "scheduler_dependency", "join", 82,
          "dag_daily.dws_product_sales", {"dag_id": "dag_daily", "task_id": "dws_product_sales"}),
    _edge("dwd_coupon_grant_detail", "dws_coupon_use_1d", "scheduler_dependency", "aggregate", 82,
          "dag_daily.dws_coupon_use", {"dag_id": "dag_daily", "task_id": "dws_coupon_use"}),
    _edge("dwd_shipment_detail", "dws_logistics_perf_1d", "scheduler_dependency", "aggregate", 82,
          "dag_daily.dws_logistics_perf", {"dag_id": "dag_daily", "task_id": "dws_logistics_perf"}),
    _edge("dwd_ticket_detail", "dws_service_quality_1d", "scheduler_dependency", "aggregate", 80,
          "dag_daily.dws_service_quality", {"dag_id": "dag_daily", "task_id": "dws_service_quality"}),
    _edge("dwd_member_detail", "dws_member_growth_1d", "scheduler_dependency", "aggregate", 80,
          "dag_daily.dws_member_growth", {"dag_id": "dag_daily", "task_id": "dws_member_growth"}),
    _edge("dwd_refund_detail", "dws_refund_1d", "scheduler_dependency", "aggregate", 80,
          "dag_daily.dws_refund", {"dag_id": "dag_daily", "task_id": "dws_refund"}),
    _edge("dwd_settlement_detail", "dws_settlement_1d", "scheduler_dependency", "aggregate", 82,
          "dag_daily.dws_settlement", {"dag_id": "dag_daily", "task_id": "dws_settlement"}),
    _edge("dwd_login_log", "dws_user_active_1d", "scheduler_dependency", "aggregate", 78,
          "dag_daily.dws_user_active", {"dag_id": "dag_daily", "task_id": "dws_user_active"}),
]

# BI 消费依赖（ADS 报表间依赖，superset 数据集链）
BI_EDGES = [
    {"source_ref": "superset://dataset/gmv_daily", "source_model_key": "ads_gmv_daily",
     "target_model_key": "ads_shop_rank", "lineage_type": "bi_consumption",
     "transformation_type": "lookup", "confidence": 70,
     "job_key": "superset.dataset_refresh", "metadata": {"dashboard": "经营驾驶舱"}},
    {"source_ref": "superset://dataset/shop_gmv", "source_model_key": "dws_shop_gmv_1d",
     "target_model_key": "ads_gmv_daily", "lineage_type": "bi_consumption",
     "transformation_type": "lookup", "confidence": 65,
     "job_key": "superset.dataset_refresh", "metadata": {"dashboard": "经营驾驶舱"}},
    {"source_ref": "superset://dataset/traffic_conv", "source_model_key": "dws_traffic_conv_1d",
     "target_model_key": "ads_order_conv_funnel", "lineage_type": "bi_consumption",
     "transformation_type": "lookup", "confidence": 68,
     "job_key": "superset.dataset_refresh", "metadata": {"dashboard": "流量看板"}},
    {"source_ref": "superset://dataset/user_order", "source_model_key": "dws_user_order_1d",
     "target_model_key": "ads_user_rfm", "lineage_type": "bi_consumption",
     "transformation_type": "lookup", "confidence": 72,
     "job_key": "superset.dataset_refresh", "metadata": {"dashboard": "用户看板"}},
    {"source_ref": "superset://dataset/coupon_use", "source_model_key": "dws_coupon_use_1d",
     "target_model_key": "ads_coupon_roi", "lineage_type": "bi_consumption",
     "transformation_type": "lookup", "confidence": 66,
     "job_key": "superset.dataset_refresh", "metadata": {"dashboard": "营销看板"}},
    {"source_ref": "superset://dataset/settlement", "source_model_key": "dws_settlement_1d",
     "target_model_key": "ads_finance_settlement", "lineage_type": "bi_consumption",
     "transformation_type": "lookup", "confidence": 75,
     "job_key": "superset.dataset_refresh", "metadata": {"dashboard": "财务看板"}},
    {"source_ref": "superset://dataset/member_growth", "source_model_key": "dws_member_growth_1d",
     "target_model_key": "ads_member_retention", "lineage_type": "bi_consumption",
     "transformation_type": "lookup", "confidence": 64,
     "job_key": "superset.dataset_refresh", "metadata": {"dashboard": "用户看板"}},
    {"source_ref": "superset://dataset/logistics_perf", "source_model_key": "dws_logistics_perf_1d",
     "target_model_key": "ads_logistics_ontime", "lineage_type": "bi_consumption",
     "transformation_type": "lookup", "confidence": 70,
     "job_key": "superset.dataset_refresh", "metadata": {"dashboard": "履约看板"}},
]

# 人工登记（other 类型，演示血缘类型全集）
MANUAL_EDGES = [
    {"source_ref": "manual://registry/order-shop-map", "source_model_key": "dim_shop",
     "target_model_key": "dwd_order_detail", "lineage_type": "other",
     "transformation_type": "unknown", "confidence": 50,
     "job_key": "manual.registry", "metadata": {"registered_by": "data.steward",
                                                "note": "人工登记的店铺-订单归属口径"}},
    {"source_ref": "manual://registry/channel-dict", "source_model_key": "dim_channel",
     "target_model_key": "dwd_page_view_detail", "lineage_type": "other",
     "transformation_type": "unknown", "confidence": 50,
     "job_key": "manual.registry", "metadata": {"registered_by": "data.steward",
                                                "note": "人工登记的渠道字典映射"}},
]

# dbt manifest（DWS→ADS 的 dbt 依赖）
DBT_TARGETS = {
    "ads_gmv_daily": ["dws_shop_gmv_1d", "dim_shop"],
    "ads_order_conv_funnel": ["dws_traffic_conv_1d", "dim_channel"],
    "ads_user_rfm": ["dws_user_order_1d", "dim_user"],
    "ads_coupon_roi": ["dws_coupon_use_1d", "dim_coupon"],
    "ads_product_rank": ["dws_product_sales_1d", "dim_product"],
    "ads_traffic_source_daily": ["dws_traffic_source_1d", "dim_channel"],
    "ads_logistics_ontime": ["dws_logistics_perf_1d", "dim_warehouse"],
    "ads_service_satisfaction": ["dws_service_quality_1d"],
    "ads_member_retention": ["dws_member_growth_1d"],
    "ads_finance_settlement": ["dws_settlement_1d", "dim_shop"],
    "ads_shop_rank": ["dws_shop_gmv_1d", "dim_shop"],
    "ads_category_gmv": ["dws_category_sales_1d", "dim_category"],
    "ads_campaign_roi": ["dws_campaign_effect_1d", "dim_campaign"],
    "ads_refund_reason": ["dws_refund_1d"],
    "ads_inventory_alert": ["dws_warehouse_stock_1d", "dim_sku"],
    "ads_search_hot_word": ["dws_search_word_1d"],
    "ads_user_growth": ["dws_member_growth_1d", "dim_user"],
    "ads_vip_profile": ["dws_user_order_1d", "dim_user"],
}


def build_dbt_manifest() -> dict:
    nodes, parent_map = {}, {}
    for tgt, parents in DBT_TARGETS.items():
        tgt_uid = f"model.ecom.{tgt}"
        nodes[tgt_uid] = {"resource_type": "model", "name": tgt,
                          "materialized": "view", "database": "ecom_dw",
                          "schema": "ads", "alias": tgt}
        parent_map[tgt_uid] = []
        for p in parents:
            src_uid = f"model.ecom.{p}"
            nodes[src_uid] = {"resource_type": "model", "name": p,
                              "materialized": "table", "database": "ecom_dw",
                              "schema": "dws", "alias": p}
            parent_map[tgt_uid].append(src_uid)
    return {
        "metadata": {"dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
                     "project_name": "ecom_dw"},
        "nodes": nodes,
        "sources": {},
        "parent_map": parent_map,
    }


# ---------------------------------------------------------------- 主流程

def main() -> int:
    t0 = time.time()
    print("== 1. 创建项目 ==")
    proj = must("POST", "/api/projects", {
        "project_key": "PRJ_ECOM", "name": "电商数据中台",
        "description": "电商行业数仓演示项目：交易/用户/商品/营销/流量/物流/服务/财务八域，"
                       "ODS-DWD-DIM-DWS-ADS 五层架构。",
    }, what="创建项目")
    if not proj:
        print("项目创建失败（可能已存在），终止。")
        return 1
    pid = proj["id"]
    print(f"  项目 #{pid} {proj['name']}")

    print("== 2. 创建主题域 ==")
    area_ids = {}
    for akey, (aname, adesc) in AREAS.items():
        a = must("POST", f"/api/projects/{pid}/subject-areas", {
            "area_key": akey, "name": aname, "description": adesc,
            "owner": "data.architect"}, what=f"主题域 {akey}")
        if a:
            area_ids[akey] = a["id"]
    print(f"  {len(area_ids)}/{len(AREAS)} 个主题域")

    print("== 3. 创建业务需求 ==")
    req_ids = {}
    for (rkey, title, domain, prio, status, desc, accept, _mks) in REQS:
        r = must("POST", f"/api/projects/{pid}/requirements", {
            "requirement_key": rkey, "title": title, "business_domain": domain,
            "description": desc, "acceptance_criteria": accept,
            "priority": prio, "status": status, "owner": "business.owner",
        }, what=f"需求 {rkey}")
        if r:
            req_ids[rkey] = r["id"]
    print(f"  {len(req_ids)}/{len(REQS)} 个需求")

    print(f"== 4. 创建模型（{len(MODELS)} 个，含字段） ==")
    model_ids: dict[str, int] = {}
    for spec in MODELS:
        body = {
            "model_key": spec["key"], "name": spec["name"],
            "description": spec["desc"], "layer": spec["layer"],
            "model_type": spec["mtype"], "target_engine": spec["engine"],
            "subject_area_id": area_ids.get(spec["area"]),
            "requirement_ids": [req_ids[k] for k in spec.get("reqs", []) if k in req_ids],
            "fields": spec["fields"],
        }
        m = must("POST", f"/api/projects/{pid}/models", body, what=f"模型 {spec['key']}")
        if m:
            model_ids[spec["key"]] = m["id"]
    print(f"  {len(model_ids)}/{len(MODELS)} 个模型就绪")

    # 第二遍建表级约束与索引：约束/索引仅草稿态可建（_require_draft），
    # 且外键的 referenced_model_id 需在全部模型就绪后才能解析（前向引用）。
    print("== 4b. 创建表级约束与索引（第二遍，全部草稿态） ==")
    cons_cnt = idx_cnt = 0
    for spec in MODELS:
        mid = model_ids.get(spec["key"])
        if not mid:
            continue
        for c in spec.get("constraints", []):
            cb = {"constraint_name": c["name"], "constraint_type": c["type"],
                  "field_codes": c.get("fields", []), "expression": c.get("expression", ""),
                  "referenced_model_id": model_ids.get(c.get("ref_model", "")),
                  "referenced_field_codes": c.get("ref_fields", []),
                  "on_delete": c.get("on_delete", "NO ACTION"),
                  "description": c.get("desc", "")}
            if soft("POST", f"/api/models/{mid}/constraints", cb,
                    what=f"{spec['key']} 约束 {c['name']}"):
                cons_cnt += 1
        for ix in spec.get("indexes", []):
            ib = {"index_name": ix["name"], "index_type": ix.get("type", "btree"),
                  "field_codes": ix["fields"], "is_unique": ix.get("unique", False),
                  "description": ix.get("desc", "")}
            if soft("POST", f"/api/models/{mid}/indexes", ib,
                    what=f"{spec['key']} 索引 {ix['name']}"):
                idx_cnt += 1
    print(f"  约束 {cons_cnt} 个 / 索引 {idx_cnt} 个")

    print("== 5. 创建模型关系 ==")
    rel_count = 0
    for spec in MODELS:
        src_id = model_ids.get(spec["key"])
        if not src_id:
            continue
        for (tgt, rtype, sf, tf, card, desc) in spec.get("links", []):
            tgt_id = model_ids.get(tgt)
            if not tgt_id:
                STATS["skip"] += 1
                print(f"  [跳过] 关系目标缺失 {spec['key']} -> {tgt}")
                continue
            ok, _ = call("POST", f"/api/models/{src_id}/relationships", {
                "target_model_id": tgt_id, "relationship_type": rtype,
                "source_fields": sf, "target_fields": tf,
                "cardinality": card, "description": desc})
            if ok:
                rel_count += 1
            else:
                STATS["skip"] += 1
    print(f"  {rel_count} 条关系")

    print("== 6. 生命周期流转（校验→评审→发布） ==")
    pub = appr = rev = 0
    for spec in MODELS:
        mid = model_ids.get(spec["key"])
        if not mid:
            continue
        target = spec["status"]
        if target == "draft":
            continue
        cur = soft("GET", f"/api/models/{mid}")
        if not cur or cur.get("status") != "draft":
            continue
        ok, r = call("POST", f"/api/models/{mid}/submit-review")
        if not ok:
            if spec.get("problem"):
                print(f"  [预期拦截] {spec['key']} 问题草稿被校验拒绝 -> {r}")
            else:
                print(f"  [提交失败] {spec['key']} -> {r}")
            continue
        rev += 1
        if target == "in_review":
            continue
        ok, _ = call("POST", f"/api/models/{mid}/approve", {"comment": "演示数据批量评审通过"})
        if not ok:
            continue
        appr += 1
        if target == "approved":
            continue
        ok, r = call("POST", f"/api/models/{mid}/publish",
                     {"comment": f"{spec['name']} 初始发布 V1"})
        if ok:
            pub += 1
        else:
            print(f"  [发布失败] {spec['key']} -> {r}")
    print(f"  提交评审 {rev} / 通过 {appr} / 发布 {pub}")

    print("== 7. 二次变更（多版本 diff 演示） ==")
    v2_count = 0
    for spec in MODELS:
        if not spec.get("v2"):
            continue
        mid = model_ids.get(spec["key"])
        if not mid:
            continue
        cur = soft("GET", f"/api/models/{mid}")
        if not cur or cur.get("status") != "published":
            continue
        ok, _ = call("POST", f"/api/models/{mid}/start-change")
        if not ok:
            continue
        cur = soft("GET", f"/api/models/{mid}")
        if not cur:
            continue
        # 在 V1 字段集上应用 v2 变更（spec 字段即 FieldIn 形状，避免回读键名漂移）
        new_fields = [dict(f) for f in spec["fields"]]
        for mod_code, attrs in (spec["v2"].get("modify") or {}).items():
            for f_ in new_fields:
                if f_["field_code"] == mod_code:
                    f_.update(attrs)
        new_fields.extend(spec["v2"].get("add", []))
        ok, r = call("PUT", f"/api/models/{mid}", {
            "model_key": cur["model_key"], "name": cur["name"],
            "description": cur["description"], "layer": cur["layer"],
            "model_type": cur["model_type"], "target_engine": cur["target_engine"],
            "subject_area_id": cur.get("subject_area_id"),
            "requirement_ids": [req_ids[k] for k in spec.get("reqs", []) if k in req_ids],
            "fields": new_fields, "expected_revision": cur["edit_revision"]})
        if not ok:
            print(f"  [变更失败] {spec['key']} -> {r}")
            continue
        ok, _ = call("POST", f"/api/models/{mid}/submit-review")
        if not ok:
            continue
        ok, _ = call("POST", f"/api/models/{mid}/approve", {"comment": "变更评审通过"})
        if not ok:
            continue
        ok, _ = call("POST", f"/api/models/{mid}/publish",
                     {"comment": spec["v2"]["note"]})
        if ok:
            v2_count += 1
            print(f"  {spec['key']}: {spec['v2']['note']}")
    print(f"  {v2_count} 个模型完成 V2 再发布")

    # 制造一个"变更中"草稿：已发布模型 start-change 后做模型级修改并保持草稿，
    # 使影响分析（draft vs 最近发布快照）有模型级变更可向下游传播。
    print("== 7b. 制造变更中草稿（影响分析演示） ==")
    spec = next(s for s in MODELS if s["key"] == "dwd_ticket_detail")
    mid = model_ids[spec["key"]]
    cur = soft("GET", f"/api/models/{mid}")
    if cur and cur.get("status") == "published":
        ok, _ = call("POST", f"/api/models/{mid}/start-change")
        if ok:
            cur = soft("GET", f"/api/models/{mid}")
            ok, r = call("PUT", f"/api/models/{mid}", {
                "model_key": cur["model_key"], "name": cur["name"],
                "description": cur["description"] + "（变更中：补充解决时长口径）",
                "layer": cur["layer"], "model_type": cur["model_type"],
                "target_engine": cur["target_engine"],
                "subject_area_id": cur.get("subject_area_id"),
                "requirement_ids": [req_ids[k] for k in spec.get("reqs", []) if k in req_ids],
                "fields": [dict(f) for f in spec["fields"]],
                "expected_revision": cur["edit_revision"]})
            if ok:
                print(f"  dwd_ticket_detail: 已进入变更中（草稿，未再提交评审）")
            else:
                print(f"  [变更失败] dwd_ticket_detail -> {r}")

    print("== 8. 导入外部血缘 ==")
    r = soft("POST", f"/api/projects/{pid}/lineage/import", {
        "source_system": "airflow", "run_id": "airflow-daily-20261027",
        "scan_mode": "incremental", "scope_key": "etl-daily", "complete_scan": False,
        "observed_at": "2026-10-27T01:30:00+00:00",
        "edges": AIRFLOW_ETL_EDGES}, what="airflow ETL 血缘")
    if r:
        print(f"  airflow/etl-daily: 收到 {r['received']} 新增 {r['inserted']}")
    r = soft("POST", f"/api/projects/{pid}/lineage/import", {
        "source_system": "airflow", "run_id": "airflow-sched-20261027",
        "scan_mode": "incremental", "scope_key": "scheduler", "complete_scan": False,
        "observed_at": "2026-10-27T02:00:00+00:00",
        "edges": AIRFLOW_SCHED_EDGES}, what="airflow 调度血缘")
    if r:
        print(f"  airflow/scheduler: 收到 {r['received']} 新增 {r['inserted']}")
    r = soft("POST", f"/api/projects/{pid}/lineage/import", {
        "source_system": "superset", "run_id": "bi-daily-20261027",
        "scan_mode": "incremental", "scope_key": "bi-daily", "complete_scan": False,
        "observed_at": "2026-10-27T03:00:00+00:00",
        "edges": BI_EDGES}, what="superset BI 血缘")
    if r:
        print(f"  superset/bi-daily: 收到 {r['received']} 新增 {r['inserted']}")
    r = soft("POST", f"/api/projects/{pid}/lineage/import", {
        "source_system": "manual-registry", "run_id": "manual-reg-20261027",
        "scan_mode": "incremental", "scope_key": "default", "complete_scan": False,
        "edges": MANUAL_EDGES}, what="人工登记血缘")
    if r:
        print(f"  manual-registry: 收到 {r['received']} 新增 {r['inserted']}")
    # dbt manifest 连接器
    r = soft("POST", f"/api/projects/{pid}/lineage/import/dbt-manifest", {
        "manifest": build_dbt_manifest(), "run_id": "dbt-run-20261027",
        "source_system": "dbt", "scope_key": "ecom_dw"}, what="dbt manifest")
    if r:
        print(f"  dbt manifest: 收到 {r['received']} 新增 {r['inserted']}")
    # 故意失败的批次（引用不存在的模型）：演示 import-runs 失败态
    r = soft("POST", f"/api/projects/{pid}/lineage/import", {
        "source_system": "airflow", "run_id": "airflow-retry-bad-20261027",
        "scan_mode": "incremental", "scope_key": "etl-retry", "complete_scan": False,
        "edges": [{"source_ref": "airflow://ods_missing=>dwd_order_detail",
                   "source_model_key": "ods_missing_table",
                   "target_model_key": "dwd_order_detail",
                   "lineage_type": "etl_data_flow", "confidence": 90}]},
        what="故意失败批次")
    print("  airflow/etl-retry: 预期失败（引用不存在模型），已落 failed 批次")

    print("== 9. 汇总验证 ==")
    time.sleep(0.5)
    ok, g = call("GET", f"/api/projects/{pid}/model-graph?view=current")
    if ok:
        print(f"  current 视图：节点 {len(g['nodes'])} / 设计边 {len(g['edges'])}")
    ok, gl = call("GET", f"/api/projects/{pid}/model-graph?view=lineage")
    if ok:
        ext = gl.get("external_edges", [])
        print(f"  lineage 视图：节点 {len(gl['nodes'])} / 设计边 {len(gl['edges'])} / "
              f"外部边 {len(ext)}")
    ok, snaps = call("GET", f"/api/projects/{pid}/model-graph/snapshots")
    if ok:
        items = snaps if isinstance(snaps, list) else snaps.get("items", [])
        print(f"  图谱快照：{len(items)} 个（每次发布一个）")
    ok, runs = call("GET", f"/api/projects/{pid}/lineage/import-runs")
    if ok:
        items = runs if isinstance(runs, list) else runs.get("items", [])
        print(f"  导入批次：{len(items)} 个")
    # 状态分布
    ok, models = call("GET", f"/api/projects/{pid}/models")
    if ok:
        dist: dict[str, int] = {}
        for m in models:
            dist[m["status"]] = dist.get(m["status"], 0) + 1
        print(f"  模型状态分布：{dist}")

    print(f"\n===== 完成：API {STATS['api']} 次 / 跳过 {STATS['skip']} / 失败 {STATS['fail']}，"
          f"耗时 {time.time() - t0:.1f}s =====")
    if FAILURES:
        print("失败明细（前 20）：")
        for f_ in FAILURES[:20]:
            print("  - " + f_)
    return 0 if not FAILURES else 2


if __name__ == "__main__":
    sys.exit(main())
