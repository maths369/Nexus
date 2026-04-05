import { FormEvent, useMemo, useState } from "react";

export type PageSummary = {
  page_id: string;
  relative_path: string;
  title: string;
  page_type: string;
  updated_at?: string | null;
};

/** Section label mapping */
const SECTION_META: Record<string, { label: string; icon: string; order: number }> = {
  pages:    { label: "页面",   icon: "📄", order: 0 },
  inbox:    { label: "收件箱", icon: "📥", order: 1 },
  journals: { label: "日志",   icon: "📔", order: 2 },
  meetings: { label: "会议",   icon: "🤝", order: 3 },
  knowledge:{ label: "知识库", icon: "🧠", order: 4 },
  strategy: { label: "策略",   icon: "🎯", order: 5 },
  rnd:      { label: "研发",   icon: "🔬", order: 6 },
  life:     { label: "生活",   icon: "🌿", order: 7 },
};

type SectionNode = {
  name: string;
  label: string;
  icon?: string;
  pages: PageSummary[];
};

function comparePages(a: PageSummary, b: PageSummary): number {
  const aTime = a.updated_at ? Date.parse(a.updated_at) : 0;
  const bTime = b.updated_at ? Date.parse(b.updated_at) : 0;
  if (aTime !== bTime) return bTime - aTime;
  return a.title.localeCompare(b.title, "zh-CN");
}

/** Build a top-level-only tree from flat page list. */
function buildTree(pages: PageSummary[]): SectionNode[] {
  const sectionMap = new Map<string, SectionNode>();

  for (const page of pages) {
    const sectionKey = page.relative_path.split("/")[0];
    if (!sectionMap.has(sectionKey)) {
      const meta = SECTION_META[sectionKey] ?? { label: sectionKey, icon: "📁", order: 99 };
      sectionMap.set(sectionKey, {
        name: sectionKey,
        label: meta.label,
        icon: meta.icon,
        pages: [],
      });
    }
    sectionMap.get(sectionKey)!.pages.push(page);
  }

  for (const section of sectionMap.values()) {
    section.pages.sort(comparePages);
  }

  return Array.from(sectionMap.values()).sort((a, b) => {
    const oa = SECTION_META[a.name]?.order ?? 99;
    const ob = SECTION_META[b.name]?.order ?? 99;
    return oa - ob;
  });
}

// -------- Sub-components --------

function FolderItem({
  node,
  selectedPath,
  onSelectPage,
  onDeletePage,
  defaultOpen,
}: {
  node: SectionNode;
  selectedPath: string;
  onSelectPage: (path: string) => void;
  onDeletePage: (page: PageSummary) => void;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen ?? true);
  const total = node.pages.length;

  if (total === 0) return null;

  return (
    <div className="tree-folder" style={{ "--depth": 0 } as React.CSSProperties}>
      <button
        className={`tree-folder-header ${open ? "open" : ""}`}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="tree-chevron">{open ? "▼" : "▶"}</span>
        {node.icon && <span className="tree-icon">{node.icon}</span>}
        <span className="tree-folder-label">{node.label}</span>
        <span className="tree-count">{total}</span>
      </button>
      {open && (
        <div className="tree-folder-children">
          {node.pages.map((page) => (
            <div
              key={page.page_id || page.relative_path}
              className={`tree-page-item ${selectedPath === page.relative_path ? "selected" : ""}`}
              style={{ "--depth": 1 } as React.CSSProperties}
              title={page.relative_path}
            >
              <button
                className="tree-page-select"
                onClick={() => onSelectPage(page.relative_path)}
              >
                <span className="tree-page-title">{page.title}</span>
              </button>
              <button
                className="tree-page-delete"
                onClick={(event) => {
                  event.stopPropagation();
                  onDeletePage(page);
                }}
                title={`删除 ${page.title}`}
                aria-label={`删除 ${page.title}`}
              >
                删除
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// -------- Main Component --------

interface DocumentSidebarProps {
  pages: PageSummary[];
  selectedPath: string;
  onSelectPage: (path: string) => void;
  onDeletePage: (page: PageSummary) => void;
  onRefresh: () => void;
  docsStatus: string;
  newPageTitle: string;
  onNewPageTitleChange: (v: string) => void;
  onCreatePage: (e: FormEvent) => void;
  mobile?: boolean;
}

export default function DocumentSidebar({
  pages,
  selectedPath,
  onSelectPage,
  onDeletePage,
  onRefresh,
  docsStatus,
  newPageTitle,
  onNewPageTitleChange,
  onCreatePage,
  mobile = false,
}: DocumentSidebarProps) {
  const tree = useMemo(() => buildTree(pages), [pages]);

  return (
    <div className={`sidebar-inner ${mobile ? "sidebar-inner-mobile" : ""}`}>
      <div className="sidebar-header">
        <h2>文档</h2>
        <button className="ghost-button" onClick={onRefresh}>刷新</button>
      </div>
      <p className="docs-status">{docsStatus}</p>

      {/* Simplified create form: title only */}
      <form className="create-page-form" onSubmit={onCreatePage}>
        <div className="create-page-row">
          <input
            value={newPageTitle}
            onChange={(e) => onNewPageTitleChange(e.target.value)}
            placeholder={mobile ? "新建移动笔记" : "新页面标题"}
          />
          <button type="submit" className="create-page-btn" title="创建">+</button>
        </div>
      </form>

      {/* Tree view */}
      <div className="page-tree">
        {tree.map((section) => (
          <FolderItem
            key={section.name}
            node={section}
            selectedPath={selectedPath}
            onSelectPage={onSelectPage}
            onDeletePage={onDeletePage}
            defaultOpen={section.name === "pages"}
          />
        ))}
      </div>
    </div>
  );
}
