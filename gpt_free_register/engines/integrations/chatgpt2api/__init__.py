"""ChatGPT 注册结果 → chatgpt2api 号池桥接。"""

from .client import ChatGPT2APIClient, ChatGPT2APIError
from .mapper import map_register_result_to_account

__all__ = [
    "ChatGPT2APIClient",
    "ChatGPT2APIError",
    "map_register_result_to_account",
]
