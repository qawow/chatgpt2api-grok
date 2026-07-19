"use client";

import { useEffect, useRef, useState } from "react";
import {
  Eye,
  EyeOff,
  Link2,
  LoaderCircle,
  Pencil,
  Plus,
  RefreshCcw,
  Save,
  ServerCog,
  Trash2,
  Unplug,
  Upload,
} from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  createG2AServer,
  deleteG2ACredential,
  deleteG2AServer,
  fetchG2ACredentials,
  fetchG2AServers,
  pingG2AServer,
  pushLocalGrokToG2A,
  updateG2AServer,
  type G2ARemoteCredential,
  type G2AServer,
} from "@/lib/api";

export function G2AConnections() {
  const didLoadRef = useRef(false);

  const [servers, setServers] = useState<G2AServer[]>([]);
  const [isLoading, setIsLoading] = useState(true);

  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<G2AServer | null>(null);
  const [formName, setFormName] = useState("");
  const [formBaseUrl, setFormBaseUrl] = useState("");
  const [formAdminKey, setFormAdminKey] = useState("");
  const [formNote, setFormNote] = useState("");
  const [formProxy, setFormProxy] = useState("");
  const [showSecret, setShowSecret] = useState(false);
  const [isSaving, setIsSaving] = useState(false);

  const [busyId, setBusyId] = useState<string | null>(null);
  const [credentialsOpen, setCredentialsOpen] = useState(false);
  const [activeServer, setActiveServer] = useState<G2AServer | null>(null);
  const [credentials, setCredentials] = useState<G2ARemoteCredential[]>([]);
  const [isLoadingCreds, setIsLoadingCreds] = useState(false);
  const [deletingCredId, setDeletingCredId] = useState<string | null>(null);

  const loadServers = async (silent = false) => {
    if (!silent) setIsLoading(true);
    try {
      const data = await fetchG2AServers();
      setServers(data.servers || []);
    } catch (error) {
      if (!silent) {
        toast.error(error instanceof Error ? error.message : "加载 GrokCLI2API 连接失败");
      }
    } finally {
      if (!silent) setIsLoading(false);
    }
  };

  useEffect(() => {
    if (didLoadRef.current) return;
    didLoadRef.current = true;
    void loadServers();
  }, []);

  const openAdd = () => {
    setEditing(null);
    setFormName("");
    setFormBaseUrl("http://127.0.0.1:8088");
    setFormAdminKey("");
    setFormNote("");
    setFormProxy("");
    setShowSecret(false);
    setDialogOpen(true);
  };

  const openEdit = (server: G2AServer) => {
    setEditing(server);
    setFormName(server.name || "");
    setFormBaseUrl(server.base_url || "");
    setFormAdminKey("");
    setFormNote(server.note || "");
    setFormProxy(server.proxy || "");
    setShowSecret(false);
    setDialogOpen(true);
  };

  const saveServer = async () => {
    if (!formBaseUrl.trim()) {
      toast.error("请输入 grokcli2api-go 地址");
      return;
    }
    if (!editing && !formAdminKey.trim()) {
      toast.error("请输入 GROK_ADMIN_KEY");
      return;
    }
    setIsSaving(true);
    try {
      if (editing) {
        const data = await updateG2AServer(editing.id, {
          name: formName.trim(),
          base_url: formBaseUrl.trim(),
          admin_key: formAdminKey.trim() || undefined,
          note: formNote.trim(),
          proxy: formProxy.trim(),
        });
        setServers(data.servers);
        toast.success("连接已更新");
      } else {
        const data = await createG2AServer({
          name: formName.trim(),
          base_url: formBaseUrl.trim(),
          admin_key: formAdminKey.trim(),
          note: formNote.trim(),
          proxy: formProxy.trim(),
        });
        setServers(data.servers);
        toast.success("连接已添加");
      }
      setDialogOpen(false);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存失败");
    } finally {
      setIsSaving(false);
    }
  };

  const removeServer = async (server: G2AServer) => {
    setBusyId(server.id);
    try {
      const data = await deleteG2AServer(server.id);
      setServers(data.servers);
      toast.success("连接已删除");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "删除失败");
    } finally {
      setBusyId(null);
    }
  };

  const pingServer = async (server: G2AServer) => {
    setBusyId(server.id);
    try {
      const data = await pingG2AServer(server.id);
      setServers(data.servers);
      toast.success(`连通正常，远程凭证约 ${data.count ?? 0} 条`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "探测失败");
      void loadServers(true);
    } finally {
      setBusyId(null);
    }
  };

  const pushAll = async (server: G2AServer) => {
    setBusyId(server.id);
    try {
      const data = await pushLocalGrokToG2A(server.id, []);
      setServers(data.servers);
      if (data.failed > 0 && data.pushed === 0) {
        toast.error(`推送失败 ${data.failed} 条（本地 Grok 号池可能为空或远程拒绝）`);
      } else {
        toast.success(`已推送 ${data.pushed}/${data.total}，失败 ${data.failed}`);
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "推送失败");
      void loadServers(true);
    } finally {
      setBusyId(null);
    }
  };

  const browseCredentials = async (server: G2AServer) => {
    setActiveServer(server);
    setCredentialsOpen(true);
    setIsLoadingCreds(true);
    try {
      const data = await fetchG2ACredentials(server.id);
      setCredentials(data.items || []);
      if (data.servers) setServers(data.servers);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "拉取远程凭证失败");
      setCredentials([]);
    } finally {
      setIsLoadingCreds(false);
    }
  };

  const removeCredential = async (credential: G2ARemoteCredential) => {
    if (!activeServer || !credential.id) return;
    setDeletingCredId(credential.id);
    try {
      await deleteG2ACredential(activeServer.id, credential.id);
      setCredentials((prev) => prev.filter((item) => item.id !== credential.id));
      toast.success("远程凭证已删除");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "删除远程凭证失败");
    } finally {
      setDeletingCredId(null);
    }
  };

  return (
    <>
      <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
        <CardContent className="space-y-6 p-6">
          <div className="flex items-start justify-between gap-3">
            <div className="flex items-center gap-3">
              <div className="flex size-10 items-center justify-center rounded-xl bg-stone-100">
                <ServerCog className="size-5 text-stone-600" />
              </div>
              <div>
                <h2 className="text-lg font-semibold tracking-tight">GrokCLI2API 连接</h2>
                <p className="text-sm text-stone-500">
                  对接{" "}
                  <a
                    className="underline decoration-stone-300 underline-offset-2 hover:text-stone-700"
                    href="https://github.com/Futureppo/grokcli2api-go"
                    target="_blank"
                    rel="noreferrer"
                  >
                    Futureppo/grokcli2api-go
                  </a>
                  ：用 Admin Key 管理远程凭证，并将本地 Grok 号池推送到其 auths。
                </p>
              </div>
            </div>
            <div className="flex items-center gap-2">
              {servers.length > 0 ? (
                <Badge className="rounded-md px-2.5 py-1">{servers.length} 个连接</Badge>
              ) : null}
              <Button
                className="h-9 rounded-xl bg-stone-950 px-4 text-white hover:bg-stone-800"
                onClick={openAdd}
              >
                <Plus className="size-4" />
                添加连接
              </Button>
            </div>
          </div>

          <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
            远程 <code className="rounded bg-white/70 px-1">GET /v1/admin/credentials</code>{" "}
            只返回脱敏状态，不含 token，因此<strong>不能</strong>从 grokcli2api-go 反向导入本地号池。
            支持方向：本地 Grok 号池 → 远程上传；以及查看/删除远程脱敏凭证。
            管理请求默认<strong>直连</strong>（忽略系统 HTTP_PROXY），避免误打到只支持 CONNECT 的代理出现 405。
            base URL 填服务根地址，例如 <code className="rounded bg-white/70 px-1">http://127.0.0.1:8088</code>
            ，不要填 <code className="rounded bg-white/70 px-1">/v1</code> 或本地代理端口。
          </div>

          {isLoading ? (
            <div className="flex items-center justify-center py-10">
              <LoaderCircle className="size-5 animate-spin text-stone-400" />
            </div>
          ) : servers.length === 0 ? (
            <div className="flex flex-col items-center justify-center gap-3 rounded-xl bg-stone-50 px-6 py-10 text-center">
              <ServerCog className="size-8 text-stone-300" />
              <div className="space-y-1">
                <p className="text-sm font-medium text-stone-600">暂无 GrokCLI2API 连接</p>
                <p className="text-sm text-stone-400">
                  填写 base URL（默认 :8088）与 <code>GROK_ADMIN_KEY</code>。
                </p>
              </div>
            </div>
          ) : (
            <div className="space-y-3">
              {servers.map((server) => {
                const busy = busyId === server.id;
                return (
                  <div
                    key={server.id}
                    className="flex flex-col gap-3 rounded-xl border border-stone-200 bg-white px-4 py-3"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="text-sm font-medium text-stone-800">
                          {server.name || server.base_url}
                        </div>
                        <div className="truncate text-xs text-stone-400">{server.base_url}</div>
                        {server.proxy ? (
                          <div className="truncate text-xs text-stone-400">proxy: {server.proxy}</div>
                        ) : (
                          <div className="truncate text-xs text-stone-400">proxy: 直连（忽略环境代理）</div>
                        )}
                        <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-stone-500">
                          <Badge variant="secondary" className="rounded-md">
                            {server.has_admin_key ? "Admin Key 已配置" : "缺少 Admin Key"}
                          </Badge>
                          {server.last_ok_at ? <span>上次成功：{server.last_ok_at}</span> : null}
                          {server.last_error ? (
                            <span className="break-all text-rose-500">错误：{server.last_error}</span>
                          ) : null}
                        </div>
                      </div>
                      <div className="flex items-center gap-1">
                        <button
                          type="button"
                          className="rounded-lg p-2 text-stone-400 transition hover:bg-stone-100 hover:text-stone-700"
                          onClick={() => openEdit(server)}
                          disabled={busy}
                          title="编辑"
                        >
                          <Pencil className="size-4" />
                        </button>
                        <button
                          type="button"
                          className="rounded-lg p-2 text-stone-400 transition hover:bg-rose-50 hover:text-rose-500"
                          onClick={() => void removeServer(server)}
                          disabled={busy}
                          title="删除连接"
                        >
                          {busy ? (
                            <LoaderCircle className="size-4 animate-spin" />
                          ) : (
                            <Trash2 className="size-4" />
                          )}
                        </button>
                      </div>
                    </div>

                    <div className="flex flex-wrap items-center gap-2">
                      <Button
                        variant="outline"
                        className="h-8 rounded-lg border-stone-200 bg-white px-3 text-xs text-stone-600"
                        disabled={busy}
                        onClick={() => void pingServer(server)}
                      >
                        {busy ? (
                          <LoaderCircle className="size-3.5 animate-spin" />
                        ) : (
                          <RefreshCcw className="size-3.5" />
                        )}
                        探测连通
                      </Button>
                      <Button
                        variant="outline"
                        className="h-8 rounded-lg border-stone-200 bg-white px-3 text-xs text-stone-600"
                        disabled={busy}
                        onClick={() => void browseCredentials(server)}
                      >
                        查看远程凭证
                      </Button>
                      <Button
                        className="h-8 rounded-lg bg-stone-950 px-3 text-xs text-white hover:bg-stone-800"
                        disabled={busy}
                        onClick={() => void pushAll(server)}
                      >
                        <Upload className="size-3.5" />
                        推送本地 Grok 号池
                      </Button>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent showCloseButton={false} className="rounded-2xl p-6">
          <DialogHeader className="gap-2">
            <DialogTitle>{editing ? "编辑 GrokCLI2API 连接" : "添加 GrokCLI2API 连接"}</DialogTitle>
            <DialogDescription className="text-sm leading-6">
              对应远程环境变量 <code>GROK_ADMIN_KEY</code> 与服务地址（默认 8088）。
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-2">
              <label className="text-sm font-medium text-stone-700">名称（可选）</label>
              <Input
                value={formName}
                onChange={(event) => setFormName(event.target.value)}
                placeholder="例如：本机 grokcli2api-go"
                className="h-11 rounded-xl border-stone-200 bg-white"
              />
            </div>
            <div className="space-y-2">
              <label className="flex items-center gap-1.5 text-sm font-medium text-stone-700">
                <Link2 className="size-3.5" />
                服务地址
              </label>
              <Input
                value={formBaseUrl}
                onChange={(event) => setFormBaseUrl(event.target.value)}
                placeholder="http://127.0.0.1:8088"
                className="h-11 rounded-xl border-stone-200 bg-white"
              />
            </div>
            <div className="space-y-2">
              <label className="flex items-center gap-1.5 text-sm font-medium text-stone-700">
                <Unplug className="size-3.5" />
                GROK_ADMIN_KEY
              </label>
              <div className="relative">
                <Input
                  type={showSecret ? "text" : "password"}
                  value={formAdminKey}
                  onChange={(event) => setFormAdminKey(event.target.value)}
                  placeholder={editing ? "留空则不修改密钥" : "管理员密钥"}
                  className="h-11 rounded-xl border-stone-200 bg-white pr-10"
                />
                <button
                  type="button"
                  className="absolute top-1/2 right-3 -translate-y-1/2 text-stone-400 transition hover:text-stone-600"
                  onClick={() => setShowSecret(!showSecret)}
                >
                  {showSecret ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                </button>
              </div>
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium text-stone-700">备注（可选）</label>
              <Input
                value={formNote}
                onChange={(event) => setFormNote(event.target.value)}
                placeholder="用途说明"
                className="h-11 rounded-xl border-stone-200 bg-white"
              />
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium text-stone-700">出站代理（可选）</label>
              <Input
                value={formProxy}
                onChange={(event) => setFormProxy(event.target.value)}
                placeholder="留空=直连；需要时填 http://host:port 或 socks5h://host:port"
                className="h-11 rounded-xl border-stone-200 bg-white"
              />
              <p className="text-xs text-stone-500">
                仅用于访问 grokcli2api-go 管理接口。默认直连，不会走系统 HTTP_PROXY（可避免
                only CONNECT supported 的 405）。
              </p>
            </div>
          </div>
          <DialogFooter className="pt-2">
            <Button
              variant="secondary"
              className="h-10 rounded-xl bg-stone-100 px-5 text-stone-700 hover:bg-stone-200"
              onClick={() => setDialogOpen(false)}
              disabled={isSaving}
            >
              取消
            </Button>
            <Button
              className="h-10 rounded-xl bg-stone-950 px-5 text-white hover:bg-stone-800"
              onClick={() => void saveServer()}
              disabled={isSaving}
            >
              {isSaving ? <LoaderCircle className="size-4 animate-spin" /> : <Save className="size-4" />}
              {editing ? "保存修改" : "添加"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={credentialsOpen} onOpenChange={setCredentialsOpen}>
        <DialogContent className="max-w-2xl rounded-2xl p-6">
          <DialogHeader className="gap-2">
            <DialogTitle>远程凭证（脱敏）</DialogTitle>
            <DialogDescription className="text-sm leading-6">
              {activeServer?.name || activeServer?.base_url || "grokcli2api-go"} · 不含 token
            </DialogDescription>
          </DialogHeader>
          {isLoadingCreds ? (
            <div className="flex items-center justify-center py-10">
              <LoaderCircle className="size-5 animate-spin text-stone-400" />
            </div>
          ) : credentials.length === 0 ? (
            <div className="rounded-xl bg-stone-50 px-4 py-8 text-center text-sm text-stone-500">
              暂无远程凭证，或列表为空。
            </div>
          ) : (
            <div className="max-h-[50vh] space-y-2 overflow-y-auto">
              {credentials.map((item) => (
                <div
                  key={item.id || item.email || Math.random()}
                  className="flex items-center justify-between gap-3 rounded-xl border border-stone-200 px-3 py-2"
                >
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium text-stone-800">
                      {item.email || item.id || "(unknown)"}
                    </div>
                    <div className="truncate text-xs text-stone-400">
                      id={item.id || "-"} · {item.status || (item.disabled ? "disabled" : "active")}
                    </div>
                  </div>
                  {item.id ? (
                    <Button
                      variant="outline"
                      className="h-8 rounded-lg border-rose-200 px-3 text-xs text-rose-600 hover:bg-rose-50"
                      disabled={deletingCredId === item.id}
                      onClick={() => void removeCredential(item)}
                    >
                      {deletingCredId === item.id ? (
                        <LoaderCircle className="size-3.5 animate-spin" />
                      ) : (
                        <Trash2 className="size-3.5" />
                      )}
                      删除
                    </Button>
                  ) : null}
                </div>
              ))}
            </div>
          )}
          <DialogFooter>
            <Button
              variant="secondary"
              className="h-10 rounded-xl bg-stone-100 px-5 text-stone-700"
              onClick={() => setCredentialsOpen(false)}
            >
              关闭
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
