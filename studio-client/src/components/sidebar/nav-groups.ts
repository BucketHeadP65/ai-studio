import {
  CalendarClock,
  Database,
  FolderKanban,
  House,
  LayoutGrid,
  Library,
  type LucideIcon,
  MessageSquare,
  ScanSearch,
  Settings,
  Settings2,
} from "lucide-react";

// The sidebar's groups and entries, in nav order. Accessible names + hrefs here
// are the binding UI contract the smoke e2e asserts, do not rename or reorder
// without updating that spec.
//
// `isActive` decides the route-aware highlight per entry, not a blanket prefix
// match: three entries share the `/workflows` family (Fleet, Library,
// Schedules) and need to disagree about which of them is "here".

export interface NavEntry {
  href: string;
  label: string;
  icon: LucideIcon;
  isActive: (pathname: string) => boolean;
}

export interface NavGroup {
  id: string;
  label: string;
  entries: NavEntry[];
}

function exact(href: string): (pathname: string) => boolean {
  return (pathname) => pathname === href || pathname.startsWith(`${href}/`);
}

export const NAV_GROUPS: NavGroup[] = [
  {
    id: "main",
    label: "Main",
    entries: [
      // The root, and the only entry `exact` cannot describe: every path starts with "/", so
      // Home is here and nowhere else.
      { href: "/", label: "Home", icon: House, isActive: (pathname) => pathname === "/" },
      { href: "/chat", label: "Chat", icon: MessageSquare, isActive: exact("/chat") },
    ],
  },
  {
    id: "workflows",
    label: "Workflows",
    entries: [
      {
        href: "/workflows",
        label: "Fleet",
        icon: LayoutGrid,
        isActive: (pathname) =>
          pathname === "/workflows" ||
          (pathname.startsWith("/workflows/") &&
            !pathname.startsWith("/workflows/library") &&
            !pathname.startsWith("/workflows/schedules")),
      },
      {
        href: "/workflows/library",
        label: "Library",
        icon: Library,
        isActive: exact("/workflows/library"),
      },
      {
        href: "/workflows/schedules",
        label: "Schedules",
        icon: CalendarClock,
        isActive: exact("/workflows/schedules"),
      },
    ],
  },
  {
    id: "workspace",
    label: "Workspace",
    entries: [
      { href: "/projects", label: "Projects", icon: FolderKanban, isActive: exact("/projects") },
      {
        href: "/knowledge-bases",
        label: "Knowledge bases",
        icon: Database,
        isActive: exact("/knowledge-bases"),
      },
    ],
  },
  {
    id: "system",
    label: "System",
    entries: [
      { href: "/harness", label: "Harness", icon: Settings2, isActive: exact("/harness") },
      { href: "/inspection", label: "Inspection", icon: ScanSearch, isActive: exact("/inspection") },
      { href: "/settings", label: "Settings", icon: Settings, isActive: exact("/settings") },
    ],
  },
];
