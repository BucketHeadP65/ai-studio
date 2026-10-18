"use client";

import { usePathname } from "next/navigation";
import { type ReactElement, type ReactNode, useRef } from "react";

import { useStoredChoice } from "@/lib/use-stored";

import { Companion } from "./companion";
import { PLACEMENTS, PLACEMENT_KEY, type Placement } from "./placement";

// The shell around every station's page: the page column itself, and the companion in whichever
// slot it is set to. A rail takes its own column beside the page, the bottom panel and the
// floating window live inside the page column, which is why that column is the positioned one.
//
// The chat station gets the page and nothing else -- it is already a chat.

export function CompanionShell({ children }: { children: ReactNode }): ReactElement {
  const pathname = usePathname();
  const [placement] = useStoredChoice<Placement>(PLACEMENT_KEY, "left", PLACEMENTS);
  const columnRef = useRef<HTMLDivElement>(null);

  // `relative` makes this scroller the containing block for any absolutely positioned
  // descendant (visually hidden inputs included), so nothing inside a page can escape the
  // clip and stretch the document into a second scrollbar.
  const page = <main className="relative flex-1 overflow-auto px-6 py-6">{children}</main>;

  if (pathname === "/chat" || pathname.startsWith("/chat/")) {
    return <div className="flex min-w-0 flex-1 flex-col">{page}</div>;
  }

  const companion = <Companion columnRef={columnRef} />;

  return (
    <>
      {placement === "left" ? companion : null}
      <div ref={columnRef} className="relative flex min-w-0 flex-1 flex-col">
        {page}
        {placement === "bottom" || placement === "floating" ? companion : null}
      </div>
      {placement === "right" ? companion : null}
    </>
  );
}
