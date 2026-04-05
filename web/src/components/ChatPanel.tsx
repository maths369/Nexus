import { FormEvent, useEffect, useRef } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export type MessageType = "ack" | "status" | "blocked" | "result" | "clarify" | "error" | "system";

export type UiMessage = {
  id: string;
  type: MessageType;
  content: string;
  sessionId?: string;
};

interface ChatPanelProps {
  messages: UiMessage[];
  input: string;
  onInputChange: (value: string) => void;
  onSend: (e: FormEvent) => void;
  onPing: () => void;
  connected: boolean;
}

export default function ChatPanel({
  messages,
  input,
  onInputChange,
  onSend,
  onPing,
  connected,
}: ChatPanelProps) {
  const listRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  return (
    <>
      <div className="panel panel-log">
        <div className="panel-header">
          <h2>消息流</h2>
          <button className="ghost-button" onClick={onPing} disabled={!connected}>
            Ping
          </button>
        </div>
        <div className="message-list" ref={listRef}>
          {messages.map((message) => (
            <article key={message.id} className={`message message-${message.type}`}>
              <header>
                <span className="message-type">{message.type}</span>
                {message.sessionId ? (
                  <span className="message-session">{message.sessionId}</span>
                ) : null}
              </header>
              <div className="message-body">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
              </div>
            </article>
          ))}
        </div>
      </div>

      <div className="panel panel-compose">
        <form onSubmit={onSend} className="composer">
          <textarea
            value={input}
            onChange={(e) => onInputChange(e.target.value)}
            placeholder="例如：帮我整理今天的会议纪要，提取行动项并写入 Vault。"
            rows={8}
          />
          <div className="composer-footer">
            <button type="submit" disabled={!connected || !input.trim()}>
              发送
            </button>
          </div>
        </form>
      </div>
    </>
  );
}
