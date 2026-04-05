/**
 * Hub API client — all communication with the Nexus Hub backend.
 * Desktop chat IS the Sidecar. No separate Ollama fallback path.
 * Routing: Hub (remote), Mac (local Sidecar), Auto (Hub→Sidecar fallback).
 */

import type { RouteMode } from "../types";

const HUB_URL = "http://100.121.67.94:18100";
const SIDECAR_URL = "http://localhost:8765"; // Local Sidecar (agent loop + tools)

// ---------------------------------------------------------------------------
// Chat (SSE streaming)
// ---------------------------------------------------------------------------

export interface SSEEvent {
  type: "ack" | "status" | "result" | "error" | "clarify" | "blocked";
  session_id: string;
  content: string;
  metadata?: Record<string, unknown>;
}

export interface SendMessageOptions {
  content: string;
  senderId?: string;
  deviceId?: string;
  routeMode?: RouteMode;
  providerName?: string;  // e.g. "minimax", "kimi", "ollama"
  onEvent: (event: SSEEvent) => void;
  onDone: () => void;
  onError: (err: Error) => void;
}

/** Read SSE stream from a fetch Response */
async function readSSEStream(
  res: Response,
  opts: Pick<SendMessageOptions, "onEvent" | "onDone" | "onError">
) {
  if (!res.ok || !res.body) {
    opts.onError(new Error(`HTTP ${res.status}`));
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const lines = buffer.split("\n");
    buffer = lines.pop() || "";

    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed.startsWith("data: ")) continue;
      try {
        const event: SSEEvent = JSON.parse(trimmed.slice(6));
        opts.onEvent(event);
      } catch {
        // skip malformed lines
      }
    }
  }
  opts.onDone();
}

/** Send via Hub (proxied through Sidecar to avoid CORS) */
function sendViaHub(opts: SendMessageOptions, controller: AbortController) {
  const body = JSON.stringify({
    content: opts.content,
    sender_id: opts.senderId || "desktop_default",
    device_id: opts.deviceId || "mac",
    route_mode: opts.routeMode || "auto",
    provider_name: opts.providerName || undefined,
  });

  fetch(`${SIDECAR_URL}/hub/desktop/message`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body,
    signal: controller.signal,
  })
    .then((res) => readSSEStream(res, opts))
    .catch((err) => {
      if (err.name !== "AbortError") opts.onError(err);
    });
}

/** Send via Mac Sidecar (SSE streaming to avoid WKWebView timeout) */
function sendViaMac(opts: SendMessageOptions, controller: AbortController) {
  opts.onEvent({ type: "ack", session_id: "local", content: "" });

  fetch(`${SIDECAR_URL}/local-command`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      task: opts.content,
      provider_name: opts.providerName || undefined,
    }),
    signal: controller.signal,
  })
    .then(async (res) => {
      if (!res.ok) throw new Error(`Sidecar HTTP ${res.status}`);
      // Read SSE stream from sidecar
      if (!res.body) throw new Error("No response body");
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed.startsWith("data: ")) continue;
          try {
            const event = JSON.parse(trimmed.slice(6));
            if (event.type === "status") {
              opts.onEvent({
                type: "status",
                session_id: "local",
                content: event.content,
              });
            } else if (event.type === "result" && event.result) {
              const result = event.result;
              if (result.success) {
                opts.onEvent({
                  type: "result",
                  session_id: result.run_id || "local",
                  content: result.output,
                  metadata: {
                    model: result.model,
                    duration_ms: result.duration_ms,
                    event_count: result.event_count,
                    via: "sidecar",
                  },
                });
              } else {
                opts.onEvent({
                  type: "error",
                  session_id: result.run_id || "local",
                  content: result.error || "Local command failed",
                });
              }
            } else if (event.type === "error") {
              opts.onEvent({
                type: "error",
                session_id: "local",
                content: event.content || "Sidecar error",
              });
            }
          } catch {
            // skip malformed SSE lines
          }
        }
      }
      opts.onDone();
    })
    .catch((err) => {
      if (err.name === "AbortError") return;
      opts.onError(new Error(`Sidecar unavailable: ${err.message}`));
    });
}

export function sendMessage(opts: SendMessageOptions): AbortController {
  const controller = new AbortController();
  const mode = opts.routeMode || "auto";

  if (mode === "hub") {
    // Hub only — no fallback
    sendViaHub(opts, controller);
  } else if (mode === "mac") {
    // Mac only — direct to local Sidecar (no Hub involved)
    sendViaMac(opts, controller);
  } else {
    // Auto: send to Hub via Sidecar proxy — Hub's TaskRouter decides
    // whether to execute locally or delegate to Mac via mesh.
    // Only fallback to local Sidecar if Hub is completely unreachable.
    fetch(`${SIDECAR_URL}/hub/desktop/message`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        content: opts.content,
        sender_id: opts.senderId || "desktop_default",
        device_id: opts.deviceId || "mac",
        route_mode: "auto",
        provider_name: opts.providerName || undefined,
      }),
      signal: controller.signal,
    })
      .then((res) => {
        if (!res.ok) throw new Error(`Hub HTTP ${res.status}`);
        return readSSEStream(res, opts);
      })
      .catch((err) => {
        if (err.name === "AbortError") return;
        console.warn("Hub unreachable, falling back to Sidecar:", err.message);
        sendViaMac(opts, controller);
      });
  }

  return controller;
}

/** Check Sidecar availability */
export async function macHealthCheck(): Promise<{ available: boolean }> {
  try {
    const res = await fetch(`${SIDECAR_URL}/health`, { signal: AbortSignal.timeout(3000) });
    if (res.ok) return { available: true };
  } catch { /* continue */ }
  return { available: false };
}

/** Fetch sidecar status (node info, tools, model) */
export async function fetchSidecarStatus(): Promise<Record<string, unknown> | null> {
  try {
    const res = await fetch(`${SIDECAR_URL}/status`, { signal: AbortSignal.timeout(3000) });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

/** Fetch sidecar configured providers (models) */
export interface ProviderInfo {
  name: string;
  model: string;
  provider_type: string;
  status: string;
}
export async function fetchSidecarProviders(): Promise<ProviderInfo[]> {
  try {
    const res = await fetch(`${SIDECAR_URL}/providers`, { signal: AbortSignal.timeout(3000) });
    if (!res.ok) return [];
    const data = await res.json();
    return data.providers || [];
  } catch {
    return [];
  }
}

/** Fetch sidecar available tools */
export async function fetchSidecarTools(): Promise<Array<{ name: string; description: string }>> {
  try {
    const res = await fetch(`${SIDECAR_URL}/tools`, { signal: AbortSignal.timeout(3000) });
    if (!res.ok) return [];
    const data = await res.json();
    return data.tools || [];
  } catch {
    return [];
  }
}

// ---------------------------------------------------------------------------
// Vault CRUD
// ---------------------------------------------------------------------------

export interface VaultFileNode {
  id: string;
  name: string;
  path: string;
  is_dir: boolean;
  children: VaultFileNode[];
}

export interface VaultTreeResult {
  tree: VaultFileNode[];
  root: string;
}

// Vault CRUD — proxied through Sidecar to avoid CORS issues
// Sidecar forwards to Hub: localhost:8765/vault/* → Hub/vault/*

export async function vaultTree(path = "", depth = 6): Promise<VaultTreeResult> {
  const url = new URL(`${SIDECAR_URL}/vault/tree`);
  if (path) url.searchParams.set("path", path);
  url.searchParams.set("depth", String(depth));
  const res = await fetch(url.toString());
  if (!res.ok) throw new Error(`vault/tree failed: ${res.status}`);
  const data = await res.json();
  return { tree: data.tree, root: data.root || "" };
}

export async function vaultRead(path: string): Promise<string> {
  const url = new URL(`${SIDECAR_URL}/vault/read`);
  url.searchParams.set("path", path);
  const res = await fetch(url.toString());
  if (!res.ok) throw new Error(`vault/read failed: ${res.status}`);
  const data = await res.json();
  return data.content;
}

export function vaultFileUrl(path: string): string {
  const url = new URL(`${SIDECAR_URL}/vault/file`);
  url.searchParams.set("path", path);
  return url.toString();
}

export async function vaultWrite(path: string, content: string): Promise<void> {
  const res = await fetch(`${SIDECAR_URL}/vault/write`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, content }),
  });
  if (!res.ok) throw new Error(`vault/write failed: ${res.status}`);
}

export async function vaultCreate(path: string, isDir: boolean): Promise<void> {
  const res = await fetch(`${SIDECAR_URL}/vault/create`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, is_dir: isDir }),
  });
  if (!res.ok) throw new Error(`vault/create failed: ${res.status}`);
}

export async function vaultDelete(path: string): Promise<void> {
  const url = new URL(`${SIDECAR_URL}/vault/delete`);
  url.searchParams.set("path", path);
  const res = await fetch(url.toString(), { method: "DELETE" });
  if (!res.ok) throw new Error(`vault/delete failed: ${res.status}`);
}

// ---------------------------------------------------------------------------
// Health check
// ---------------------------------------------------------------------------

export async function healthCheck(): Promise<boolean> {
  try {
    const res = await fetch(`${SIDECAR_URL}/health`, { signal: AbortSignal.timeout(5000) });
    return res.ok;
  } catch {
    return false;
  }
}
