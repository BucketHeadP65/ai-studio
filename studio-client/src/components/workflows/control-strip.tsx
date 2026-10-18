"use client";

import { Button } from "client-core/ui/button";
import { ConfirmDialog } from "client-core/ui/confirm-dialog";
import type { ReactElement } from "react";
import { useState } from "react";

import type { GateMode, PendingGate } from "@/api/studio-types";
import { GatePanel } from "@/components/chat/gate-panel";

// The one honest place every act on the live run happens: Run when nothing is
// active, Pause/Resume once something is, Stop from either of those or from a
// waiting gate, and the gate's own Approve/Reject when one is open. Sticky at
// the top of the side column so scrolling the parameter form or a long result
// never carries the acting buttons out of view. Stop asks first -- stopping
// ends the run for good, unlike a pause -- and only fires once that is
// confirmed. "Schedule it" only appears once a caller hands it a handler; the
// workflow page has none yet, so no dead button shows there. The automode
// switch shows only while the run is in flight (running, or waiting at a
// gate) -- the one stretch the backend accepts flipping it, since a paused
// or idle run has no live task to carry the flag. `gateMode`, read live off
// run/state, adds a static line beside it once it is anything but wait --
// the switch alone can only say auto or not, and cannot say orchestrate.

const GATE_MODE_LINE: Partial<Record<GateMode, string>> = {
  orchestrate: "gates decided by the orchestrator",
  auto: "gates pass automatically",
};

export interface ControlStripProps {
  gate: PendingGate | null;
  onDecide: (approve: boolean, feedback?: string) => void;
  deciding: boolean;
  running: boolean;
  paused: boolean;
  onRun: () => void;
  runDisabled: boolean;
  starting: boolean;
  onPause: () => void;
  pausing: boolean;
  onResume: () => void;
  resuming: boolean;
  onStop: () => void;
  stopping: boolean;
  onSchedule?: () => void;
  automode: boolean;
  onAutomode: (on: boolean) => void;
  settingAutomode: boolean;
  gateMode: GateMode;
}

export function ControlStrip({
  gate,
  onDecide,
  deciding,
  running,
  paused,
  onRun,
  runDisabled,
  starting,
  onPause,
  pausing,
  onResume,
  resuming,
  onStop,
  stopping,
  onSchedule,
  automode,
  onAutomode,
  settingAutomode,
  gateMode,
}: ControlStripProps): ReactElement {
  const [confirmingStop, setConfirmingStop] = useState(false);
  const canStop = running || paused || gate !== null;

  return (
    <div className="sticky top-0 z-10 -mx-4 space-y-2 border-b border-line bg-surface px-4 py-2.5">
      {gate ? (
        <GatePanel pending={gate} onDecide={onDecide} deciding={deciding} />
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          {!running && !paused ? (
            <Button
              onClick={onRun}
              disabled={runDisabled}
              className="h-8 bg-ok-ink px-4 text-caption hover:opacity-90"
            >
              {starting ? "Running" : "Run"}
            </Button>
          ) : null}
          {running ? (
            <Button
              variant="secondary"
              onClick={onPause}
              disabled={pausing}
              className="h-8 px-3.5 text-caption"
            >
              Pause
            </Button>
          ) : null}
          {paused ? (
            <Button onClick={onResume} disabled={resuming} className="h-8 px-3.5 text-caption">
              Resume
            </Button>
          ) : null}
        </div>
      )}

      {canStop || onSchedule ? (
        <div className="flex flex-wrap items-center gap-2">
          {canStop ? (
            <Button
              variant="danger"
              onClick={() => setConfirmingStop(true)}
              disabled={stopping}
              className="h-8 px-3.5 text-caption"
            >
              Stop
            </Button>
          ) : null}
          {onSchedule ? (
            <Button variant="secondary" onClick={onSchedule} className="h-8 px-3.5 text-caption">
              Schedule it
            </Button>
          ) : null}
        </div>
      ) : null}

      {running ? (
        <label className="flex items-center gap-2 text-caption text-label-2">
          <input
            type="checkbox"
            checked={automode}
            disabled={settingAutomode}
            onChange={(event) => onAutomode(event.target.checked)}
          />
          Auto approve gates (recorded as automatic)
        </label>
      ) : null}

      {running && GATE_MODE_LINE[gateMode] ? (
        <p className="text-caption text-label-2">{GATE_MODE_LINE[gateMode]}</p>
      ) : null}

      <ConfirmDialog
        open={confirmingStop}
        title="Stop for good?"
        message="Nothing will be resumable."
        confirmLabel="Stop"
        destructive
        onConfirm={() => {
          setConfirmingStop(false);
          onStop();
        }}
        onCancel={() => setConfirmingStop(false)}
      />
    </div>
  );
}
