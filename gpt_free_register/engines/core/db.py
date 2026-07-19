"""精简数据库层 —— 只保留注册机运行所需的 provider / 平台能力表。

从 any-auto-register 的 core/db.py 剥离账号池、任务队列、生命周期等
非注册表结构，方便二次开发时独立使用注册引擎。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import UniqueConstraint, inspect
from sqlmodel import Field, SQLModel, Session, create_engine, select


def _utcnow():
    return datetime.now(timezone.utc)


def _default_database_url() -> str:
    database_path = Path(__file__).resolve().parent.parent / "register_engines.db"
    return f"sqlite:///{database_path}"


DATABASE_URL = os.getenv("REGISTER_ENGINES_DATABASE_URL") or os.getenv(
    "ACCOUNT_MANAGER_DATABASE_URL", _default_database_url()
)
engine = create_engine(DATABASE_URL)


class ProviderDefinitionModel(SQLModel, table=True):
    __tablename__ = "provider_definitions"
    __table_args__ = (
        UniqueConstraint("provider_type", "provider_key", name="uq_provider_definitions_type_key"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    provider_type: str = Field(index=True)
    provider_key: str = Field(index=True)
    label: str = ""
    description: str = ""
    driver_type: str = ""
    default_auth_mode: str = ""
    enabled: bool = True
    is_builtin: bool = False
    category: str = ""
    auth_modes_json: str = "[]"
    fields_json: str = "[]"
    metadata_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_auth_modes(self) -> list[dict]:
        return json.loads(self.auth_modes_json or "[]")

    def set_auth_modes(self, data: list[dict]):
        self.auth_modes_json = json.dumps(data or [], ensure_ascii=False)

    def get_fields(self) -> list[dict]:
        return json.loads(self.fields_json or "[]")

    def set_fields(self, data: list[dict]):
        self.fields_json = json.dumps(data or [], ensure_ascii=False)

    def get_metadata(self) -> dict:
        return json.loads(self.metadata_json or "{}")

    def set_metadata(self, data: dict):
        self.metadata_json = json.dumps(data or {}, ensure_ascii=False)


class ProviderSettingModel(SQLModel, table=True):
    __tablename__ = "provider_settings"
    __table_args__ = (
        UniqueConstraint("provider_type", "provider_key", name="uq_provider_settings_type_key"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    provider_type: str = Field(index=True)
    provider_key: str = Field(index=True)
    display_name: str = ""
    auth_mode: str = ""
    enabled: bool = True
    is_default: bool = False
    config_json: str = "{}"
    auth_json: str = "{}"
    metadata_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_config(self) -> dict:
        return json.loads(self.config_json or "{}")

    def set_config(self, data: dict):
        self.config_json = json.dumps(data or {}, ensure_ascii=False)

    def get_auth(self) -> dict:
        return json.loads(self.auth_json or "{}")

    def set_auth(self, data: dict):
        self.auth_json = json.dumps(data or {}, ensure_ascii=False)

    def get_metadata(self) -> dict:
        return json.loads(self.metadata_json or "{}")

    def set_metadata(self, data: dict):
        self.metadata_json = json.dumps(data or {}, ensure_ascii=False)


class PlatformCapabilityOverrideModel(SQLModel, table=True):
    __tablename__ = "platform_capability_overrides"
    __table_args__ = (
        UniqueConstraint("platform_name", name="uq_platform_capability_overrides_platform"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    platform_name: str = Field(index=True)
    capabilities_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_capabilities(self) -> dict:
        return json.loads(self.capabilities_json or "{}")

    def set_capabilities(self, data: dict):
        self.capabilities_json = json.dumps(data or {}, ensure_ascii=False)


class ProxyModel(SQLModel, table=True):
    """可选静态代理池（协议注册时可直接在 config.proxy 传入，不必用库）。"""

    __tablename__ = "proxies"

    id: Optional[int] = Field(default=None, primary_key=True)
    url: str = Field(index=True)
    region: str = ""
    is_active: bool = True
    success_count: int = 0
    fail_count: int = 0
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class ConfigItem(SQLModel, table=True):
    __tablename__ = "configs"
    key: str = Field(primary_key=True)
    value: str = ""


def _ensure_column(table: str, column: str, col_type: str):
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if table not in tables:
        return
    existing = {c["name"] for c in inspector.get_columns(table)}
    if column in existing:
        return
    with engine.begin() as conn:
        conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def init_db():
    """初始化注册引擎所需的最小表结构，并 seed 内置 provider 定义。"""
    SQLModel.metadata.create_all(engine)
    _ensure_column("provider_definitions", "category", "TEXT DEFAULT ''")
    SQLModel.metadata.create_all(engine)

    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository

    with Session(engine) as session:
        ProviderDefinitionsRepository().ensure_seeded()
        session.commit()


def get_session():
    return Session(engine)
