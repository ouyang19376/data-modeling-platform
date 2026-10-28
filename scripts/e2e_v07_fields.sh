#!/bin/bash
# V0.7 字段设计器浏览器 E2E 实测（playwright-cli + 系统 Chrome）
# 关键机制：playwright-cli 的 snapshot 把完整 YAML 内联输出到 stdout；
# .playwright-cli/page-*.yml 文件每会话只写一次，不可作为轮询依据。
set -x
cd "C:\Users\54425\.workbuddy\binaries\node\workspace" || exit 1
export NODE_PATH="C:\Users\54425\.workbuddy\binaries\node\workspace\node_modules"
P="C:\Users\54425\.workbuddy\binaries\node\versions\22.22.2-6\node.exe node_modules/@playwright/cli/playwright-cli.js"
OUT="G:/zcode_project/test/.playwright-cli-e2e"
mkdir -p "$OUT"

# 快照：捕获 stdout（内联 YAML）
snap() { $P snapshot 2>&1; }
wait_for() {
  for i in $(seq 1 "${2:-15}"); do
    OUT_S=$(snap)
    if echo "$OUT_S" | grep -q "$1"; then echo "WAIT_OK: $1"; return 0; fi
    sleep 1
  done
  echo "WAIT_TIMEOUT: $1"; return 1
}

# ---------- 1. 打开 + 等待工作台 ----------
$P open "http://127.0.0.1:8000/" >/dev/null 2>&1
wait_for "最近模型" 20 || exit 1
echo "STEP1 dashboard ok"

# ---------- 2. 进入模型中心 ----------
$P click "button[data-view='models']" >/dev/null 2>&1
wait_for "客户维表" 15 || exit 1
snap > "$OUT/02-models.yml"
echo "STEP2 models view ok"

# ---------- 3. 打开 dim_customer（草稿，id=2）编辑器 ----------
$P click "[data-open-model='2']" >/dev/null 2>&1
wait_for "字段设计" 15 || exit 1
snap > "$OUT/03-editor.yml"
echo "STEP3 editor ok"

# ---------- 4. 进入字段设计 tab ----------
$P click ".tab[data-tab='fields']" >/dev/null 2>&1
sleep 1.5
S=$(snap); echo "$S" > "$OUT/04-fields-tab.yml"
echo "STEP4 fields-tab: config_btns=$(echo "$S" | grep -c 'button "配置"') unique_hdr=$(echo "$S" | grep -c 'columnheader "唯一"') pk_hdr=$(echo "$S" | grep -c 'columnheader "主键"')"

# ---------- 5. 打开第一个字段的详细配置 ----------
$P click "[data-fld-config='0']" >/dev/null 2>&1
sleep 1
S=$(snap); echo "$S" > "$OUT/05-detail-open.yml"
echo "STEP5 detail-open: preview=$(echo "$S" | grep -oE 'BIGINT' | head -1) pk=$(echo "$S" | grep 'checkbox "主键"') uniq=$(echo "$S" | grep 'checkbox "唯一"') null=$(echo "$S" | grep 'checkbox "可空"')"

# ---------- 6. 类型切换联动：BIGINT -> VARCHAR（应自动建议长度 255） ----------
$P select "#fd-type" "VARCHAR" >/dev/null 2>&1
sleep 1
S=$(snap)
echo "STEP6 type-switch: preview=$(echo "$S" | grep -oE 'VARCHAR\(255\)' | head -1) len_val=$(echo "$S" | grep -oE 'textbox[^:]*: 255' | head -1)"

# ---------- 7. 勾选唯一 ----------
$P check "#fd-unique" >/dev/null 2>&1
sleep 0.8
S=$(snap)
echo "STEP7 unique: $(echo "$S" | grep 'checkbox "唯一"')"

# ---------- 8. 主键/可空互斥联动 ----------
$P check "#fd-nullable" >/dev/null 2>&1
sleep 0.8
S=$(snap)
echo "STEP8a nullable-on: $(echo "$S" | grep -E 'checkbox "(主键|可空)"' | tr '\n' ' ')"
$P check "#fd-pk" >/dev/null 2>&1
sleep 0.8
S=$(snap)
echo "STEP8b pk-on: $(echo "$S" | grep -E 'checkbox "(主键|可空)"' | tr '\n' ' ')"

# ---------- 9. 关闭详情面板 ----------
$P click "#fd-close" >/dev/null 2>&1
sleep 0.8
S=$(snap); echo "$S" > "$OUT/09-detail-closed.yml"
echo "STEP9 detail-closed: detail_gone=$(echo "$S" | grep -c '字段详细配置')"

# ---------- 10. 字段筛选（只影响显示） ----------
$P fill "#fld-filter" "level" >/dev/null 2>&1
sleep 0.8
S=$(snap)
echo "STEP10 filter-level: visible_rows=$(echo "$S" | grep -c 'button "配置"')"
$P fill "#fld-filter" "" >/dev/null 2>&1
sleep 0.5
S=$(snap)
echo "STEP10b filter-cleared: visible_rows=$(echo "$S" | grep -c 'button "配置"')"

# ---------- 11. TSV 批量导入（14 列模板，追加模式） ----------
$P click "#fld-import" >/dev/null 2>&1
sleep 1
TSV=$(printf 'field_code\tfield_name\tlogical_type\tlength\tprecision\tscale\tis_primary_key\tis_unique\tnullable\tdefault_value\tdescription\tsource_table\tsource_field\tsensitivity\npay_channel\t支付渠道\tVARCHAR\t32\t\t\tfalse\tfalse\ttrue\t\t支付渠道编码\tods_order\tpay_channel\tinternal\norder_remark\t订单备注\tSTRING\t\t\t\tfalse\tfalse\ttrue\t\t订单备注\t\t\tinternal')
$P fill "#tsv-area" "$TSV" >/dev/null 2>&1
sleep 1
S=$(snap); echo "$S" > "$OUT/11-import-preview.yml"
echo "STEP11 import-preview: $(echo "$S" | grep -oE '解析到 [0-9]+ 行（[^）]*）' | head -1)"
$P click "#modal-footer button.btn-primary" >/dev/null 2>&1
sleep 1.5
S=$(snap)
echo "STEP11b imported: toast=$(echo "$S" | grep -oE '已导入 [0-9]+ 行' | head -1) tab_label=$(echo "$S" | grep -oE '字段设计 \([0-9]+\)' | head -1)"

# ---------- 12. 常用技术字段（取消勾选 id，避免双主键） ----------
$P click "#fld-common" >/dev/null 2>&1
sleep 1
S=$(snap); echo "$S" > "$OUT/12-common-fields.yml"
echo "STEP12 common-fields: checkboxes=$(echo "$S" | grep -c 'checkbox')"
$P uncheck "[data-common='0']" >/dev/null 2>&1
sleep 0.5
$P click "#modal-footer button.btn-primary" >/dev/null 2>&1
sleep 1.5
S=$(snap)
echo "STEP12b inserted: toast=$(echo "$S" | grep -oE '已插入 [0-9]+ 个常用字段' | head -1) tab_label=$(echo "$S" | grep -oE '字段设计 \([0-9]+\)' | head -1)"

# ---------- 13. 给 customer_name 勾选唯一（验证 DDL UNIQUE 输出） ----------
$P click "[data-fld-config='1']" >/dev/null 2>&1
sleep 1
$P check "#fd-unique" >/dev/null 2>&1
sleep 0.5
$P click "#fd-close" >/dev/null 2>&1
sleep 0.8
S=$(snap)
echo "STEP13 name-unique: $(echo "$S" | grep -c 'button "配置"') rows"

# ---------- 14. 保存草稿 ----------
$P click "#fld-save" >/dev/null 2>&1
sleep 3
S=$(snap); echo "$S" > "$OUT/14-after-save.yml"
echo "STEP14 saved: toast=$(echo "$S" | grep -oE '草稿已保存' | head -1) rev=$(echo "$S" | grep -oE '修订 R[0-9]+' | head -1)"

# ---------- 15. 运行校验 ----------
$P click "#fld-validate" >/dev/null 2>&1
sleep 3
S=$(snap); echo "$S" > "$OUT/15-validation.yml"
echo "STEP15 validation: $(echo "$S" | grep -oE '校验结果|通过|未通过|PK_UNIQUE_REDUNDANT|阻断错误 [0-9]+|警告 [0-9]+' | sort -u | tr '\n' ' ')"

# ---------- 16. DDL 预览（验证 UNIQUE 约束输出） ----------
$P click ".tab[data-tab='ddl']" >/dev/null 2>&1
sleep 1
$P click "#ddl-run" >/dev/null 2>&1
sleep 2
S=$(snap); echo "$S" > "$OUT/16-ddl.yml"
echo "STEP16 ddl: $(echo "$S" | grep -oE 'UNIQUE \("[a-z_]+"\)|PRIMARY KEY \("[a-z_]+"\)|CREATE TABLE' | sort -u | tr '\n' ' ')"

# ---------- 17. 截图 ----------
$P screenshot >/dev/null 2>&1
ls -t .playwright-cli/*.png 2>/dev/null | head -1
echo "E2E_DONE"
