"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export function RichText({ markdown, className = "" }: { markdown: string; className?: string }) {
  return <div className={`rich-text ${className}`.trim()}><ReactMarkdown remarkPlugins={[remarkGfm]}>{markdown}</ReactMarkdown></div>;
}
