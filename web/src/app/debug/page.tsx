"use client";

import { LoaderCircle } from "lucide-react";

import { useAuthGuard } from "@/lib/use-auth-guard";

import { SkillPanel } from "./components/skill-panel";

export default function DebugPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);

  if (isCheckingAuth || !session || session.role !== "admin") {
    return (
      <div className="flex min-h-[calc(100vh-49px)] items-center justify-center">
        <LoaderCircle className="size-5 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <div className="mx-auto flex min-h-[calc(100vh-49px)] w-full max-w-[1600px] flex-col gap-4 px-4 pt-3 pb-6 md:px-8">
      <SkillPanel />
    </div>
  );
}
