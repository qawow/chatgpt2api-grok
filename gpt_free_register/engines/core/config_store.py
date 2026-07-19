"""全局配置持久化 - 存储在 SQLite"""
from sqlmodel import Session, select

from .db import ConfigItem, engine


class ConfigStore:
    """简单 key-value 配置存储"""

    def get(self, key: str, default: str = "") -> str:
        with Session(engine) as s:
            item = s.get(ConfigItem, key)
            return item.value if item else default

    def set(self, key: str, value: str) -> None:
        with Session(engine) as s:
            item = s.get(ConfigItem, key)
            if item:
                item.value = value
            else:
                item = ConfigItem(key=key, value=value)
            s.add(item)
            s.commit()

    def get_all(self) -> dict:
        with Session(engine) as s:
            items = s.exec(select(ConfigItem)).all()
            return {i.key: i.value for i in items}

    def set_many(self, data: dict) -> None:
        with Session(engine) as s:
            for key, value in data.items():
                item = s.get(ConfigItem, key)
                if item:
                    item.value = value
                else:
                    item = ConfigItem(key=key, value=value)
                s.add(item)
            s.commit()


config_store = ConfigStore()
