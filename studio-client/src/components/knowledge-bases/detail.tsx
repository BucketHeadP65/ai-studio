"use client";

import { cn } from "client-core/lib/cn";
import { formatRelative } from "client-core/lib/format-relative";
import { Badge } from "client-core/ui/badge";
import { Button } from "client-core/ui/button";
import { Card } from "client-core/ui/card";
import { Input } from "client-core/ui/input";
import { useRef, useState } from "react";

import type { KnowledgeBaseDetail } from "@/api/types";
import { useTargetRegistration } from "@/components/follow-me/targets";
import { KnowledgeBaseGraphView } from "@/components/graph/knowledge-base-graph-view";

import { DecisionsCard } from "./decisions";
import { RefreshCard } from "./refresh";

interface Props {
  detail: KnowledgeBaseDetail;
  onAddUrl: (url: string) => void;
  adding: boolean;
  addError: string | null;
  onRegister: (path: string) => void;
  onUnregister: () => void;
  registering: boolean;
  registerError: string | null;
}

type DetailTab = "overview" | "graph";

const TABS: Array<{ id: DetailTab; label: string }> = [
  { id: "overview", label: "Overview" },
  { id: "graph", label: "Graph" },
];

// One knowledge base, fully inspected: identity up top (always visible), then two tabs —
// Overview (vitals, registration, refresh, the source queue and watch list
// for a knowledge base with a source-queue connector, the pages awaiting a decision, and the
// index's standing validation) and Graph (this knowledge base's link graph). The
// registered/indexed framing mirrors the list, the two are independent truths,
// shown as separate badges.
export function KnowledgeBaseDetailView({
  detail,
  onAddUrl,
  adding,
  addError,
  onRegister,
  onUnregister,
  registering,
  registerError,
}: Props) {
  const [url, setUrl] = useState("");
  const [registerPath, setRegisterPath] = useState("");
  const [activeTab, setActiveTab] = useState<DetailTab>("overview");
  const showsSourceQueue = detail.sources_connectors.includes("intake");
  const tablistRef = useRef<HTMLDivElement>(null);

  useTargetRegistration(
    {
      name: "knowledge-base-tabs",
      kind: "tab",
      description: "Overview or Graph",
      params: { tabs: TABS.map((t) => t.id).join(", ") },
    },
    {
      element: () => tablistRef.current,
      readTab: () => activeTab,
      setTab: (tab) => {
        const found = TABS.find((t) => t.id === tab);
        if (!found) return false;
        setActiveTab(found.id);
        return true;
      },
    },
  );

  const submit = () => {
    const trimmed = url.trim();
    if (trimmed) {
      onAddUrl(trimmed);
      setUrl("");
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <header className="flex flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="font-semibold text-title text-label-1">{detail.name}</h1>
          {detail.broken ? (
            <Badge tone="danger" className="normal-case">
              broken
            </Badge>
          ) : (
            <>
              <Badge tone={detail.registered ? "ok" : "neutral"} className="normal-case">
                {detail.registered ? "registered" : "not registered"}
              </Badge>
              <Badge tone={detail.indexed ? "ok" : "neutral"} className="normal-case">
                {detail.indexed ? "indexed" : "not indexed"}
              </Badge>
            </>
          )}
        </div>
        <p className="text-caption text-label-3">
          {detail.path ?? "no registered path"}
          {detail.description ? `, ${detail.description}` : ""}
        </p>
      </header>

      <div role="tablist" aria-label="Knowledge base sections" className="flex gap-1" ref={tablistRef}>
        {TABS.map((tab) => (
          <button
            key={tab.id}
            type="button"
            role="tab"
            aria-selected={activeTab === tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={cn(
              "rounded-md px-2.5 py-1 text-caption",
              activeTab === tab.id
                ? "bg-accent-soft font-medium text-accent"
                : "text-label-2 hover:bg-well hover:text-label-1",
            )}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {activeTab === "graph" ? <KnowledgeBaseGraphView knowledgeBaseName={detail.name} /> : null}

      {activeTab === "overview" ? (
        <>
          <Card className="px-4 py-3">
            <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1">
              <Vital label="pages" value={detail.pages === null ? "none" : String(detail.pages)} />
              <Vital
                label="last indexed"
                value={detail.last_indexed ? formatRelative(detail.last_indexed) : "never"}
              />
              <Vital label="types" value={detail.page_types.length === 0 ? "none" : ""} />
              <span className="flex flex-wrap gap-1">
                {detail.page_types.map((pageType) => (
                  <span
                    key={pageType}
                    className="rounded bg-muted-soft px-1.5 py-0.5 text-xs text-muted-ink"
                  >
                    {pageType}
                  </span>
                ))}
              </span>
            </div>
          </Card>

          <Card className="px-4 py-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <h2 className="font-medium text-sm text-label-1">Registration</h2>
                <p className="mt-0.5 text-caption text-label-3">
                  {detail.registered
                    ? "Registered, the terminal, schema read, and source queue plugins are open here."
                    : "Not registered, name this knowledge base's directory to open its terminal, schema, and source queue plugins."}
                </p>
              </div>
              {detail.registered ? (
                <Button
                  variant="secondary"
                  className="h-7 shrink-0 px-2.5 text-caption"
                  onClick={onUnregister}
                  disabled={registering}
                >
                  {registering ? "Working..." : "Unregister"}
                </Button>
              ) : (
                <span className="flex flex-1 justify-end gap-2">
                  <Input
                    value={registerPath}
                    onChange={(event) => setRegisterPath(event.target.value)}
                    placeholder="/absolute/path/to/the/knowledge-base"
                    aria-label="Knowledge base directory"
                    className="h-7 w-72 max-w-full py-0 text-caption"
                  />
                  <Button
                    className="h-7 shrink-0 px-2.5 text-caption"
                    onClick={() => registerPath.trim() && onRegister(registerPath.trim())}
                    disabled={registering || !registerPath.trim()}
                  >
                    {registering ? "Working..." : "Register"}
                  </Button>
                </span>
              )}
            </div>
            {registerError ? (
              <p
                role="alert"
                className="mt-2 rounded-md bg-danger-soft px-2.5 py-1.5 text-caption text-danger-ink"
              >
                {registerError}
              </p>
            ) : null}
          </Card>

          <RefreshCard
            name={detail.name}
            registered={detail.registered}
            lastRun={detail.last_refresh_run}
            schedule={detail.schedule}
          />

          {showsSourceQueue ? (
            <Card className="px-4 py-3">
              <h2 className="font-medium text-sm text-label-1">Source queue</h2>
              <p className="mt-0.5 text-caption text-label-3">
                Paste a URL, the next sync brings it into this knowledge base.
              </p>
              <div className="mt-2 flex gap-2">
                <Input
                  value={url}
                  onChange={(event) => setUrl(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      submit();
                    }
                  }}
                  placeholder="https://..."
                  aria-label="Source URL"
                  className="h-8 flex-1 py-0"
                />
                <Button
                  className="h-8 px-3 text-caption"
                  onClick={submit}
                  disabled={adding || !url.trim()}
                >
                  {adding ? "Adding..." : "Add source"}
                </Button>
              </div>
              {addError ? (
                <p
                  role="alert"
                  className="mt-2 rounded-md bg-danger-soft px-2.5 py-1.5 text-caption text-danger-ink"
                >
                  {addError}
                </p>
              ) : null}
              {detail.source_queue.length > 0 ? (
                <ul className="mt-3 divide-y divide-line">
                  {detail.source_queue.map((row) => (
                    <li key={row.key} className="flex items-center gap-3 py-1.5">
                      <span className="font-mono text-caption text-label-2">{row.key}</span>
                      <Badge tone="neutral" className="normal-case">
                        {row.source_type}
                      </Badge>
                      <Badge
                        className="normal-case"
                        tone={
                          row.status === "failed"
                            ? "danger"
                            : row.status === "resolved"
                              ? "ok"
                              : "neutral"
                        }
                      >
                        {row.status}
                      </Badge>
                      <span
                        className="min-w-0 flex-1 truncate text-caption text-label-3"
                        title={row.reason ?? row.url}
                      >
                        {row.reason ?? row.url}
                      </span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="mt-3 text-caption text-label-3">Nothing in the source queue yet.</p>
              )}
              {detail.watch_list.length > 0 ? (
                <div className="mt-3 border-line border-t pt-2">
                  <h3 className="text-caption text-label-3">Watch list</h3>
                  <ul className="mt-1 divide-y divide-line">
                    {detail.watch_list.map((entry) => (
                      <li
                        key={`${entry.scanner}:${entry.ref}`}
                        className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 py-1.5"
                      >
                        <Badge tone="neutral" className="normal-case">
                          {entry.scanner}
                        </Badge>
                        <span className="min-w-0 flex-1 font-mono text-caption text-label-2">
                          {entry.ref}
                        </span>
                        <span className="text-caption text-label-3 tabular-nums">
                          {entry.last_scanned ? formatRelative(entry.last_scanned) : "never scanned"}
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </Card>
          ) : null}

          <DecisionsCard name={detail.name} />

          <Card className="px-4 py-3">
            <h2 className="font-medium text-sm text-label-1">
              Standing validation{detail.validation.length > 0 ? `, ${detail.validation.length}` : ""}
            </h2>
            {detail.validation.length === 0 ? (
              <p className="mt-1 text-caption text-label-3">
                {detail.indexed
                  ? "The last index is clean."
                  : "Never indexed, nothing to validate yet."}
              </p>
            ) : (
              <ul className="mt-2 divide-y divide-line">
                {detail.validation.map((issue) => (
                  <li
                    key={`${issue.page_id}:${issue.rule}:${issue.anchor ?? ""}:${issue.message}`}
                    className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 py-1.5"
                  >
                    <Badge
                      tone={issue.severity === "error" ? "danger" : "warn"}
                      className="normal-case"
                    >
                      {issue.severity}
                    </Badge>
                    <span className="shrink-0 font-mono text-caption text-label-2">
                      {issue.page_id}
                      {issue.anchor ? ` ^${issue.anchor}` : ""}
                    </span>
                    <span className="shrink-0 text-caption text-label-2">{issue.rule}</span>
                    <span className="min-w-0 flex-1 basis-full text-caption text-label-3">
                      {issue.message}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </>
      ) : null}
    </div>
  );
}

function Vital({ label, value }: { label: string; value: string }) {
  return (
    <span className="text-caption text-label-3">
      {label} <span className="font-medium text-body text-label-2 tabular-nums">{value}</span>
    </span>
  );
}
