#!/usr/bin/env python3
"""seed_ecommerce_demo.py 规格静态校验器（不触网，不写库）。

在运行生成脚本前，对 MODELS/REQS/AREAS 做引用一致性检查：
- 模型键唯一；links 目标存在、字段成对存在、无自引用、无重复 (src,tgt,type)
- constraints 字段存在；FK 目标模型/字段存在；CHECK 不带字段列表
- indexes 字段存在；v2 add 不重名、modify 指向既有字段
- reqs/area 键存在；layer/status/mtype/engine 取值合法
- 统计分层/状态/关系类型/基数分布
"""
from __future__ import annotations

import importlib.util
import sys
from collections import Counter

SPEC_PATH = sys.argv[1] if len(sys.argv) > 1 else "scripts/seed_ecommerce_demo.py"

spec = importlib.util.spec_from_file_location("seed_spec", SPEC_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

MODELS, REQS, AREAS = mod.MODELS, mod.REQS, mod.AREAS

LAYERS = {"ODS", "DWD", "DWS", "ADS", "DIM"}
STATUSES = {"draft", "in_review", "approved", "published"}
MTYPES = {"TABLE", "FACT", "DIMENSION", "VIEW"}
ENGINES = {"postgresql", "mysql", "hive"}
RTYPES = {"depends_on", "joins_to", "derived_from", "references"}
CARDS = {"unspecified", "one_to_one", "one_to_many", "many_to_one", "many_to_many"}
REQ_KEYS = {r[0] for r in REQS}

errors: list[str] = []
keys = [m["key"] for m in MODELS]
dup_keys = [k for k, c in Counter(keys).items() if c > 1]
if dup_keys:
    errors.append(f"模型键重复: {dup_keys}")

by_key = {m["key"]: m for m in MODELS}
field_sets = {m["key"]: {f["field_code"] for f in m["fields"]} for m in MODELS}

for m in MODELS:
    k = m["key"]
    if m["layer"] not in LAYERS:
        errors.append(f"{k}: 非法 layer {m['layer']}")
    if m["status"] not in STATUSES:
        errors.append(f"{k}: 非法 status {m['status']}")
    if m["mtype"] not in MTYPES:
        errors.append(f"{k}: 非法 mtype {m['mtype']}")
    if m["engine"] not in ENGINES:
        errors.append(f"{k}: 非法 engine {m['engine']}")
    if m["area"] not in AREAS:
        errors.append(f"{k}: 非法 area {m['area']}")
    for rq in m.get("reqs", []):
        if rq not in REQ_KEYS:
            errors.append(f"{k}: 需求键不存在 {rq}")
    fset = field_sets[k]
    fdup = [c for c, n in Counter(f["field_code"] for f in m["fields"]).items() if n > 1]
    if fdup:
        errors.append(f"{k}: 字段编码重复 {fdup}")
    # 表级约束
    for c in m.get("constraints", []):
        for fc in c.get("fields", []):
            if fc not in fset:
                errors.append(f"{k}: 约束 {c['name']} 字段不存在 {fc}")
        if c["type"] == "check" and c.get("fields"):
            errors.append(f"{k}: CHECK 约束 {c['name']} 不应带字段列表")
        if c["type"] == "foreign_key":
            rm = c.get("ref_model")
            if rm not in by_key:
                errors.append(f"{k}: FK {c['name']} 目标模型不存在 {rm}")
            else:
                for rc in c.get("ref_fields", []):
                    if rc not in field_sets[rm]:
                        errors.append(f"{k}: FK {c['name']} 目标字段不存在 {rm}.{rc}")
            if len(c.get("fields", [])) != len(c.get("ref_fields", [])):
                errors.append(f"{k}: FK {c['name']} 字段对数量不一致")
        if c["type"] == "primary_key" and len(c.get("fields", [])) < 2:
            errors.append(f"{k}: 表级主键 {c['name']} 少于 2 字段")
        if c["type"] == "primary_key" and any(f.get("is_primary_key") for f in m["fields"]):
            errors.append(f"{k}: 表级主键与字段级主键冲突")
    # 索引
    for ix in m.get("indexes", []):
        if ix.get("type") not in {"btree", "hash", "gin"}:
            errors.append(f"{k}: 索引 {ix['name']} 类型非法 {ix.get('type')}")
        for fc in ix["fields"]:
            if fc not in fset:
                errors.append(f"{k}: 索引 {ix['name']} 字段不存在 {fc}")
    # v2
    v2 = m.get("v2") or {}
    for add in v2.get("add", []):
        if add["field_code"] in fset:
            errors.append(f"{k}: v2 新增字段重名 {add['field_code']}")
    for mc in (v2.get("modify") or {}):
        if mc not in fset:
            errors.append(f"{k}: v2 修改字段不存在 {mc}")
    # 关系
    seen_rel: set[tuple[str, str, str]] = set()
    for (tgt, rtype, sf, tf, card, desc) in m.get("links", []):
        if tgt not in by_key:
            errors.append(f"{k}: 关系目标不存在 {tgt}")
            continue
        if tgt == k:
            errors.append(f"{k}: 自引用关系被平台拒绝 -> {tgt}")
        if rtype not in RTYPES:
            errors.append(f"{k}: 关系类型非法 {rtype}")
        if card not in CARDS:
            errors.append(f"{k}: 基数非法 {card}")
        if len(sf) != len(tf):
            errors.append(f"{k}->{tgt}: 关系字段对数量不一致 {sf} vs {tf}")
        for fc in sf:
            if fc not in fset:
                errors.append(f"{k}->{tgt} ({rtype}): 源字段不存在 {fc}")
        for fc in tf:
            if fc not in field_sets[tgt]:
                errors.append(f"{k}->{tgt} ({rtype}): 目标字段不存在 {fc}")
        rk = (k, tgt, rtype)
        if rk in seen_rel:
            errors.append(f"{k}->{tgt}: 重复关系类型 {rtype}")
        seen_rel.add(rk)

# 需求引用的模型键应存在
for (rkey, _t, _d, _p, _s, _desc, _acc, mks) in REQS:
    for mk in mks:
        if mk not in by_key:
            errors.append(f"需求 {rkey}: 引用模型不存在 {mk}")

# ---- 统计 ----
layer_dist = Counter(m["layer"] for m in MODELS)
status_dist = Counter(m["status"] for m in MODELS)
engine_dist = Counter(m["engine"] for m in MODELS)
mtype_dist = Counter(m["mtype"] for m in MODELS)
rtype_dist: Counter[str] = Counter()
card_dist: Counter[str] = Counter()
total_links = 0
for m in MODELS:
    for (tgt, rtype, sf, tf, card, desc) in m.get("links", []):
        if tgt in by_key:
            rtype_dist[rtype] += 1
            card_dist[card] += 1
            total_links += 1
cons_total = sum(len(m.get("constraints", [])) for m in MODELS)
cons_type = Counter(c["type"] for m in MODELS for c in m.get("constraints", []))
idx_total = sum(len(m.get("indexes", [])) for m in MODELS)
idx_type = Counter(ix.get("type", "btree") for m in MODELS for ix in m.get("indexes", []))
v2_models = [m["key"] for m in MODELS if m.get("v2")]
problem = [m["key"] for m in MODELS if m.get("problem")]
field_total = sum(len(m["fields"]) for m in MODELS)

print(f"模型 {len(MODELS)} 个 / 字段 {field_total} 个 / 关系 {total_links} 条 / "
      f"约束 {cons_total} 个 / 索引 {idx_total} 个")
print(f"分层: {dict(layer_dist)}")
print(f"状态: {dict(status_dist)}")
print(f"引擎: {dict(engine_dist)}  类型: {dict(mtype_dist)}")
print(f"关系类型: {dict(rtype_dist)}")
print(f"基数: {dict(card_dist)}")
print(f"约束类型: {dict(cons_type)}  索引类型: {dict(idx_type)}")
print(f"V2 模型 {len(v2_models)}: {v2_models}")
print(f"问题草稿 {len(problem)}: {problem}")
print(f"需求 {len(REQS)} 个 / 主题域 {len(AREAS)} 个")

if errors:
    print(f"\n===== 发现 {len(errors)} 个问题 =====")
    for e in errors:
        print("  - " + e)
    sys.exit(1)
print("\n===== 静态校验全部通过 =====")
