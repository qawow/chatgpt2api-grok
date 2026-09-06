"use client";

import { useEffect, useMemo, useState } from "react";
import { Copy, Download } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import webConfig from "@/constants/common-env";
import { fetchSettingsConfig } from "@/lib/api";
import { getStoredAuthSession } from "@/store/auth";

export function SkillPanel() {
  const [browserBaseUrl, setBrowserBaseUrl] = useState("");
  const [configuredBaseUrl, setConfiguredBaseUrl] = useState("");
  const [authKey, setAuthKey] = useState("");

  useEffect(() => {
    setBrowserBaseUrl(window.location.origin);
    void fetchSettingsConfig().then((data) => setConfiguredBaseUrl(String(data.config.base_url || "").replace(/\/$/, ""))).catch(() => undefined);
    void getStoredAuthSession().then((session) => setAuthKey(session?.key || ""));
  }, []);

  const apiBaseUrl = configuredBaseUrl || webConfig.apiUrl.replace(/\/$/, "") || browserBaseUrl;
  const skillZh = useMemo(() => `---
name: chatgpt2api-image
description: 当用户需要生成或编辑图片时，调用本地 chatgpt2api 生图接口。
---

# ChatGPT2API 生图

当用户要求生成图片、改图、出海报或插画时，使用这个 skill。当前服务只开放生图模型，不要调用搜索或纯文本聊天接口。

## 接口

POST ${apiBaseUrl}/v1/images/generations

Headers:

Authorization: Bearer ${authKey}
Content-Type: application/json

Body:

{
  "model": "gpt-image-2",
  "prompt": "<用户的生图提示词>",
  "n": 1,
  "response_format": "b64_json"
}

可用模型：\`gpt-image-2\`、\`codex-gpt-image-2\`、\`grok-imagine-image\`、\`grok-2-image\`。

改图使用 \`POST ${apiBaseUrl}/v1/images/edits\`，上传参考图并附带 prompt。

## 返回处理

- 优先使用 \`data[].url\`；没有 URL 时再解码 \`data[].b64_json\`。
- 如果接口报错，简要说明错误并询问是否重试。`, [apiBaseUrl, authKey]);

  const skillEn = useMemo(() => `---
name: chatgpt2api-image
description: Use when the user needs image generation or image edits through this chatgpt2api server.
---

# ChatGPT2API Image

Use this skill when the user asks to generate or edit images. This backend only exposes image models. Do not call search or plain-text chat endpoints.

## Request

POST ${apiBaseUrl}/v1/images/generations

Headers:

Authorization: Bearer ${authKey}
Content-Type: application/json

JSON body:

{
  "model": "gpt-image-2",
  "prompt": "<image prompt>",
  "n": 1,
  "response_format": "b64_json"
}

Models: \`gpt-image-2\`, \`codex-gpt-image-2\`, \`grok-imagine-image\`, \`grok-2-image\`.

For edits, POST ${apiBaseUrl}/v1/images/edits with a reference image and prompt.

## Response handling

- Prefer \`data[].url\`; otherwise decode \`data[].b64_json\`.
- If the endpoint returns an error, summarize it and ask whether to retry.`, [apiBaseUrl, authKey]);

  const zhPrompt = useMemo(() => `请帮我在本机安装一个用于本地生图的 skill。

要求：
1. 请按你当前环境的 skill 安装规范，把它安装成本地 skill。
2. skill 名称为：chatgpt2api-image
3. 文件名为：SKILL.md
4. 如果你无法确定本地 skills 目录在哪里，先告诉我需要放到哪个目录，不要猜路径。
5. 只创建或更新这个 skill 文件，不要修改其他无关文件。
6. SKILL.md 请写入下面的完整内容。

SKILL.md 内容：

\`\`\`markdown
${skillZh}
\`\`\``, [skillZh]);

  const enPrompt = useMemo(() => `Please install a local image-generation skill on this machine.

Requirements:
1. Install this as a local skill according to the skill installation rules of your current environment.
2. Skill name: chatgpt2api-image
3. File name: SKILL.md
4. If you cannot determine the local skills directory, tell me which directory is required before writing files.
5. Only create or update this skill file. Do not modify unrelated files.
6. Write the full content below into SKILL.md.

SKILL.md content:

\`\`\`markdown
${skillEn}
\`\`\``, [skillEn]);

  const copyText = async (text: string) => {
    await navigator.clipboard.writeText(text);
    toast.success("已复制");
  };

  const downloadSkill = (text: string) => {
    const url = URL.createObjectURL(new Blob([text], { type: "text/markdown;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = "SKILL.md";
    link.click();
    URL.revokeObjectURL(url);
  };

  const versions = [
    { title: "中文安装指令", desc: "复制后直接发给 Codex 或 Claude，让它安装到本地。", prompt: zhPrompt, skill: skillZh },
    { title: "English install prompt", desc: "Copy and send this to Codex or Claude to install locally.", prompt: enPrompt, skill: skillEn },
  ];

  return (
    <section className="grid items-stretch gap-4 lg:grid-cols-2">
      {versions.map((item) => (
        <div key={item.title} className="flex flex-col overflow-hidden rounded-lg border border-slate-200 bg-white shadow-sm dark:border-white/10 dark:bg-white/[0.04]">
          <div className="flex items-center justify-between gap-3 border-b border-slate-200/70 bg-slate-50/80 p-4 dark:border-white/10 dark:bg-white/[0.03]">
            <div>
              <h2 className="font-medium text-slate-900 dark:text-slate-100">{item.title}</h2>
              <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">{item.desc}</p>
            </div>
            <div className="flex gap-2">
              <Button size="sm" variant="outline" className="cursor-pointer" onClick={() => downloadSkill(item.skill)}>
                <Download />
                下载
              </Button>
              <Button size="sm" className="cursor-pointer" onClick={() => void copyText(item.prompt)}>
                <Copy />
                复制
              </Button>
            </div>
          </div>
          <pre className="flex-1 whitespace-pre-wrap p-4 font-mono text-sm leading-6">
            {item.prompt}
          </pre>
        </div>
      ))}
    </section>
  );
}
