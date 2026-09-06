"use client";

import { useEffect, useState } from "react";
import { ChevronDown, FileArchive, KeyRound, ListChecks, type LucideIcon } from "lucide-react";

import { Card, CardContent } from "@/components/ui/card";
import webConfig from "@/constants/common-env";
import { getStoredAuthSession } from "@/store/auth";

type ParamRow = [string, string, string];

type ApiDoc = {
  title: string;
  method: string;
  path: string;
  icon: LucideIcon;
  input: ParamRow[];
  output: ParamRow[];
  example: (baseUrl: string, key: string) => string;
};

const docs: ApiDoc[] = [
  {
    title: "模型列表",
    method: "GET",
    path: "/v1/models",
    icon: ListChecks,
    input: [
      ["Authorization", "header", "Bearer <auth-key>。"],
    ],
    output: [
      ["data", "array", "模型列表，包含 id、object、created、owned_by。"],
    ],
    example: (baseUrl: string, key: string) => `curl ${baseUrl}/models \\
  -H "Authorization: Bearer ${key}"`,
  },
  {
    title: "聊天补全",
    method: "POST",
    path: "/v1/chat/completions",
    icon: FileText,
    input: [
      ["model", "string", "仅生图：gpt-image-2 / grok-imagine-image / grok-2-image。grok-4.5 是对话模型，已关闭。"],
      ["messages", "array", "OpenAI 兼容消息数组。"],
      ["stream", "boolean", "可选，是否流式返回。"],
      ["n", "number", "可选，图片兼容场景会解析为生成数量。"],
    ],
    output: [
      ["id", "string", "响应 ID。"],
      ["choices", "array", "OpenAI 兼容 choices。"],
      ["usage", "object", "可选，token 使用信息。"],
    ],
    example: (baseUrl: string, key: string) => `curl ${baseUrl}/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${key}" \\
  -d '{"model":"gpt-image-2","messages":[{"role":"user","content":"一只橘猫"}]}'`,
  },
  {
    title: "Responses",
    method: "POST",
    path: "/v1/responses",
    icon: FileText,
    input: [
      ["model", "string", "模型名。"],
      ["input", "string | array | object", "用户输入，图片生成会从中解析提示词。"],
      ["tools", "array", "可选，Responses 工具定义。"],
      ["stream", "boolean", "可选，是否流式返回。"],
    ],
    output: [
      ["id", "string", "响应 ID。"],
      ["output", "array", "Responses 兼容输出。"],
      ["status", "string", "响应状态。"],
    ],
    example: (baseUrl: string, key: string) => `curl ${baseUrl}/responses \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${key}" \\
  -d '{"model":"gpt-image-2","input":"生成一张未来城市图片","tools":[{"type":"image_generation"}]}'`,
  },
  {
    title: "搜索",
    method: "POST",
    path: "/v1/search",
    icon: ListChecks,
    input: [
      ["prompt", "string", "搜索问题或检索指令。"],
    ],
    output: [
      ["answer", "string", "搜索后的回答内容，具体字段以返回结果为准。"],
      ["sources", "array", "可选，搜索引用来源。"],
      ["_account_email", "string", "本次使用的账号邮箱。"],
    ],
    example: (baseUrl: string, key: string) => `curl ${baseUrl}/search \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${key}" \\
  -d '{"prompt":"搜索 chatgpt2api 最新使用方式"}'`,
  },
  {
    title: "图片生成",
    method: "POST",
    path: "/v1/images/generations",
    icon: FileArchive,
    input: [
      ["prompt", "string", "图片生成提示词。"],
      ["model", "string", "可选，默认 gpt-image-2。"],
      ["n", "number", "可选，生成数量，当前限制 1-4。"],
      ["size", "string", "可选，图片尺寸。"],
      ["quality", "string", "可选，默认 auto。"],
      ["response_format", "string", "可选，默认 b64_json。"],
    ],
    output: [
      ["data", "array", "图片结果列表。"],
      ["data[].b64_json", "string", "base64 图片内容。"],
      ["data[].url", "string", "部分配置下返回图片 URL。"],
    ],
    example: (baseUrl: string, key: string) => `curl ${baseUrl}/images/generations \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${key}" \\
  -d '{"model":"gpt-image-2","prompt":"一张极简产品海报","n":1}'`,
  },
  {
    title: "图片编辑",
    method: "POST",
    path: "/v1/images/edits",
    icon: FileArchive,
    input: [
      ["image", "file | file[] | URL", "参考图，支持 multipart 上传，也支持 JSON 图片链接。"],
      ["prompt", "string", "编辑提示词。"],
      ["model", "string", "可选，默认 gpt-image-2。"],
      ["n", "number", "可选，生成数量，当前限制 1-4。"],
      ["size", "string", "可选，图片尺寸。"],
      ["quality", "string", "可选，默认 auto。"],
    ],
    output: [
      ["data", "array", "编辑后的图片结果列表。"],
      ["data[].b64_json", "string", "base64 图片内容。"],
      ["data[].url", "string", "部分配置下返回图片 URL。"],
    ],
    example: (baseUrl: string, key: string) => `curl ${baseUrl}/images/edits \\
  -H "Authorization: Bearer ${key}" \\
  -F "model=gpt-image-2" \\
  -F "prompt=改成赛博朋克夜景" \\
  -F "image=@./input.png"`,
  },
];

const usableModels = ["gpt-image-2", "codex-gpt-image-2", "grok-2-image", "grok-imagine-image", "grok-imagine"];

function ParamTable({ rows }: { rows: ParamRow[] }) {
  return (
    <div className="overflow-hidden rounded-lg border border-stone-200">
      <table className="w-full text-left text-xs">
        <thead className="bg-stone-50 text-stone-500">
          <tr>
            <th className="px-3 py-2 font-medium">参数</th>
            <th className="px-3 py-2 font-medium">类型</th>
            <th className="px-3 py-2 font-medium">说明</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-stone-100 bg-white">
          {rows.map(([name, type, desc]) => (
            <tr key={name}>
              <td className="px-3 py-2 font-mono text-stone-800">{name}</td>
              <td className="px-3 py-2 font-mono text-stone-500">{type}</td>
              <td className="px-3 py-2 text-stone-600">{desc}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ApiDocsCard() {
  const [authKey, setAuthKey] = useState("");
  const serviceBaseUrl = webConfig.apiUrl.replace(/\/$/, "") || (typeof window !== "undefined" ? window.location.origin : "");
  const openAIBaseUrl = `${serviceBaseUrl}/v1`;
  const displayKey = authKey || "<当前密钥>";

  useEffect(() => {
    let active = true;
    void getStoredAuthSession().then((session) => {
      if (active) setAuthKey(session?.key || "");
    });
    return () => {
      active = false;
    };
  }, []);

  return (
    <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
      <CardContent className="space-y-5 p-6">
        <div>
          <div className="flex items-center gap-2 text-base font-semibold text-stone-900">
            <KeyRound className="size-5 text-stone-500" />
            接口接入说明
          </div>
          <p className="mt-1 text-xs leading-6 text-stone-500">
            第三方应用按 OpenAI 兼容接口接入；文件任务接口也使用同一套鉴权方式。
          </p>
        </div>

        <div className="grid gap-3 md:grid-cols-2">
          <div className="space-y-1 rounded-xl border border-stone-200 bg-white px-3 py-2">
            <div className="text-xs text-stone-500">服务地址</div>
            <div className="break-all font-mono text-xs text-stone-800">{serviceBaseUrl}</div>
          </div>
          <div className="space-y-1 rounded-xl border border-stone-200 bg-white px-3 py-2">
            <div className="text-xs text-stone-500">Base URL（OpenAI）</div>
            <div className="break-all font-mono text-xs text-stone-800">{openAIBaseUrl}</div>
          </div>
          <div className="space-y-1 rounded-xl border border-stone-200 bg-white px-3 py-2">
            <div className="text-xs text-stone-500">API Key</div>
            <div className="break-all font-mono text-xs text-stone-800">{displayKey}</div>
          </div>
          <div className="space-y-1 rounded-xl border border-stone-200 bg-white px-3 py-2">
            <div className="text-xs text-stone-500">请求头</div>
            <div className="break-all font-mono text-xs text-stone-800">Authorization: Bearer {displayKey}</div>
          </div>
        </div>

        <div className="space-y-2">
          <div className="text-xs font-medium text-stone-600">常用模型，也可请求 /v1/models 获取</div>
          <div className="flex flex-wrap gap-2">
            {usableModels.map((model) => (
              <span key={model} className="rounded-md border border-stone-200 bg-white px-2 py-1 font-mono text-xs text-stone-700">{model}</span>
            ))}
          </div>
        </div>

        <div className="space-y-3">
          {docs.map((item) => {
            const Icon = item.icon;
            return (
              <details key={item.path} className="group rounded-xl border border-stone-200 bg-white px-4 py-3">
                <summary className="flex cursor-pointer list-none items-center justify-between gap-3">
                  <span className="flex min-w-0 items-center gap-3">
                    <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-stone-100 text-stone-600">
                      <Icon className="size-4" />
                    </span>
                    <span className="min-w-0">
                      <span className="block text-sm font-semibold text-stone-900">{item.title}</span>
                      <span className="mt-1 block truncate font-mono text-xs text-stone-500">{item.method} {item.path}</span>
                    </span>
                  </span>
                  <ChevronDown className="size-4 shrink-0 text-stone-400 transition group-open:rotate-180" />
                </summary>

                <div className="mt-4 grid gap-4 lg:grid-cols-2">
                  <div className="space-y-2">
                    <h3 className="text-xs font-semibold text-stone-700">输入参数</h3>
                    <ParamTable rows={item.input} />
                  </div>
                  <div className="space-y-2">
                    <h3 className="text-xs font-semibold text-stone-700">输出参数</h3>
                    <ParamTable rows={item.output} />
                  </div>
                  <div className="space-y-2 lg:col-span-2">
                    <h3 className="text-xs font-semibold text-stone-700">调用示例</h3>
                    <pre className="overflow-auto whitespace-pre-wrap break-all rounded-xl bg-stone-950 px-3 py-3 text-xs leading-5 text-stone-100">{item.example(openAIBaseUrl, displayKey)}</pre>
                  </div>
                </div>
              </details>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}
