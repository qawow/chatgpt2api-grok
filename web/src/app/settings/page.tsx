"use client";

import { useEffect, useRef } from "react";
import { LoaderCircle } from "lucide-react";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useAuthGuard } from "@/lib/use-auth-guard";

import { BackupSettingsCard } from "./components/backup-settings-card";
import { ApiDocsCard } from "./components/api-docs-card";
import { ConfigCard } from "./components/config-card";
import { ProxyRuntimeCard } from "./components/proxy-runtime-card";
import { SettingsHeader } from "./components/settings-header";
import { GptRegisterCard } from "./components/gpt-register-card";
import { ThirdPartyAppsCard } from "./components/third-party-apps-card";
import { UserKeysCard } from "./components/user-keys-card";
import { useSettingsStore } from "./store";

const settingsTabs = [
  { value: "basic", title: "基础配置" },
  { value: "backup", title: "备份" },
  { value: "keys", title: "用户密钥" },
  { value: "api-docs", title: "接口接入" },
  { value: "canvas", title: "画布入口" },
  { value: "proxy", title: "FlareSolverr" },
  { value: "gpt-register", title: "GPT注册" },
];

function SettingsDataController() {
  const didLoadRef = useRef(false);
  const initialize = useSettingsStore((state) => state.initialize);
  const loadBackups = useSettingsStore((state) => state.loadBackups);
  const backupState = useSettingsStore((state) => state.backupState);

  useEffect(() => {
    if (didLoadRef.current) {
      return;
    }
    didLoadRef.current = true;
    void initialize();
  }, [initialize]);

  useEffect(() => {
    if (!backupState?.running) {
      return;
    }
    const timer = window.setInterval(() => {
      void loadBackups(true);
    }, 3000);
    return () => window.clearInterval(timer);
  }, [backupState?.running, loadBackups]);

  return null;
}

function SettingsPageContent() {
  return (
    <>
      <SettingsDataController />
      <SettingsHeader />
      <Tabs defaultValue="basic" className="space-y-4">
        <div className="sticky top-3 z-20 overflow-x-auto rounded-xl border border-white/80 bg-white/90 px-3 py-2 shadow-sm backdrop-blur">
          <TabsList variant="line" className="min-w-max justify-start">
            {settingsTabs.map((tab) => (
              <TabsTrigger key={tab.value} value={tab.value} className="px-4">
                {tab.title}
              </TabsTrigger>
            ))}
          </TabsList>
        </div>
        <TabsContent value="basic">
          <ConfigCard />
        </TabsContent>
        <TabsContent value="proxy">
          <ProxyRuntimeCard />
        </TabsContent>
        <TabsContent value="backup">
          <BackupSettingsCard />
        </TabsContent>
        <TabsContent value="keys">
          <UserKeysCard />
        </TabsContent>
        <TabsContent value="canvas">
          <ThirdPartyAppsCard />
        </TabsContent>
        <TabsContent value="api-docs">
          <ApiDocsCard />
        </TabsContent>
        <TabsContent value="gpt-register">
          <GptRegisterCard />
        </TabsContent>
      </Tabs>
    </>
  );
}

export default function SettingsPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);

  if (isCheckingAuth || !session || session.role !== "admin") {
    return (
      <div className="flex min-h-[40vh] items-center justify-center">
        <LoaderCircle className="size-5 animate-spin text-stone-400" />
      </div>
    );
  }

  return <SettingsPageContent />;
}
