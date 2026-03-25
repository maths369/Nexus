/**
 * SelectionToolbar — floating AI action menu that appears when text is selected.
 *
 * Actions: Translate, Polish, Expand, Condense, Rewrite
 */

import { useEffect, useState, useRef, useCallback } from "react";
import type { Editor } from "@tiptap/react";
import type { EditorView } from "@tiptap/pm/view";
import { requestTransform, type TransformAction } from "../../services/editorAI";
import { Languages, Sparkles, Expand, Shrink, RefreshCw } from "lucide-react";
import "./SelectionToolbar.css";

interface Props {
  editor: Editor;
  docPath?: string;
}

interface ToolbarPosition {
  top: number;
  left: number;
}

const MIN_SELECTION_LENGTH = 1;
const TOOLBAR_HEIGHT = 44;
const VIEWPORT_PADDING = 12;

function clamp(value: number, min: number, max: number) {
  return Math.min(Math.max(value, min), max);
}

type InternalEditorViewRef = {
  editorView?: EditorView | null;
};

function getMountedEditorView(editor: Editor): EditorView | null {
  const view = (editor as unknown as InternalEditorViewRef).editorView ?? null;

  if (!view || view.isDestroyed) {
    return null;
  }

  return view;
}

function resolveSelectionInfo(editor: Editor) {
  const view = getMountedEditorView(editor);
  if (!view) return null;

  const domSelection = window.getSelection();
  if (domSelection && domSelection.rangeCount > 0 && !domSelection.isCollapsed) {
    const range = domSelection.getRangeAt(0);
    if (view.dom.contains(range.commonAncestorContainer)) {
      const rect = range.getBoundingClientRect();
      return {
        from: editor.state.selection.from,
        to: editor.state.selection.to,
        text: range.toString(),
        top: rect.top,
        left: rect.left,
        right: rect.right,
      };
    }
  }

  const { from, to } = editor.state.selection;
  if (from === to) return null;

  try {
    const start = view.coordsAtPos(from);
    const end = view.coordsAtPos(to);
    return {
      from,
      to,
      text: editor.state.doc.textBetween(from, to, "\n"),
      top: Math.min(start.top, end.top),
      left: Math.min(start.left, end.left),
      right: Math.max(start.right, end.right),
    };
  } catch {
    return null;
  }
}

const actions: {
  id: TransformAction;
  label: string;
  icon: React.ReactNode;
  shortLabel: string;
}[] = [
  { id: "translate", label: "Translate", icon: <Languages size={14} />, shortLabel: "Translate" },
  { id: "polish", label: "Polish", icon: <Sparkles size={14} />, shortLabel: "Polish" },
  { id: "expand", label: "Expand", icon: <Expand size={14} />, shortLabel: "Expand" },
  { id: "condense", label: "Condense", icon: <Shrink size={14} />, shortLabel: "Condense" },
  { id: "rewrite", label: "Rewrite", icon: <RefreshCw size={14} />, shortLabel: "Rewrite" },
];

export default function SelectionToolbar({ editor, docPath }: Props) {
  const [visible, setVisible] = useState(false);
  const [position, setPosition] = useState<ToolbarPosition>({ top: 0, left: 0 });
  const [loading, setLoading] = useState(false);
  const [preview, setPreview] = useState<string | null>(null);
  const [selectedText, setSelectedText] = useState("");
  const toolbarRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const hideTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const scheduleHide = useCallback(() => {
    if (hideTimerRef.current) {
      clearTimeout(hideTimerRef.current);
    }
    hideTimerRef.current = setTimeout(() => {
      if (!toolbarRef.current?.matches(":hover")) {
        setVisible(false);
        setPreview(null);
      }
    }, 200);
  }, []);

  const syncToolbarFromSelection = useCallback(() => {
    const info = resolveSelectionInfo(editor);

    if (!info || info.from === info.to || info.text.trim().length < MIN_SELECTION_LENGTH) {
      scheduleHide();
      return;
    }

    if (hideTimerRef.current) {
      clearTimeout(hideTimerRef.current);
      hideTimerRef.current = null;
    }

    setPosition({
      top: clamp(
        info.top - TOOLBAR_HEIGHT,
        VIEWPORT_PADDING,
        window.innerHeight - TOOLBAR_HEIGHT - VIEWPORT_PADDING
      ),
      left: clamp(
        (info.left + info.right) / 2,
        VIEWPORT_PADDING,
        window.innerWidth - VIEWPORT_PADDING
      ),
    });
    setSelectedText(info.text);
    setVisible(true);
    setPreview(null);
  }, [editor, scheduleHide]);

  // Track selection changes
  useEffect(() => {
    const scheduleSync = () => syncToolbarFromSelection();
    let frameId = 0;
    let cleanupDomListeners: (() => void) | null = null;

    const attach = () => {
      const view = getMountedEditorView(editor);
      if (!view) {
        frameId = requestAnimationFrame(attach);
        return;
      }

      view.dom.addEventListener("mouseup", scheduleSync);
      view.dom.addEventListener("keyup", scheduleSync);
      view.dom.addEventListener("touchend", scheduleSync);
      cleanupDomListeners = () => {
        view.dom.removeEventListener("mouseup", scheduleSync);
        view.dom.removeEventListener("keyup", scheduleSync);
        view.dom.removeEventListener("touchend", scheduleSync);
      };
    };

    editor.on("selectionUpdate", scheduleSync);
    document.addEventListener("selectionchange", scheduleSync);
    attach();

    return () => {
      if (frameId) {
        cancelAnimationFrame(frameId);
      }
      if (hideTimerRef.current) {
        clearTimeout(hideTimerRef.current);
      }
      if (abortRef.current) {
        abortRef.current.abort();
      }
      editor.off("selectionUpdate", scheduleSync);
      document.removeEventListener("selectionchange", scheduleSync);
      cleanupDomListeners?.();
    };
  }, [editor, syncToolbarFromSelection]);

  const handleAction = useCallback(
    async (action: TransformAction) => {
      if (loading) return;

      // Cancel any pending request
      if (abortRef.current) abortRef.current.abort();
      abortRef.current = new AbortController();

      setLoading(true);
      setPreview(null);

      const { from, to } = editor.state.selection;

      // Get surrounding context
      const contextBefore = editor.state.doc.textBetween(
        Math.max(0, from - 500),
        from,
        "\n"
      );
      const contextAfter = editor.state.doc.textBetween(
        to,
        Math.min(to + 500, editor.state.doc.content.size),
        "\n"
      );

      // Auto-detect target language for translation
      let targetLanguage: "zh" | "en" | "" = "";
      if (action === "translate") {
        // Simple heuristic: if mostly CJK chars, translate to English; otherwise to Chinese
        const cjkCount = (selectedText.match(/[\u4e00-\u9fff]/g) || []).length;
        targetLanguage = cjkCount > selectedText.length * 0.3 ? "en" : "zh";
      }

      try {
        const result = await requestTransform(
          {
            action,
            selectedText,
            contextBefore,
            contextAfter,
            docPath,
            targetLanguage,
          },
          abortRef.current.signal
        );

        if (result) {
          setPreview(result);
        }
      } catch {
        // Aborted or failed
      } finally {
        setLoading(false);
      }
    },
    [editor, selectedText, loading, docPath]
  );

  const acceptPreview = useCallback(() => {
    if (!preview) return;
    const { from, to } = editor.state.selection;
    editor.chain().focus().deleteRange({ from, to }).insertContentAt(from, preview).run();
    setPreview(null);
    setVisible(false);
  }, [editor, preview]);

  const rejectPreview = useCallback(() => {
    setPreview(null);
  }, []);

  if (!visible) return null;

  return (
    <div
      ref={toolbarRef}
      className="selection-toolbar"
      style={{
        top: `${position.top}px`,
        left: `${position.left}px`,
      }}
      onMouseDown={(e) => e.preventDefault()} // Prevent losing selection
    >
      {preview ? (
        <div className="selection-toolbar-preview">
          <div className="preview-text">{preview}</div>
          <div className="preview-actions">
            <button className="preview-btn accept" onClick={acceptPreview}>
              Accept
            </button>
            <button className="preview-btn reject" onClick={rejectPreview}>
              Reject
            </button>
          </div>
        </div>
      ) : (
        <div className="selection-toolbar-buttons">
          {actions.map((action) => (
            <button
              key={action.id}
              className={`selection-action-btn ${loading ? "disabled" : ""}`}
              onClick={() => handleAction(action.id)}
              title={action.label}
              disabled={loading}
            >
              {action.icon}
              <span>{action.shortLabel}</span>
            </button>
          ))}
          {loading && <span className="selection-loading" />}
        </div>
      )}
    </div>
  );
}
