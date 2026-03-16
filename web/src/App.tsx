import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import ChatPanel, { type UiMessage } from "./components/ChatPanel";
import DocumentSidebar, { type PageSummary } from "./components/DocumentSidebar";
import DocumentEditor from "./components/DocumentEditor";

type WsPayload =
  | { type: "ack" | "status" | "blocked" | "result" | "clarify" | "error"; content: string; session_id?: string }
  | { type: "pong" }
  | { type: "error"; content: string };

const defaultWsUrl = (() => {
  const fromEnv = (import.meta as ImportMeta & { env?: Record<string, string> }).env?.VITE_NEXUS_WS_URL;
  if (fromEnv) return fromEnv;
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  return `${protocol}://${window.location.host}/ws`;
})();

const defaultHttpUrl = (() => {
  const fromEnv = (import.meta as ImportMeta & { env?: Record<string, string> }).env?.VITE_NEXUS_HTTP_URL;
  if (fromEnv) return fromEnv;
  return `${window.location.origin}`;
})();

function shouldHideFromSidebar(page: PageSummary): boolean {
  const path = page.relative_path;
  const title = page.title;
  return (
    path.startsWith("_system/") ||
    path.includes("outlook_calendar_") ||
    title.includes("Outlook 日历事件") ||
    path.includes("outlook_emails_") ||
    title.includes("Outlook 邮件")
  );
}

function App() {
  const [wsUrl] = useState(defaultWsUrl);
  const [httpUrl] = useState(defaultHttpUrl);
  const [connection, setConnection] = useState<"connecting" | "connected" | "disconnected">("connecting");
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<UiMessage[]>([
    {
      id: "welcome",
      type: "system",
      content: "欢迎使用 **星策（Nexus）**。输入自然语言与 AI 对话。",
    },
  ]);
  const socketRef = useRef<WebSocket | null>(null);

  const [pages, setPages] = useState<PageSummary[]>([]);
  const [selectedPath, setSelectedPath] = useState<string>("");
  const [selectedTitle, setSelectedTitle] = useState<string>("");
  const [newPageTitle, setNewPageTitle] = useState("");
  const [docsStatus, setDocsStatus] = useState("正在加载文档…");
  const [editorKey, setEditorKey] = useState(0);
  const [chatOpen, setChatOpen] = useState(false);

  // --- WebSocket ---
  useEffect(() => {
    const ws = new WebSocket(wsUrl);
    socketRef.current = ws;
    setConnection("connecting");
    ws.onopen = () => setConnection("connected");
    ws.onclose = () => setConnection("disconnected");
    ws.onerror = () => setConnection("disconnected");
    ws.onmessage = (event) => {
      const payload = JSON.parse(event.data) as WsPayload;
      if (payload.type === "pong") return;
      setMessages((cur) => [
        ...cur,
        {
          id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
          type: payload.type,
          content: payload.content,
          sessionId: "session_id" in payload ? payload.session_id : undefined,
        },
      ]);
      setChatOpen(true);
      // Refresh page tree in case agent moved/created files
      void refreshPages();
    };
    return () => { ws.close(); socketRef.current = null; };
  }, [wsUrl]);

  useEffect(() => { void refreshPages(); }, [httpUrl]);

  const connectionLabel = useMemo(() => {
    if (connection === "connected") return "已连接";
    if (connection === "connecting") return "连接中";
    return "已断开";
  }, [connection]);

  const refreshPages = async () => {
    setDocsStatus("正在刷新…");
    try {
      const res = await fetch(`${httpUrl}/documents/pages?limit=100`);
      if (!res.ok) { setDocsStatus(`加载失败：HTTP ${res.status}`); return; }
      const payload = await res.json();
      const all = (payload.pages ?? []) as PageSummary[];
      const items = all.filter((p) => !shouldHideFromSidebar(p));
      setPages(items);
      setDocsStatus(items.length ? `${items.length} 个页面` : "暂无页面");
      if (selectedPath && !items.some((p) => p.relative_path === selectedPath)) {
        setSelectedPath("");
        setSelectedTitle("");
      }
    } catch (err) {
      setDocsStatus(`加载失败：${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const handleSelectPage = (path: string) => {
    setSelectedPath(path);
    const page = pages.find((p) => p.relative_path === path);
    setSelectedTitle(page?.title || "");
    setEditorKey((k) => k + 1);
  };

  const deletePage = async (page: PageSummary) => {
    const ok = window.confirm(`确定要删除“${page.title}”吗？\n\n路径：${page.relative_path}\n\n删除前会自动创建备份。`);
    if (!ok) return;
    try {
      const res = await fetch(`${httpUrl}/documents/page?path=${encodeURIComponent(page.relative_path)}`, {
        method: "DELETE",
      });
      if (!res.ok) {
        const payload = await res.json().catch(() => ({}));
        throw new Error(payload.error || `HTTP ${res.status}`);
      }
      if (selectedPath === page.relative_path) {
        setSelectedPath("");
        setSelectedTitle("");
      }
      setMessages((cur) => [
        ...cur,
        {
          id: `local-delete-${Date.now()}`,
          type: "system",
          content: `已删除页面：${page.title}（${page.relative_path}）`,
        },
      ]);
      await refreshPages();
    } catch (err) {
      setDocsStatus(`删除失败：${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const sendMessage = (event: FormEvent) => {
    event.preventDefault();
    const value = input.trim();
    if (!value || !socketRef.current || socketRef.current.readyState !== WebSocket.OPEN) return;
    socketRef.current.send(
      JSON.stringify({ type: "message", seq: Date.now(), sender_id: "web-user", content: value }),
    );
    setMessages((cur) => [...cur, { id: `local-${Date.now()}`, type: "system", content: `> ${value}` }]);
    setInput("");
  };

  const handlePing = () => { socketRef.current?.send(JSON.stringify({ type: "ping" })); };

  const createPage = async (event: FormEvent) => {
    event.preventDefault();
    const title = newPageTitle.trim();
    if (!title) return;
    try {
      const res = await fetch(`${httpUrl}/documents/page`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title, section: "pages", body: `# ${title}\n\n` }),
      });
      if (!res.ok) { setDocsStatus(`创建失败：HTTP ${res.status}`); return; }
      const payload = await res.json();
      const newPath = payload.page.relative_path as string;
      setNewPageTitle("");
      await refreshPages();
      handleSelectPage(newPath);
    } catch (err) {
      setDocsStatus(`创建失败：${err instanceof Error ? err.message : String(err)}`);
    }
  };

  return (
    <div className="app-layout">
      {/* ---- Top Bar ---- */}
      <header className="topbar">
        <div className="topbar-brand">
          <span className="topbar-logo">Nexus</span>
          <span className="topbar-sub">星策</span>
        </div>
        <div className="topbar-actions">
          <span className={`topbar-status status-${connection}`}>
            <span className="status-dot" />
            {connectionLabel}
          </span>
          <button
            className={`topbar-chat-toggle ${chatOpen ? "active" : ""}`}
            onClick={() => setChatOpen((v) => !v)}
          >
            AI 对话
            {messages.length > 1 && <span className="chat-badge">{messages.length - 1}</span>}
          </button>
        </div>
      </header>

      {/* ---- Main Area ---- */}
      <div className="main-area">
        {/* Left: Page Sidebar */}
        <aside className="sidebar">
          <DocumentSidebar
            pages={pages}
            selectedPath={selectedPath}
            onSelectPage={handleSelectPage}
            onDeletePage={deletePage}
            onRefresh={() => void refreshPages()}
            docsStatus={docsStatus}
            newPageTitle={newPageTitle}
            onNewPageTitleChange={setNewPageTitle}
            onCreatePage={createPage}
          />
        </aside>

        {/* Center: Document Editor (primary) */}
        <main className="editor-main">
          <div className="panel panel-editor">
            {selectedPath ? (
              <DocumentEditor
                key={`${selectedPath}-${editorKey}`}
                httpUrl={httpUrl}
                relativePath={selectedPath}
                title={selectedTitle}
                onSaved={() => void refreshPages()}
              />
            ) : (
              <p className="empty-state">选择一个页面开始编辑，或创建新页面。</p>
            )}
          </div>
        </main>

        {/* Right: AI Chat (auxiliary, collapsible) */}
        {chatOpen && (
          <aside className="chat-aside">
            <ChatPanel
              messages={messages}
              input={input}
              onInputChange={setInput}
              onSend={sendMessage}
              onPing={handlePing}
              connected={connection === "connected"}
            />
          </aside>
        )}
      </div>
    </div>
  );
}

export default App;
