import { useEffect, useCallback, useRef, useState } from "react";
import Sidebar from "./components/sidebar/Sidebar";
import Editor from "./components/editor/Editor";
import ChatPanel from "./components/chat/ChatPanel";
import RightPanel from "./components/rightpanel/RightPanel";
import { useAppStore } from "./stores/appStore";
import type { Document, ChatMessage, TaskStep, WorkingFolder } from "./types";
import * as hub from "./services/hub";

const IMAGE_EXTENSIONS = new Set([".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"]);
const VAULT_TREE_DEPTH = 6;

function isImagePath(path: string): boolean {
  const normalized = path.toLowerCase();
  return Array.from(IMAGE_EXTENSIONS).some((ext) => normalized.endsWith(ext));
}

export default function App() {
  const store = useAppStore();
  const abortRef = useRef<AbortController | null>(null);
  const [vaultSources, setVaultSources] = useState<WorkingFolder[]>([]);
  const [pendingCreate, setPendingCreate] = useState<{ parentPath: string; isDir: boolean } | null>(null);
  const [createName, setCreateName] = useState("");
  const [pendingDeletePath, setPendingDeletePath] = useState<string | null>(null);

  // ------------------------------------------------------------------
  // File tree — load from Hub Vault
  // ------------------------------------------------------------------
  const loadFiles = useCallback(async () => {
    try {
      const result = await hub.vaultTree("", VAULT_TREE_DEPTH);
      store.setFiles(result.tree as any);
      setVaultSources([{
        path: result.root,
        name: result.root.split("/").filter(Boolean).pop() || "vault",
        source: "hub",
        fileCount: result.tree.length,
      }]);
    } catch (e) {
      console.error("Failed to load vault tree:", e);
      store.setFiles(getDemoFiles());
      setVaultSources([{
        path: "(offline)",
        name: "Demo",
        source: "mac",
        fileCount: 2,
      }]);
    }
  }, []);

  useEffect(() => {
    loadFiles();
    // Clear demo data on mount
    store.setTaskSteps([]);
    store.setSkills([]);
  }, []);

  // ------------------------------------------------------------------
  // File operations — all go to Hub Vault
  // ------------------------------------------------------------------
  const handleOpenFile = useCallback(
    async (doc: Document) => {
      const existing = store.openDocs.find((openDoc) => openDoc.id === doc.id);
      if (existing) {
        store.setActiveDocId(doc.id);
        store.setPanelMode("editor");
        if (existing.modified) return;
      }

      try {
        if (isImagePath(doc.path)) {
          const previewUrl = hub.vaultFileUrl(doc.path);
          const imageDoc: Document = {
            ...doc,
            content: "",
            modified: false,
            kind: "image",
            previewUrl,
          };
          if (existing) {
            store.patchDocument(doc.id, imageDoc);
          } else {
            store.openDocument(imageDoc);
          }
          return;
        }
        const content = await hub.vaultRead(doc.path);
        if (existing) {
          store.patchDocument(doc.id, {
            path: doc.path,
            title: doc.title,
            content,
            modified: false,
            kind: "text",
            previewUrl: undefined,
          });
        } else {
          store.openDocument({ ...doc, content, modified: false, kind: "text", previewUrl: undefined });
        }
      } catch {
        if (!existing) {
          store.openDocument({ ...doc, kind: "text", previewUrl: undefined });
        }
      }
    },
    [store.openDocs, store.openDocument, store.patchDocument, store.setActiveDocId, store.setPanelMode]
  );

  const handleSave = useCallback(
    async (docId: string, content: string) => {
      const doc = store.openDocs.find((d) => d.id === docId);
      if (!doc) return;
      try {
        await hub.vaultWrite(doc.path, content);
        const persistedContent = await hub.vaultRead(doc.path);
        const persistedMatchesDraft = persistedContent === content;
        if (!persistedMatchesDraft) {
          console.warn("Saved content differs from editor draft after round-trip read:", doc.path);
        }
        store.patchDocument(docId, {
          content: persistedContent,
          modified: !persistedMatchesDraft,
        });
      } catch (e) {
        console.error("Save failed:", e);
      }
    },
    [store.openDocs, store.patchDocument]
  );

  const requestCreateFile = useCallback((parentPath: string, isDir: boolean) => {
    setPendingCreate({ parentPath, isDir });
    setCreateName("");
  }, []);

  const handleCreateFile = useCallback(
    async (parentPath: string, isDir: boolean) => {
      const name = createName.trim();
      if (!name) return;
      const base = parentPath || "";
      const fullPath = `${base}${base ? "/" : ""}${name}${!isDir && !name.includes(".") ? ".md" : ""}`;
      try {
        await hub.vaultCreate(fullPath, isDir);
        setPendingCreate(null);
        setCreateName("");
        loadFiles();
      } catch (e) {
        console.error("Create failed:", e);
      }
    },
    [createName, loadFiles]
  );

  const requestDeleteFile = useCallback((path: string) => {
    setPendingDeletePath(path);
  }, []);

  const handleDeleteFile = useCallback(
    async (path: string) => {
      try {
        await hub.vaultDelete(path);
        for (const openDoc of store.openDocs) {
          if (openDoc.path === path || openDoc.path.startsWith(`${path}/`)) {
            store.closeDocument(openDoc.id);
          }
        }
        setPendingDeletePath(null);
        loadFiles();
      } catch (e) {
        console.error("Delete failed:", e);
      }
    },
    [loadFiles, store.closeDocument, store.openDocs]
  );

  // ------------------------------------------------------------------
  // Chat — SSE streaming to Hub /desktop/message
  // ------------------------------------------------------------------
  const handleSendMessage = useCallback(
    (content: string) => {
      // Add user message
      const userMsg: ChatMessage = {
        id: crypto.randomUUID(),
        role: "user",
        content,
        timestamp: Date.now(),
      };
      store.addChatMessage(userMsg);

      // Cancel any in-flight request
      if (abortRef.current) abortRef.current.abort();

      // Reset progress for new request
      store.setTaskSteps([{
        id: "init",
        label: content.length > 60 ? content.slice(0, 57) + "..." : content,
        status: "running",
      }]);

      // Create placeholder for assistant streaming response
      const assistantId = crypto.randomUUID();
      const placeholderMsg: ChatMessage = {
        id: assistantId,
        role: "assistant",
        content: "",
        timestamp: Date.now(),
      };
      store.addChatMessage(placeholderMsg);

      abortRef.current = hub.sendMessage({
        content,
        routeMode: store.routeMode,
        providerName: store.providerName || undefined,
        onEvent: (event) => {
          // Extract mesh_plan steps from metadata if present
          const meta = event.metadata as Record<string, unknown> | undefined;
          if (meta?.mesh_plan) {
            const plan = meta.mesh_plan as { steps?: Array<{ step_id: string; description: string; state: string }> };
            if (plan.steps && plan.steps.length > 0) {
              const STATE_MAP: Record<string, TaskStep["status"]> = {
                pending: "pending", assigned: "pending",
                running: "running", waiting_for_node: "running",
                completed: "completed", failed: "error",
              };
              store.setTaskSteps(plan.steps.map((s) => ({
                id: s.step_id,
                label: s.description,
                status: STATE_MAP[s.state] || "pending",
              })));
            }
          }

          if (event.type === "ack") {
            store.updateChatMessage(assistantId, (prev) => ({
              ...prev,
              content: prev.content || "Processing...",
            }));
            // Update init step to show ack
            store.setTaskSteps((prev) => {
              if (prev.length === 1 && prev[0].id === "init") {
                return [{ ...prev[0], status: "running" as const }];
              }
              return prev;
            });
          } else if (event.type === "status") {
            store.updateChatMessage(assistantId, (prev) => ({
              ...prev,
              content: event.content,
            }));
            // If no mesh_plan steps, show status as a running step
            if (!meta?.mesh_plan) {
              store.setTaskSteps((prev) => {
                // Keep existing multi-step plan if present
                if (prev.length > 1) return prev;
                return [{
                  id: "status",
                  label: event.content.replace(/^当前状态：/, ""),
                  status: "running",
                }];
              });
            }
          } else if (event.type === "result") {
            store.updateChatMessage(assistantId, () => ({
              id: assistantId,
              role: "assistant" as const,
              content: event.content,
              timestamp: Date.now(),
            }));
            // Mark all steps completed
            store.setTaskSteps((prev) =>
              prev.map((s) => ({ ...s, status: "completed" as const }))
            );
          } else if (event.type === "error") {
            store.updateChatMessage(assistantId, () => ({
              id: assistantId,
              role: "assistant" as const,
              content: `Error: ${event.content}`,
              timestamp: Date.now(),
            }));
            // Mark current running step as error
            store.setTaskSteps((prev) =>
              prev.map((s) => s.status === "running" ? { ...s, status: "error" as const } : s)
            );
          } else if (event.type === "clarify") {
            store.updateChatMessage(assistantId, () => ({
              id: assistantId,
              role: "assistant" as const,
              content: event.content,
              timestamp: Date.now(),
            }));
          }
        },
        onDone: () => {
          store.updateChatMessage(assistantId, (prev) => {
            if (!prev.content || prev.content === "Processing...") {
              return { ...prev, content: "(No response)" };
            }
            return prev;
          });
          // Mark all remaining running steps as completed
          store.setTaskSteps((prev) =>
            prev.map((s) => s.status === "running" ? { ...s, status: "completed" as const } : s)
          );
          abortRef.current = null;
        },
        onError: (err) => {
          store.updateChatMessage(assistantId, () => ({
            id: assistantId,
            role: "assistant" as const,
            content: `Connection error: ${err.message}`,
            timestamp: Date.now(),
          }));
          store.setTaskSteps((prev) =>
            prev.map((s) => s.status === "running" ? { ...s, status: "error" as const } : s)
          );
          abortRef.current = null;
        },
      });
    },
    [store.addChatMessage, store.updateChatMessage, store.routeMode, store.providerName]
  );

  // ------------------------------------------------------------------
  // Render
  // ------------------------------------------------------------------
  return (
    <div className="app-layout">
      {store.sidebarOpen && (
        <Sidebar
          files={store.files}
          activeDocId={store.activeDocId}
          onOpenFile={handleOpenFile}
          onCreateFile={requestCreateFile}
          onDeleteFile={requestDeleteFile}
          onSwitchToChat={() => store.setPanelMode("chat")}
          onRefresh={loadFiles}
        />
      )}

      <main className="main-area">
        <div className="main-header titlebar-drag">
          {store.activeDoc && store.panelMode === "editor" && (
            <div className="tab-bar">
              {store.openDocs.map((doc) => (
                <button
                  key={doc.id}
                  className={`tab ${doc.id === store.activeDocId ? "active" : ""}`}
                  onClick={() => store.setActiveDocId(doc.id)}
                >
                  <span className="tab-name">
                    {doc.modified && <span className="tab-dot" />}
                    {doc.title}
                  </span>
                  <span
                    className="tab-close"
                    onClick={(e) => {
                      e.stopPropagation();
                      store.closeDocument(doc.id);
                    }}
                  >
                    ×
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="main-content">
          {store.panelMode === "editor" && store.activeDoc ? (
            <Editor
              doc={store.activeDoc}
              onContentChange={store.updateDocContent}
              onSave={handleSave}
            />
          ) : store.panelMode === "chat" || !store.activeDoc ? (
            <ChatPanel
              messages={store.chatMessages}
              routeMode={store.routeMode}
              onRouteChange={store.setRouteMode}
              providerName={store.providerName}
              onProviderChange={store.setProviderName}
              onSendMessage={handleSendMessage}
            />
          ) : null}
        </div>
      </main>

      {store.rightPanelOpen && (
        <RightPanel
          taskSteps={store.taskSteps}
          skills={store.skills}
          workingFolders={vaultSources}
        />
      )}

      {pendingCreate && (
        <div className="dialog-backdrop" onClick={() => setPendingCreate(null)}>
          <div className="dialog-card" onClick={(e) => e.stopPropagation()}>
            <div className="dialog-title">
              {pendingCreate.isDir ? "Create Folder" : "Create File"}
            </div>
            <div className="dialog-subtitle">
              {pendingCreate.parentPath || "vault root"}
            </div>
            <input
              autoFocus
              className="dialog-input"
              placeholder={pendingCreate.isDir ? "Folder name" : "File name"}
              value={createName}
              onChange={(e) => setCreateName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  void handleCreateFile(pendingCreate.parentPath, pendingCreate.isDir);
                } else if (e.key === "Escape") {
                  setPendingCreate(null);
                }
              }}
            />
            <div className="dialog-actions">
              <button className="dialog-btn" onClick={() => setPendingCreate(null)}>
                Cancel
              </button>
              <button
                className="dialog-btn dialog-btn-primary"
                disabled={!createName.trim()}
                onClick={() => void handleCreateFile(pendingCreate.parentPath, pendingCreate.isDir)}
              >
                Create
              </button>
            </div>
          </div>
        </div>
      )}

      {pendingDeletePath && (
        <div className="dialog-backdrop" onClick={() => setPendingDeletePath(null)}>
          <div className="dialog-card" onClick={(e) => e.stopPropagation()}>
            <div className="dialog-title">Delete Item</div>
            <div className="dialog-subtitle">{pendingDeletePath}</div>
            <div className="dialog-body">
              This action will remove the selected file or folder from the vault.
            </div>
            <div className="dialog-actions">
              <button className="dialog-btn" onClick={() => setPendingDeletePath(null)}>
                Cancel
              </button>
              <button
                className="dialog-btn dialog-btn-danger"
                onClick={() => void handleDeleteFile(pendingDeletePath)}
              >
                Delete
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// === Demo data fallback (when Hub is unreachable) ===
function getDemoFiles() {
  return [
    {
      id: "1", name: "Projects", path: "Projects", is_dir: true,
      children: [
        { id: "1a", name: "nexus-roadmap.md", path: "Projects/nexus-roadmap.md", is_dir: false, children: [] },
      ],
    },
    {
      id: "2", name: "Notes", path: "Notes", is_dir: true,
      children: [
        { id: "2a", name: "meeting-notes.md", path: "Notes/meeting-notes.md", is_dir: false, children: [] },
      ],
    },
  ];
}
