import { useState, useCallback } from "react";
import {
  ChevronRight,
  ChevronDown,
  FileText,
  Folder,
  FolderOpen,
  Plus,
  FolderPlus,
  Search,
  MessageSquare,
  Trash2,
  RefreshCw,
} from "lucide-react";
import type { FileNode, Document } from "../../types";
import "./Sidebar.css";

interface Props {
  files: FileNode[];
  activeDocId: string | null;
  onOpenFile: (doc: Document) => void;
  onCreateFile: (parentPath: string, isDir: boolean) => void;
  onDeleteFile: (path: string) => void;
  onSwitchToChat: () => void;
  onRefresh: () => void;
}

export default function Sidebar({
  files,
  activeDocId,
  onOpenFile,
  onCreateFile,
  onDeleteFile,
  onSwitchToChat,
  onRefresh,
}: Props) {
  const [searchQuery, setSearchQuery] = useState("");

  return (
    <aside className="sidebar">
      <div className="sidebar-header titlebar-drag">
        <div className="sidebar-title">Nexus</div>
        <div className="sidebar-actions">
          <button
            className="sidebar-btn"
            title="New file"
            onClick={() => onCreateFile("", false)}
          >
            <Plus size={16} />
          </button>
          <button
            className="sidebar-btn"
            title="New folder"
            onClick={() => onCreateFile("", true)}
          >
            <FolderPlus size={16} />
          </button>
        </div>
      </div>

      <div className="sidebar-search">
        <Search size={14} className="search-icon" />
        <input
          type="text"
          placeholder="Search..."
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
        />
      </div>

      <button className="sidebar-chat-btn" onClick={onSwitchToChat}>
        <MessageSquare size={16} />
        <span>AI Conversation</span>
      </button>

      <div className="sidebar-section-header">
        <div className="sidebar-section-label">Documents</div>
        <button
          className="sidebar-section-btn"
          title="Refresh file tree"
          onClick={onRefresh}
        >
          <RefreshCw size={13} />
        </button>
      </div>

      <nav className="file-tree">
        {files.map((node) => (
          <TreeNode
            key={node.id}
            node={node}
            depth={0}
            activeDocId={activeDocId}
            searchQuery={searchQuery}
            onOpenFile={onOpenFile}
            onCreateFile={onCreateFile}
            onDeleteFile={onDeleteFile}
          />
        ))}
        {files.length === 0 && (
          <div className="tree-empty">
            No documents yet.
            <button onClick={() => onCreateFile("", false)}>
              Create one
            </button>
          </div>
        )}
      </nav>
    </aside>
  );
}

function TreeNode({
  node,
  depth,
  activeDocId,
  searchQuery,
  onOpenFile,
  onCreateFile,
  onDeleteFile,
}: {
  node: FileNode;
  depth: number;
  activeDocId: string | null;
  searchQuery: string;
  onOpenFile: (doc: Document) => void;
  onCreateFile: (parentPath: string, isDir: boolean) => void;
  onDeleteFile: (path: string) => void;
}) {
  const [expanded, setExpanded] = useState(depth < 1);
  const isActive = node.id === activeDocId;

  // Filter by search
  if (searchQuery) {
    const q = searchQuery.toLowerCase();
    const nameMatch = node.name.toLowerCase().includes(q);
    const childMatch = node.children.some((c) =>
      c.name.toLowerCase().includes(q)
    );
    if (!nameMatch && !childMatch) return null;
  }

  const handleClick = useCallback(() => {
    if (node.is_dir) {
      setExpanded((e) => !e);
    } else {
      onOpenFile({
        id: node.id,
        title: node.name.replace(/\.\w+$/, ""),
        path: node.path,
        content: "",
        modified: false,
      });
    }
  }, [node, onOpenFile]);

  return (
    <div className="tree-node">
      <div
        className={`tree-item ${isActive ? "active" : ""}`}
        style={{ paddingLeft: 12 + depth * 16 }}
        onClick={handleClick}
      >
        <span className="tree-icon">
          {node.is_dir ? (
            expanded ? (
              <ChevronDown size={14} />
            ) : (
              <ChevronRight size={14} />
            )
          ) : (
            <span style={{ width: 14 }} />
          )}
        </span>
        <span className="tree-file-icon">
          {node.is_dir ? (
            expanded ? (
              <FolderOpen size={15} />
            ) : (
              <Folder size={15} />
            )
          ) : (
            <FileText size={15} />
          )}
        </span>
        <span className="tree-name">{node.name}</span>
        <span className="tree-actions">
          {node.is_dir && (
            <button
              onClick={(e) => {
                e.stopPropagation();
                onCreateFile(node.path, false);
              }}
              title="New file here"
            >
              <Plus size={12} />
            </button>
          )}
          <button
            onClick={(e) => {
              e.stopPropagation();
              onDeleteFile(node.path);
            }}
            title="Delete"
          >
            <Trash2 size={12} />
          </button>
        </span>
      </div>
      {node.is_dir && expanded && (
        <div className="tree-children">
          {node.children.map((child) => (
            <TreeNode
              key={child.id}
              node={child}
              depth={depth + 1}
              activeDocId={activeDocId}
              searchQuery={searchQuery}
              onOpenFile={onOpenFile}
              onCreateFile={onCreateFile}
              onDeleteFile={onDeleteFile}
            />
          ))}
        </div>
      )}
    </div>
  );
}
