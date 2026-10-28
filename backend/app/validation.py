"""数据建模中心 MVP — 规范校验引擎（V0.8 规则目录）。

规则目录（设计文档 Table 13/28，错误阻断 / 警告两级），校验执行返回统一结构：
rule_id、severity、object_type、object_id、message、suggestion、location。

阻断错误必须修正后才能提交评审/发布；警告用于提高模型质量，当前不阻断发布。
发布阻断规则不可通过普通用户的"忽略"动作绕过；校验结果是快照证据。

V0.8 新增表级约束/索引规则：表级主键与字段级主键互斥、约束/索引引用字段
存在性、CHECK 表达式受限语法重放、索引引擎能力矩阵、外键目标键唯一性
（跨模型预计算）、Hive 逻辑约束提示。

默认值白名单（文档 6.1）同时被 DDL 适配器复用：默认值最终会拼接进 DDL，
因此采用白名单，而不是尝试用黑名单删掉危险片段。
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from .models import DataModel, ModelConstraint, ModelField, ModelIndex

# ---------------------------------------------------------------- 规则元数据

RULES: dict[str, dict[str, Any]] = {
    # ---- 错误（阻断评审/发布）----
    "MODEL_NAME_REQUIRED": {
        "name": "模型名称非空",
        "severity": "error",
        "blocking": True,
        "scope": "model",
    },
    "INVALID_TABLE_NAME": {
        "name": "模型编码（物理表名）标识符合法",
        "severity": "error",
        "blocking": True,
        "scope": "model",
    },
    "NO_FIELDS": {
        "name": "模型至少包含一个字段",
        "severity": "error",
        "blocking": True,
        "scope": "model",
    },
    "INVALID_FIELD_CODE": {
        "name": "字段编码符合物理标识符规则",
        "severity": "error",
        "blocking": True,
        "scope": "field",
    },
    "DUPLICATE_FIELD_CODE": {
        "name": "同一模型内字段编码不重复（大小写不敏感）",
        "severity": "error",
        "blocking": True,
        "scope": "field",
    },
    "PK_NULLABLE": {
        "name": "主键字段不可空",
        "severity": "error",
        "blocking": True,
        "scope": "field",
    },
    "VARCHAR_LENGTH_REQUIRED": {
        "name": "VARCHAR 必须提供长度",
        "severity": "error",
        "blocking": True,
        "scope": "field",
    },
    "DECIMAL_PRECISION_REQUIRED": {
        "name": "DECIMAL 必须提供总精度",
        "severity": "error",
        "blocking": True,
        "scope": "field",
    },
    "DECIMAL_SCALE_REQUIRED": {
        "name": "DECIMAL 必须提供小数位（允许 0）",
        "severity": "error",
        "blocking": True,
        "scope": "field",
    },
    "DECIMAL_SCALE_GT_PRECISION": {
        "name": "DECIMAL 小数位不得大于总精度",
        "severity": "error",
        "blocking": True,
        "scope": "field",
    },
    "UNSAFE_DEFAULT_VALUE": {
        "name": "默认值在安全白名单内",
        "severity": "error",
        "blocking": True,
        "scope": "field",
    },
    # ---- 警告（不阻断，提示提高模型质量）----
    "FIELD_DESCRIPTION_MISSING": {
        "name": "字段业务说明完整性",
        "severity": "warning",
        "blocking": False,
        "scope": "field",
    },
    "INCOMPLETE_SOURCE_MAPPING": {
        "name": "来源表与来源字段成对填写",
        "severity": "warning",
        "blocking": False,
        "scope": "field",
    },
    "PK_UNIQUE_REDUNDANT": {
        "name": "主键与唯一约束不冗余",
        "severity": "warning",
        "blocking": False,
        "scope": "field",
    },
    "PRIMARY_KEY_RECOMMENDED": {
        "name": "DWD/DIM 建议明确主键或业务唯一键",
        "severity": "warning",
        "blocking": False,
        "scope": "model",
    },
    # ---- V0.8 表级约束与索引（错误，阻断）----
    "TABLE_PK_FIELD_PK_CONFLICT": {
        "name": "表级主键与字段级主键互斥",
        "severity": "error",
        "blocking": True,
        "scope": "model",
    },
    "CONSTRAINT_FIELD_NOT_FOUND": {
        "name": "表级约束引用的字段必须存在",
        "severity": "error",
        "blocking": True,
        "scope": "constraint",
    },
    "CHECK_EXPRESSION_INVALID": {
        "name": "CHECK 表达式符合受限语法且引用字段存在",
        "severity": "error",
        "blocking": True,
        "scope": "constraint",
    },
    "INDEX_FIELD_NOT_FOUND": {
        "name": "索引引用的字段必须存在",
        "severity": "error",
        "blocking": True,
        "scope": "index",
    },
    "INDEX_ENGINE_UNSUPPORTED": {
        "name": "索引类型被目标引擎支持",
        "severity": "error",
        "blocking": True,
        "scope": "index",
    },
    # ---- V0.8 表级约束与索引（警告）----
    "FK_TARGET_KEY_NOT_UNIQUE": {
        "name": "外键目标键声明唯一性",
        "severity": "warning",
        "blocking": False,
        "scope": "constraint",
    },
    "HIVE_LOGICAL_CONSTRAINT": {
        "name": "Hive 表级约束仅逻辑元数据",
        "severity": "warning",
        "blocking": False,
        "scope": "model",
    },
}

SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")

# 常见 SQL 保留字（基础集，各引擎完整清单由适配器补充）
RESERVED_WORDS = {
    "select", "from", "where", "table", "index", "order", "group", "by",
    "insert", "update", "delete", "create", "drop", "alter", "join", "on",
    "and", "or", "not", "null", "default", "primary", "key", "foreign",
    "references", "unique", "check", "constraint", "int", "integer", "date",
    "timestamp", "boolean", "decimal", "varchar", "user", "grant", "values",
    "limit", "offset", "union", "all", "distinct", "having", "asc", "desc",
}

VALID_LOGICAL_TYPES = {
    "STRING", "VARCHAR", "INTEGER", "BIGINT", "DECIMAL", "DATE",
    "TIMESTAMP", "BOOLEAN",
}

# ---------------------------------------------------------------- 默认值白名单

# 与 model_fields.default_value 的 String(256) 列宽一致
DEFAULT_VALUE_MAX_LENGTH = 256

_NUMERIC = re.compile(r"^-?\d+(\.\d+)?$")
_STRING_LITERAL = re.compile(r"^'[^']*'$")
_ALLOWED_DEFAULT_KEYWORDS = {
    "NULL", "TRUE", "FALSE", "CURRENT_TIMESTAMP", "CURRENT_DATE",
}


def normalize_default_value(value: str | None) -> str | None:
    """默认值白名单归一化。

    返回可安全拼接进 DDL 的字符串；不在白名单内（或超长）返回 None。
    白名单（文档 6.1）：整数/小数、TRUE/FALSE/NULL、CURRENT_TIMESTAMP/
    CURRENT_DATE，以及长度受限且不含内部单引号的简单单引号常量。
    业务若需要 JSON 函数、序列调用等复杂表达式，应新增按引擎受控表达式
    配置与解析校验，而不是放开白名单。
    """
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
    if len(v) > DEFAULT_VALUE_MAX_LENGTH:
        return None
    if v.upper() in _ALLOWED_DEFAULT_KEYWORDS:
        return v.upper()
    if _NUMERIC.match(v):
        return v
    if _STRING_LITERAL.match(v):
        return v
    return None


# ---------------------------------------------------------------- CHECK 表达式白名单（V0.8）

# 受限 token：单引号字符串 / 数值 / 标识符或关键字 / 比较与算术运算符 / 括号逗号 / 空白。
# 未覆盖的字符（分号、注释、反引号、点号等）在扫描时留下空隙即判定为非法片段。
_CHECK_TOKEN_RE = re.compile(
    r"""('(?:[^']|'')*'          # 单引号字符串（含 '' 转义）
      | -?\d+(?:\.\d+)?         # 数值字面量
      | [A-Za-z_][A-Za-z0-9_]*  # 标识符 / 关键字
      | <=|>=|<>|!=|=|<|>       # 比较运算符（两字符优先）
      | \+|-|\*|/|%             # 算术运算符
      | [(),]                   # 括号与逗号
      | \s+                     # 空白
    )""",
    re.VERBOSE,
)

# 受限关键字（大小写不敏感）；其余标识符必须命中模型字段编码
CHECK_KEYWORDS = {
    "AND", "OR", "NOT", "IS", "NULL", "IN", "BETWEEN", "LIKE", "TRUE", "FALSE",
}


def validate_check_expression(expression: str, field_codes: set[str]) -> str | None:
    """CHECK 表达式受限语法校验（文档 5.1/5.2）。

    返回 None 表示通过；否则返回可向用户展示的拒绝原因。
    白名单之外一律拒绝：分号、SQL 注释、未知字段、任意函数调用、
    子查询、无法解析的 token。有限 tokenizer，不是完整 SQL parser，
    即使通过也需在目标引擎验证。
    """
    if not expression or not expression.strip():
        return "CHECK 表达式不能为空"
    pos = 0
    tokens: list[str] = []
    for m in _CHECK_TOKEN_RE.finditer(expression):
        if m.start() != pos:
            bad = expression[pos:m.start()].strip()
            return (
                f"CHECK 表达式包含无法解析的片段 '{bad}'"
                "（禁止分号、SQL 注释与任意 SQL 片段）"
            )
        pos = m.end()
        tok = m.group()
        if tok.strip():
            tokens.append(tok)
    if pos != len(expression):
        bad = expression[pos:].strip()
        return (
            f"CHECK 表达式包含无法解析的片段 '{bad}'"
            "（禁止分号、SQL 注释与任意 SQL 片段）"
        )
    if not tokens:
        return "CHECK 表达式不能为空"

    for i, tok in enumerate(tokens):
        if tok.startswith("'"):
            continue  # 字符串字面量
        if re.match(r"^-?\d", tok):
            continue  # 数值字面量
        if re.match(r"^[A-Za-z_]", tok):
            upper = tok.upper()
            if upper in CHECK_KEYWORDS:
                continue
            if tok not in field_codes:
                return f"CHECK 表达式引用了不存在的字段 '{tok}'"
            if i + 1 < len(tokens) and tokens[i + 1] == "(":
                return f"CHECK 表达式不允许函数调用 '{tok}(...)'"
    depth = 0
    for tok in tokens:
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
            if depth < 0:
                return "CHECK 表达式括号不配对"
    if depth != 0:
        return "CHECK 表达式括号不配对"
    return None


# ---------------------------------------------------------------- 索引引擎能力矩阵（V0.8）


def engine_supports_index(engine: str, index_type: str, is_unique: bool) -> tuple[bool, str]:
    """索引类型 × 目标引擎支持矩阵（文档 Table 14）。

    返回 (是否支持, 不支持原因)。Hive 不生成物理索引但允许作为逻辑
    元数据存在，故不限制；MySQL 仅 btree；PostgreSQL 拒绝唯一 GIN。
    """
    if engine == "hive":
        return True, ""
    if index_type == "btree":
        return True, ""
    if engine == "mysql":
        return False, f"MySQL 仅支持 btree 索引（当前类型 {index_type}）"
    if index_type == "gin" and is_unique:
        return False, "PostgreSQL 不支持唯一 GIN 索引"
    return True, ""


# ---------------------------------------------------------------- 校验结果


@dataclass
class Issue:
    rule_id: str
    severity: str
    object_type: str
    object_id: str
    message: str
    suggestion: str
    location: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "object_type": self.object_type,
            "object_id": self.object_id,
            "message": self.message,
            "suggestion": self.suggestion,
            "location": self.location,
        }


@dataclass
class ValidationResult:
    run_id: str
    status: str  # PASSED / FAILED
    summary: dict[str, int]
    issues: list[Issue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "summary": self.summary,
            "issues": [i.to_dict() for i in self.issues],
        }

    @property
    def blocking_errors(self) -> int:
        return sum(
            1
            for i in self.issues
            if i.severity == "error" and RULES.get(i.rule_id, {}).get("blocking")
        )


# ---------------------------------------------------------------- 校验执行


def validate_model(
    model: DataModel,
    fields: list[ModelField],
    engine: str = "postgresql",
    constraints: list[ModelConstraint] | None = None,
    indexes: list[ModelIndex] | None = None,
    fk_at_risk: set[str] | None = None,
) -> ValidationResult:
    """对模型草稿执行完整校验（V0.8 规则目录）。

    逻辑类型合法性由 API Schema 的枚举保证；引擎映射差异在 DDL 生成时暴露。
    constraints / indexes 为 V0.8 表级对象（缺省按空处理，兼容旧调用方）。
    fk_at_risk 为外键约束名集合：目标键未声明唯一性或目标字段缺失
    （跨模型信息由调用方预计算后传入，保持本模块无数据库访问）。
    """
    issues: list[Issue] = []
    model_loc = f"model:{model.model_key}"
    constraints = constraints or []
    indexes = indexes or []
    fk_at_risk = fk_at_risk or set()
    field_code_set = {f.field_code for f in fields}

    # ---- MODEL_NAME_REQUIRED ----
    if not (model.name or "").strip():
        issues.append(Issue(
            "MODEL_NAME_REQUIRED", "error", "model", str(model.id),
            "模型名称为空",
            "填写业务可读的模型名称",
            model_loc,
        ))

    # ---- INVALID_TABLE_NAME ----
    if not SNAKE_CASE.match(model.model_key) or len(model.model_key) > 128:
        issues.append(Issue(
            "INVALID_TABLE_NAME", "error", "model", str(model.id),
            f"模型编码 '{model.model_key}' 不是合法物理标识符"
            "（snake_case，最长 128）",
            "只使用英文字母、数字、下划线，首位不能为数字",
            model_loc,
        ))
    elif model.model_key.lower() in RESERVED_WORDS:
        issues.append(Issue(
            "INVALID_TABLE_NAME", "error", "model", str(model.id),
            f"模型编码 '{model.model_key}' 是 SQL 保留字",
            "更换为非保留字的编码",
            model_loc,
        ))

    # ---- NO_FIELDS ----
    if not fields:
        issues.append(Issue(
            "NO_FIELDS", "error", "model", str(model.id),
            "模型没有字段",
            "至少增加一个字段",
            model_loc,
        ))

    # ---- 字段级规则 ----
    seen_codes: set[str] = set()
    for f in fields:
        loc = f"field:{model.model_key}.{f.field_code}"

        # INVALID_FIELD_CODE
        if not SNAKE_CASE.match(f.field_code) or len(f.field_code) > 128:
            issues.append(Issue(
                "INVALID_FIELD_CODE", "error", "field", str(f.id),
                f"字段编码 '{f.field_code}' 不符合物理标识符规则",
                "只使用英文字母、数字、下划线，首位不能为数字",
                loc,
            ))
        elif f.field_code.lower() in RESERVED_WORDS:
            issues.append(Issue(
                "INVALID_FIELD_CODE", "error", "field", str(f.id),
                f"字段编码 '{f.field_code}' 是 SQL 保留字",
                "更换为非保留字的编码",
                loc,
            ))

        # DUPLICATE_FIELD_CODE（大小写不敏感）
        key = f.field_code.lower()
        if key in seen_codes:
            issues.append(Issue(
                "DUPLICATE_FIELD_CODE", "error", "field", str(f.id),
                f"字段编码 '{f.field_code}' 在模型内重复",
                "保留一个编码，修改另一个编码",
                loc,
            ))
        seen_codes.add(key)

        # PK_NULLABLE
        if f.is_primary_key and f.nullable:
            issues.append(Issue(
                "PK_NULLABLE", "error", "field", str(f.id),
                f"主键字段 '{f.field_code}' 允许为空",
                "关闭可空；UI 勾选主键时会自动联动",
                loc,
            ))

        # VARCHAR_LENGTH_REQUIRED
        if f.logical_type == "VARCHAR" and f.length is None:
            issues.append(Issue(
                "VARCHAR_LENGTH_REQUIRED", "error", "field", str(f.id),
                f"字段 '{f.field_code}' 为 VARCHAR 但没有长度",
                "在字段配置中填写 length（1-65535）",
                loc,
            ))

        # DECIMAL_PRECISION_REQUIRED / DECIMAL_SCALE_REQUIRED
        if f.logical_type == "DECIMAL":
            if f.precision is None:
                issues.append(Issue(
                    "DECIMAL_PRECISION_REQUIRED", "error", "field", str(f.id),
                    f"字段 '{f.field_code}' 为 DECIMAL 但没有总精度",
                    "填写 precision（1-38）",
                    loc,
                ))
            if f.scale is None:
                issues.append(Issue(
                    "DECIMAL_SCALE_REQUIRED", "error", "field", str(f.id),
                    f"字段 '{f.field_code}' 为 DECIMAL 但没有小数位",
                    "填写 scale（0-18，允许 0）",
                    loc,
                ))
            # DECIMAL_SCALE_GT_PRECISION
            if (
                f.precision is not None
                and f.scale is not None
                and f.scale > f.precision
            ):
                issues.append(Issue(
                    "DECIMAL_SCALE_GT_PRECISION", "error", "field", str(f.id),
                    f"字段 '{f.field_code}' 小数位 {f.scale} 大于总精度 "
                    f"{f.precision}",
                    "调整为 scale <= precision",
                    loc,
                ))

        # UNSAFE_DEFAULT_VALUE
        if f.default_value and normalize_default_value(f.default_value) is None:
            issues.append(Issue(
                "UNSAFE_DEFAULT_VALUE", "error", "field", str(f.id),
                f"字段 '{f.field_code}' 默认值 '{f.default_value}' "
                "不在安全白名单内",
                "使用数字、TRUE/FALSE/NULL、CURRENT_DATE/CURRENT_TIMESTAMP "
                "或简单单引号常量；复杂表达式暂不支持",
                loc,
            ))

        # FIELD_DESCRIPTION_MISSING（警告）
        if not (f.description or "").strip():
            issues.append(Issue(
                "FIELD_DESCRIPTION_MISSING", "warning", "field", str(f.id),
                f"字段 '{f.field_code}' 缺少业务说明",
                "说明字段口径、枚举、单位或业务语义",
                loc,
            ))

        # INCOMPLETE_SOURCE_MAPPING（警告：只填一项）
        has_table = bool((f.source_table or "").strip())
        has_field = bool((f.source_field or "").strip())
        if has_table != has_field:
            issues.append(Issue(
                "INCOMPLETE_SOURCE_MAPPING", "warning", "field", str(f.id),
                f"字段 '{f.field_code}' 只填写了来源表或来源字段其中一项",
                "补齐另一项，或在后续版本明确虚拟/派生字段规则",
                loc,
            ))

        # PK_UNIQUE_REDUNDANT（警告）
        if f.is_primary_key and f.is_unique:
            issues.append(Issue(
                "PK_UNIQUE_REDUNDANT", "warning", "field", str(f.id),
                f"字段 '{f.field_code}' 同时标记主键和唯一约束",
                "通常可以去掉重复的唯一声明",
                loc,
            ))

    # ---- PRIMARY_KEY_RECOMMENDED（模型级警告）----
    if model.layer in ("DWD", "DIM") and fields:
        has_key = any(f.is_primary_key or f.is_unique for f in fields)
        if not has_key and not any(
            c.constraint_type in ("primary_key", "unique") for c in constraints
        ):
            issues.append(Issue(
                "PRIMARY_KEY_RECOMMENDED", "warning", "model", str(model.id),
                f"模型 '{model.model_key}'（{model.layer}）未明确主键或业务唯一键",
                "检查业务粒度及唯一性；不代表强制所有模型设置物理主键",
                model_loc,
            ))

    # ---- V0.8 表级约束与索引规则 ----
    table_pks = [c for c in constraints if c.constraint_type == "primary_key"]
    field_pks = [f for f in fields if f.is_primary_key]

    # TABLE_PK_FIELD_PK_CONFLICT（互斥）
    if table_pks and field_pks:
        issues.append(Issue(
            "TABLE_PK_FIELD_PK_CONFLICT", "error", "model", str(model.id),
            f"模型同时存在表级主键 {[c.constraint_name for c in table_pks]} "
            f"与字段级主键 {[f.field_code for f in field_pks]}",
            "移除字段级主键开关，或删除表级主键约束（二者只能选其一）",
            model_loc,
        ))

    for c in constraints:
        c_loc = f"constraint:{model.model_key}.{c.constraint_name}"
        if c.constraint_type == "check":
            # CHECK_EXPRESSION_INVALID（重放校验：防字段删除后悬挂引用）
            msg = validate_check_expression(c.expression, field_code_set)
            if msg:
                issues.append(Issue(
                    "CHECK_EXPRESSION_INVALID", "error", "constraint", str(c.id),
                    f"CHECK 约束 '{c.constraint_name}'：{msg}",
                    "修正表达式后重新保存约束",
                    c_loc,
                ))
        else:
            # CONSTRAINT_FIELD_NOT_FOUND
            for code in json.loads(c.field_codes_json or "[]"):
                if code not in field_code_set:
                    issues.append(Issue(
                        "CONSTRAINT_FIELD_NOT_FOUND", "error", "constraint",
                        str(c.id),
                        f"约束 '{c.constraint_name}' 引用的字段 '{code}' "
                        "在模型中不存在",
                        "删除该约束，或恢复对应字段",
                        c_loc,
                    ))
        # FK_TARGET_KEY_NOT_UNIQUE（目标键唯一性由调用方预计算）
        if c.constraint_type == "foreign_key" and c.constraint_name in fk_at_risk:
            issues.append(Issue(
                "FK_TARGET_KEY_NOT_UNIQUE", "warning", "constraint", str(c.id),
                f"外键 '{c.constraint_name}' 的目标字段未声明主键或唯一约束"
                "（或目标字段已不存在），目标引擎执行 DDL 可能失败",
                "在目标模型为引用列声明主键/唯一约束，或更换引用列",
                c_loc,
            ))

    for idx in indexes:
        i_loc = f"index:{model.model_key}.{idx.index_name}"
        # INDEX_FIELD_NOT_FOUND
        for code in json.loads(idx.field_codes_json or "[]"):
            if code not in field_code_set:
                issues.append(Issue(
                    "INDEX_FIELD_NOT_FOUND", "error", "index", str(idx.id),
                    f"索引 '{idx.index_name}' 引用的字段 '{code}' "
                    "在模型中不存在",
                    "删除该索引，或恢复对应字段",
                    i_loc,
                ))
        # INDEX_ENGINE_UNSUPPORTED
        supported, reason = engine_supports_index(
            engine, idx.index_type, idx.is_unique
        )
        if not supported:
            issues.append(Issue(
                "INDEX_ENGINE_UNSUPPORTED", "error", "index", str(idx.id),
                f"索引 '{idx.index_name}'（{idx.index_type}"
                f"{'，唯一' if idx.is_unique else ''}）不被目标引擎 "
                f"{engine} 支持：{reason}",
                "MySQL 仅支持 btree；PostgreSQL 不支持唯一 GIN；"
                "请调整索引类型或目标引擎",
                i_loc,
            ))

    # HIVE_LOGICAL_CONSTRAINT（警告）
    if engine == "hive" and constraints:
        issues.append(Issue(
            "HIVE_LOGICAL_CONSTRAINT", "warning", "model", str(model.id),
            f"Hive 不强制表级约束，{len(constraints)} 个约束仅作为逻辑元数据呈现",
            "如需强制完整性，请在 PostgreSQL/MySQL 目标或上层质量校验中实现",
            model_loc,
        ))

    errors = sum(1 for i in issues if i.severity == "error")
    warnings = sum(1 for i in issues if i.severity == "warning")
    infos = sum(1 for i in issues if i.severity == "info")
    return ValidationResult(
        run_id=uuid.uuid4().hex,
        status="FAILED" if errors else "PASSED",
        summary={"errors": errors, "warnings": warnings, "infos": infos},
        issues=issues,
    )
