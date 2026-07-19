"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  LoaderCircle,
  Play,
  Save,
  Square,
  UserPlus,
} from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import {
  cancelGptRegisterJob,
  fetchGptRegisterJob,
  fetchGptRegisterJobs,
  fetchGptRegisterSettings,
  saveGptRegisterSettings,
  startGptRegisterJob,
  type GptRegisterJob,
  type GptRegisterSettings,
} from "@/lib/api";

const DEFAULT_FORM: GptRegisterSettings = {
  engines_dir: "", // empty → builtin gpt_free_register/engines
  python_bin: "",
  count: 1,
  concurrency: 1,
  interval_secs: 2,
  timeout_secs: 600,
  executor: "protocol",
  mail_provider: "cloudflare_d1_api",
  captcha: "",
  proxy: "",
  bind_register_proxy: true,
  plan_type: "free",
  source_type: "",
  cfd1_domain: "",
  push_enabled: true,
  push_mode: "local",
  chatgpt2api_base_url: "",
  chatgpt2api_auth_key: "",
  has_chatgpt2api_auth_key: false,
  dry_run: false,
};

function isActiveJob(job?: GptRegisterJob | null) {
  return job?.status === "pending" || job?.status === "running";
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-2">
      <label className="text-sm text-stone-700">{label}</label>
      {children}
      {hint ? <p className="text-xs text-stone-500">{hint}</p> : null}
    </div>
  );
}

export function GptRegisterCard() {
  const didLoadRef = useRef(false);
  const [form, setForm] = useState<GptRegisterSettings>(DEFAULT_FORM);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [isStarting, setIsStarting] = useState(false);
  const [isCancelling, setIsCancelling] = useState(false);
  const [job, setJob] = useState<GptRegisterJob | null>(null);

  const setField = <K extends keyof GptRegisterSettings>(key: K, value: GptRegisterSettings[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  const load = async (silent = false) => {
    if (!silent) setIsLoading(true);
    try {
      const [settingsRes, jobsRes] = await Promise.all([
        fetchGptRegisterSettings(),
        fetchGptRegisterJobs(),
      ]);
      setForm({
        ...DEFAULT_FORM,
        ...settingsRes.settings,
        chatgpt2api_auth_key: "",
      });
      const jobs = jobsRes.jobs || [];
      const active = jobs.find((item) => isActiveJob(item)) || jobs[0] || null;
      setJob(active);
    } catch (error) {
      if (!silent) {
        toast.error(error instanceof Error ? error.message : "加载 GPT 注册配置失败");
      }
    } finally {
      if (!silent) setIsLoading(false);
    }
  };

  useEffect(() => {
    if (didLoadRef.current) return;
    didLoadRef.current = true;
    void load();
  }, []);

  useEffect(() => {
    if (!isActiveJob(job)) return;
    const timer = window.setInterval(() => {
      void (async () => {
        try {
          if (!job?.job_id) return;
          const data = await fetchGptRegisterJob(job.job_id);
          setJob(data.job);
        } catch {
          // keep last known job state
        }
      })();
    }, 2000);
    return () => window.clearInterval(timer);
  }, [job?.job_id, job?.status]);

  const progress = useMemo(() => {
    if (!job?.total) return 0;
    return Math.min(100, Math.round((job.completed / job.total) * 100));
  }, [job?.completed, job?.total]);

  const saveSettings = async () => {
    setIsSaving(true);
    try {
      const payload: Partial<GptRegisterSettings> = {
        ...form,
        count: Number(form.count) || 1,
        concurrency: Number(form.concurrency) || 1,
        interval_secs: Number(form.interval_secs) || 0,
        timeout_secs: Number(form.timeout_secs) || 600,
      };
      if (!String(payload.chatgpt2api_auth_key || "").trim()) {
        delete payload.chatgpt2api_auth_key;
      }
      const data = await saveGptRegisterSettings(payload);
      setForm({
        ...DEFAULT_FORM,
        ...data.settings,
        chatgpt2api_auth_key: "",
      });
      toast.success("注册配置已保存");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存失败");
    } finally {
      setIsSaving(false);
    }
  };

  const startJob = async () => {
    setIsStarting(true);
    try {
      // save first so defaults stick
      const payload: Partial<GptRegisterSettings> = {
        ...form,
        count: Number(form.count) || 1,
        concurrency: Number(form.concurrency) || 1,
        interval_secs: Number(form.interval_secs) || 0,
        timeout_secs: Number(form.timeout_secs) || 600,
      };
      if (!String(payload.chatgpt2api_auth_key || "").trim()) {
        delete payload.chatgpt2api_auth_key;
      }
      await saveGptRegisterSettings(payload);
      const data = await startGptRegisterJob(payload);
      setJob(data.job);
      toast.success("批量注册已启动");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "启动失败");
    } finally {
      setIsStarting(false);
    }
  };

  const cancelJob = async () => {
    if (!job?.job_id) return;
    setIsCancelling(true);
    try {
      const data = await cancelGptRegisterJob(job.job_id);
      setJob(data.job);
      toast.success("已请求取消");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "取消失败");
    } finally {
      setIsCancelling(false);
    }
  };

  const running = isActiveJob(job);

  return (
    <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
      <CardContent className="space-y-6 p-6">
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-center gap-3">
            <div className="flex size-10 items-center justify-center rounded-xl bg-stone-100">
              <UserPlus className="size-5 text-stone-600" />
            </div>
            <div>
              <h2 className="text-lg font-semibold tracking-tight">GPT Free 批量注册</h2>
              <p className="text-sm text-stone-500">
                内置 gpt_free_register 模块纯协议注册 ChatGPT free 号，成功后自动写入本机号池。
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {running ? <Badge className="rounded-md px-2.5 py-1">运行中</Badge> : null}
            <Button
              variant="outline"
              className="h-9 rounded-xl border-stone-200 bg-white px-4 text-stone-700"
              onClick={() => void saveSettings()}
              disabled={isSaving || isLoading || running}
            >
              {isSaving ? <LoaderCircle className="size-4 animate-spin" /> : <Save className="size-4" />}
              保存配置
            </Button>
            {running ? (
              <Button
                variant="outline"
                className="h-9 rounded-xl border-rose-200 bg-white px-4 text-rose-600 hover:bg-rose-50"
                onClick={() => void cancelJob()}
                disabled={isCancelling}
              >
                {isCancelling ? (
                  <LoaderCircle className="size-4 animate-spin" />
                ) : (
                  <Square className="size-4" />
                )}
                取消任务
              </Button>
            ) : (
              <Button
                className="h-9 rounded-xl bg-stone-950 px-4 text-white hover:bg-stone-800"
                onClick={() => void startJob()}
                disabled={isStarting || isLoading}
              >
                {isStarting ? (
                  <LoaderCircle className="size-4 animate-spin" />
                ) : (
                  <Play className="size-4" />
                )}
                开始注册
              </Button>
            )}
          </div>
        </div>

        {isLoading ? (
          <div className="flex items-center justify-center py-10">
            <LoaderCircle className="size-5 animate-spin text-stone-400" />
          </div>
        ) : (
          <>
            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
              <Field label="注册数量" hint="单次任务 1-50">
                <Input
                  type="number"
                  min={1}
                  max={50}
                  value={String(form.count)}
                  onChange={(e) => setField("count", Number(e.target.value) || 1)}
                  className="h-10 rounded-xl border-stone-200 bg-white"
                  disabled={running}
                />
              </Field>
              <Field label="并发" hint="建议 1；邮箱/代理不稳时不要超过 2">
                <Input
                  type="number"
                  min={1}
                  max={5}
                  value={String(form.concurrency)}
                  onChange={(e) => setField("concurrency", Number(e.target.value) || 1)}
                  className="h-10 rounded-xl border-stone-200 bg-white"
                  disabled={running}
                />
              </Field>
              <Field label="间隔（秒）" hint="每批之间的等待">
                <Input
                  type="number"
                  min={0}
                  max={600}
                  step={0.5}
                  value={String(form.interval_secs)}
                  onChange={(e) => setField("interval_secs", Number(e.target.value) || 0)}
                  className="h-10 rounded-xl border-stone-200 bg-white"
                  disabled={running}
                />
              </Field>
              <Field label="单号超时（秒）">
                <Input
                  type="number"
                  min={60}
                  max={3600}
                  value={String(form.timeout_secs)}
                  onChange={(e) => setField("timeout_secs", Number(e.target.value) || 600)}
                  className="h-10 rounded-xl border-stone-200 bg-white"
                  disabled={running}
                />
              </Field>
              <Field label="执行器" hint="protocol = 纯协议（推荐）">
                <select
                  value={form.executor}
                  onChange={(e) => setField("executor", e.target.value)}
                  disabled={running}
                  className="h-10 w-full rounded-xl border border-stone-200 bg-white px-3 text-sm"
                >
                  <option value="protocol">protocol</option>
                  <option value="headless">headless</option>
                  <option value="headed">headed</option>
                </select>
              </Field>
              <Field label="邮箱 Provider">
                <Input
                  value={form.mail_provider}
                  onChange={(e) => setField("mail_provider", e.target.value)}
                  placeholder="cloudflare_d1_api"
                  className="h-10 rounded-xl border-stone-200 bg-white"
                  disabled={running}
                />
              </Field>
              <Field label="CFD1 域名覆盖" hint="留空用 data/gpt_register.env 或环境变量 CFD1_DOMAIN">
                <Input
                  value={form.cfd1_domain}
                  onChange={(e) => setField("cfd1_domain", e.target.value)}
                  placeholder="mail.example.com"
                  className="h-10 rounded-xl border-stone-200 bg-white"
                  disabled={running}
                />
              </Field>
              <Field label="验证码 Provider" hint="留空则用注册机默认/自动">
                <Input
                  value={form.captcha}
                  onChange={(e) => setField("captcha", e.target.value)}
                  placeholder="yescaptcha_api / auto / 留空"
                  className="h-10 rounded-xl border-stone-200 bg-white"
                  disabled={running}
                />
              </Field>
              <Field label="出站代理" hint="留空读 REGISTER_PROXY_DEFAULT；WARP 例 socks5h://127.0.0.1:40000">
                <Input
                  value={form.proxy}
                  onChange={(e) => setField("proxy", e.target.value)}
                  placeholder="socks5h://127.0.0.1:40000"
                  className="h-10 rounded-xl border-stone-200 bg-white"
                  disabled={running}
                />
              </Field>
              <Field label="入库 plan_type">
                <Input
                  value={form.plan_type}
                  onChange={(e) => setField("plan_type", e.target.value)}
                  placeholder="free"
                  className="h-10 rounded-xl border-stone-200 bg-white"
                  disabled={running}
                />
              </Field>
              <Field label="source_type" hint="留空自动（register / codex）">
                <Input
                  value={form.source_type}
                  onChange={(e) => setField("source_type", e.target.value)}
                  placeholder="register"
                  className="h-10 rounded-xl border-stone-200 bg-white"
                  disabled={running}
                />
              </Field>
              <Field label="推送 Base URL" hint="默认 push_mode=local 不走 HTTP；http 模式 Docker 内用 :80，宿主机开发用 :8000">
                <Input
                  value={form.chatgpt2api_base_url}
                  onChange={(e) => setField("chatgpt2api_base_url", e.target.value)}
                  className="h-10 rounded-xl border-stone-200 bg-white"
                  placeholder="(auto)"
                  disabled={running}
                />
              </Field>
              <Field
                label="推送 Auth Key"
                hint={
                  form.has_chatgpt2api_auth_key
                    ? "已配置（留空保持原值；也可填本机 auth-key）"
                    : "留空则使用本机 config.auth_key"
                }
              >
                <Input
                  type="password"
                  value={form.chatgpt2api_auth_key || ""}
                  onChange={(e) => setField("chatgpt2api_auth_key", e.target.value)}
                  placeholder={form.has_chatgpt2api_auth_key ? "••••••••" : "auth-key"}
                  className="h-10 rounded-xl border-stone-200 bg-white"
                  disabled={running}
                />
              </Field>
              <Field
                label="注册机目录"
                hint="留空=内置 gpt_free_register/engines（推荐）。旧路径 /root/any-register-engines 会自动迁移。"
              >
                <Input
                  value={form.engines_dir}
                  onChange={(e) => setField("engines_dir", e.target.value)}
                  placeholder="(builtin) gpt_free_register/engines"
                  className="h-10 rounded-xl border-stone-200 bg-white"
                  disabled={running}
                />
              </Field>
              <Field label="Python 路径" hint="仅 subprocess 模式需要；默认 inprocess 不走外部 Python">
                <Input
                  value={form.python_bin}
                  onChange={(e) => setField("python_bin", e.target.value)}
                  placeholder="(inprocess 模式可留空)"
                  className="h-10 rounded-xl border-stone-200 bg-white"
                  disabled={running}
                />
              </Field>
            </div>

            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
              <label className="flex items-center gap-3 rounded-xl border border-stone-200 bg-white px-4 py-3 text-sm text-stone-700">
                <Checkbox
                  checked={Boolean(form.push_enabled)}
                  onCheckedChange={(checked) => setField("push_enabled", Boolean(checked))}
                  disabled={running}
                />
                成功后推送到本机号池
              </label>
              <label className="flex items-center gap-3 rounded-xl border border-stone-200 bg-white px-4 py-3 text-sm text-stone-700">
                <Checkbox
                  checked={Boolean(form.bind_register_proxy)}
                  onCheckedChange={(checked) => setField("bind_register_proxy", Boolean(checked))}
                  disabled={running}
                />
                把注册代理绑定到账号
              </label>
              <label className="flex items-center gap-3 rounded-xl border border-stone-200 bg-white px-4 py-3 text-sm text-stone-700">
                <Checkbox
                  checked={Boolean(form.dry_run)}
                  onCheckedChange={(checked) => setField("dry_run", Boolean(checked))}
                  disabled={running}
                />
                干跑（只注册不入库）
              </label>
              <div className="flex items-center gap-3 rounded-xl border border-stone-200 bg-white px-4 py-3 text-sm text-stone-700">
                <span className="text-stone-500">push_mode</span>
                <select
                  value={form.push_mode}
                  onChange={(e) => setField("push_mode", e.target.value)}
                  disabled={running}
                  className="h-8 flex-1 rounded-lg border border-stone-200 bg-white px-2 text-sm"
                >
                  <option value="local">local（推荐）</option>
                  <option value="http">http</option>
                </select>
              </div>
            </div>

            {job ? (
              <div className="space-y-3 rounded-xl bg-stone-50 px-4 py-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="text-xs font-medium tracking-[0.16em] text-stone-400 uppercase">
                    最近任务 {job.job_id.slice(0, 8)}
                  </div>
                  <div className="text-xs text-stone-500">
                    状态 {job.status} · 成功 {job.success} · 失败 {job.failed} · 入库 {job.added}
                  </div>
                </div>
                <div className="h-2 overflow-hidden rounded-full bg-stone-200">
                  <div
                    className="h-full rounded-full bg-stone-800 transition-all"
                    style={{ width: `${progress}%` }}
                  />
                </div>
                <div className="text-sm text-stone-600">
                  进度 {job.completed}/{job.total}（{progress}%）
                  {job.error ? <span className="ml-2 text-rose-500">{job.error}</span> : null}
                </div>
                {job.items && job.items.length > 0 ? (
                  <div className="max-h-40 space-y-1 overflow-auto rounded-lg border border-stone-200 bg-white p-3 text-xs">
                    {job.items.slice().reverse().map((item) => (
                      <div key={`${item.index}-${item.email || item.error || ""}`} className="flex gap-2">
                        <span className="text-stone-400">#{item.index}</span>
                        <span className={item.ok ? "text-emerald-600" : "text-rose-500"}>
                          {item.ok ? "OK" : "FAIL"}
                        </span>
                        <span className="truncate text-stone-600">
                          {item.email || item.error || "-"}
                          {item.added ? ` · +${item.added}` : ""}
                        </span>
                      </div>
                    ))}
                  </div>
                ) : null}
                {job.logs && job.logs.length > 0 ? (
                  <div className="max-h-36 space-y-1 overflow-auto rounded-lg border border-stone-200 bg-white p-3 font-mono text-[11px] text-stone-500">
                    {job.logs.slice(-30).map((log, idx) => (
                      <div key={`${log.at}-${idx}`}>
                        <span className="text-stone-300">{log.at ? log.at.slice(11, 19) : "--:--:--"} </span>
                        {log.message}
                      </div>
                    ))}
                  </div>
                ) : null}
              </div>
            ) : (
              <div className="rounded-xl bg-stone-50 px-4 py-6 text-center text-sm text-stone-400">
                还没有注册任务。填好数量等参数后点「开始注册」。
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}
