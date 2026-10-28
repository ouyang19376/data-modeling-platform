"""数据建模中心 MVP — 多引擎 DDL 适配器（V0.8）。

统一逻辑类型 → 目标引擎物理类型映射；标识符白名单校验；默认值白名单
（与 validation.py 共用同一份白名单实现，DDL 生成前再次校验，避免只依赖前端）。
单列唯一约束：PostgreSQL/MySQL 输出 UNIQUE (column)；Hive 输出逻辑唯一注释。
表级约束（V0.8）：复合主键/复合唯一/CHECK/外键以命名 CONSTRAINT 子句输出；
CHECK 表达式在生成前重放受限 token 校验（防字段删除后悬挂引用）。
索引（V0.8）：PG 输出 CREATE INDEX [USING btree/hash/gin]；MySQL 仅 btree
（hash/gin 生成前拒绝）；Hive 仅输出逻辑注释。unique+gin 在任何引擎均拒绝。
DDL 只生成不执行（execute_on_target=false）；生成不等于目标引擎执行成功。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .models import DataModel, ModelConstraint, ModelField, ModelIndex
from .validation import normalize_default_value, validate_check_expression

SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")

RESERVED_WORDS = {
    "select", "from", "where", "table", "index", "order", "group", "by",
    "insert", "update", "delete", "create", "drop", "alter", "join", "on",
    "and", "or", "not", "null", "default", "primary", "key", "foreign",
    "references", "unique", "check", "constraint", "user", "grant", "values",
    "limit", "offset", "union", "all", "distinct", "having", "asc", "desc",
}

# 逻辑类型 → 各引擎物理类型（长度/精度由字段属性注入）
TYPE_MAPPING: dict[str, dict[str, str]] = {
    "STRING": {"postgresql": "TEXT", "mysql": "VARCHAR(255)", "hive": "STRING"},
    "VARCHAR": {"postgresql": "VARCHAR", "mysql": "VARCHAR", "hive": "VARCHAR"},
    "INTEGER": {"postgresql": "INTEGER", "mysql": "INT", "hive": "INT"},
    "BIGINT": {"postgresql": "BIGINT", "mysql": "BIGINT", "hive": "BIGINT"},
    "DECIMAL": {"postgresql": "NUMERIC", "mysql": "DECIMAL", "hive": "DECIMAL"},
    "DATE": {"postgresql": "DATE", "mysql": "DATE", "hive": "DATE"},
    "TIMESTAMP": {"postgresql": "TIMESTAMP", "mysql": "TIMESTAMP", "hive": "TIMESTAMP"},
    "BOOLEAN": {"postgresql": "BOOLEAN", "mysql": "BOOLEAN", "hive": "BOOLEAN"},
}

IDENTIFIER_MAX_LENGTH = {"postgresql": 63, "mysql": 64, "hive": 128}


@dataclass
class DDLArtifact:
    target_engine: str
    ddl: str
    warnings: list[str] = field(default_factory=list)
    unsupported_features: list[str] = field(default_factory=list)
    execute_on_target: bool = False
    generator_version: str = "mvp-0.8.0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_engine": self.target_engine,
            "ddl": self.ddl,
            "warnings": self.warnings,
            "unsupported_features": self.unsupported_features,
            "execute_on_target": self.execute_on_target,
            "generator_version": self.generator_version,
        }


class DDLGenerationError(Exception):
    """DDL 生成失败（标识符非法、类型无映射、默认值不在白名单、
    不支持的索引/约束组合、外键目标不可解析等）。"""


def _check_identifier(name: str, engine: str, kind: str) -> None:
    max_len = IDENTIFIER_MAX_LENGTH[engine]
    if not SNAKE_CASE.match(name):
        raise DDLGenerationError(
            f"{kind} '{name}' 不是合法标识符（snake_case，引擎 {engine}）"
        )
    if len(name) > max_len:
        raise DDLGenerationError(
            f"{kind} '{name}' 长度 {len(name)} 超过引擎 {engine} 上限 {max_len}"
        )
    if name.lower() in RESERVED_WORDS:
        raise DDLGenerationError(
            f"{kind} '{name}' 是 SQL 保留字（引擎 {engine}）"
        )


def _check_default(value: str | None, engine: str, field_code: str) -> str | None:
    """默认值白名单复检（与 validation.UNSAFE_DEFAULT_VALUE 同一份实现）。

    空值视为"不设置默认"；非空但不在白名单则阻断 DDL 生成。
    """
    normalized = normalize_default_value(value)
    if value is not None and value.strip() and normalized is None:
        raise DDLGenerationError(
            f"字段 '{field_code}' 默认值 '{value}' 不在白名单内"
            f"（引擎 {engine} 仅支持数值/布尔/NULL/日期时间常量与简单单引号字符串）"
        )
    return normalized


def map_type(field: ModelField, engine: str) -> str:
    """逻辑类型 → 物理类型（含长度/精度注入）。"""
    logical = field.logical_type
    if logical not in TYPE_MAPPING or engine not in TYPE_MAPPING[logical]:
        raise DDLGenerationError(
            f"逻辑类型 '{logical}' 在引擎 {engine} 无映射"
        )
    base = TYPE_MAPPING[logical][engine]
    if logical == "VARCHAR":
        if field.length is None:
            raise DDLGenerationError(
                f"字段 '{field.field_code}' 为 VARCHAR 但未提供长度"
            )
        return f"{base}({field.length})"
    if logical == "DECIMAL":
        if field.precision is None or field.scale is None:
            raise DDLGenerationError(
                f"字段 '{field.field_code}' 为 DECIMAL 但未提供精度/小数位"
            )
        return f"{base}({field.precision},{field.scale})"
    return base


def _quote(engine: str, name: str) -> str:
    if engine == "mysql":
        return f"`{name}`"
    if engine == "postgresql":
        return f'"{name}"'
    return name  # Hive 标识符已通过白名单校验，无需引号


def _sql_literal(text: str) -> str:
    """单引号字符串字面量（转义内部单引号）。"""
    return "'" + text.replace("'", "''") + "'"


def _column_line(field: ModelField, engine: str, schema: str | None) -> tuple[str, list[str]]:
    warnings: list[str] = []
    _check_identifier(field.field_code, engine, "字段编码")
    physical = map_type(field, engine)
    parts = [_quote(engine, field.field_code), physical]
    if not field.nullable:
        parts.append("NOT NULL")
    default = _check_default(field.default_value, engine, field.field_code)
    if default:
        parts.append(f"DEFAULT {default}")
    line = " ".join(parts)
    if engine in ("hive", "mysql") and field.description:
        comment = field.description.replace("'", "''")
        line += f" COMMENT '{comment}'"
    return line, warnings


def _cols(engine: str, codes: list[str]) -> str:
    return ", ".join(_quote(engine, c) for c in codes)


# ---------------------------------------------------------------- 表级约束（V0.8）


def _fk_target_key_covered(
    ref_codes: list[str], unique_keys: list[frozenset[str]] | None
) -> bool:
    """外键目标列是否被目标模型唯一/PK 覆盖（与 validation._referenced_key_is_unique 同语义）。

    unique_keys 为 None 表示调用方未提供目标信息，不做覆盖判断（返回 True 不告警）。
    组合引用列：精确匹配某个表级 PK/unique，或每个引用列单独具备字段级 PK/unique
    （单列唯一则其任意超集组合同样唯一）。
    """
    if unique_keys is None:
        return True
    ref_set = frozenset(ref_codes)
    if ref_set in unique_keys:
        return True
    return all(frozenset({c}) in unique_keys for c in ref_codes)


def _table_constraint_clause(
    c: ModelConstraint,
    engine: str,
    field_code_set: set[str],
    referenced_table_names: dict[int, str] | None,
    warnings: list[str],
    fk_target_unique_keys: dict[int, list[frozenset[str]]] | None,
) -> str | None:
    """生成单条表级约束的命名 CONSTRAINT 子句；Hive 返回 None（走逻辑注释）。"""
    name = c.constraint_name
    _check_identifier(name, engine, "约束名")
    if c.constraint_type not in ("primary_key", "unique", "check", "foreign_key"):
        raise DDLGenerationError(
            f"约束 '{name}' 类型 '{c.constraint_type}' 无法生成 DDL"
        )

    if c.constraint_type == "check":
        # 生成前重放受限 token 校验（防字段删除后悬挂引用）
        expr = (c.expression or "").strip()
        msg = validate_check_expression(expr, field_code_set)
        if msg:
            raise DDLGenerationError(f"约束 '{name}' 的 CHECK 表达式无法生成 DDL：{msg}")
        if engine == "hive":
            return None
        return f"CONSTRAINT {_quote(engine, name)} CHECK ({expr})"

    codes = json.loads(c.field_codes_json or "[]")
    missing = [x for x in codes if x not in field_code_set]
    if missing:
        raise DDLGenerationError(
            f"约束 '{name}' 引用了不存在的字段：{', '.join(missing)}"
        )

    if c.constraint_type == "primary_key":
        if engine == "hive":
            return None
        return f"CONSTRAINT {_quote(engine, name)} PRIMARY KEY ({_cols(engine, codes)})"

    if c.constraint_type == "unique":
        if engine == "hive":
            return None
        return f"CONSTRAINT {_quote(engine, name)} UNIQUE ({_cols(engine, codes)})"

    # foreign_key
    if engine == "hive":
        return None
    target_id = c.referenced_model_id
    target_table = (referenced_table_names or {}).get(target_id or 0)
    if not target_table:
        raise DDLGenerationError(
            f"外键 '{name}' 的目标模型不可解析（id={target_id}），无法生成 REFERENCES 子句"
        )
    ref_codes = json.loads(c.referenced_field_codes_json or "[]")
    if not ref_codes:
        raise DDLGenerationError(
            f"外键 '{name}' 未配置目标字段，无法生成 REFERENCES 子句"
        )
    # FK 目标唯一性警告（数据库执行前置条件；规则器提醒而非替代 catalog 检查）
    unique_keys = (fk_target_unique_keys or {}).get(target_id or 0)
    if not _fk_target_key_covered(ref_codes, unique_keys):
        warnings.append(
            f"外键 '{name}' 的目标列 ({', '.join(ref_codes)}) 未被目标模型 "
            f"'{target_table}' 的唯一约束覆盖，引擎执行前需在目标环境确认"
        )
    on_delete = c.on_delete or "NO ACTION"
    return (
        f"CONSTRAINT {_quote(engine, name)} "
        f"FOREIGN KEY ({_cols(engine, codes)}) "
        f"REFERENCES {_quote(engine, target_table)} ({_cols(engine, ref_codes)}) "
        f"ON DELETE {on_delete}"
    )


def _hive_constraint_comments(
    constraints: list[ModelConstraint],
    referenced_table_names: dict[int, str] | None,
) -> tuple[list[str], list[str]]:
    """Hive 逻辑约束注释（不强制）+ unsupported_features 提示。"""
    lines: list[str] = []
    for c in constraints:
        if c.constraint_type == "primary_key":
            codes = ", ".join(json.loads(c.field_codes_json or "[]"))
            lines.append(f"-- 逻辑主键约束（Hive 不强制）: {c.constraint_name} ({codes})")
        elif c.constraint_type == "unique":
            codes = ", ".join(json.loads(c.field_codes_json or "[]"))
            lines.append(f"-- 逻辑唯一约束（Hive 不强制）: {c.constraint_name} ({codes})")
        elif c.constraint_type == "check":
            lines.append(
                f"-- 逻辑校验约束（Hive 不强制）: {c.constraint_name} ({c.expression})"
            )
        elif c.constraint_type == "foreign_key":
            codes = ", ".join(json.loads(c.field_codes_json or "[]"))
            ref_codes = ", ".join(json.loads(c.referenced_field_codes_json or "[]"))
            target_table = (referenced_table_names or {}).get(c.referenced_model_id or 0, "?")
            lines.append(
                f"-- 逻辑外键（Hive 不强制）: {c.constraint_name} ({codes}) "
                f"-> {target_table} ({ref_codes}) ON DELETE {c.on_delete or 'NO ACTION'}"
            )
    unsupported: list[str] = []
    if lines:
        unsupported.append("Hive 不强制表级约束（主键/唯一/校验/外键），仅作为逻辑注释呈现")
    return lines, unsupported


# ---------------------------------------------------------------- 索引（V0.8）


def _index_statements(
    indexes: list[ModelIndex],
    engine: str,
    table_ref: str,
    field_code_set: set[str],
) -> list[str]:
    """生成 CREATE INDEX 语句（PG 全类型、MySQL 仅 btree；Hive 返回空）。"""
    if engine == "hive":
        return []
    statements: list[str] = []
    for idx in indexes:
        _check_identifier(idx.index_name, engine, "索引名")
        codes = json.loads(idx.field_codes_json or "[]")
        missing = [x for x in codes if x not in field_code_set]
        if missing:
            raise DDLGenerationError(
                f"索引 '{idx.index_name}' 引用了不存在的字段：{', '.join(missing)}"
            )
        unique_kw = "UNIQUE " if idx.is_unique else ""
        cols = _cols(engine, codes)
        if engine == "mysql":
            if idx.index_type != "btree":
                raise DDLGenerationError(
                    f"索引 '{idx.index_name}' 类型 '{idx.index_type}' 在 MySQL "
                    f"不被支持（仅 btree），生成前拒绝"
                )
            statements.append(
                f"CREATE {unique_kw}INDEX {_quote(engine, idx.index_name)} "
                f"ON {table_ref} ({cols});"
            )
        else:  # postgresql
            if idx.is_unique and idx.index_type == "gin":
                raise DDLGenerationError(
                    f"唯一 GIN 索引 '{idx.index_name}' 在 PostgreSQL 不被支持，生成前拒绝"
                )
            using = idx.index_type if idx.index_type in ("btree", "hash", "gin") else "btree"
            statements.append(
                f"CREATE {unique_kw}INDEX {_quote(engine, idx.index_name)} "
                f"ON {table_ref} USING {using} ({cols});"
            )
    return statements


def _hive_index_comments(indexes: list[ModelIndex]) -> tuple[list[str], list[str]]:
    lines: list[str] = []
    for idx in indexes:
        codes = ", ".join(json.loads(idx.field_codes_json or "[]"))
        unique_mark = " UNIQUE" if idx.is_unique else ""
        lines.append(
            f"-- 逻辑索引（Hive 不生成物理索引）: {idx.index_name} "
            f"[{idx.index_type}{unique_mark}] ({codes})"
        )
    unsupported = (
        ["Hive 不生成物理索引，索引仅作为逻辑注释呈现"] if lines else []
    )
    return lines, unsupported


def generate_create_table(
    model: DataModel,
    fields: list[ModelField],
    engine: str,
    schema_name: str | None = None,
    constraints: list[ModelConstraint] | None = None,
    indexes: list[ModelIndex] | None = None,
    referenced_table_names: dict[int, str] | None = None,
    fk_target_unique_keys: dict[int, list[frozenset[str]]] | None = None,
) -> DDLArtifact:
    """生成 CREATE TABLE 预览。不连接、不执行目标数据库。

    referenced_table_names: model_id → 物理表名，用于解析外键 REFERENCES 目标；
    fk_target_unique_keys: model_id → 目标模型唯一/PK 列集合列表，用于外键
    目标唯一性警告（不提供则不做该检查）。
    """
    if engine not in TYPE_MAPPING["STRING"]:
        raise DDLGenerationError(f"不支持的引擎：{engine}")
    _check_identifier(model.model_key, engine, "模型编码（物理表名）")
    if schema_name:
        _check_identifier(schema_name, engine, "Schema 名")

    constraints = constraints or []
    indexes = indexes or []
    warnings: list[str] = []
    unsupported: list[str] = []
    column_lines: list[str] = []
    ordered = sorted(fields, key=lambda x: x.ordinal)
    for f in ordered:
        line, w = _column_line(f, engine, schema_name)
        column_lines.append(line)
        warnings.extend(w)
    field_code_set = {f.field_code for f in ordered}

    pk_fields = [f for f in ordered if f.is_primary_key]
    # 单列唯一约束：主键本身已隐含唯一，跳过避免冗余（PK_UNIQUE_REDUNDANT 为警告级）
    unique_fields = [
        f for f in ordered if f.is_unique and not f.is_primary_key
    ]

    # 表级主键与字段级主键互斥（与 TABLE_PK_FIELD_PK_CONFLICT 同一规则的生成期防线）
    table_pks = [c for c in constraints if c.constraint_type == "primary_key"]
    if table_pks and pk_fields:
        raise DDLGenerationError(
            f"模型同时存在字段级主键 {[f.field_code for f in pk_fields]} 与表级主键 "
            f"{[c.constraint_name for c in table_pks]}，二者互斥，请先修复校验错误"
        )

    constraint_clauses: list[str] = []
    if pk_fields:
        pk_cols = ", ".join(_quote(engine, f.field_code) for f in pk_fields)
        if engine == "hive":
            unsupported.append("Hive 不强制主键约束，主键仅作为说明呈现")
        else:
            constraint_clauses.append(f"PRIMARY KEY ({pk_cols})")
    if unique_fields:
        if engine == "hive":
            unsupported.append("Hive 不强制唯一约束，唯一性仅作为说明呈现")
        else:
            for f in unique_fields:
                constraint_clauses.append(
                    f"UNIQUE ({_quote(engine, f.field_code)})"
                )

    hive_constraint_notes: list[str] = []
    if constraints:
        for c in constraints:
            clause = _table_constraint_clause(
                c, engine, field_code_set, referenced_table_names,
                warnings, fk_target_unique_keys,
            )
            if clause:
                constraint_clauses.append(clause)
        if engine == "hive":
            hive_constraint_notes, hive_unsupported = _hive_constraint_comments(
                constraints, referenced_table_names
            )
            unsupported.extend(hive_unsupported)

    # 物理表引用（CREATE TABLE / CREATE INDEX / PG 注释共用）
    if engine == "postgresql":
        table_ref = f'{_quote(engine, schema_name)}.{_quote(engine, model.model_key)}' if schema_name else _quote(engine, model.model_key)
    elif engine == "mysql":
        table_ref = f"{_quote(engine, schema_name)}.{_quote(engine, model.model_key)}" if schema_name else _quote(engine, model.model_key)
    else:
        table_ref = f"{schema_name + '.' if schema_name else ''}{model.model_key}"

    index_statements = _index_statements(indexes, engine, table_ref, field_code_set)
    hive_index_notes: list[str] = []
    if indexes and engine == "hive":
        hive_index_notes, hive_idx_unsupported = _hive_index_comments(indexes)
        unsupported.extend(hive_idx_unsupported)

    if engine == "postgresql":
        body = ",\n  ".join(column_lines + constraint_clauses)
        ddl = f"CREATE TABLE IF NOT EXISTS {table_ref} (\n  {body}\n);"
        ddl += "\n\n" + "\n".join(index_statements) if index_statements else ""
        if model.description:
            ddl += f"\n\nCOMMENT ON TABLE {table_ref} IS '{model.description.replace(chr(39), chr(39) * 2)}';"
        for f in ordered:
            if f.description:
                col_ref = f"{table_ref}.{_quote(engine, f.field_code)}" if schema_name else f"{_quote(engine, model.model_key)}.{_quote(engine, f.field_code)}"
                ddl += f"\nCOMMENT ON COLUMN {col_ref} IS '{f.description.replace(chr(39), chr(39) * 2)}';"
    elif engine == "mysql":
        body = ",\n  ".join(column_lines + constraint_clauses)
        ddl = (
            f"CREATE TABLE IF NOT EXISTS {table_ref} (\n  {body}\n)"
            f" ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='{model.description.replace(chr(39), chr(39) * 2) or model.name}';"
        )
        ddl += "\n" + "\n".join(index_statements) if index_statements else ""
    else:  # hive
        body = ",\n  ".join(column_lines)
        table_comment = f" COMMENT '{model.description.replace(chr(39), chr(39) * 2) or model.name}'" if (model.description or model.name) else ""
        ddl = f"CREATE TABLE IF NOT EXISTS {table_ref} (\n  {body}\n){table_comment};"
        if pk_fields:
            pk_note = ", ".join(f.field_code for f in pk_fields)
            ddl += f"\n-- 逻辑主键（Hive 不强制）: {pk_note}"
        if unique_fields:
            unique_note = ", ".join(f.field_code for f in unique_fields)
            ddl += f"\n-- 逻辑唯一约束（Hive 不强制）: {unique_note}"
        ddl += "\n" + "\n".join(hive_constraint_notes) if hive_constraint_notes else ""
        ddl += "\n" + "\n".join(hive_index_notes) if hive_index_notes else ""

    return DDLArtifact(
        target_engine=engine,
        ddl=ddl,
        warnings=warnings,
        unsupported_features=unsupported,
    )


def engine_supports_type(engine: str, logical_type: str) -> bool:
    return logical_type in TYPE_MAPPING and engine in TYPE_MAPPING[logical_type]
