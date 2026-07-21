"use client";

import { useRef, useState } from "react";
import { FileJson, LoaderCircle, Upload } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import {
  createGrokAccounts,
  importGrokAccountFiles,
  type Account,
  type AccountImportPayload,
} from "@/lib/api";

type GrokImportDialogProps = {
  disabled?: boolean;
  onImported: (items: Account[]) => void;
};

function asAccountPayload(value: unknown): AccountImportPayload | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const raw = value as Record<string, unknown>;
  const tokenValue = raw.access_token ?? raw.accessToken ?? raw.token;
  const token = typeof tokenValue === "string" ? tokenValue.trim() : "";
  if (!token) {
    return null;
  }
  return { ...raw, access_token: token } as AccountImportPayload;
}

function extractAccounts(value: unknown): AccountImportPayload[] {
  if (Array.isArray(value)) {
    return value.map(asAccountPayload).filter((item): item is AccountImportPayload => Boolean(item));
  }
  if (!value || typeof value !== "object") {
    return [];
  }
  const raw = value as Record<string, unknown>;
  const nested = raw.accounts ?? raw.items;
  if (Array.isArray(nested)) {
    return nested.map(asAccountPayload).filter((item): item is AccountImportPayload => Boolean(item));
  }
  const single = asAccountPayload(value);
  return single ? [single] : [];
}

export function GrokImportDialog({ disabled, onImported }: GrokImportDialogProps) {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [isImporting, setIsImporting] = useState(false);

  const close = () => {
    if (isImporting) return;
    setOpen(false);
    setText("");
  };

  const handleImportText = async () => {
    const raw = text.trim();
    if (!raw) {
      toast.error("请粘贴 cliproxy / xAI 账号 JSON");
      return;
    }
    setIsImporting(true);
    try {
      const parsed = JSON.parse(raw) as unknown;
      const accounts = extractAccounts(parsed);
      if (accounts.length === 0) {
        toast.error("未解析到有效 Grok 账号（需要 access_token，type 建议 xai）");
        return;
      }
      const data = await createGrokAccounts(accounts);
      onImported(data.items);
      toast.success(`导入完成：新增 ${data.added ?? 0}，跳过/合并 ${data.skipped ?? 0}`);
      close();
    } catch (error) {
      const message = error instanceof Error ? error.message : "导入失败";
      toast.error(message);
    } finally {
      setIsImporting(false);
    }
  };

  const handleImportFiles = async (fileList: FileList | null) => {
    if (!fileList || fileList.length === 0) return;
    setIsImporting(true);
    try {
      const files: Array<{ name: string; content: string }> = [];
      for (const file of Array.from(fileList)) {
        files.push({ name: file.name, content: await file.text() });
      }
      const data = await importGrokAccountFiles(files);
      onImported(data.items);
      const parseErrors = data.parse_errors?.length ?? 0;
      toast.success(
        `文件导入完成：新增 ${data.added ?? 0}，跳过/合并 ${data.skipped ?? 0}` +
          (parseErrors ? `，解析失败 ${parseErrors}` : ""),
      );
      if (parseErrors) {
        for (const err of data.parse_errors || []) {
          toast.error(`${err.name}: ${err.error}`);
        }
      }
      close();
    } catch (error) {
      const message = error instanceof Error ? error.message : "导入失败";
      toast.error(message);
    } finally {
      setIsImporting(false);
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
    }
  };

  return (
    <>
      <Button
        variant="outline"
        className="h-10 rounded-xl border-stone-200 bg-white/80 px-4 text-stone-700 hover:bg-white"
        onClick={() => setOpen(true)}
        disabled={disabled}
      >
        <Upload className="size-4" />
        导入 Grok 账号
      </Button>

      <Dialog open={open} onOpenChange={(next) => (!next ? close() : setOpen(true))}>
        <DialogContent showCloseButton={false} className="max-w-2xl rounded-2xl p-6">
          <DialogHeader className="gap-2">
            <DialogTitle>导入 Grok / xAI 账号</DialogTitle>
            <DialogDescription className="text-sm leading-6">
              支持 cliproxy 风格 JSON（type=xai，含 access_token / refresh_token）。与 ChatGPT 号池完全隔离，写入
              data/grok_accounts.json。
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4">
            <div className="flex flex-wrap gap-2">
              <input
                ref={fileInputRef}
                type="file"
                accept="application/json,.json"
                multiple
                className="hidden"
                onChange={(event) => void handleImportFiles(event.target.files)}
              />
              <Button
                variant="outline"
                className="h-10 rounded-xl border-stone-200 bg-white px-4"
                disabled={isImporting}
                onClick={() => fileInputRef.current?.click()}
              >
                {isImporting ? <LoaderCircle className="size-4 animate-spin" /> : <FileJson className="size-4" />}
                选择 JSON 文件
              </Button>
            </div>

            <div className="space-y-2">
              <label className="text-sm font-medium text-stone-700">或粘贴 JSON</label>
              <Textarea
                value={text}
                onChange={(event) => setText(event.target.value)}
                placeholder='{"type":"xai","access_token":"...","refresh_token":"...","email":"..."}'
                className="min-h-[220px] rounded-xl border-stone-200 bg-white font-mono text-xs"
                disabled={isImporting}
              />
            </div>
          </div>

          <DialogFooter className="pt-2">
            <Button
              variant="secondary"
              className="h-10 rounded-xl bg-stone-100 px-5 text-stone-700 hover:bg-stone-200"
              onClick={close}
              disabled={isImporting}
            >
              取消
            </Button>
            <Button
              className="h-10 rounded-xl bg-stone-950 px-5 text-white hover:bg-stone-800"
              onClick={() => void handleImportText()}
              disabled={isImporting}
            >
              {isImporting ? <LoaderCircle className="size-4 animate-spin" /> : null}
              导入到 Grok 号池
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
