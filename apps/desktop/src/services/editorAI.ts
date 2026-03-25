/**
 * Editor AI Client — inline completion & text transformation.
 * Communicates with the Sidecar's /editor/* endpoints via SSE.
 */

// 使用当前页面的 origin 而非硬编码，避免 WKWebView 中 localhost vs 127.0.0.1 跨域问题
const SIDECAR_URL = typeof window !== "undefined" && window.location.origin !== "null"
  ? window.location.origin
  : "http://127.0.0.1:8765";

// ---------------------------------------------------------------------------
// Inline Completion
// ---------------------------------------------------------------------------

export interface CompletionRequest {
  contextBefore: string;
  contextAfter: string;
  docPath?: string;
  maxTokens?: number;
}

/**
 * Request an inline completion suggestion.
 * Returns the suggestion text, or empty string if none.
 * The caller should create an AbortController and pass its signal.
 */
export async function requestCompletion(
  req: CompletionRequest,
  signal?: AbortSignal
): Promise<string> {
  const res = await fetch(`${SIDECAR_URL}/editor/complete`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      context_before: req.contextBefore,
      context_after: req.contextAfter,
      doc_path: req.docPath || "",
      max_tokens: req.maxTokens || 200,
    }),
    signal,
  });

  if (!res.ok || !res.body) return "";

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let suggestion = "";

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
        if (event.type === "suggestion") {
          suggestion = event.text;
        }
      } catch {
        // skip malformed
      }
    }
  }

  return suggestion;
}

// ---------------------------------------------------------------------------
// Text Transformation
// ---------------------------------------------------------------------------

export type TransformAction =
  | "translate"
  | "polish"
  | "expand"
  | "condense"
  | "rewrite";

export interface TransformRequest {
  action: TransformAction;
  selectedText: string;
  contextBefore?: string;
  contextAfter?: string;
  docPath?: string;
  targetLanguage?: "zh" | "en" | "";
}

/**
 * Transform selected text (translate, polish, etc.).
 * Returns the transformed text.
 */
export async function requestTransform(
  req: TransformRequest,
  signal?: AbortSignal
): Promise<string> {
  const res = await fetch(`${SIDECAR_URL}/editor/transform`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      action: req.action,
      selected_text: req.selectedText,
      context_before: req.contextBefore || "",
      context_after: req.contextAfter || "",
      doc_path: req.docPath || "",
      target_language: req.targetLanguage || "",
    }),
    signal,
  });

  if (!res.ok || !res.body) return "";

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result = "";

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
        if (event.type === "transform") {
          result = event.text;
        }
      } catch {
        // skip malformed
      }
    }
  }

  return result;
}
