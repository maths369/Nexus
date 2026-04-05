import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import ChatPanel, { type UiMessage } from "./components/ChatPanel";
import DocumentSidebar, { type PageSummary } from "./components/DocumentSidebar";
import DocumentEditor from "./components/DocumentEditor";
import TokenLogin from "./components/TokenLogin";

type WsPayload =
  | { type: "ack" | "status" | "blocked" | "result" | "clarify" | "error"; content: string; session_id?: string }
  | { type: "pong" }
  | { type: "error"; content: string };

type AuthState = "checking" | "ready" | "required";
type MobilePane = "docs" | "editor" | "chat";
type EditorMode = "read" | "edit";

const MOBILE_BREAKPOINT = 768;
const AUTH_STORAGE_KEY = "nexus.auth_token";
const AUTH_COOKIE_NAME = "__nexus_token";

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

function getStoredToken(): string {
  try {
    return window.localStorage.getItem(AUTH_STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

function persistToken(token: string): void {
  try {
    window.localStorage.setItem(AUTH_STORAGE_KEY, token);
  } catch {
    // ignore storage failures
  }
  document.cookie = `${AUTH_COOKIE_NAME}=${encodeURIComponent(token)}; Path=/; SameSite=Lax`;
}

function clearStoredToken(): void {
  try {
    window.localStorage.removeItem(AUTH_STORAGE_KEY);
  } catch {
    // ignore storage failures
  }
  document.cookie = `${AUTH_COOKIE_NAME}=; Path=/; Max-Age=0; SameSite=Lax`;
}

function buildAuthHeaders(token: string | null): HeadersInit {
  if (!token) return {};
  return { Authorization: `Bearer ${token}` };
}

function buildWsEndpoint(baseUrl: string, token: string | null): string {
  const url = new URL(baseUrl, window.location.origin);
  if (token) url.searchParams.set("token", token);
  return url.toString();
}

function parseRouteState(): { path: string; mode: EditorMode } {
  const url = new URL(window.location.href);
  return {
    path: url.searchParams.get("path") || "",
    mode: url.searchParams.get("mode") === "edit" ? "edit" : "read",
  };
}

function syncRouteState(path: string, mode: EditorMode): void {
  const url = new URL(window.location.href);
  if (!path) {
    url.searchParams.delete("path");
    url.searchParams.delete("mode");
  } else {
    url.searchParams.set("path", path);
    url.searchParams.set("mode", mode);
  }
  window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
}

function App() {
  const initialRoute = useMemo(() => parseRouteState(), []);
  const [wsUrl] = useState(defaultWsUrl);
  const [httpUrl] = useState(defaultHttpUrl);
  const [authState, setAuthState] = useState<AuthState>("checking");
  const [authToken, setAuthToken] = useState<string>(() => getStoredToken());
  const [authError, setAuthError] = useState("");
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
  const [isMobile, setIsMobile] = useState(() => window.innerWidth < MOBILE_BREAKPOINT);
  const [mobilePane, setMobilePane] = useState<MobilePane>(initialRoute.path ? "editor" : "docs");
  const [editorMode, setEditorMode] = useState<EditorMode>(initialRoute.mode);

  const [pages, setPages] = useState<PageSummary[]>([]);
  const [selectedPath, setSelectedPath] = useState<string>(initialRoute.path);
  const [selectedTitle, setSelectedTitle] = useState<string>("");
  const [newPageTitle, setNewPageTitle] = useState("");
  const [docsStatus, setDocsStatus] = useState("正在加载文档…");
  const [editorKey, setEditorKey] = useState(0);
  const [chatOpen, setChatOpen] = useState(false);

  const connectionLabel = useMemo(() => {
    if (connection === "connected") return "已连接";
    if (connection === "connecting") return "连接中";
    return "已断开";
  }, [connection]);

  const authorizedFetch = useCallback(
    (input: RequestInfo | URL, init?: RequestInit) => {
      const headers = new Headers(init?.headers || undefined);
      if (authToken) {
        headers.set("Authorization", `Bearer ${authToken}`);
      }
      return fetch(input, {
        ...init,
        headers,
        credentials: "include",
      });
    },
    [authToken],
  );

  const verifySession = useCallback(
    async (candidateToken: string | null) => {
      const response = await fetch(`${httpUrl}/documents/pages?limit=1`, {
        headers: buildAuthHeaders(candidateToken),
        credentials: "include",
      });
      if (response.status === 401) {
        clearStoredToken();
        setAuthToken("");
        setAuthState("required");
        return false;
      }
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      setAuthState("ready");
      return true;
    },
    [httpUrl],
  );

  const refreshPages = useCallback(async () => {
    setDocsStatus("正在刷新…");
    try {
      const res = await authorizedFetch(`${httpUrl}/documents/pages?limit=100`);
      if (res.status === 401) {
        setAuthState("required");
        setDocsStatus("认证已失效，请重新登录。");
        return;
      }
      if (!res.ok) {
        setDocsStatus(`加载失败：HTTP ${res.status}`);
        return;
      }
      const payload = await res.json();
      const all = (payload.pages ?? []) as PageSummary[];
      const items = all.filter((page) => !shouldHideFromSidebar(page));
      setPages(items);
      setDocsStatus(items.length ? `${items.length} 个页面` : "暂无页面");

      if (!selectedPath) return;
      const current = items.find((page) => page.relative_path === selectedPath);
      if (!current) {
        setSelectedPath("");
        setSelectedTitle("");
        return;
      }
      setSelectedTitle(current.title);
    } catch (err) {
      setDocsStatus(`加载失败：${err instanceof Error ? err.message : String(err)}`);
    }
  }, [authorizedFetch, httpUrl, selectedPath]);

  useEffect(() => {
    const onResize = () => {
      const nextIsMobile = window.innerWidth < MOBILE_BREAKPOINT;
      setIsMobile(nextIsMobile);
      if (!nextIsMobile) return;
      if (selectedPath) {
        setMobilePane("editor");
      }
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [selectedPath]);

  useEffect(() => {
    let cancelled = false;
    const token = getStoredToken();
    setAuthToken(token);
    setAuthState("checking");
    setAuthError("");

    (async () => {
      try {
        const ok = await verifySession(token || null);
        if (!cancelled && ok) {
          setAuthError("");
        }
      } catch (err) {
        if (cancelled) return;
        setAuthState("required");
        setAuthError(`认证检查失败：${err instanceof Error ? err.message : String(err)}`);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [verifySession]);

  useEffect(() => {
    if (authState !== "ready") {
      socketRef.current?.close();
      socketRef.current = null;
      setConnection(authState === "checking" ? "connecting" : "disconnected");
      return;
    }

    const ws = new WebSocket(buildWsEndpoint(wsUrl, authToken || null));
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
      if (isMobile) setMobilePane("chat");
      void refreshPages();
    };

    return () => {
      ws.close();
      socketRef.current = null;
    };
  }, [authState, authToken, isMobile, refreshPages, wsUrl]);

  useEffect(() => {
    if (authState !== "ready") return;
    void refreshPages();
  }, [authState, refreshPages]);

  useEffect(() => {
    const handlePopState = () => {
      const route = parseRouteState();
      setSelectedPath(route.path);
      setEditorMode(route.mode);
      if (route.path) setMobilePane("editor");
    };
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  useEffect(() => {
    syncRouteState(selectedPath, editorMode);
  }, [editorMode, selectedPath]);

  const handleSelectPage = (path: string) => {
    setSelectedPath(path);
    const page = pages.find((item) => item.relative_path === path);
    setSelectedTitle(page?.title || "");
    setEditorKey((key) => key + 1);
    if (isMobile) {
      setEditorMode("read");
      setMobilePane("editor");
    }
  };

  const deletePage = async (page: PageSummary) => {
    const ok = window.confirm(`确定要删除“${page.title}”吗？\n\n路径：${page.relative_path}\n\n删除前会自动创建备份。`);
    if (!ok) return;
    try {
      const res = await authorizedFetch(
        `${httpUrl}/documents/page?path=${encodeURIComponent(page.relative_path)}`,
        { method: "DELETE" },
      );
      if (res.status === 401) {
        setAuthState("required");
        return;
      }
      if (!res.ok) {
        const payload = await res.json().catch(() => ({}));
        throw new Error(payload.error || `HTTP ${res.status}`);
      }
      if (selectedPath === page.relative_path) {
        setSelectedPath("");
        setSelectedTitle("");
        if (isMobile) setMobilePane("docs");
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

  const handlePing = () => {
    socketRef.current?.send(JSON.stringify({ type: "ping" }));
  };

  const createPage = async (event: FormEvent) => {
    event.preventDefault();
    const title = newPageTitle.trim();
    if (!title) return;
    try {
      const res = await authorizedFetch(`${httpUrl}/documents/page`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title, section: "pages", body: `# ${title}\n\n` }),
      });
      if (res.status === 401) {
        setAuthState("required");
        return;
      }
      if (!res.ok) {
        setDocsStatus(`创建失败：HTTP ${res.status}`);
        return;
      }
      const payload = await res.json();
      const newPath = payload.page.relative_path as string;
      setNewPageTitle("");
      await refreshPages();
      handleSelectPage(newPath);
    } catch (err) {
      setDocsStatus(`创建失败：${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const handleLogin = async (token: string) => {
    setAuthError("");
    const trimmed = token.trim();
    persistToken(trimmed);
    setAuthToken(trimmed);
    try {
      const ok = await verifySession(trimmed);
      if (!ok) {
        setAuthError("Token 无效，请重试。");
        return;
      }
      await refreshPages();
    } catch (err) {
      clearStoredToken();
      setAuthToken("");
      setAuthState("required");
      setAuthError(`登录失败：${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const activeMobilePane = useMemo<MobilePane>(() => {
    if (!isMobile) return "editor";
    if (mobilePane === "editor" && !selectedPath) return "docs";
    return mobilePane;
  }, [isMobile, mobilePane, selectedPath]);

  if (authState === "checking") {
    return (
      <div className="token-login-shell">
        <div className="token-login-card">
          <div className="token-login-head">
            <span className="token-login-badge">Workspace</span>
            <h1>正在连接 Nexus</h1>
            <p>验证会话并准备移动工作区…</p>
          </div>
        </div>
      </div>
    );
  }

  if (authState === "required") {
    return (
      <TokenLogin
        busy={false}
        error={authError}
        initialToken={authToken}
        onSubmit={handleLogin}
      />
    );
  }

  return (
    <div className={`app-layout ${isMobile ? "app-layout-mobile" : ""}`}>
      <header className="topbar">
        <div className="topbar-brand">
          <span className="topbar-logo">Nexus</span>
          <span className="topbar-sub">星策</span>
        </div>
        <div className="topbar-actions">
          {!isMobile && (
            <span className={`topbar-status status-${connection}`}>
              <span className="status-dot" />
              {connectionLabel}
            </span>
          )}
          {isMobile ? (
            <div className="topbar-nav">
              <button
                className={`topbar-nav-button ${activeMobilePane === "docs" ? "active" : ""}`}
                onClick={() => setMobilePane("docs")}
              >
                文档
              </button>
              <button
                className={`topbar-nav-button ${activeMobilePane === "editor" ? "active" : ""}`}
                disabled={!selectedPath}
                onClick={() => setMobilePane("editor")}
              >
                文稿
              </button>
              <button
                className={`topbar-nav-button ${activeMobilePane === "chat" ? "active" : ""}`}
                onClick={() => setMobilePane("chat")}
              >
                AI
              </button>
            </div>
          ) : (
            <button
              className={`topbar-chat-toggle ${chatOpen ? "active" : ""}`}
              onClick={() => setChatOpen((value) => !value)}
            >
              AI 对话
              {messages.length > 1 && <span className="chat-badge">{messages.length - 1}</span>}
            </button>
          )}
        </div>
      </header>

      <div className={`main-area ${isMobile ? `mobile-pane-${activeMobilePane}` : ""}`}>
        {(!isMobile || activeMobilePane === "docs") && (
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
              mobile={isMobile}
            />
          </aside>
        )}

        {(!isMobile || activeMobilePane === "editor") && (
          <main className="editor-main">
            <div className="panel panel-editor">
              {selectedPath ? (
                <DocumentEditor
                  key={`${selectedPath}-${editorKey}-${editorMode}`}
                  httpUrl={httpUrl}
                  authToken={authToken}
                  relativePath={selectedPath}
                  title={selectedTitle}
                  mobile={isMobile}
                  mode={editorMode}
                  onModeChange={setEditorMode}
                  onSaved={() => void refreshPages()}
                />
              ) : (
                <div className="empty-state mobile-empty-state">
                  <p>选择一个页面开始阅读或编辑。</p>
                  {isMobile && (
                    <button className="ghost-button" onClick={() => setMobilePane("docs")}>
                      打开文档列表
                    </button>
                  )}
                </div>
              )}
            </div>
          </main>
        )}

        {(!isMobile && chatOpen) || (isMobile && activeMobilePane === "chat") ? (
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
        ) : null}
      </div>
    </div>
  );
}

export default App;
