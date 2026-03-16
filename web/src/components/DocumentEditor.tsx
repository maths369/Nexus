import { useCallback, useEffect, useState } from "react";
import NexusEditor from "./NexusEditor";

interface DocumentEditorProps {
  httpUrl: string;
  relativePath: string;
  title: string;
  onSaved?: () => void;
}

export default function DocumentEditor({ httpUrl, relativePath, title, onSaved }: DocumentEditorProps) {
  const [content, setContent] = useState<string | null>(null);
  const [status, setStatus] = useState<string>("加载中...");
  const [loading, setLoading] = useState(true);

  // Load page content
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setStatus("加载中...");

    (async () => {
      try {
        const res = await fetch(`${httpUrl}/documents/page?path=${encodeURIComponent(relativePath)}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const payload = await res.json();
        if (cancelled) return;
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
  }, [httpUrl, relativePath]);

  // Autosave handler
  const handleAutosave = useCallback(
    async (markdown: string) => {
      setStatus("保存中...");
      try {
        const res = await fetch(`${httpUrl}/documents/page/update`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
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
    [httpUrl, relativePath, title, onSaved],
  );

  const handleChange = useCallback((md: string) => {
    setContent(md);
  }, []);

  if (loading || content === null) {
    return <p className="editor-status">{status || "加载中..."}</p>;
  }

  return (
    <div className="document-editor">
      <div className="editor-topbar">
        <div className="editor-topbar-info">
          <strong>{title}</strong>
          <span className="editor-topbar-path">{relativePath}</span>
        </div>
        {status && <span className="editor-topbar-status">{status}</span>}
      </div>
      <NexusEditor
        value={content}
        onChange={handleChange}
        onAutosave={handleAutosave}
      />
    </div>
  );
}
