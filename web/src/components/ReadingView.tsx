import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

function readAttribute(block: string, name: string): string {
  const match = block.match(new RegExp(`${name}="([^"]*)"`, "i"));
  return match?.[1] ?? "";
}

function escapeMarkdownText(value: string): string {
  return value.replace(/[[\]\\]/g, "\\$&");
}

function buildDocumentFileUrl(httpUrl: string | undefined, relativePath: string): string {
  const base = httpUrl || window.location.origin;
  return `${base}/documents/file?path=${encodeURIComponent(relativePath)}`;
}

function transformCustomBlocks(markdown: string, httpUrl?: string): string {
  return markdown
    .replace(/<image-block\b[\s\S]*?<\/image-block>/gi, (block) => {
      const src = readAttribute(block, "src");
      if (!src) return "";
      const alt = escapeMarkdownText(readAttribute(block, "alt") || "image");
      return `![${alt}](${buildDocumentFileUrl(httpUrl, src)})`;
    })
    .replace(/<file-block\b[\s\S]*?<\/file-block>/gi, (block) => {
      const src = readAttribute(block, "src");
      if (!src) return "";
      const label = escapeMarkdownText(readAttribute(block, "label") || src);
      return `[${label}](${buildDocumentFileUrl(httpUrl, src)})`;
    })
    .replace(/<audio-block\b[\s\S]*?<\/audio-block>/gi, (block) => {
      const src = readAttribute(block, "src");
      if (!src) return "";
      return `[音频文件](${buildDocumentFileUrl(httpUrl, src)})`;
    });
}

interface ReadingViewProps {
  markdown: string;
  httpUrl?: string;
  className?: string;
}

export default function ReadingView({ markdown, httpUrl, className }: ReadingViewProps) {
  const renderedMarkdown = transformCustomBlocks(markdown, httpUrl);
  return (
    <article className={["reading-view", className].filter(Boolean).join(" ")}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ node: _node, ...props }) => (
            <a {...props} target="_blank" rel="noreferrer" />
          ),
          img: ({ node: _node, ...props }) => <img {...props} loading="lazy" />,
        }}
      >
        {renderedMarkdown}
      </ReactMarkdown>
    </article>
  );
}
