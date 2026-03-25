/**
 * GhostText — TipTap extension for inline AI completion.
 *
 * Shows a gray "ghost" suggestion after the cursor.
 * - Tab to accept
 * - Esc to dismiss
 * - Continue typing to auto-dismiss and re-trigger after debounce
 */

import { Extension } from "@tiptap/react";
import { Plugin, PluginKey } from "@tiptap/pm/state";
import { Decoration, DecorationSet } from "@tiptap/pm/view";
import { requestCompletion } from "../../../services/editorAI";

const DEBOUNCE_MS = 600;
const MIN_CONTEXT_LENGTH = 5;

const ghostTextPluginKey = new PluginKey("ghostText");

/**
 * Extract plain text before and after the cursor from the document.
 */
function getContext(state: typeof Plugin.prototype.spec.state) {
  // This function receives the EditorState
  return null; // placeholder, actual implementation below
}

export interface GhostTextOptions {
  docPath?: string;
  enabled?: boolean;
}

export const GhostText = Extension.create<GhostTextOptions>({
  name: "ghostText",

  addOptions() {
    return {
      docPath: "",
      enabled: true,
    };
  },

  addProseMirrorPlugins() {
    const extensionOptions = this.options;

    return [
      new Plugin({
        key: ghostTextPluginKey,

        state: {
          init() {
            return {
              suggestion: "" as string,
              pos: 0 as number,
              visible: false as boolean,
            };
          },
          apply(tr, prev) {
            // If the transaction has ghost-text metadata, update state
            const meta = tr.getMeta(ghostTextPluginKey);
            if (meta) {
              return meta;
            }
            // Any document change clears the ghost text
            if (tr.docChanged || tr.selectionSet) {
              return { suggestion: "", pos: 0, visible: false };
            }
            return prev;
          },
        },

        props: {
          decorations(state) {
            const pluginState = ghostTextPluginKey.getState(state);
            if (!pluginState?.visible || !pluginState.suggestion) {
              return DecorationSet.empty;
            }

            const { pos, suggestion } = pluginState;
            // Validate position is within document
            if (pos < 0 || pos > state.doc.content.size) {
              return DecorationSet.empty;
            }

            const widget = Decoration.widget(pos, () => {
              const span = document.createElement("span");
              span.className = "ghost-text";
              span.textContent = suggestion;
              return span;
            }, { side: 1 });

            return DecorationSet.create(state.doc, [widget]);
          },

          handleKeyDown(view, event) {
            const pluginState = ghostTextPluginKey.getState(view.state);
            if (!pluginState?.visible || !pluginState.suggestion) {
              return false;
            }

            if (event.key === "Tab") {
              event.preventDefault();
              // Accept: insert the suggestion text at cursor
              const { pos, suggestion } = pluginState;
              const tr = view.state.tr.insertText(suggestion, pos);
              // Clear ghost state
              tr.setMeta(ghostTextPluginKey, {
                suggestion: "",
                pos: 0,
                visible: false,
              });
              view.dispatch(tr);
              return true;
            }

            if (event.key === "Escape") {
              event.preventDefault();
              // Dismiss
              const tr = view.state.tr.setMeta(ghostTextPluginKey, {
                suggestion: "",
                pos: 0,
                visible: false,
              });
              view.dispatch(tr);
              return true;
            }

            return false;
          },
        },

        view(editorView) {
          let debounceTimer: ReturnType<typeof setTimeout> | null = null;
          let abortController: AbortController | null = null;

          const scheduleCompletion = () => {
            if (!extensionOptions.enabled) return;

            // Cancel pending request
            if (abortController) {
              abortController.abort();
              abortController = null;
            }
            if (debounceTimer) {
              clearTimeout(debounceTimer);
            }

            debounceTimer = setTimeout(async () => {
              const { state } = editorView;
              const { from } = state.selection;

              // Only trigger at end of a text block (not in the middle of selection)
              if (!state.selection.empty) return;

              // Extract text before cursor
              const beforeSlice = state.doc.textBetween(
                0,
                Math.min(from, state.doc.content.size),
                "\n",
                "\n"
              );

              if (beforeSlice.trim().length < MIN_CONTEXT_LENGTH) return;

              // Extract text after cursor
              const afterSlice = state.doc.textBetween(
                Math.min(from, state.doc.content.size),
                state.doc.content.size,
                "\n",
                "\n"
              );

              abortController = new AbortController();
              try {
                const suggestion = await requestCompletion(
                  {
                    contextBefore: beforeSlice.slice(-2000),
                    contextAfter: afterSlice.slice(0, 500),
                    docPath: extensionOptions.docPath,
                  },
                  abortController.signal
                );

                if (suggestion && !abortController.signal.aborted) {
                  // Check the cursor hasn't moved
                  const currentFrom = editorView.state.selection.from;
                  if (currentFrom === from) {
                    const tr = editorView.state.tr.setMeta(ghostTextPluginKey, {
                      suggestion,
                      pos: from,
                      visible: true,
                    });
                    editorView.dispatch(tr);
                  }
                }
              } catch {
                // Aborted or failed — silently ignore
              }
            }, DEBOUNCE_MS);
          };

          return {
            update(view, prevState) {
              // Only trigger on document changes (user typing)
              if (view.state.doc.eq(prevState.doc)) return;
              scheduleCompletion();
            },
            destroy() {
              if (debounceTimer) clearTimeout(debounceTimer);
              if (abortController) abortController.abort();
            },
          };
        },
      }),
    ];
  },
});
