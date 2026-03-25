import { useState, useCallback } from "react";
import type { FileNode, Document, ChatMessage, TaskStep, SkillInfo, PanelMode, RouteMode } from "../types";

export function useAppStore() {
  const [files, setFiles] = useState<FileNode[]>([]);
  const [openDocs, setOpenDocs] = useState<Document[]>([]);
  const [activeDocId, setActiveDocId] = useState<string | null>(null);
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [taskSteps, setTaskSteps] = useState<TaskStep[]>([]);
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [panelMode, setPanelMode] = useState<PanelMode>("chat");
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [rightPanelOpen, setRightPanelOpen] = useState(true);
  const [routeMode, setRouteMode] = useState<RouteMode>("auto");
  const [providerName, setProviderName] = useState<string | null>(null);  // null = use default

  const activeDoc = openDocs.find((d) => d.id === activeDocId) || null;

  const openDocument = useCallback(
    (doc: Document) => {
      setOpenDocs((prev) => {
        if (prev.find((d) => d.id === doc.id)) return prev;
        return [...prev, doc];
      });
      setActiveDocId(doc.id);
      setPanelMode("editor");
    },
    []
  );

  const closeDocument = useCallback(
    (docId: string) => {
      setOpenDocs((prev) => {
        const closingIndex = prev.findIndex((d) => d.id === docId);
        const nextDocs = prev.filter((d) => d.id !== docId);

        setActiveDocId((current) => {
          if (current !== docId) return current;
          if (nextDocs.length === 0) {
            setPanelMode("chat");
            return null;
          }
          const fallbackIndex = Math.max(0, Math.min(closingIndex, nextDocs.length - 1));
          setPanelMode("editor");
          return nextDocs[fallbackIndex]?.id ?? null;
        });

        return nextDocs;
      });
    },
    []
  );

  const updateDocContent = useCallback(
    (docId: string, content: string) => {
      setOpenDocs((prev) =>
        prev.map((d) =>
          d.id === docId ? { ...d, content, modified: true } : d
        )
      );
    },
    []
  );

  const patchDocument = useCallback(
    (docId: string, patch: Partial<Document>) => {
      setOpenDocs((prev) =>
        prev.map((d) =>
          d.id === docId ? { ...d, ...patch } : d
        )
      );
    },
    []
  );

  const markDocSaved = useCallback(
    (docId: string) => {
      setOpenDocs((prev) =>
        prev.map((d) =>
          d.id === docId ? { ...d, modified: false } : d
        )
      );
    },
    []
  );

  const addChatMessage = useCallback(
    (msg: ChatMessage) => {
      setChatMessages((prev) => [...prev, msg]);
    },
    []
  );

  const updateChatMessage = useCallback(
    (msgId: string, updater: (prev: ChatMessage) => ChatMessage) => {
      setChatMessages((prev) =>
        prev.map((m) => (m.id === msgId ? updater(m) : m))
      );
    },
    []
  );

  return {
    files, setFiles,
    openDocs, activeDoc, activeDocId,
    openDocument, closeDocument, updateDocContent, patchDocument, markDocSaved,
    setActiveDocId,
    chatMessages, addChatMessage, updateChatMessage, setChatMessages,
    taskSteps, setTaskSteps,
    skills, setSkills,
    panelMode, setPanelMode,
    sidebarOpen, setSidebarOpen,
    rightPanelOpen, setRightPanelOpen,
    routeMode, setRouteMode,
    providerName, setProviderName,
  };
}
