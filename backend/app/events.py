"""数据建模中心 — 活动事件与安全审计记录（V1.0 抽出，供 main/auth 共用）。

事件与审计随业务事务同库写入：``record_*`` 只 db.add，由调用方负责
commit/回滚，保证业务操作与留痕的原子性。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from . import models


def record_event(
    db: Session,
    project_id: int,
    event_type: str,
    actor: str,
    detail: dict[str, Any],
    request_id: str,
) -> None:
    db.add(models.ActivityEvent(
        project_id=project_id,
        event_type=event_type,
        actor=actor,
        detail_json=json.dumps(detail, ensure_ascii=False),
        request_id=request_id,
    ))


def record_audit(
    db: Session,
    object_type: str,
    object_id: str,
    action: str,
    actor: str,
    outcome: str,
    request_id: str,
) -> None:
    db.add(models.AuditLog(
        object_type=object_type,
        object_id=str(object_id),
        action=action,
        actor=actor,
        outcome=outcome,
        request_id=request_id,
    ))
