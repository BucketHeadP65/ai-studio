"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, PanelLeftClose, Pin } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { type ReactElement, useState } from "react";

import { runKeys } from "@/api/query-keys";
import { sessionsApi } from "@/api/sessions";
import { useStoredFlag } from "@/lib/use-stored";

import { NAV_GROUPS, type NavEntry, type NavGroup } from "./nav-groups";

// The control room's left navigation: route-aware ink-pill entries in labeled
// groups. Hidden by default and summoned as an overlay from the screen's left
// edge, or pinned into the flow; the pin and each group's open state are
// per-viewer and outlive a reload (useStoredFlag), never server state.
const DIVIDER_BEFORE = new Set(["workspace", "system"]);

// The one nav entry that carries a live count: the fleet holds runs that
// need a person, and every viewer should see that from anywhere in the app.
// Polling the same query key the fleet board itself uses shares one
// subscription and one network request between the two.
const FLEET_HREF = "/workflows";

function waitingSentence(count: number): string {
  return count === 1 ? "1 gate is waiting on you." : `${count} gates are waiting on you.`;
}

function BrandMark(): ReactElement {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24" className="size-6 shrink-0">
      <circle cx="6" cy="12" r="4.2" className="fill-label-1" />
      <line x1="10.5" y1="12" x2="14" y2="12" strokeWidth="1.6" className="stroke-line-strong" />
      <rect
        x="15"
        y="9"
        width="6"
        height="6"
        rx="1"
        transform="rotate(45 18 12)"
        className="fill-warn"
      />
    </svg>
  );
}

function DiamondIcon({ size, className }: { size: number; className?: string }): ReactElement {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="currentColor"
      aria-hidden="true"
      className={className}
    >
      <path d="M8 1.4l6.6 6.6L8 14.6 1.4 8z" />
    </svg>
  );
}

function navItemClasses(active: boolean, collapsed: boolean): string {
  const base = "relative flex items-center rounded-md text-body transition-colors";
  const spacing = collapsed ? "justify-center p-2.5" : "gap-2.5 px-2.5 py-2";
  const tone = active
    ? "bg-label-1 font-semibold text-surface"
    : "font-medium text-label-2 hover:bg-well hover:text-label-1";
  return `${base} ${spacing} ${tone}`;
}

function NavEntryRow({
  entry,
  pathname,
  collapsed,
  waitingCount,
}: {
  entry: NavEntry;
  pathname: string;
  collapsed: boolean;
  waitingCount: number;
}): ReactElement {
  const active = entry.isActive(pathname);
  const Icon = entry.icon;
  const showBadge = entry.href === FLEET_HREF && waitingCount > 0;
  const accessibleName = showBadge ? `${entry.label}, ${waitingCount} waiting on you` : entry.label;

  return (
    <li>
      <Link
        href={entry.href}
        aria-current={active ? "page" : undefined}
        aria-label={showBadge ? accessibleName : undefined}
        title={collapsed ? accessibleName : undefined}
        className={navItemClasses(active, collapsed)}
      >
        <Icon aria-hidden size={17} strokeWidth={1.8} className="shrink-0" />
        {!collapsed ? <span className="truncate">{entry.label}</span> : null}
        {!collapsed && showBadge ? (
          <span className="ml-auto inline-flex items-center gap-1 rounded-full bg-warn-ink px-2 py-0.5 font-mono text-micro font-semibold text-surface">
            <DiamondIcon size={7} />
            {waitingCount}
          </span>
        ) : null}
        {collapsed && showBadge ? (
          <span
            aria-hidden="true"
            className="-right-1 -top-1 absolute flex size-4 items-center justify-center rounded-full bg-warn-ink font-mono text-micro text-surface ring-2 ring-surface"
          >
            {waitingCount}
          </span>
        ) : null}
      </Link>
    </li>
  );
}

function NavGroupSection({
  group,
  pathname,
  collapsed,
  waitingCount,
}: {
  group: NavGroup;
  pathname: string;
  collapsed: boolean;
  waitingCount: number;
}): ReactElement {
  const [open, setOpen] = useStoredFlag(`sidebar:group:${group.id}`, true);
  const showEntries = collapsed || open;
  const ChevronIcon = open ? ChevronDown : ChevronRight;

  return (
    <div
      className={collapsed ? "flex w-full flex-col items-center gap-2" : "flex flex-col gap-1.5"}
    >
      {collapsed && DIVIDER_BEFORE.has(group.id) ? (
        <div aria-hidden className="h-px w-6 bg-line" />
      ) : null}
      {!collapsed ? (
        <button
          type="button"
          onClick={() => setOpen(!open)}
          aria-expanded={open}
          className="flex w-full items-center gap-1.5 rounded-md px-2.5 py-1 text-label-3 text-micro font-semibold uppercase tracking-caps hover:text-label-2"
        >
          <ChevronIcon aria-hidden size={12} className="shrink-0" />
          {group.label}
        </button>
      ) : null}
      {showEntries ? (
        <ul
          className={collapsed ? "flex w-full flex-col items-center gap-1" : "flex flex-col gap-1"}
        >
          {group.entries.map((entry) => (
            <NavEntryRow
              key={entry.href}
              entry={entry}
              pathname={pathname}
              collapsed={collapsed}
              waitingCount={waitingCount}
            />
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function FleetBanner({
  waitingCount,
  collapsed,
}: {
  waitingCount: number;
  collapsed: boolean;
}): ReactElement | null {
  if (waitingCount === 0) return null;
  const sentence = waitingSentence(waitingCount);

  if (collapsed) {
    return (
      <Link
        href={FLEET_HREF}
        aria-label={sentence}
        title={sentence}
        className="flex shrink-0 items-center justify-center border-ground border-t bg-warn-soft py-3.5 text-warn-ink transition-colors hover:bg-warn/15"
      >
        <DiamondIcon size={14} className="text-warn" />
      </Link>
    );
  }

  return (
    <Link
      href={FLEET_HREF}
      className="flex shrink-0 items-center gap-2 border-ground border-t bg-warn-soft px-4 py-3.5 text-warn-ink transition-colors hover:bg-warn/15"
    >
      <DiamondIcon size={14} className="shrink-0 text-warn" />
      <span className="flex-1 text-caption font-semibold">{sentence}</span>
      <ChevronRight aria-hidden size={14} className="shrink-0" />
    </Link>
  );
}

export function Sidebar(): ReactElement {
  const pathname = usePathname();
  // Navigation costs nothing while it is not being used: hidden by default, summoned as an
  // overlay from the left edge (its hot strip, or the grip), and pinnable back into the flow
  // for whoever wants it standing. Only the pin is remembered.
  const [pinned, setPinned] = useStoredFlag("sidebar:pinned", false);
  const [overlay, setOverlay] = useState(false);

  const runsQuery = useQuery({
    queryKey: runKeys.live,
    queryFn: () => sessionsApi.runs(),
  });
  const waitingCount = (runsQuery.data ?? []).filter((run) => run.state === "waiting").length;

  const body = (
    <>
      <div className="flex items-center gap-2.5 border-line border-b px-4 py-4">
        <BrandMark />
        <span className="flex-1 truncate text-body font-semibold text-label-1">
          AI Factory Studio
        </span>
        {pinned ? (
          <button
            type="button"
            onClick={() => setPinned(false)}
            aria-label="Hide sidebar"
            title="Hide sidebar, it comes back from the left edge"
            className="shrink-0 rounded-md p-1.5 text-label-3 hover:bg-well hover:text-label-1"
          >
            <PanelLeftClose aria-hidden size={16} strokeWidth={1.8} />
          </button>
        ) : (
          <button
            type="button"
            onClick={() => {
              setPinned(true);
              setOverlay(false);
            }}
            aria-label="Pin sidebar"
            title="Pin sidebar open"
            className="shrink-0 rounded-md p-1.5 text-label-3 hover:bg-well hover:text-label-1"
          >
            <Pin aria-hidden size={16} strokeWidth={1.8} />
          </button>
        )}
      </div>

      <nav
        aria-label="Stations"
        className="flex min-h-0 flex-1 flex-col gap-3.5 overflow-y-auto px-2.5 py-3"
      >
        {NAV_GROUPS.map((group) => (
          <NavGroupSection
            key={group.id}
            group={group}
            pathname={pathname}
            collapsed={false}
            waitingCount={waitingCount}
          />
        ))}
      </nav>

      <FleetBanner waitingCount={waitingCount} collapsed={false} />
    </>
  );

  if (pinned) {
    return (
      <aside className="flex h-full w-60 shrink-0 flex-col border-line border-r bg-surface">
        {body}
      </aside>
    );
  }

  return (
    <>
      {/* The hot strip: brushing the screen's left edge summons the navigation. Thin enough
          that nothing interactive ever sits under it. */}
      <div
        aria-hidden
        onMouseEnter={() => setOverlay(true)}
        className="fixed inset-y-0 left-0 z-40 w-1.5"
      />
      <button
        type="button"
        onClick={() => setOverlay(true)}
        onMouseEnter={() => setOverlay(true)}
        aria-label={
          waitingCount > 0 ? `Open navigation, ${waitingSentence(waitingCount)}` : "Open navigation"
        }
        title="Navigation"
        className="-translate-y-1/2 fixed top-1/2 left-0 z-40 rounded-r-md py-3 pr-1 pl-0.5 text-label-3 hover:bg-well hover:text-label-1"
      >
        <span
          aria-hidden
          className={`block h-9 w-1 rounded-full ${waitingCount > 0 ? "bg-warn" : "bg-line-strong"}`}
        />
      </button>
      {overlay ? (
        <aside
          aria-label="Navigation"
          onMouseLeave={() => setOverlay(false)}
          onClick={(event) => {
            if (event.target instanceof Element && event.target.closest("a")) setOverlay(false);
          }}
          onKeyDown={(event) => {
            if (event.key === "Escape") setOverlay(false);
          }}
          className="fixed inset-y-0 left-0 z-50 flex w-60 flex-col border-line border-r bg-surface shadow-xl"
        >
          {body}
        </aside>
      ) : null}
    </>
  );
}
