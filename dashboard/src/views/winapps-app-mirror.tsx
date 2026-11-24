/**
 * Windows Apps Lab mirror: a draggable, resizable view of one real desktop window.
 * The inventory and captures are read-only; there is no input control in this phase.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowClockwiseIcon, CircleNotchIcon, EyeIcon, PushPinIcon, XIcon
} from "@phosphor-icons/react";
import { Badge, Empty, Field, Skeleton, Status } from "../components/ui";
import { api, message, usePoll, type WinApp, type WinAppsInfo } from "../lib/api";

const MIRROR_INTERVAL = 1500;
const INVENTORY_INTERVAL = 5000;
const MIN_WIDTH = 280;
const MIN_HEIGHT = 240;
const FPS_HOVER = 5;
const FPS_IDLE = 1.2;
const STREAM_IDLE_TIMEOUT_MS = 10_000;
const STREAM_CONNECT_TIMEOUT_MS = 10_000;
const MAX_CAPTURE_PIXELS = 7680 * 4320;
const MAX_STREAM_FRAME_BYTES = MAX_CAPTURE_PIXELS * 4 + 1024 * 1024;
const MAX_STREAM_HEADER_BYTES = 16_384;

const clamp = (value: number, min: number, max: number) => Math.min(Math.max(value, min), max);

const HEADER_END = new TextEncoder().encode("\r\n\r\n");
const CRLF = new TextEncoder().encode("\r\n");
const PNG_SIGNATURE = new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]);

function indexOfBytes(haystack: Uint8Array, needle: Uint8Array, from: number): number {
  outer: for (let i = from; i <= haystack.length - needle.length; i++) {
    for (let j = 0; j < needle.length; j++) {
      if (haystack[i + j] !== needle[j]) continue outer;
    }
    return i;
  }
  return -1;
}

function concatBytes(a: Uint8Array, b: Uint8Array): Uint8Array {
  const result = new Uint8Array(a.length + b.length);
  result.set(a);
  result.set(b, a.length);
  return result;
}

function validatePngFrame(png: Uint8Array): void {
  if (png.length < 57 || png.length > MAX_STREAM_FRAME_BYTES
    || !PNG_SIGNATURE.every((byte, index) => png[index] === byte)) {
    throw new Error("The mirror stream returned an invalid PNG frame");
  }
  const view = new DataView(png.buffer, png.byteOffset, png.byteLength);
  if (view.getUint32(8) !== 13 || view.getUint32(12) !== 0x49484452
    || view.getUint32(png.length - 12) !== 0 || view.getUint32(png.length - 8) !== 0x49454e44
    || view.getUint32(png.length - 4) !== 0xae426082) {
    throw new Error("The mirror stream returned an invalid PNG frame");
  }
  const width = view.getUint32(16);
  const height = view.getUint32(20);
  if (!width || !height || width * height > MAX_CAPTURE_PIXELS) {
    throw new Error("The mirror stream returned invalid PNG dimensions");
  }
}

async function pumpPngStream(
  response: Response,
  onFrame: (png: Uint8Array) => void
): Promise<void> {
  if (!response.body) throw new Error("The mirror stream ended");
  const reader = response.body.getReader();
  let pending: Uint8Array<ArrayBufferLike> = new Uint8Array(0);
  let frameLength: number | null = null;
  let frameParts: Uint8Array[] = [];
  let frameBytes = 0;
  let expectCrlf = false;
  const decoder = new TextDecoder();
  for (;;) {
    let timeout: ReturnType<typeof setTimeout> | undefined;
    const read = await Promise.race([
      reader.read(),
      new Promise<never>((_resolve, reject) => {
        timeout = setTimeout(() => reject(new Error("The mirror stream stalled")), STREAM_IDLE_TIMEOUT_MS);
      })
    ]).finally(() => {
      if (timeout !== undefined) clearTimeout(timeout);
    });
    if (read.done) {
      if (frameLength !== null || expectCrlf || pending.length) {
        throw new Error("The mirror stream ended during a PNG frame");
      }
      return;
    }

    let input = concatBytes(pending, read.value);
    pending = new Uint8Array(0);
    while (input.length) {
      if (frameLength !== null) {
        const length = Math.min(frameLength - frameBytes, input.length);
        frameParts.push(input.slice(0, length));
        frameBytes += length;
        input = input.slice(length);
        if (frameBytes === frameLength) {
          const frame = new Uint8Array(frameLength);
          let offset = 0;
          for (const part of frameParts) {
            frame.set(part, offset);
            offset += part.length;
          }
          validatePngFrame(frame);
          onFrame(frame);
          frameLength = null;
          frameParts = [];
          frameBytes = 0;
          expectCrlf = true;
        }
        continue;
      }

      if (expectCrlf) {
        if (input.length < 2) {
          pending = input;
          break;
        }
        if (input[0] !== 13 || input[1] !== 10) {
          throw new Error("The mirror stream returned malformed frame data");
        }
        input = input.slice(2);
        expectCrlf = false;
        continue;
      }

      const headerEnd = indexOfBytes(input, HEADER_END, 0);
      if (headerEnd < 0) {
        pending = input;
        if (pending.length > MAX_STREAM_HEADER_BYTES) {
          throw new Error("The mirror stream returned oversized headers");
        }
        break;
      }
      if (headerEnd > MAX_STREAM_HEADER_BYTES) {
        throw new Error("The mirror stream returned oversized headers");
      }
      const header = decoder.decode(input.slice(0, headerEnd));
      const contentType = header.match(/(?:^|\r\n)Content-Type:\s*([^\r\n]+)/i)?.[1]?.trim();
      const rawLength = header.match(/(?:^|\r\n)Content-Length:\s*(\d+)\s*(?:\r\n|$)/i)?.[1];
      const length = rawLength === undefined ? NaN : Number(rawLength);
      if (!header.startsWith("--frame\r\n") || contentType?.toLowerCase() !== "image/png"
        || !Number.isSafeInteger(length) || length < 57 || length > MAX_STREAM_FRAME_BYTES) {
        throw new Error("The mirror stream returned invalid frame headers");
      }
      frameLength = length;
      input = input.slice(headerEnd + HEADER_END.length);
    }
  }
}

function labelFor(app: WinApp): string {
  const title = app.title ? ` — ${app.title.slice(0, 40)}` : "";
  return `${app.app}${title}`;
}

function constrainPanel(
  rect: { left: number; top: number; width: number; height: number },
  viewportWidth: number,
  viewportHeight: number
) {
  const margin = 12;
  const maxWidth = Math.max(1, viewportWidth - margin * 2);
  const maxHeight = Math.max(1, viewportHeight - margin * 2);
  const width = clamp(rect.width, Math.min(MIN_WIDTH, maxWidth), maxWidth);
  const height = clamp(rect.height, Math.min(MIN_HEIGHT, maxHeight), maxHeight);
  return {
    left: clamp(rect.left, margin, Math.max(margin, viewportWidth - width - margin)),
    top: clamp(rect.top, margin, Math.max(margin, viewportHeight - height - margin)),
    width,
    height
  };
}

export function WinAppsPanel() {
  const { data, error, reload } = usePoll<WinAppsInfo>(
    () => api.winappsApps(), INVENTORY_INTERVAL
  );
  const [windowId, setWindowId] = useState(0);
  const [float, setFloat] = useState(true);
  const [hidden, setHidden] = useState(false);
  const [pos, setPos] = useState({ left: 0, top: 0, width: 440, height: 420 });
  const [shot, setShot] = useState<string | null>(null);
  const [shotWindowId, setShotWindowId] = useState<number | null>(null);
  const [shotError, setShotError] = useState<string | null>(null);
  const [visible, setVisible] = useState(typeof document === "undefined" || !document.hidden);
  const [hovering, setHovering] = useState(false);
  const [streamState, setStreamState] = useState<"on" | "failed">("on");
  const shotRef = useRef<string | null>(null);
  const selectedWindowIdRef = useRef<number | null>(null);
  const streamGenerationRef = useRef(0);
  const displaySequenceRef = useRef(0);
  const snapshotControllerRef = useRef<AbortController | null>(null);
  const snapshotInFlightRef = useRef(false);

  const windows = data?.windows ?? [];
  const selected = windows.find((window) => window.windowId === windowId) ?? windows[0] ?? null;
  const fps = hovering ? FPS_HOVER : FPS_IDLE;
  selectedWindowIdRef.current = selected?.windowId ?? null;

  useEffect(() => {
    const initialize = () => setPos((current) => {
      const initial = current.left === 0 && current.top === 0
        ? { ...current, left: Math.max(12, window.innerWidth - 480), top: 220 }
        : current;
      return constrainPanel(initial, window.innerWidth, window.innerHeight);
    });
    const resize = () => setPos((current) => constrainPanel(current, window.innerWidth, window.innerHeight));
    initialize();
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, []);

  useEffect(() => {
    if (selected && selected.windowId !== windowId) setWindowId(selected.windowId);
  }, [selected, windowId]);

  useEffect(() => {
    displaySequenceRef.current += 1;
    snapshotControllerRef.current?.abort();
    snapshotControllerRef.current = null;
    snapshotInFlightRef.current = false;
    if (shotRef.current) URL.revokeObjectURL(shotRef.current);
    shotRef.current = null;
    setShot(null);
    setShotWindowId(null);
    setShotError(null);
    setStreamState("on");
  }, [selected?.windowId]);

  useEffect(() => {
    const update = () => setVisible(!document.hidden);
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);

  const setShotFromBytes = useCallback((
    targetWindowId: number,
    png: Uint8Array,
    isCurrent: () => boolean
  ) => {
    if (!isCurrent() || selectedWindowIdRef.current !== targetWindowId) return;
    const url = URL.createObjectURL(new Blob([png.slice().buffer as ArrayBuffer], { type: "image/png" }));
    if (shotRef.current) URL.revokeObjectURL(shotRef.current);
    shotRef.current = url;
    setShot(url);
    setShotWindowId(targetWindowId);
    setShotError(null);
  }, []);

  const grab = useCallback(async () => {
    const targetWindowId = selected?.windowId;
    if (!targetWindowId || hidden || snapshotInFlightRef.current) return;
    snapshotInFlightRef.current = true;
    const sequence = ++displaySequenceRef.current;
    const controller = new AbortController();
    snapshotControllerRef.current = controller;
    const timeout = setTimeout(() => controller.abort(), STREAM_CONNECT_TIMEOUT_MS);
    try {
      const response = await fetch(api.winappsAppScreenshotUrl(targetWindowId), {
        cache: "no-store",
        signal: controller.signal
      });
      if (!response.ok) {
        const body = (await response.json().catch(() => ({}))) as { error?: string };
        throw new Error(body.error || `${response.status} ${response.statusText}`);
      }
      const png = new Uint8Array(await response.arrayBuffer());
      setShotFromBytes(targetWindowId, png, () =>
        sequence === displaySequenceRef.current && selectedWindowIdRef.current === targetWindowId
      );
    } catch (e) {
      if (sequence !== displaySequenceRef.current || selectedWindowIdRef.current !== targetWindowId) return;
      setShot(null);
      setShotWindowId(null);
      setShotError(message(e));
    } finally {
      clearTimeout(timeout);
      if (snapshotControllerRef.current === controller) {
        snapshotControllerRef.current = null;
        snapshotInFlightRef.current = false;
      }
    }
  }, [hidden, selected?.windowId, setShotFromBytes]);

  useEffect(() => () => {
    if (shotRef.current) URL.revokeObjectURL(shotRef.current);
  }, []);

  useEffect(() => {
    if (!selected || hidden) return;
    setStreamState("on");
    void grab();
  }, [selected?.windowId, hidden, grab]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!selected || hidden || streamState !== "failed") return;
    const timer = setInterval(() => {
      if (!document.hidden) void grab();
    }, MIRROR_INTERVAL);
    return () => clearInterval(timer);
  }, [selected?.windowId, hidden, streamState, grab]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!selected || hidden || streamState !== "on" || !visible) return;
    const targetWindowId = selected.windowId;
    const generation = ++streamGenerationRef.current;
    let active = true;
    let connected = false;
    let timedOut = false;
    const controller = new AbortController();
    const connectionTimer = setTimeout(() => {
      if (!connected) {
        timedOut = true;
        controller.abort();
      }
    }, STREAM_CONNECT_TIMEOUT_MS);
    (async () => {
      try {
        const response = await fetch(api.winappsAppStreamUrl(targetWindowId, fps), {
          signal: controller.signal,
          cache: "no-store"
        });
        connected = true;
        clearTimeout(connectionTimer);
        if (!response.ok) {
          const body = (await response.json().catch(() => ({}))) as { error?: string };
          throw new Error(body.error || `${response.status} ${response.statusText}`);
        }
        await pumpPngStream(response, (png) => {
          if (!active || streamGenerationRef.current !== generation
            || selectedWindowIdRef.current !== targetWindowId) return;
          const sequence = ++displaySequenceRef.current;
          setShotFromBytes(targetWindowId, png, () =>
            active && streamGenerationRef.current === generation
            && selectedWindowIdRef.current === targetWindowId
            && displaySequenceRef.current === sequence
          );
        });
        throw new Error("The mirror stream ended");
      } catch (e) {
        if (!active || streamGenerationRef.current !== generation
          || selectedWindowIdRef.current !== targetWindowId) return;
        controller.abort();
        setShotError(timedOut ? "The mirror stream connection timed out" : message(e));
        setStreamState("failed");
      }
    })();
    return () => {
      active = false;
      clearTimeout(connectionTimer);
      controller.abort();
      if (streamGenerationRef.current === generation) streamGenerationRef.current += 1;
    };
  }, [selected?.windowId, hidden, streamState, visible, fps, setShotFromBytes]);

  const startDrag = (event: React.PointerEvent) => {
    if (!float || (event.target instanceof Element && event.target.closest("button"))) return;
    event.preventDefault();
    const origin = { x: event.clientX, y: event.clientY, left: pos.left, top: pos.top };
    const move = (pointer: PointerEvent) => {
      setPos((current) => ({
        ...current,
        left: clamp(origin.left + pointer.clientX - origin.x, 12,
          Math.max(12, window.innerWidth - current.width - 12)),
        top: clamp(origin.top + pointer.clientY - origin.y, 12,
          Math.max(12, window.innerHeight - current.height - 12))
      }));
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };

  const startResize = (event: React.PointerEvent) => {
    if (!float) return;
    event.preventDefault();
    event.stopPropagation();
    const origin = { x: event.clientX, y: event.clientY, width: pos.width, height: pos.height };
    const move = (pointer: PointerEvent) => {
      setPos((current) => {
        const maxWidth = Math.max(1, window.innerWidth - current.left - 12);
        const maxHeight = Math.max(1, window.innerHeight - current.top - 12);
        return {
          ...current,
          width: clamp(origin.width + pointer.clientX - origin.x, Math.min(MIN_WIDTH, maxWidth), maxWidth),
          height: clamp(origin.height + pointer.clientY - origin.y, Math.min(MIN_HEIGHT, maxHeight), maxHeight)
        };
      });
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };

  const header = (
    <div
      className={`flex items-center gap-2 border-b border-(--iux-border) px-4 py-2.5 ${float ? "cursor-move touch-none select-none" : ""}`}
      onPointerDown={startDrag}
    >
      <EyeIcon size={15} className="text-[#f36c21]" aria-hidden />
      <span className="text-sm font-semibold">App mirror</span>
      <span className="ml-auto flex items-center gap-1">
        <button
          type="button"
          className="iux-btn"
          data-variant="ghost"
          title={float ? "Dock the panel" : "Float the panel"}
          aria-label={float ? "Dock the panel" : "Float the panel"}
          onClick={() => setFloat((current) => !current)}
        >
          <PushPinIcon size={14} />
        </button>
        <button
          type="button"
          className="iux-btn"
          data-variant="ghost"
          title="Hide the app mirror"
          aria-label="Hide the app mirror"
          onClick={() => setHidden(true)}
        >
          <XIcon size={14} />
        </button>
      </span>
    </div>
  );

  const currentShot = selected && shotWindowId === selected.windowId ? shot : null;
  const body = (
    <div className="flex flex-col gap-3">
      {error ? <Status value={{ ok: false, text: error }} /> : null}
      {streamState === "failed" && shotError ? <Status value={{ ok: false, text: shotError }} /> : null}
      {!data ? (
        <Skeleton rows={2} />
      ) : !data.available ? (
        <Empty>{data.reason ?? "The Windows app mirror is not available here."}</Empty>
      ) : windows.length === 0 ? (
        <Empty>No visible desktop application windows were found.</Empty>
      ) : selected ? (
        <>
          <Field label="Window">
            <select
              className="iux-input"
              value={selected.windowId}
              onChange={(event) => setWindowId(Number(event.target.value))}
              aria-label="Window"
            >
              {windows.map((window) => (
                <option key={window.windowId} value={window.windowId}>{labelFor(window)}</option>
              ))}
            </select>
          </Field>
          <div className="flex flex-wrap items-center gap-2">
            <button type="button" className="iux-btn" data-variant="outline" onClick={() => {
              reload();
              void grab();
            }}>
              <ArrowClockwiseIcon size={14} /> Refresh
            </button>
            <Badge tone={selected.width > 0 && selected.height > 0 ? "success" : "warning"}>
              {`${selected.width}×${selected.height}`}
            </Badge>
            {selected.elevated ? <Badge tone="warning">Elevated</Badge> : null}
          </div>
          <div
            className="relative w-full overflow-hidden rounded-xl border border-(--iux-border) bg-black"
            style={{
              aspectRatio: `${selected.width || 4} / ${selected.height || 3}`,
              maxWidth: `calc(70vh * ${selected.width || 4} / ${selected.height || 3})`,
              marginInline: "auto"
            }}
            onPointerEnter={() => setHovering(true)}
            onPointerLeave={() => setHovering(false)}
          >
            {currentShot ? (
              <img
                src={currentShot}
                alt={`Mirror of ${selected.app}`}
                className="absolute inset-0 h-full w-full object-fill"
                draggable={false}
              />
            ) : shotError ? (
              <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 p-4 text-center">
                <p className="text-xs text-zinc-400">{shotError}</p>
                <button
                  type="button"
                  className="iux-btn"
                  data-variant="outline"
                  onClick={() => {
                    setStreamState("on");
                    setShotError(null);
                    void grab();
                  }}
                >
                  Retry
                </button>
              </div>
            ) : (
              <div className="absolute inset-0 flex items-center justify-center" aria-busy="true">
                <CircleNotchIcon size={22} className="animate-spin text-zinc-500" aria-hidden />
              </div>
            )}
          </div>
        </>
      ) : null}
    </div>
  );

  if (hidden) {
    return (
      <section className="iux-card items-start gap-3 py-4">
        <div className="flex items-center gap-2 px-4">
          <EyeIcon size={15} className="text-[#f36c21]" aria-hidden />
          <span className="text-sm font-semibold">App mirror</span>
        </div>
        <div className="px-4">
          <button type="button" className="iux-btn" data-variant="outline" onClick={() => setHidden(false)}>
            Show the app mirror
          </button>
        </div>
      </section>
    );
  }

  return float ? (
    <div
      className="iux-card fixed z-40 flex flex-col overflow-hidden shadow-[0_18px_50px_-12px_rgba(0,0,0,0.55)]"
      style={{
        left: pos.left, top: pos.top, width: pos.width, height: pos.height,
        maxWidth: "calc(100vw - 24px)", maxHeight: "calc(100dvh - 24px)"
      }}
    >
      {header}
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">{body}</div>
      <div
        className="absolute right-0 bottom-0 h-5 w-5 cursor-nwse-resize touch-none"
        onPointerDown={startResize}
        aria-hidden
      />
    </div>
  ) : (
    <section className="iux-card gap-3 py-4">
      {header}
      <div className="px-4">{body}</div>
    </section>
  );
}
