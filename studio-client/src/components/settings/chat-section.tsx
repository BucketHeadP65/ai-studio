"use client";

import type { ReactElement } from "react";

import { RadioCardGroup, type RadioCardOption } from "@/components/workflows/radio-card";
import { useFoldSettledSteps } from "@/lib/chat-preferences";

import { SettingsSectionCard } from "./settings-section-card";

type StepsValue = "open" | "fold";

const STEPS_OPTIONS: readonly RadioCardOption<StepsValue>[] = [
  {
    value: "open",
    label: "Keep open",
    description: "Every step of a finished turn stays in view, in order.",
  },
  {
    value: "fold",
    label: "Fold after the answer",
    description: "A finished turn folds its earlier steps under one line above the answer.",
  },
];

export function ChatSection(): ReactElement {
  const [foldSettled, setFoldSettled] = useFoldSettledSteps();

  return (
    <SettingsSectionCard title="Chat" explainer="How a turn's steps read once it is done.">
      <div className="p-4">
        <RadioCardGroup
          name="earlier-steps"
          legend="Earlier steps"
          value={foldSettled ? "fold" : "open"}
          onChange={(value) => setFoldSettled(value === "fold")}
          options={STEPS_OPTIONS}
        />
      </div>
    </SettingsSectionCard>
  );
}
