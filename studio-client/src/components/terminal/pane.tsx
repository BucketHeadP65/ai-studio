"use client";

import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { Button } from "client-core/ui/button";
import { Modal, ModalTitle } from "client-core/ui/modal";
import { useEffect, useState } from "react";

import { STUDIO_API_URL } from "@/api/studio-config";

import { resizeMessage, terminalWsUrl } from "./protocol";

// The xterm + fit-addon pair as the one call the mount effect needs: open the DOM node, size to
// it, move bytes both ways, and dispose everything on teardown. A test seam — jsdom has no
// canvas, so a test supplies a fake here instead of mounting real xterm.
export interface TerminalHandle {
  open: (container: HTMLElement) => void;
  fit: () => void;
  readonly cols: number;
  readonly rows: number;
  onData: (listener: (data: string) => void) => { dispose: () => void };
  write: (data: Uint8Array) => void;
  dispose: () => void;
}

// The slice of WebSocket the mount effect drives: binary frames carry pty bytes, one text
// frame shape is resize control (protocol.ts). A test seam — jsdom has no real WebSocket
// transport, so a test supplies a fake here instead of opening a real socket.
export interface TerminalSocket {
  binaryType: BinaryType;
  readonly readyState: number;
  send: (data: string | ArrayBufferView) => void;
  close: () => void;
  addEventListener(type: "open", listener: () => void): void;
  addEventListener(type: "message", listener: (event: MessageEvent) => void): void;
  addEventListener(type: "close", listener: (event: CloseEvent) => void): void;
  addEventListener(type: "error", listener: () => void): void;
}

function createXtermHandle(): TerminalHandle {
  const term = new Terminal({ convertEol: true, cursorBlink: true });
  const fit = new FitAddon();
  term.loadAddon(fit);
  return {
    open: (container) => term.open(container),
    fit: () => fit.fit(),
    get cols() {
      return term.cols;
    },
    get rows() {
      return term.rows;
    },
    onData: (listener) => term.onData(listener),
    write: (data) => term.write(data),
    dispose: () => term.dispose(),
  };
}

function openTerminalSocket(url: string): TerminalSocket {
  return new WebSocket(url);
}

interface Props {
  cwd: string | null;
  onClose: () => void;
  createTerminal?: () => TerminalHandle;
  createSocket?: (url: string) => TerminalSocket;
}

// The dashboard's one xterm.js mount, wired to the agent server's raw WS terminal, see
// protocol.ts for the wire framing this glue speaks. The mount itself (matchMedia, canvas) isn't
// unit-testable under jsdom, the same split graph-canvas.tsx draws against sigma's WebGL mount;
// what's pure here (the URL, the resize frame) lives in protocol.ts and is tested there.
//
// The mount div sits inside the radix Dialog's portal, which renders null on its first commit
// and only creates its DOM node on a later one — so the mount effect keys off the container as
// STATE (set via a callback ref), not a plain ref read once on mount, or it would fire while the
// portal is still null and never get another chance to run.
export function TerminalPane({
  cwd,
  onClose,
  createTerminal = createXtermHandle,
  createSocket = openTerminalSocket,
}: Props) {
  const [container, setContainer] = useState<HTMLDivElement | null>(null);
  const [connectionError, setConnectionError] = useState<string | null>(null);

  useEffect(() => {
    if (!container || cwd === null) return;

    const term = createTerminal();
    term.open(container);
    term.fit();

    const socket = createSocket(terminalWsUrl(STUDIO_API_URL, cwd));
    socket.binaryType = "arraybuffer";

    function sendResize() {
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(resizeMessage(term.cols, term.rows));
      }
    }

    socket.addEventListener("open", () => {
      setConnectionError(null);
      sendResize();
    });
    socket.addEventListener("message", (event) => {
      if (typeof event.data === "string") return; // control frames are ours to send, not receive
      term.write(new Uint8Array(event.data as ArrayBuffer));
    });
    socket.addEventListener("close", (event) => {
      if (!event.wasClean) {
        setConnectionError(event.reason || "the terminal disconnected");
      }
    });
    socket.addEventListener("error", () => {
      setConnectionError("could not reach the terminal");
    });

    const dataSubscription = term.onData((data) => {
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(new TextEncoder().encode(data));
      }
    });

    function handleResize() {
      term.fit();
      sendResize();
    }
    window.addEventListener("resize", handleResize);

    return () => {
      window.removeEventListener("resize", handleResize);
      dataSubscription.dispose();
      socket.close();
      term.dispose();
    };
  }, [cwd, container, createTerminal, createSocket]);

  return (
    <Modal
      open={cwd !== null}
      onClose={onClose}
      overlayClassName="bg-slate-900/50"
      className="flex h-[70vh] max-w-3xl flex-col gap-2 bg-slate-950 p-3 ring-slate-700"
    >
      <div className="flex items-center justify-between gap-3">
        <ModalTitle className="truncate text-slate-100">Terminal, {cwd}</ModalTitle>
        <Button variant="secondary" className="h-7 shrink-0 px-2.5 text-[12.5px]" onClick={onClose}>
          Close
        </Button>
      </div>
      {connectionError ? (
        <p role="alert" className="text-[13px] text-red-400">
          {connectionError}
        </p>
      ) : null}
      <div ref={setContainer} className="min-h-0 flex-1" />
    </Modal>
  );
}
