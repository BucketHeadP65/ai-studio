import type { ReactElement } from "react";

import { AppearanceSection } from "@/components/settings/appearance-section";
import { ChatSection } from "@/components/settings/chat-section";
import { FontSection } from "@/components/settings/font-section";

export default function SettingsPage(): ReactElement {
  return (
    <div className="-m-6 flex flex-col">
      <div className="flex h-[70px] items-center gap-3 border-line border-b bg-surface px-7">
        <div className="min-w-0">
          <h1 className="font-bold text-label-1 text-title">Settings</h1>
          <p className="text-caption text-label-3">Applies to this browser.</p>
        </div>
      </div>

      <div className="flex max-w-[880px] flex-col gap-4 px-7 pt-6 pb-8">
        <AppearanceSection />
        <FontSection />
        <ChatSection />
      </div>
    </div>
  );
}
