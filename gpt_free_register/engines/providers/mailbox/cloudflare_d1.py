"""CloudflareD1Mailbox — Email Routing + Worker + D1 HTTP API."""
from core.base_mailbox import CloudflareD1Mailbox  # noqa: F401
from providers.registry import register_provider

register_provider("mailbox", "cloudflare_d1_api")(CloudflareD1Mailbox)
register_provider("mailbox", "cloudflare_d1")(CloudflareD1Mailbox)
register_provider("mailbox", "cfd1")(CloudflareD1Mailbox)
