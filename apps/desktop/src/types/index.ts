export interface FileNode {
  id: string;
  name: string;
  path: string;
  is_dir: boolean;
  children: FileNode[];
  icon?: string;
}

export interface Document {
  id: string;
  title: string;
  path: string;
  content: string;
  modified: boolean;
  kind?: "text" | "image";
  previewUrl?: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  timestamp: number;
}

export interface TaskStep {
  id: string;
  label: string;
  status: "pending" | "running" | "completed" | "error";
}

export interface SkillInfo {
  id: string;
  name: string;
  icon?: string;
  active: boolean;
}

export type PanelMode = "editor" | "chat" | "split";

export type RouteMode = "auto" | "hub" | "mac";

export interface WorkingFolder {
  path: string;
  name: string;
  source: "hub" | "mac";
  fileCount: number;
}
