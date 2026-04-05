import { useCallback, useEffect, useMemo, useState } from "react";
import NexusEditor from "./NexusEditor";
import ReadingView from "./ReadingView";

interface DocumentEditorProps {
  httpUrl: string;
  authToken?: string;
  relativePath: string;
  title: string;
  mobile?: boolean;
  mode?: "read" | "edit";
  onModeChange?: (mode: "read" | "edit") => void;
  onSaved?: () => void;
}

export default function DocumentEditor({
  httpUrl,
  authToken,
  relativePath,
  title,
  mobile = false,
  mode = "edit",
  onModeChange,
  onSaved,
}: DocumentEditorProps) {
  const [content, setContent] = useState<string | null>(null);
  const [pageType, setPageType] = useState<string>("note");
  const [status, setStatus] = useState<string>("加载中...");
  const [loading, setLoading] = useState(true);
  const isReadMode = mobile && mode === "read";
  const authHeaders = useMemo<Record<string, string>>(() => {
    const headers: Record<string, string> = {};
    if (authToken) {
      headers.Authorization = `Bearer ${authToken}`;
    }
    return headers;
  }, [authToken]);

  // Load page content
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setStatus("加载中...");

    (async () => {
      try {
        const res = await fetch(`${httpUrl}/documents/page?path=${encodeURIComponent(relativePath)}`, {
          headers: authHeaders,
          credentials: "include",
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const payload = await res.json();
        if (cancelled) return;
        setPageType(payload.page?.page_type ?? "note");
        setContent(payload.page?.content ?? "");
        setStatus("");
      } catch (err) {
        if (cancelled) return;
        setStatus(`加载失败：${err instanceof Error ? err.message : String(err)}`);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => { cancelled = true; };
  }, [authHeaders, httpUrl, relativePath]);

  // Autosave handler
  const handleAutosave = useCallback(
    async (markdown: string) => {
      setStatus("保存中...");
      try {
        const res = await fetch(`${httpUrl}/documents/page/update`, {
          method: "POST",
          headers: { "Content-Type": "application/json", ...authHeaders },
          credentials: "include",
          body: JSON.stringify({
            relative_path: relativePath,
            content: markdown,
            title,
          }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        setStatus("已保存");
        onSaved?.();
      } catch (err) {
        setStatus(`保存失败：${err instanceof Error ? err.message : String(err)}`);
      }
    },
    [authHeaders, httpUrl, relativePath, title, onSaved],
  );

  const handleChange = useCallback((md: string) => {
    setContent(md);
  }, []);

  const hasRenderablePreview = (pageType === "image_capture" || /<image-block\b/i.test(content ?? ""));

  if (loading || content === null) {
    return <p className="editor-status">{status || "加载中..."}</p>;
  }

  return (
    <div className={`document-editor ${mobile ? "document-editor-mobile" : ""}`}>
      <div className="editor-topbar">
        <div className="editor-topbar-info">
          <strong>{title}</strong>
          <span className="editor-topbar-path">{relativePath}</span>
        </div>
        <div className="editor-topbar-actions">
          {status && <span className="editor-topbar-status">{status}</span>}
          {mobile && (
            <button
              className="ghost-button"
              onClick={() => onModeChange?.(isReadMode ? "edit" : "read")}
            >
              {isReadMode ? "编辑" : "完成"}
            </button>
          )}
        </div>
      </div>
      {isReadMode ? (
        <ReadingView markdown={content} httpUrl={httpUrl} />
      ) : (
        <>
          {hasRenderablePreview && (
            <section className="document-preview-panel">
              <div className="document-preview-label">预览</div>
              <ReadingView markdown={content} httpUrl={httpUrl} className="document-preview" />
            </section>
          )}
          <NexusEditor
            value={content}
            onChange={handleChange}
            onAutosave={handleAutosave}
          />
        </>
      )}
    </div>
  );
}
