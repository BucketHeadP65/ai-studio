import {
  Ban,
  BookOpen,
  Brain,
  Braces,
  CircleHelp,
  Cpu,
  FileText,
  Layers,
  MessageSquare,
  MessageSquareWarning,
  PackageX,
  Play,
  Regex,
  Terminal,
  TriangleAlert,
  User,
  Wrench,
  type LucideIcon,
} from "lucide-react";

// One icon per kind the screens mark: the symptom, a root cause, a fix surface, a transcript part. Words stay
// beside every icon; an icon alone never carries a meaning.

export const SYMPTOM_ICON: LucideIcon = TriangleAlert;

const ROOT_CAUSE_ICON: Record<string, LucideIcon> = {
  agent_reasoning: Brain,
  tool_error_message: MessageSquareWarning,
  tool_description: FileText,
  tool_schema: Regex,
  tool_result_shape: Braces,
  tool_design: Wrench,
  missing_tool: PackageX,
  overlapping_tools: Layers,
  instructions_say_nothing: FileText,
  instructions_say_wrong: FileText,
  model_tier: Cpu,
  user_prompt: User,
};

const SURFACE_ICON: Record<string, LucideIcon> = { tool: Wrench, skill: BookOpen, system_prompt: FileText, model_tier: Cpu };

const PART_ICON: Record<string, LucideIcon> = {
  "tool-return": Terminal,
  "tool-call": Play,
  thinking: Brain,
  text: MessageSquare,
  "user-prompt": User,
  "system-prompt": FileText,
  "retried-return": Ban,
};

export function rootCauseIcon(name: string): LucideIcon {
  return ROOT_CAUSE_ICON[name] ?? CircleHelp;
}

export function surfaceIcon(kind: string): LucideIcon {
  return SURFACE_ICON[kind] ?? CircleHelp;
}

export function partIcon(kind: string): LucideIcon {
  return PART_ICON[kind] ?? CircleHelp;
}
