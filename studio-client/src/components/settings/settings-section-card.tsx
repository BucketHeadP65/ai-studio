import type { ReactElement, ReactNode } from "react";

// The card chrome every settings section shares: a title and a one-line
// explainer over a body the section itself fills in.

export interface SettingsSectionCardProps {
  title: string;
  explainer: string;
  children: ReactNode;
}

export function SettingsSectionCard({
  title,
  explainer,
  children,
}: SettingsSectionCardProps): ReactElement {
  return (
    <section className="rounded-xl border border-line bg-surface shadow-sm">
      <div className="border-ground border-b px-5 py-4">
        <h2 className="text-subtitle font-semibold text-label-1">{title}</h2>
        <p className="text-caption text-label-2">{explainer}</p>
      </div>
      {children}
    </section>
  );
}
