"""数据建模中心 V1.2 — 应用配置。

集中管理运行配置，避免环境变量散落在各模块。

配置来源与优先级（V1.2 新增配置文件支持）：
1. **环境变量**（最高优先）：适合容器 / CI / 临时覆盖；
2. **配置文件 `config.yaml`**（默认读取项目根目录，可用 `APP_CONFIG_FILE`
   指定其他路径）：适合本地开发与服务器部署，键名与下方 dataclass 字段
   一一对应，模板见仓库根目录 `config.example.yaml`；
3. dataclass 默认值。

`config.yaml` 不纳入版本库（.gitignore），随仓库分发的只有模板
`config.example.yaml`；未知配置键直接报错（fail-fast，防止拼写错误被
静默忽略）。

启动门禁（V1.0 确立，V1.2 不变）：
- AUTH_MODE=demo（默认）：仅允许 local/dev/development/test；X-Actor 演示
  身份生效，行为与 V0.9.2 完全一致。
- AUTH_MODE=api_key：允许任意合法环境（含 production/staging），所有受
  保护端点必须 Bearer dmc_<key_id>_<secret>；X-Actor 被忽略。
- 其他未知 AUTH_MODE 或未知 APP_ENV：拒绝启动（fail-closed）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path

import yaml

ALLOWED_ENVS = {"local", "dev", "development", "test"}
BLOCKED_ENVS = {"production", "staging", "prod"}
AUTH_MODES = {"demo", "api_key"}

# 项目根目录（backend/app/config.py 的上两级）；配置文件的默认解析基准，
# 与进程工作目录无关（uvicorn 从仓库根或 backend/ 启动都能找到同一份配置）。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_FILE = "config.yaml"


@dataclass
class Settings:
    app_env: str = "local"
    database_url: str = "sqlite:///./data/platform.db"
    auto_migrate: bool = True
    seed_demo_data: bool = True
    allow_demo_actor_header: bool = True
    adopt_legacy_schema: bool = False
    log_level: str = "INFO"
    data_dir: str = "./data"
    # ---- V1.0 认证授权与治理配置 ----
    auth_mode: str = "demo"
    bootstrap_token: str = ""
    # V1.3 密码登录会话时长（小时）：登录签发的会话密钥 expires_at；
    # 到期后前端 401 自动引导重登。0 表示不过期（不推荐）。
    auth_session_hours: int = 12
    # full_snapshot 停用比例阈值（M5）：planned/active_total 超阈值必须显式确认
    full_snapshot_deactivate_ratio_threshold: float = 0.5
    # dbt manifest 连接器规模上限（M3）：序列化后字节数与推导边数
    dbt_manifest_max_bytes: int = 8 * 1024 * 1024
    dbt_manifest_max_edges: int = 1000
    # API Key 认证缓存 TTL 秒（吊销/禁用即时失效不依赖 TTL）
    auth_cache_ttl_seconds: int = 60

    @classmethod
    def from_env(cls) -> "Settings":
        """按「环境变量 > config.yaml > 默认值」装配配置。"""
        file_values = _load_config_file(os.environ.get("APP_CONFIG_FILE"))
        allowed = {f.name for f in fields(cls)}
        unknown = sorted(set(file_values) - allowed)
        if unknown:
            raise RuntimeError(
                f"配置文件包含未知配置项 {unknown}；"
                f"允许的配置项见 config.example.yaml（{sorted(allowed)}）"
            )

        env = str(_pick(file_values, "app_env", "APP_ENV", "local")).strip().lower()
        database_url = str(
            _pick(file_values, "database_url", "DATABASE_URL", "sqlite:///./data/platform.db")
        ).strip()
        auto_migrate = _as_bool(
            _pick(file_values, "auto_migrate", "AUTO_MIGRATE", env not in BLOCKED_ENVS)
        )
        seed_demo_data = _as_bool(
            _pick(file_values, "seed_demo_data", "SEED_DEMO_DATA", True)
        )
        allow_demo_actor_header = _as_bool(
            _pick(file_values, "allow_demo_actor_header", "ALLOW_DEMO_ACTOR_HEADER", True)
        )
        adopt_legacy_schema = _as_bool(
            _pick(file_values, "adopt_legacy_schema", "ADOPT_LEGACY_SCHEMA", False)
        )
        log_level = str(
            _pick(file_values, "log_level", "LOG_LEVEL", "INFO")
        ).strip().upper()
        data_dir = str(
            _pick(file_values, "data_dir", "DATA_DIR", "./data")
        ).strip()
        auth_mode = str(
            _pick(file_values, "auth_mode", "AUTH_MODE", "demo")
        ).strip().lower()
        bootstrap_token = str(
            _pick(file_values, "bootstrap_token", "BOOTSTRAP_TOKEN", "")
        ).strip()
        auth_session_hours = _as_int(
            _pick(
                file_values, "auth_session_hours", "AUTH_SESSION_HOURS", 12
            )
        )
        if auth_session_hours < 0:
            raise RuntimeError("auth_session_hours 不能为负数（0 表示不过期）")
        threshold = _as_float(
            _pick(
                file_values,
                "full_snapshot_deactivate_ratio_threshold",
                "FULL_SNAPSHOT_DEACTIVATE_RATIO_THRESHOLD",
                0.5,
            )
        )
        return cls(
            app_env=env,
            database_url=database_url,
            auto_migrate=auto_migrate,
            seed_demo_data=seed_demo_data,
            allow_demo_actor_header=allow_demo_actor_header,
            adopt_legacy_schema=adopt_legacy_schema,
            log_level=log_level,
            data_dir=data_dir,
            auth_mode=auth_mode,
            bootstrap_token=bootstrap_token,
            auth_session_hours=auth_session_hours,
            full_snapshot_deactivate_ratio_threshold=threshold,
            dbt_manifest_max_bytes=_as_int(
                _pick(
                    file_values, "dbt_manifest_max_bytes", "DBT_MANIFEST_MAX_BYTES",
                    8 * 1024 * 1024,
                )
            ),
            dbt_manifest_max_edges=_as_int(
                _pick(
                    file_values, "dbt_manifest_max_edges", "DBT_MANIFEST_MAX_EDGES", 1000
                )
            ),
            auth_cache_ttl_seconds=_as_int(
                _pick(
                    file_values, "auth_cache_ttl_seconds", "AUTH_CACHE_TTL_SECONDS", 60
                )
            ),
        )

    def validate_startup(self) -> None:
        """启动安全闸门（V1.0）：按 AUTH_MODE × APP_ENV 放行/拒绝。

        - demo + local/dev/development/test：放行（V0.9.2 兼容行为）。
        - demo + production/staging：拒绝（演示身份不得暴露于生产）。
        - api_key + 任意已知环境：放行；DATABASE_URL 非 PostgreSQL 时
          仅记录 WARN（生产部署要求由运维侧保障，HTTPS 终止在网关）。
        - 未知 AUTH_MODE / 未知 APP_ENV：拒绝（fail-closed）。
        """
        if self.auth_mode not in AUTH_MODES:
            raise RuntimeError(
                f"AUTH_MODE={self.auth_mode!r} 不被支持："
                f"必须是 {sorted(AUTH_MODES)} 之一。"
            )
        if self.app_env not in ALLOWED_ENVS and self.app_env not in BLOCKED_ENVS:
            raise RuntimeError(
                f"APP_ENV={self.app_env} 不在已知环境列表 "
                f"{sorted(ALLOWED_ENVS | BLOCKED_ENVS)} 中。"
            )
        if self.auth_mode == "demo" and self.app_env in BLOCKED_ENVS:
            raise RuntimeError(
                f"AUTH_MODE=demo 且 APP_ENV={self.app_env} 被拒绝启动："
                "演示身份（X-Actor）不得用于生产/预发环境；"
                "生产部署请设置 AUTH_MODE=api_key 并完成 bootstrap 引导。"
            )

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


def _load_config_file(explicit: str | None) -> dict:
    """读取 YAML 配置文件；不存在时返回空 dict（全部走默认值/环境变量）。

    解析顺序：`APP_CONFIG_FILE` 显式指定（绝对路径 / 相对工作目录 /
    相对项目根目录依次尝试）→ 项目根目录 `config.yaml` → 工作目录
    `config.yaml`。文件存在但内容非法（非映射 / YAML 语法错误）直接抛错。
    """
    candidates: list[Path] = []
    if explicit:
        p = Path(explicit)
        candidates.append(p if p.is_absolute() else Path.cwd() / p)
        candidates.append(PROJECT_ROOT / explicit)
    else:
        candidates.append(PROJECT_ROOT / DEFAULT_CONFIG_FILE)
        candidates.append(Path.cwd() / DEFAULT_CONFIG_FILE)
    for path in candidates:
        if not path.is_file():
            continue
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise RuntimeError(
                f"配置文件 {path} 顶层必须是键值映射（YAML mapping），"
                f"当前为 {type(data).__name__}"
            )
        return data
    return {}


def _pick(file_values: dict, key: str, env_name: str, default):
    """取值优先级：环境变量 > 配置文件 > 默认值。"""
    raw = os.environ.get(env_name)
    if raw is None:
        raw = file_values.get(key)
    return default if raw is None else raw


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_float(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"配置值 {value!r} 不是合法数字") from exc
    return max(0.0, min(number, 1.0))


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"配置值 {value!r} 不是合法整数") from exc


settings = Settings.from_env()
