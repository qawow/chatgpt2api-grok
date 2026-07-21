from __future__ import annotations

import os

import uvicorn
from api import create_app

app = create_app()

if __name__ == "__main__":
    # Bind all interfaces so LAN clients can reach this host.
    # Override with CHATGPT2API_HOST / CHATGPT2API_PORT if needed.
    host = str(os.environ.get("CHATGPT2API_HOST") or "0.0.0.0").strip() or "0.0.0.0"
    port = int(os.environ.get("CHATGPT2API_PORT") or "8000")
    uvicorn.run(app, host=host, port=port, access_log=False, log_level="info")
