// The jest-dom matcher import stays here (not only inside client-core's setup)
// so its `declare module "vitest"` augmentation is visible to studio-client's own
// `tsc`. This file lives in studio-client's tsconfig include, client-core's does
// not. Runtime-wise it is idempotent (installTestLifecycle imports it too).
import "@testing-library/jest-dom/vitest";

import { installTestLifecycle } from "client-core/test-support/setup";
import { afterEach } from "vitest";

import { server } from "./msw/server";

// The shared lifecycle (jest-dom matchers, scrollIntoView stub, MSW
// listen/reset/close with onUnhandledFrame:"error") lives in client-core. The
// MSW server instance + its handlers stay app-local.
installTestLifecycle(server);

// MSW answers 500 when a handler throws, so an `expect` inside a handler would fail only the request,
// never the test. Every exception a handler raises fails the test it ran in.
const handlerExceptions: unknown[] = [];
server.events.on("unhandledException", ({ error }) => {
  handlerExceptions.push(error);
});
afterEach(() => {
  const raised = handlerExceptions.splice(0);
  if (raised.length > 0) {
    throw raised[0];
  }
});

// React Flow (@xyflow/react) needs a handful of browser APIs jsdom lacks —
// this is xyflow's own documented test shim. ResizeObserver fires its callback
// once on observe so the flow believes nodes were measured; DOMMatrixReadOnly
// parses the zoom out of a transform; offsetWidth/offsetHeight read the inline
// style so measurement returns SOMETHING deterministic instead of 0.
class ResizeObserverShim {
  callback: ResizeObserverCallback;
  constructor(callback: ResizeObserverCallback) {
    this.callback = callback;
  }
  observe(target: Element) {
    this.callback(
      [{ target, contentRect: target.getBoundingClientRect() } as ResizeObserverEntry],
      this as unknown as ResizeObserver,
    );
  }
  unobserve() {}
  disconnect() {}
}

class DOMMatrixReadOnlyShim {
  m22: number;
  constructor(transform?: string) {
    const scale = transform?.match(/scale\(([1-9.]+)\)/)?.[1];
    this.m22 = scale === undefined ? 1 : +scale;
  }
}

globalThis.ResizeObserver = ResizeObserverShim as unknown as typeof ResizeObserver;
globalThis.DOMMatrixReadOnly = DOMMatrixReadOnlyShim as unknown as typeof DOMMatrixReadOnly;
Object.defineProperties(globalThis.HTMLElement.prototype, {
  offsetHeight: {
    get(this: HTMLElement) {
      return Number.parseFloat(this.style.height) || 1;
    },
  },
  offsetWidth: {
    get(this: HTMLElement) {
      return Number.parseFloat(this.style.width) || 1;
    },
  },
});
(globalThis.SVGElement.prototype as unknown as { getBBox: () => object }).getBBox = () => ({
  x: 0,
  y: 0,
  width: 0,
  height: 0,
});
