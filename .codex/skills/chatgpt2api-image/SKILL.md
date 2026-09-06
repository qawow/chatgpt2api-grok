---
name: chatgpt2api-image
description: Use when the user needs image generation or image edits through this chatgpt2api server.
---

# ChatGPT2API Image

Use this skill when the user asks to generate or edit images. This backend only exposes image models.

## Endpoint

POST http://127.0.0.1:8000/v1/images/generations

Headers:

Authorization: Bearer chatgpt2api
Content-Type: application/json

Body:

{
  "model": "gpt-image-2",
  "prompt": "<image prompt>",
  "n": 1,
  "response_format": "b64_json"
}

Models: `gpt-image-2`, `codex-gpt-image-2`, `grok-imagine-image`, `grok-2-image`.

For edits, POST http://127.0.0.1:8000/v1/images/edits with a reference image and prompt.

## Response

Prefer `data[].url`; otherwise decode `data[].b64_json`.
