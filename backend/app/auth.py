"""数据建模中心 V1.0 — 认证与项目级授权（M1/M2）。

身份模型（设计文档第 3 章）：
- ``AUTH_MODE=demo``（默认）：``X-Actor`` 演示身份，所有授权检查短路，
  行为与 V0.9.2 完全一致；启动时幂等播种演示管理员与项目 owner 成员。
- ``AUTH_MODE=api_key``：``Authorization: Bearer dmc_<key_id>_<secret>``；
  服务端只存 ``SHA-256(secret)`` 十六进制摘要，``hmac.compare_digest``
  常量时间比较；进程内密钥缓存 TTL 60s，吊销/禁用即时失效（主动
  invalidate），``last_used_at`` 节流（≥60s）落库。
- 授权：项目三角色 owner/editor/viewer（viewer < editor < owner）；
  平台 admin（``users.is_admin``）旁路全部项目检查。

错误语义（设计文档 3.3/第 7 章）：项目不存在 → 404 PROJECT_NOT_FOUND；
已认证但非成员 → 403 PROJECT_ACCESS_DENIED；角色不足 → 403
PROJECT_ROLE_INSUFFICIENT。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets as pysecrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models
from .config import settings
from .db import SessionLocal, get_db
from .errors import ApiError, not_found
from .events import record_audit

DEMO_ADMIN_KEY = "u:demo.admin"
DEMO_ADMIN_NAME = "演示管理员"

# key_id 固定结构：ak_ 前缀 + 10-40 位字母数字（不含下划线）。
# 令牌整体匹配：dmc_<key_id>_<secret>。secret 为 base64url（43 字符，
# 可能含 -/_），不能简单用 partition("_") 切分（key_id 的 ak_ 前缀与
# secret 均可能含下划线）；key_id 的字母数字段长度受 {10,40} 约束且
# 与实际生成的 22 位十六进制固定一致，贪婪匹配在分隔下划线处止步，
# 切分结果确定无歧义。
_TOKEN_PATTERN = re.compile(r"^dmc_(ak_[A-Za-z0-9]{10,40})_(.+)$")
_USER_KEY_PATTERN = r"^[a-z]+:[A-Za-z0-9_.-]{1,64}$"

ROLE_LEVEL = {"viewer": 1, "editor": 2, "owner": 3}

# api_key 模式下免认证路径：探活与一次性引导（引导自身发钥匙）。
_AUTH_EXEMPT_PATHS = {
    "/api/health/live",
    "/api/health/ready",
    "/api/health",
    "/api/auth/bootstrap",
}


# ---------------------------------------------------------------- 身份与错误


@dataclass(frozen=True)
class Identity:
    """已认证主体（api_key 模式注入 request.state.identity）。"""

    user_id: int
    user_key: str
    display_name: str
    is_admin: bool

    @property
    def actor(self) -> str:
        return self.user_key


def _as_aware(dt: datetime | None) -> datetime | None:
    """SQLite 读回的 DateTime(timezone=True) 为 naive：按 UTC 补时区。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------- 密钥材料


def hash_secret(secret: str) -> str:
    """SHA-256(secret) 十六进制摘要（不明文存储 secret）。"""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def generate_key_material() -> tuple[str, str, str]:
    """生成 (key_id, secret, key_hash)。

    secret 为 256 位随机数 base64url（43 字符，可能含 -/_）；key_id 为
    ak_ + 22 位十六进制（字母数字，不含下划线）。令牌整体由
    _TOKEN_PATTERN 正则锚定切分，key_id/secret 含下划线均无歧义。
    """
    secret = pysecrets.token_urlsafe(32)
    key_id = "ak_" + pysecrets.token_hex(11)  # ak_ + 22 位十六进制
    return key_id, secret, hash_secret(secret)


def _parse_bearer(header: str | None) -> tuple[str, str] | None:
    """解析 Authorization: Bearer dmc_<key_id>_<secret>。

    secret 为 base64url（含 -/_），与 key_id 的 ak_ 前缀同样可能含
    下划线，故用 _TOKEN_PATTERN 整体锚定：key_id 段贪婪匹配字母数字
    在分隔下划线处止步，其余全部作为 secret，切分确定无歧义。
    """
    if not header:
        return None
    parts = header.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    m = _TOKEN_PATTERN.match(parts[1].strip())
    if m is None:
        return None
    return m.group(1), m.group(2)


# ---------------------------------------------------------------- 密钥缓存


@dataclass
class _CacheEntry:
    user_id: int
    user_key: str
    display_name: str
    is_admin: bool
    key_hash: str
    key_status: str
    user_status: str
    expires_at: datetime | None
    last_used_write_at: datetime | None
    fetched_at: datetime


class KeyCache:
    """进程内密钥认证结果缓存（TTL 秒，默认 60）。

    吊销（revoke）与用户禁用通过 invalidate 即时生效，不依赖 TTL；
    expires_at 每次请求实时校验，过期密钥即时拒绝。
    """

    def __init__(self) -> None:
        self._entries: dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()

    def get(self, key_id: str) -> _CacheEntry | None:
        with self._lock:
            entry = self._entries.get(key_id)
            if entry is None:
                return None
            age = (datetime.now(timezone.utc) - entry.fetched_at).total_seconds()
            if age >= settings.auth_cache_ttl_seconds:
                del self._entries[key_id]
                return None
            return entry

    def put(self, key_id: str, entry: _CacheEntry) -> None:
        with self._lock:
            self._entries[key_id] = entry

    def touch_used(self, key_id: str, when: datetime) -> None:
        with self._lock:
            entry = self._entries.get(key_id)
            if entry is not None:
                entry.last_used_write_at = when

    def invalidate(self, key_id: str) -> None:
        with self._lock:
            self._entries.pop(key_id, None)


key_cache = KeyCache()


def _load_cache_entry(db: Session, key_id: str) -> _CacheEntry | None:
    row = db.execute(
        select(models.ApiKey, models.User)
        .where(
            models.ApiKey.key_id == key_id,
            models.ApiKey.user_id == models.User.id,
        )
    ).first()
    if row is None:
        return None
    key, user = row
    return _CacheEntry(
        user_id=user.id,
        user_key=user.user_key,
        display_name=user.display_name,
        is_admin=bool(user.is_admin),
        key_hash=key.key_hash,
        key_status=key.status,
        user_status=user.status,
        expires_at=_as_aware(key.expires_at),
        last_used_write_at=_as_aware(key.last_used_at),
        fetched_at=datetime.now(timezone.utc),
    )


def _resolve_entry(db: Session, key_id: str) -> _CacheEntry | None:
    entry = key_cache.get(key_id)
    if entry is None:
        entry = _load_cache_entry(db, key_id)
        if entry is not None:
            key_cache.put(key_id, entry)
    return entry


def _maybe_touch_last_used(db: Session, key_id: str,
                           entry: _CacheEntry) -> None:
    """last_used_at 节流更新：距上次写入 ≥60s 才落库。"""
    now = datetime.now(timezone.utc)
    last = entry.last_used_write_at
    if last is not None and (now - last).total_seconds() < 60:
        return
    key = db.get(models.ApiKey, _key_row_id(db, key_id))
    if key is None:
        return
    key.last_used_at = now
    db.commit()
    key_cache.touch_used(key_id, now)
    entry.last_used_write_at = now


def _key_row_id(db: Session, key_id: str) -> int | None:
    return db.scalar(
        select(models.ApiKey.id).where(models.ApiKey.key_id == key_id)
    )


# ---------------------------------------------------------------- 认证入口


def authenticate(request: Request) -> Identity | ApiError:
    """Bearer 认证（api_key 模式由中间件调用）。

    返回 Identity 或 ApiError；失败分类见错误码表：AUTH_REQUIRED /
    API_KEY_INVALID / API_KEY_REVOKED / API_KEY_EXPIRED / USER_DISABLED。
    """
    parsed = _parse_bearer(request.headers.get("authorization"))
    if parsed is None:
        return ApiError(
            401, "AUTH_REQUIRED",
            "缺少或非法的 Authorization 头；需要 Bearer dmc_<key_id>_<secret>",
        )
    key_id, secret = parsed
    with SessionLocal() as db:
        entry = _resolve_entry(db, key_id)
        if entry is None:
            return ApiError(
                401, "API_KEY_INVALID",
                "密钥不存在或 secret 不匹配", {"key_id": key_id},
            )
        if entry.key_status != "active":
            return ApiError(
                401, "API_KEY_REVOKED", "密钥已吊销", {"key_id": key_id},
            )
        if entry.user_status != "active":
            return ApiError(
                403, "USER_DISABLED",
                f"主体 {entry.user_key} 已禁用，全部密钥即时失效",
                {"user_key": entry.user_key},
            )
        expires_at = _as_aware(entry.expires_at)
        if expires_at is not None and datetime.now(timezone.utc) >= expires_at:
            return ApiError(
                401, "API_KEY_EXPIRED", "密钥已过期", {"key_id": key_id},
            )
        if not hmac.compare_digest(entry.key_hash, hash_secret(secret)):
            return ApiError(
                401, "API_KEY_INVALID",
                "密钥不存在或 secret 不匹配", {"key_id": key_id},
            )
        _maybe_touch_last_used(db, key_id, entry)
    return Identity(
        user_id=entry.user_id,
        user_key=entry.user_key,
        display_name=entry.display_name,
        is_admin=entry.is_admin,
    )


def record_auth_denied(request: Request, code: str, details: dict) -> None:
    """认证失败留痕：审计 auth.denied + 进程内计数（M6 指标族）。

    AuditLog 表无 details 列，reason 摘要落在 object_id（设计偏差，
    见实施报告）。
    """
    request_id = getattr(request.state, "request_id", "")
    try:
        with SessionLocal() as db:
            record_audit(
                db, "auth", f"reason={code}", "auth.denied",
                "", "denied", request_id,
            )
            db.commit()
    except Exception:  # noqa: BLE001  审计失败不阻断认证拒绝响应
        pass
    metrics = getattr(request.app.state, "metrics", None)
    if metrics is not None:
        metrics.record_auth_denied(code)


# ---------------------------------------------------------------- 授权依赖


def _identity_of(request: Request) -> Identity | None:
    return getattr(request.state, "identity", None)


def _check_project_role(
    db: Session, project_id: int, minimum: str, identity: Identity
) -> str:
    """项目角色校验；返回实际角色（admin 返回 'admin'）。"""
    if db.get(models.Project, project_id) is None:
        raise not_found("PROJECT_NOT_FOUND", f"项目 {project_id} 不存在")
    if identity.is_admin:
        return "admin"
    role = db.scalar(
        select(models.ProjectMember.role).where(
            models.ProjectMember.project_id == project_id,
            models.ProjectMember.user_id == identity.user_id,
        )
    )
    if role is None:
        raise ApiError(
            403, "PROJECT_ACCESS_DENIED",
            f"主体 {identity.user_key} 不是项目 {project_id} 的成员",
            {"project_id": project_id},
        )
    if ROLE_LEVEL[role] < ROLE_LEVEL[minimum]:
        raise ApiError(
            403, "PROJECT_ROLE_INSUFFICIENT",
            f"需要 {minimum} 或更高角色，当前角色为 {role}",
            {"project_id": project_id, "current_role": role,
             "required_role": minimum},
        )
    return role


def require_project_role(minimum: str):
    """项目级授权依赖工厂：GET 用 viewer，写操作用 editor，成员管理用 owner。"""

    def dep(
        project_id: int,
        request: Request,
        db: Session = Depends(get_db),
    ) -> None:
        if settings.auth_mode == "demo":
            return  # V0.9.2 兼容：演示模式无授权
        identity = _identity_of(request)
        if identity is None:
            raise ApiError(401, "AUTH_REQUIRED", "缺少身份信息")
        _check_project_role(db, project_id, minimum, identity)

    return dep


def require_model_role(minimum: str):
    """模型级授权依赖工厂：先按 model_id 解析项目再校验角色。"""

    def dep(
        model_id: int,
        request: Request,
        db: Session = Depends(get_db),
    ) -> None:
        if settings.auth_mode == "demo":
            return
        identity = _identity_of(request)
        if identity is None:
            raise ApiError(401, "AUTH_REQUIRED", "缺少身份信息")
        model = db.get(models.DataModel, model_id)
        if model is None:
            raise not_found("MODEL_NOT_FOUND", f"模型 {model_id} 不存在")
        _check_project_role(db, model.project_id, minimum, identity)

    return dep


def require_requirement_role(minimum: str):
    """需求级授权依赖工厂：先按 requirement_id 解析项目再校验角色。"""

    def dep(
        requirement_id: int,
        request: Request,
        db: Session = Depends(get_db),
    ) -> None:
        if settings.auth_mode == "demo":
            return
        identity = _identity_of(request)
        if identity is None:
            raise ApiError(401, "AUTH_REQUIRED", "缺少身份信息")
        requirement = db.get(models.BusinessRequirement, requirement_id)
        if requirement is None:
            raise not_found(
                "REQUIREMENT_NOT_FOUND", f"需求 {requirement_id} 不存在"
            )
        _check_project_role(
            db, requirement.project_id, minimum, identity
        )

    return dep


def require_authenticated(request: Request) -> Identity:
    """api_key 模式下取当前身份（/metrics 等非项目级端点使用）。"""
    identity = _identity_of(request)
    if identity is None:
        raise ApiError(401, "AUTH_REQUIRED", "缺少身份信息")
    return identity


def require_admin(request: Request) -> Identity:
    """平台级授权依赖：仅 admin（demo 模式短路，与项目级依赖一致）。

    /api/users 用户管理族使用；非 admin → 403 ADMIN_REQUIRED。
    demo 模式无 request.state.identity（中间件直接放行），返回合成的
    演示管理员身份（user_id=0 不会命中任何真实行的自操作防护）。
    """
    if settings.auth_mode == "demo":
        return Identity(
            user_id=0,
            user_key=DEMO_ADMIN_KEY,
            display_name=DEMO_ADMIN_NAME,
            is_admin=True,
        )
    identity = require_authenticated(request)
    if not identity.is_admin:
        raise ApiError(
            403, "ADMIN_REQUIRED",
            f"主体 {identity.user_key} 不是平台管理员",
        )
    return identity


# ---------------------------------------------------------------- 能力矩阵（V1.2）

# 平台级能力（设计文档 2.3）：单一事实源，供 /api/auth/me capabilities 与
# 前端显隐共用。is_admin 是唯一平台角色，非 admin 无任何平台能力。
PLATFORM_CAPABILITIES: dict[str, list[str]] = {
    "admin": ["user.manage", "api_key.provision", "metrics.view"],
    "member": [],
}

# 项目级能力族（设计文档 2.2 的粗粒度投影）：前端显隐只需能力族粒度，
# 逐端点角色校验仍由 require_*_role 依赖工厂执行（两处共用同一矩阵语义）。
PROJECT_CAPABILITIES: dict[str, list[str]] = {
    "viewer": ["model.read"],
    "editor": ["model.read", "model.write"],
    "owner": ["model.read", "model.write", "member.manage"],
}


def platform_capabilities(is_admin: bool) -> list[str]:
    """平台级有效能力列表。"""
    return list(PLATFORM_CAPABILITIES["admin" if is_admin else "member"])


def project_capabilities(role: str) -> list[str]:
    """项目角色 → 能力族（未知角色按最低权限 viewer 处理）。"""
    return list(PROJECT_CAPABILITIES.get(role, PROJECT_CAPABILITIES["viewer"]))


def count_active_admins(db: Session) -> int:
    """active 状态的平台管理员数量（P1 最后 admin 保护判定）。"""
    return int(db.scalar(
        select(func.count(models.User.id)).where(
            models.User.is_admin.is_(True),
            models.User.status == "active",
        )
    ) or 0)


def invalidate_user_keys(db: Session, user_id: int) -> int:
    """使用户全部密钥缓存即时失效（P6：状态/角色变更不等 TTL）。

    返回失效的 active 密钥数；仅清进程内缓存，不改库（状态变更由调用方
    的事务统一提交）。缓存 entry 携带 user_status 与 is_admin，变更后
    不失效则旧令牌在 TTL 内仍按旧身份放行（降级 admin 的越权窗口）。
    """
    key_ids = list(db.scalars(
        select(models.ApiKey.key_id).where(
            models.ApiKey.user_id == user_id,
            models.ApiKey.status == "active",
        )
    ))
    for key_id in key_ids:
        key_cache.invalidate(key_id)
    return len(key_ids)


# ---------------------------------------------------------------- 账号管理


def get_or_create_user(
    db: Session, user_key: str, display_name: str,
    kind: str = "human", is_admin: bool = False, created_by: str = "",
) -> tuple[models.User, bool]:
    """按 user_key 幂等获取/创建主体；返回 (user, created)。"""
    user = db.scalar(
        select(models.User).where(models.User.user_key == user_key)
    )
    if user is not None:
        return user, False
    user = models.User(
        user_key=user_key,
        display_name=display_name,
        kind=kind,
        is_admin=is_admin,
        auth_source="local",
        status="active",
    )
    db.add(user)
    db.flush()
    return user, True


def create_api_key(
    db: Session,
    user: models.User,
    name: str,
    expires_in_days: int | None,
    created_by: str,
) -> tuple[models.ApiKey, str]:
    """创建密钥；返回 (row, secret)。secret 仅此函数调用方可见一次。"""
    key_id, secret, key_hash = generate_key_material()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=expires_in_days)
        if expires_in_days
        else None
    )
    key = models.ApiKey(
        key_id=key_id,
        key_hash=key_hash,
        user_id=user.id,
        name=name,
        status="active",
        expires_at=expires_at,
        created_by=created_by,
    )
    db.add(key)
    db.flush()
    return key, secret


def revoke_api_key(db: Session, key: models.ApiKey) -> None:
    """吊销密钥（幂等）；吊销后主动失效缓存，旧令牌即时 401。"""
    if key.status != "revoked":
        key.status = "revoked"
        db.flush()
    key_cache.invalidate(key.key_id)


def bootstrap_admin(
    db: Session, user_key: str, display_name: str
) -> tuple[models.User, models.ApiKey, str]:
    """一次性引导：创建首个平台管理员并返回其密钥。"""
    user, _ = get_or_create_user(
        db, user_key, display_name, kind="human", is_admin=True,
        created_by="bootstrap",
    )
    user.is_admin = True
    db.flush()
    key, secret = create_api_key(
        db, user, "bootstrap-admin-key", expires_in_days=None,
        created_by="bootstrap",
    )
    return user, key, secret


def ensure_demo_admin(db: Session) -> models.User:
    """demo 模式幂等播种：演示管理员 + 每个既有项目的 owner 成员。

    与 SEED_DEMO_DATA 解耦（设计文档 3.4）：仅影响 users /
    project_members，不触碰业务种子。只 flush 不 commit，由调用方
    在自身事务边界提交。
    """
    user, _ = get_or_create_user(
        db, DEMO_ADMIN_KEY, DEMO_ADMIN_NAME, kind="human", is_admin=True,
        created_by="system",
    )
    for project in db.scalars(select(models.Project)):
        exists = db.scalar(
            select(models.ProjectMember.id).where(
                models.ProjectMember.project_id == project.id,
                models.ProjectMember.user_id == user.id,
            )
        )
        if exists is None:
            db.add(models.ProjectMember(
                project_id=project.id,
                user_id=user.id,
                role="owner",
                created_by="system",
            ))
    db.flush()
    return user


def upsert_project_member(
    db: Session,
    project_id: int,
    user: models.User,
    role: str,
    actor: str,
) -> tuple[models.ProjectMember, bool]:
    """项目成员 upsert（(project,user) 唯一）；返回 (row, created)。"""
    member = db.scalar(
        select(models.ProjectMember).where(
            models.ProjectMember.project_id == project_id,
            models.ProjectMember.user_id == user.id,
        )
    )
    if member is None:
        member = models.ProjectMember(
            project_id=project_id,
            user_id=user.id,
            role=role,
            created_by=actor,
        )
        db.add(member)
        db.flush()
        return member, True
    member.role = role
    db.flush()
    return member, False
