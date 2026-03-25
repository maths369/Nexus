import type { Editor } from "@tiptap/react";
import {
  Bold,
  Italic,
  Strikethrough,
  Code,
  Heading1,
  Heading2,
  Heading3,
  List,
  ListOrdered,
  ListChecks,
  Quote,
  Minus,
  CodeSquare,
  Undo2,
  Redo2,
} from "lucide-react";
import "./EditorToolbar.css";

interface Props {
  editor: Editor;
}

export default function EditorToolbar({ editor }: Props) {
  const btn = (
    label: string,
    icon: React.ReactNode,
    action: () => void,
    isActive?: boolean
  ) => (
    <button
      className={`toolbar-btn ${isActive ? "active" : ""}`}
      onClick={action}
      title={label}
    >
      {icon}
    </button>
  );

  return (
    <div className="editor-toolbar">
      <div className="toolbar-group">
        {btn("Bold", <Bold size={15} />, () => editor.chain().focus().toggleBold().run(), editor.isActive("bold"))}
        {btn("Italic", <Italic size={15} />, () => editor.chain().focus().toggleItalic().run(), editor.isActive("italic"))}
        {btn("Strikethrough", <Strikethrough size={15} />, () => editor.chain().focus().toggleStrike().run(), editor.isActive("strike"))}
        {btn("Code", <Code size={15} />, () => editor.chain().focus().toggleCode().run(), editor.isActive("code"))}
      </div>
      <div className="toolbar-divider" />
      <div className="toolbar-group">
        {btn("Heading 1", <Heading1 size={15} />, () => editor.chain().focus().toggleHeading({ level: 1 }).run(), editor.isActive("heading", { level: 1 }))}
        {btn("Heading 2", <Heading2 size={15} />, () => editor.chain().focus().toggleHeading({ level: 2 }).run(), editor.isActive("heading", { level: 2 }))}
        {btn("Heading 3", <Heading3 size={15} />, () => editor.chain().focus().toggleHeading({ level: 3 }).run(), editor.isActive("heading", { level: 3 }))}
      </div>
      <div className="toolbar-divider" />
      <div className="toolbar-group">
        {btn("Bullet List", <List size={15} />, () => editor.chain().focus().toggleBulletList().run(), editor.isActive("bulletList"))}
        {btn("Ordered List", <ListOrdered size={15} />, () => editor.chain().focus().toggleOrderedList().run(), editor.isActive("orderedList"))}
        {btn("Task List", <ListChecks size={15} />, () => editor.chain().focus().toggleTaskList().run(), editor.isActive("taskList"))}
      </div>
      <div className="toolbar-divider" />
      <div className="toolbar-group">
        {btn("Quote", <Quote size={15} />, () => editor.chain().focus().toggleBlockquote().run(), editor.isActive("blockquote"))}
        {btn("Code Block", <CodeSquare size={15} />, () => editor.chain().focus().toggleCodeBlock().run(), editor.isActive("codeBlock"))}
        {btn("Divider", <Minus size={15} />, () => editor.chain().focus().setHorizontalRule().run())}
      </div>
      <div className="toolbar-spacer" />
      <div className="toolbar-group">
        {btn("Undo", <Undo2 size={15} />, () => editor.chain().focus().undo().run())}
        {btn("Redo", <Redo2 size={15} />, () => editor.chain().focus().redo().run())}
      </div>
    </div>
  );
}
