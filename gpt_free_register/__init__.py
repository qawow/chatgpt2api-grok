"""In-process GPT free registrar (vendored any-register-engines, ChatGPT only).

This package is self-contained under ``chatgpt2api/gpt_free_register/`` so
deployments no longer require a separate ``/root/any-register-engines`` tree.

Layout::

    gpt_free_register/
      __init__.py
      runner.py          # process-local register() entry
      engines/           # vendored engines (chatgpt platform + core + providers)
      README.md

Secrets (CFD1 token, proxy password, etc.) stay outside the tree:

- process env
- ``data/gpt_register.env``
- optional ``gpt_free_register/engines/.env`` (local only, gitignored)
"""
from __future__ import annotations

from gpt_free_register.runner import (
    ENGINES_DIR,
    default_engines_dir,
    register_chatgpt_once,
)

__all__ = [
    "ENGINES_DIR",
    "default_engines_dir",
    "register_chatgpt_once",
]
