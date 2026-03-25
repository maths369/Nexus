import { useState, useRef, useEffect } from "react";
import { Send, Paperclip, Mic, Slash, ChevronDown, Cpu } from "lucide-react";
import type { ChatMessage, RouteMode } from "../../types";
import { fetchSidecarProviders, type ProviderInfo } from "../../services/hub";
import "./ChatPanel.css";

interface Props {
  messages: ChatMessage[];
  routeMode: RouteMode;
  onRouteChange: (mode: RouteMode) => void;
  providerName: string | null;
  onProviderChange: (name: string | null) => void;
  onSendMessage: (content: string) => void;
}

const SLASH_COMMANDS = [
  { cmd: "/new", label: "New session", desc: "Start a fresh conversation" },
  { cmd: "/compress", label: "Compress", desc: "Compress conversation context" },
  { cmd: "/pause", label: "Pause", desc: "Pause current task" },
  { cmd: "/resume", label: "Resume", desc: "Resume paused task" },
  { cmd: "/cancel", label: "Cancel", desc: "Cancel current task" },
  { cmd: "/status", label: "Status", desc: "Query task status" },
  { cmd: "/restart", label: "Restart", desc: "Restart Hub service" },
  { cmd: "/help", label: "Help", desc: "Show available commands" },
];

const ROUTE_LABELS: Record<RouteMode, { label: string; desc: string }> = {
  auto: { label: "Auto", desc: "Hub decides, fallback to Mac" },
  hub: { label: "Hub", desc: "Remote Hub server" },
  mac: { label: "Mac", desc: "Local Mac sidecar" },
};

export default function ChatPanel({ messages, routeMode, onRouteChange, providerName, onProviderChange, onSendMessage }: Props) {
  const [input, setInput] = useState("");
  const [showSlash, setShowSlash] = useState(false);
  const [slashFilter, setSlashFilter] = useState("");
  const [slashIndex, setSlashIndex] = useState(0);
  const [showRouteMenu, setShowRouteMenu] = useState(false);
  const [showModelMenu, setShowModelMenu] = useState(false);
  const [sidecarProviders, setSidecarProviders] = useState<ProviderInfo[]>([]);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const routeMenuRef = useRef<HTMLDivElement>(null);
  const modelMenuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.length]);

  // Fetch providers when model menu opens
  useEffect(() => {
    if (!showModelMenu) return;
    fetchSidecarProviders().then((providers) => {
      if (providers.length > 0) setSidecarProviders(providers);
    });
  }, [showModelMenu]);

  // Close menus on outside click
  useEffect(() => {
    if (!showRouteMenu && !showModelMenu) return;
    const handler = (e: MouseEvent) => {
      if (showRouteMenu && routeMenuRef.current && !routeMenuRef.current.contains(e.target as Node)) {
        setShowRouteMenu(false);
      }
      if (showModelMenu && modelMenuRef.current && !modelMenuRef.current.contains(e.target as Node)) {
        setShowModelMenu(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [showRouteMenu, showModelMenu]);

  const filteredCommands = SLASH_COMMANDS.filter((c) =>
    c.cmd.includes(slashFilter.toLowerCase())
  );

  // Current display model name
  const currentModelDisplay = providerName
    ? sidecarProviders.find((p) => p.name === providerName)?.model || providerName
    : "Default";

  const handleSend = () => {
    const text = input.trim();
    if (!text) return;
    setShowSlash(false);
    onSendMessage(text);
    setInput("");
    if (textareaRef.current) textareaRef.current.style.height = "auto";
  };

  const handleSelectCommand = (cmd: string) => {
    setShowSlash(false);
    setSlashFilter("");
    onSendMessage(cmd);
    setInput("");
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (showSlash && filteredCommands.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSlashIndex((i) => Math.min(i + 1, filteredCommands.length - 1));
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setSlashIndex((i) => Math.max(i - 1, 0));
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        handleSelectCommand(filteredCommands[slashIndex].cmd);
        return;
      }
      if (e.key === "Escape") {
        setShowSlash(false);
        return;
      }
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const val = e.target.value;
    setInput(val);
    e.target.style.height = "auto";
    e.target.style.height = Math.min(e.target.scrollHeight, 160) + "px";
    if (val.startsWith("/")) {
      setShowSlash(true);
      setSlashFilter(val);
      setSlashIndex(0);
    } else {
      setShowSlash(false);
    }
  };

  const toggleSlashMenu = () => {
    if (showSlash) {
      setShowSlash(false);
    } else {
      setShowSlash(true);
      setInput("/");
      setSlashFilter("/");
      setSlashIndex(0);
      textareaRef.current?.focus();
    }
  };

  return (
    <div className="chat-panel">
      <div className="chat-messages">
        {messages.length === 0 && (
          <div className="chat-empty">
            <div className="chat-empty-icon">N</div>
            <h3>AI Conversation</h3>
            <p>Ask anything, or type <code>/</code> for commands.</p>
          </div>
        )}
        {messages.map((msg) => (
          <div key={msg.id} className={`chat-msg chat-msg-${msg.role}`}>
            <div className="chat-msg-avatar">
              {msg.role === "user" ? "Y" : "N"}
            </div>
            <div className="chat-msg-body">
              <div className="chat-msg-role">
                {msg.role === "user" ? "You" : "Nexus"}
              </div>
              <div className="chat-msg-content">{msg.content}</div>
            </div>
          </div>
        ))}
        <div ref={messagesEndRef} />
      </div>

      <div className="chat-input-area">
        {showSlash && filteredCommands.length > 0 && (
          <div className="slash-menu">
            {filteredCommands.map((c, i) => (
              <button
                key={c.cmd}
                className={`slash-item ${i === slashIndex ? "active" : ""}`}
                onMouseDown={(e) => { e.preventDefault(); handleSelectCommand(c.cmd); }}
                onMouseEnter={() => setSlashIndex(i)}
              >
                <span className="slash-cmd">{c.cmd}</span>
                <span className="slash-desc">{c.desc}</span>
              </button>
            ))}
          </div>
        )}
        <div className="chat-input-wrapper">
          <button className="chat-attach-btn" title="Attach file">
            <Paperclip size={16} />
          </button>
          <button
            className={`chat-slash-btn ${showSlash ? "active" : ""}`}
            title="Commands"
            onClick={toggleSlashMenu}
          >
            <Slash size={14} />
          </button>
          <textarea
            ref={textareaRef}
            className="chat-textarea"
            placeholder="Reply... (type / for commands)"
            value={input}
            onChange={handleChange}
            onKeyDown={handleKeyDown}
            rows={1}
          />
          <button className="chat-voice-btn" title="Voice input">
            <Mic size={16} />
          </button>
          <button
            className="chat-send-btn"
            onClick={handleSend}
            disabled={!input.trim()}
          >
            <Send size={16} />
          </button>
        </div>
        <div className="chat-input-footer">
          <div className="route-selector" ref={routeMenuRef}>
            <button
              className="route-current"
              onClick={() => setShowRouteMenu((v) => !v)}
            >
              <span className={`route-dot route-dot-${routeMode}`} />
              <span>{ROUTE_LABELS[routeMode].label}</span>
              <ChevronDown size={12} />
            </button>
            {showRouteMenu && (
              <div className="route-menu">
                {(["auto", "hub", "mac"] as RouteMode[]).map((mode) => (
                  <button
                    key={mode}
                    className={`route-option ${mode === routeMode ? "active" : ""}`}
                    onClick={() => {
                      onRouteChange(mode);
                      setShowRouteMenu(false);
                    }}
                  >
                    <span className={`route-dot route-dot-${mode}`} />
                    <span className="route-option-label">{ROUTE_LABELS[mode].label}</span>
                    <span className="route-option-desc">{ROUTE_LABELS[mode].desc}</span>
                  </button>
                ))}
              </div>
            )}
          </div>

          <div className="model-selector" ref={modelMenuRef}>
            <button
              className="model-current"
              onClick={() => setShowModelMenu((v) => !v)}
            >
              <Cpu size={12} />
              <span>{currentModelDisplay}</span>
              <ChevronDown size={12} />
            </button>
            {showModelMenu && (
              <div className="model-menu">
                {/* Default option — let the system pick */}
                <button
                  className={`model-option ${!providerName ? "active" : ""}`}
                  onClick={() => { onProviderChange(null); setShowModelMenu(false); }}
                >
                  <span className="model-option-name">Default</span>
                  <span className="model-option-tag">System picks best</span>
                </button>

                {/* Available providers from Sidecar */}
                {sidecarProviders.length > 0 && (
                  <>
                    <div className="model-section-label">Available Models</div>
                    {sidecarProviders.map((p) => (
                      <button
                        key={p.name}
                        className={`model-option ${providerName === p.name ? "active" : ""}`}
                        onClick={() => { onProviderChange(p.name); setShowModelMenu(false); }}
                      >
                        <span className="model-option-name">{p.model}</span>
                        <span className="model-option-tag">{p.name}</span>
                      </button>
                    ))}
                  </>
                )}

                {sidecarProviders.length === 0 && (
                  <div className="model-hint">Loading providers...</div>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
