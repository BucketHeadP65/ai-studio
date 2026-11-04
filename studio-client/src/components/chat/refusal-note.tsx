import type { ReactElement } from "react";

// A refused answer's refusal, drawn between the turn it refused and the turn that retried it.
// It names no speaker: the refusal came from the run, not from the person or the model.

export function RefusalNote({ text }: { text: string }): ReactElement {
  return (
    <div className="flex w-full flex-col gap-1 rounded-md border border-line bg-danger-soft px-3 py-2">
      <p className="text-danger-ink text-micro font-semibold uppercase tracking-caps">Refused</p>
      <p className="whitespace-pre-wrap break-words text-body text-danger-ink">{text}</p>
    </div>
  );
}
