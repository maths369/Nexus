import { useEffect, useRef } from "react";
import { useEditor, EditorContent } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import { Markdown } from "@tiptap/markdown";
import Placeholder from "@tiptap/extension-placeholder";
import TaskList from "@tiptap/extension-task-list";
import TaskItem from "@tiptap/extension-task-item";
import CodeBlockLowlight from "@tiptap/extension-code-block-lowlight";
import Image from "@tiptap/extension-image";
import { Table } from "@tiptap/extension-table";
import { TableRow } from "@tiptap/extension-table-row";
import { TableCell } from "@tiptap/extension-table-cell";
import { TableHeader } from "@tiptap/extension-table-header";
import { common, createLowlight } from "lowlight";
import type { Document } from "../../types";
import EditorToolbar from "./EditorToolbar";
import { GhostText } from "./extensions/GhostText";
import SelectionToolbar from "./SelectionToolbar";
import "./Editor.css";

const lowlight = createLowlight(common);

interface Props {
  doc: Document;
  onContentChange: (docId: string, content: string) => void;
  onSave: (docId: string, content: string) => void;
}

export default function Editor({ doc, onContentChange, onSave }: Props) {
  if (doc.kind === "image" && doc.previewUrl) {
    return <ImageDocumentViewer doc={doc} />;
  }
  return <MarkdownDocumentEditor doc={doc} onContentChange={onContentChange} onSave={onSave} />;
}

function ImageDocumentViewer({ doc }: { doc: Document }) {
  return (
    <div className="editor-container">
      <div className="editor-titlebar">
        <input
          className="editor-title-input"
          value={doc.title}
          readOnly
          placeholder="Untitled"
        />
      </div>
      <div className="image-viewer-shell">
        <div className="image-viewer-meta">{doc.path}</div>
        <div className="image-viewer-stage">
          <img className="image-viewer-image" src={doc.previewUrl} alt={doc.title} />
        </div>
      </div>
    </div>
  );
}

function MarkdownDocumentEditor({ doc, onContentChange, onSave }: Props) {
  const onSaveRef = useRef(onSave);
  const onContentChangeRef = useRef(onContentChange);
  const editorRef = useRef<ReturnType<typeof useEditor> | null>(null);

  useEffect(() => {
    onSaveRef.current = onSave;
    onContentChangeRef.current = onContentChange;
  }, [onSave, onContentChange]);

  const editor = useEditor(
    {
      extensions: [
        StarterKit.configure({
          codeBlock: false,
          heading: { levels: [1, 2, 3] },
        }),
        Markdown.configure({
          markedOptions: {
            gfm: true,
            breaks: false,
          },
        }),
        Placeholder.configure({
          placeholder: ({ node }) => {
            if (node.type.name === "heading") {
              return "Heading";
            }
            return "Type '/' for commands...";
          },
        }),
        TaskList,
        TaskItem.configure({ nested: true }),
        CodeBlockLowlight.configure({ lowlight }),
        Image,
        Table.configure({ resizable: true }),
        TableRow,
        TableCell,
        TableHeader,
        GhostText.configure({ docPath: doc.path, enabled: true }),
      ],
      content: doc.content || "",
      contentType: "markdown",
      onUpdate: ({ editor }) => {
        onContentChangeRef.current(doc.id, editor.getMarkdown());
      },
      editorProps: {
        attributes: {
          class: "notion-editor",
          spellcheck: "false",
        },
        handleKeyDown: (view, event) => {
          if ((event.metaKey || event.ctrlKey) && event.key === "s") {
            event.preventDefault();
            const currentEditor = editorRef.current;
            const markdownManager = currentEditor?.storage.markdown?.manager;
            const content = markdownManager
              ? markdownManager.serialize(view.state.doc.toJSON())
              : currentEditor?.getMarkdown() ?? "";
            onSaveRef.current(doc.id, content);
            return true;
          }
          return false;
        },
      },
    },
    [doc.id]
  );

  // Sync content when switching documents
  useEffect(() => {
    editorRef.current = editor;
  }, [editor]);

  // Sync content when switching documents
  useEffect(() => {
    if (editor && !editor.isDestroyed) {
      const currentContent = editor.getMarkdown();
      if (currentContent !== doc.content) {
        editor.commands.setContent(doc.content || "", {
          contentType: "markdown",
          emitUpdate: false,
        });
      }
    }
  }, [editor, doc.id, doc.content]);

  if (!editor) return null;

  return (
    <div className="editor-container">
      <div className="editor-titlebar">
        <input
          className="editor-title-input"
          value={doc.title}
          readOnly
          placeholder="Untitled"
        />
        {doc.modified && <span className="editor-modified-badge">Modified</span>}
      </div>
      <EditorToolbar editor={editor} />
      <div className="editor-scroll">
        <EditorContent editor={editor} />
      </div>
      <SelectionToolbar editor={editor} docPath={doc.path} />
    </div>
  );
}
