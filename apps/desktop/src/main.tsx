import { Component, StrictMode, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import "./styles/global.css";
import "./styles/layout.css";
import App from "./App.tsx";

function showFatalOverlay(title: string, detail: string) {
  let el = document.getElementById("__fatal_error_overlay");
  if (!el) {
    el = document.createElement("div");
    el.id = "__fatal_error_overlay";
    el.style.cssText = [
      "position:fixed",
      "inset:16px",
      "z-index:2147483647",
      "background:rgba(20,20,20,0.96)",
      "color:#ffb4b4",
      "border:1px solid rgba(255,120,120,0.35)",
      "border-radius:12px",
      "padding:16px 18px",
      "font:13px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace",
      "white-space:pre-wrap",
      "overflow:auto",
    ].join(";");
    document.body.appendChild(el);
  }
  el.textContent = `${title}\n\n${detail}`;
}

window.addEventListener("error", (event) => {
  const error = event.error instanceof Error ? event.error.stack || event.error.message : "";
  const detail = [event.message, error].filter(Boolean).join("\n");
  showFatalOverlay("Window Error", detail || "Unknown error");
});

window.addEventListener("unhandledrejection", (event) => {
  const reason = event.reason instanceof Error
    ? event.reason.stack || event.reason.message
    : String(event.reason);
  showFatalOverlay("Unhandled Rejection", reason || "Unknown rejection");
});

class RootErrorBoundary extends Component<{ children: ReactNode }, { error: string | null }> {
  state = { error: null as string | null };

  static getDerivedStateFromError(error: unknown) {
    const detail = error instanceof Error ? error.stack || error.message : String(error);
    return { error: detail };
  }

  componentDidCatch(error: unknown) {
    const detail = error instanceof Error ? error.stack || error.message : String(error);
    showFatalOverlay("React Render Error", detail);
  }

  render() {
    if (this.state.error) {
      return null;
    }
    return this.props.children;
  }
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <RootErrorBoundary>
      <App />
    </RootErrorBoundary>
  </StrictMode>,
);
