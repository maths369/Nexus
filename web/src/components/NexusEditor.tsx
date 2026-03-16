import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { EditorContent, BubbleMenu, useEditor } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import { Markdown } from "tiptap-markdown";
import Placeholder from "@tiptap/extension-placeholder";
import TaskList from "@tiptap/extension-task-list";
import TaskItem from "@tiptap/extension-task-item";
import Table from "@tiptap/extension-table";
import TableRow from "@tiptap/extension-table-row";
import TableCell from "@tiptap/extension-table-cell";
import TableHeader from "@tiptap/extension-table-header";
import Image from "@tiptap/extension-image";
import { SlashCommand, type SlashItem } from "./SlashCommandExtension";
import { ToggleExtension, ToggleSummary, ToggleContent } from "./ToggleExtension";

interface NexusEditorProps {
  value: string;
  onChange: (markdown: string) => void;
  onAutosave?: (markdown: string) => Promise<void> | void;
  disabled?: boolean;
}

const slashItems: SlashItem[] = [
  // 格式
  {
    title: "标题 H1",
    description: "大标题",
    icon: "H1",
    category: "格式",
    command: ({ editor, range }) =>
      editor.chain().focus().deleteRange(range).setNode("heading", { level: 1 }).run(),
  },
  {
    title: "标题 H2",
    description: "章节标题",
    icon: "H2",
    category: "格式",
    command: ({ editor, range }) =>
      editor.chain().focus().deleteRange(range).setNode("heading", { level: 2 }).run(),
  },
  {
    title: "标题 H3",
    description: "小节标题",
    icon: "H3",
    category: "格式",
    command: ({ editor, range }) =>
      editor.chain().focus().deleteRange(range).setNode("heading", { level: 3 }).run(),
  },
  {
    title: "段落",
    description: "普通文本",
    icon: "\u00b6",
    category: "格式",
    command: ({ editor, range }) =>
      editor.chain().focus().deleteRange(range).setNode("paragraph").run(),
  },
  {
    title: "引用",
    description: "插入引用块",
    icon: "\u275d",
    category: "格式",
    command: ({ editor, range }) =>
      editor.chain().focus().deleteRange(range).toggleBlockquote().run(),
  },
  {
    title: "代码块",
    description: "高亮代码块",
    icon: "</>",
    category: "格式",
    command: ({ editor, range }) =>
      editor.chain().focus().deleteRange(range).toggleCodeBlock().run(),
  },
  {
    title: "分隔线",
    description: "插入水平线",
    icon: "\u2500",
    category: "格式",
    command: ({ editor, range }) =>
      editor.chain().focus().deleteRange(range).setHorizontalRule().run(),
  },
  // 列表
  {
    title: "待办列表",
    description: "插入 checklist",
    icon: "\u2611",
    category: "列表",
    command: ({ editor, range }) =>
      editor.chain().focus().deleteRange(range).toggleTaskList().run(),
  },
  {
    title: "项目符号",
    description: "无序列表",
    icon: "\u2022",
    category: "列表",
    command: ({ editor, range }) =>
      editor.chain().focus().deleteRange(range).toggleBulletList().run(),
  },
  {
    title: "有序列表",
    description: "编号列表",
    icon: "1.",
    category: "列表",
    command: ({ editor, range }) =>
      editor.chain().focus().deleteRange(range).toggleOrderedList().run(),
  },
  {
    title: "折叠列表",
    description: "可折叠的内容块",
    icon: "\u25b6",
    category: "列表",
    command: ({ editor, range }) => {
      editor
        .chain()
        .focus()
        .deleteRange(range)
        .insertContent([
          {
            type: "toggleGroup",
            attrs: { open: true },
            content: [
              { type: "toggleSummary", content: [{ type: "text", text: "折叠标题" }] },
              {
                type: "toggleContent",
                content: [
                  {
                    type: "paragraph",
                    content: [{ type: "text", text: "在此输入折叠内容..." }],
                  },
                ],
              },
            ],
          },
        ])
        .run();
    },
  },
  // 表格
  {
    title: "插入表格",
    description: "3x3 表格",
    icon: "\u25a6",
    category: "表格",
    command: ({ editor, range }) =>
      editor
        .chain()
        .focus()
        .deleteRange(range)
        .insertTable({ rows: 3, cols: 3, withHeaderRow: true })
        .run(),
  },
];

export default function NexusEditor({ value, onChange, onAutosave, disabled }: NexusEditorProps) {
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);

  const extensions = useMemo(
    () => [
      StarterKit.configure({
        bulletList: { keepMarks: true },
        orderedList: { keepMarks: true },
      }),
      TaskList.configure({
        HTMLAttributes: { class: "task-list" },
      }),
      TaskItem.configure({ nested: true }),
      Table.configure({
        resizable: true,
        HTMLAttributes: { class: "table" },
      }),
      TableRow,
      TableHeader,
      TableCell,
      Image.configure({
        inline: false,
        HTMLAttributes: { class: "inline-image" },
      }),
      ToggleExtension,
      ToggleSummary,
      ToggleContent,
      Markdown.configure({
        html: true,
        transformPastedText: false,
        transformCopiedText: false,
        linkify: true,
      }),
      Placeholder.configure({
        placeholder: "输入 / 唤起命令，或直接开始写作...",
      }),
      SlashCommand(slashItems),
    ],
    [],
  );

  const editor = useEditor({
    extensions,
    content: value || "",
    autofocus: "end",
    editable: !disabled,
    onUpdate: ({ editor }) => {
      const markdown = editor.storage.markdown?.serializer.serialize(editor.state.doc);
      if (markdown !== undefined) {
        onChange(markdown);
        setDirty(true);
      }
    },
    editorProps: {
      attributes: {
        class: "tiptap nexus-editor",
        spellcheck: "false",
      },
      handleClick: (_view, _pos, event) => {
        const target = event.target as HTMLElement;
        const anchor = target?.closest("a");
        if (anchor) {
          const href = anchor.getAttribute("href");
          if (href && (href.startsWith("http://") || href.startsWith("https://"))) {
            event.preventDefault();
            window.open(href, "_blank");
            return true;
          }
        }
        return false;
      },
    },
  });

  // Sync external value changes into editor
  useEffect(() => {
    if (!editor) return;
    const currentMarkdown =
      editor.storage.markdown?.serializer.serialize(editor.state.doc) || "";
    if (value === currentMarkdown) return;

    const mdStorage = (editor.storage as any)?.markdown;
    if (mdStorage?.parser) {
      try {
        const doc = mdStorage.parser.parse(value || "");
        editor.commands.setContent(doc, false);
        return;
      } catch {
        // fallback below
      }
    }
    editor.commands.setContent(value || "", false);
  }, [editor, value]);

  // Autosave with 2s debounce
  useEffect(() => {
    if (!dirty || !onAutosave || saving) return;
    const handle = setTimeout(async () => {
      try {
        setSaving(true);
        const md = editor?.storage.markdown?.serializer.serialize(editor.state.doc) || "";
        await onAutosave(md);
        setDirty(false);
      } finally {
        setSaving(false);
      }
    }, 2000);
    return () => clearTimeout(handle);
  }, [dirty, onAutosave, editor, saving]);

  // Cmd+S manual save
  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && !event.shiftKey && event.key.toLowerCase() === "s") {
        event.preventDefault();
        if (onAutosave && editor) {
          const md = editor.storage.markdown?.serializer.serialize(editor.state.doc) || "";
          void onAutosave(md);
          setDirty(false);
        }
      }
    };
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, [onAutosave, editor]);

  return (
    <div
      className="nexus-editor-shell"
      style={disabled ? { pointerEvents: "none", opacity: 0.65 } : undefined}
    >
      <div className="editor-host">
        {editor ? (
          <BubbleMenu
            className="bubble-toolbar"
            editor={editor}
            shouldShow={({ editor }) => !editor.state.selection.empty}
          >
            <button
              className={`mini-btn${editor.isActive("bold") ? " active" : ""}`}
              onClick={() => editor.chain().focus().toggleBold().run()}
            >
              B
            </button>
            <button
              className={`mini-btn${editor.isActive("italic") ? " active" : ""}`}
              onClick={() => editor.chain().focus().toggleItalic().run()}
            >
              I
            </button>
            <button
              className={`mini-btn${editor.isActive("code") ? " active" : ""}`}
              onClick={() => editor.chain().focus().toggleCode().run()}
            >
              {"<>"}
            </button>
          </BubbleMenu>
        ) : null}
        <EditorContent editor={editor} />
      </div>
      {saving && <div className="editor-save-indicator">保存中...</div>}
    </div>
  );
}
