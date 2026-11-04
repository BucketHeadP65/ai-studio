import { code } from "@streamdown/code";
import { type ComponentProps, memo } from "react";
import { Streamdown } from "streamdown";

// streamdown draws emphasis as styled spans; the semantic tags are put back so
// assistive tech reads emphasis as emphasis.
const SEMANTIC_EMPHASIS: ComponentProps<typeof Streamdown>["components"] = {
  strong: ({ node: _node, ...rest }) => <strong className="font-semibold" {...rest} />,
  em: ({ node: _node, ...rest }) => <em className="italic" {...rest} />,
};

const PLUGINS = { code };
const LINKS_ARE_ANCHORS = { enabled: false };

// The one markdown pipeline for agent prose, wherever it speaks: gfm tables and
// lists, highlighted fences, links in the accent. Every conversation surface
// renders through this so the agent's voice is typeset once. You must say
// whether the text is still streaming in (`live`). While it is, half-arrived
// syntax (an unclosed fence, a dangling emphasis marker, an open `[`) renders as
// its finished form instead of literal markers. Finished text renders as written,
// so an unmatched `[` stays a bracket and never becomes a placeholder link.
// Streamdown owns the chrome of a fence and a table (header, copy control, a
// capped height that scrolls inside). A link is a plain anchor, not a
// confirmation step. Memoized on its props, so a bubble whose content has not
// changed skips the pipeline when the surface around it re-renders for an
// unrelated reason.
//
// The block takes its width from its container and never from its content: prose
// wraps, a long unbroken word breaks, and a wide fence or table scrolls inside
// itself. One long line must not widen the surface that holds it.
export const AgentMarkdown = memo(function AgentMarkdown({ text, live }: { text: string; live: boolean }) {
  return (
    <div className="w-full min-w-0 max-w-full space-y-2 break-words [&_a]:text-accent [&_pre]:max-w-full [&_pre]:overflow-x-auto [&_pre_code]:whitespace-pre [&_table]:max-w-full">
      {/* `data-live` is not dead markup. Streamdown's memo ignores `parseIncompleteMarkdown` but
          compares data attributes, so this attribute is what re-renders a turn that settles with
          unchanged text. Its blocks stay mounted and only the repaired block re-renders. */}
      <Streamdown
        data-live={live ? "true" : "false"}
        lineNumbers={false}
        plugins={PLUGINS}
        linkSafety={LINKS_ARE_ANCHORS}
        components={SEMANTIC_EMPHASIS}
        parseIncompleteMarkdown={live}
      >
        {text}
      </Streamdown>
    </div>
  );
});
