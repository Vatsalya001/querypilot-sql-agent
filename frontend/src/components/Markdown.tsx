import clsx from 'clsx';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { highlightSql } from '../lib/highlight';

const components: Components = {
  a: ({ href, title, children }) => (
    <a href={href} title={title} target="_blank" rel="noreferrer noopener">
      {children}
    </a>
  ),
  code: ({ className, children }) => {
    const language = /language-(\w+)/.exec(className ?? '')?.[1]?.toLowerCase();
    if (language === 'sql') {
      const source = String(children).replace(/\n$/, '');
      return <code className="sql-code" dangerouslySetInnerHTML={{ __html: highlightSql(source) }} />;
    }
    return <code className={className}>{children}</code>;
  },
};

/** Renders agent text as GitHub-flavoured Markdown (raw HTML is never rendered). */
export function Markdown({ children, className }: { children: string; className?: string }) {
  return (
    <div className={clsx('md', className)}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {children}
      </ReactMarkdown>
    </div>
  );
}
