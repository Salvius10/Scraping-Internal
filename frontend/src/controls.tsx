// Small controls shared by both feeds.

import { useEffect, useState } from "react";
import { safeHref, SOURCE_TYPE_LABEL, type SourceRef } from "./api";
import DownloadIcon from "./DownloadIcon";

/** A search box that reports its text once typing pauses. */
export function SearchBox({ value, onChange, placeholder }: {
  value: string;
  onChange: (text: string) => void;
  placeholder: string;
}) {
  const [text, setText] = useState(value);
  useEffect(() => setText(value), [value]);
  useEffect(() => {
    if (text === value) return;
    const timer = window.setTimeout(() => onChange(text.trim()), 300);
    return () => window.clearTimeout(timer);
  }, [text, value, onChange]);
  return (
    <input
      className="search"
      type="search"
      value={text}
      placeholder={placeholder}
      aria-label={placeholder}
      maxLength={200}
      onChange={(e) => setText(e.target.value)}
    />
  );
}

export function DateInputs({ start, end, onStart, onEnd, min, max, label }: {
  start: string;
  end: string;
  onStart: (v: string) => void;
  onEnd: (v: string) => void;
  min?: string;
  max?: string;
  label: string;
}) {
  return (
    <div className="dates" role="group" aria-label={label}>
      <label>
        From
        <input type="date" value={start} min={min} max={end || max}
               onChange={(e) => onStart(e.target.value)} />
      </label>
      <label>
        To
        <input type="date" value={end} min={start || min} max={max}
               onChange={(e) => onEnd(e.target.value)} />
      </label>
    </div>
  );
}

export function ExportLink({ href, disabled }: { href: string; disabled?: boolean }) {
  if (disabled) {
    return (
      <span className="btn btn--quiet" aria-disabled="true">
        <DownloadIcon /> Export to Excel
      </span>
    );
  }
  return (
    <a className="btn btn--primary" href={href} download>
      <DownloadIcon /> Export to Excel
    </a>
  );
}

/** The row's own source, plus every other source merged into it. */
export function Sources({ url, label, type, others }: {
  url: string;
  label: string;
  type: string;
  others: SourceRef[];
}) {
  const extra = others.filter((s) => s.url !== url);
  return (
    <div className="sources">
      <a href={safeHref(url)} target="_blank" rel="noreferrer noopener">{label}</a>
      <span className="muted"> · {SOURCE_TYPE_LABEL[type] ?? type}</span>
      {extra.length > 0 && (
        <details className="also">
          <summary>+{extra.length} more</summary>
          <ul>
            {extra.map((s) => (
              <li key={s.url}>
                <a href={safeHref(s.url)} target="_blank" rel="noreferrer noopener">{s.source}</a>
                <span className="muted"> · {SOURCE_TYPE_LABEL[s.type] ?? s.type}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

export function Skeleton() {
  return (
    <div aria-busy="true" aria-label="Loading">
      {Array.from({ length: 6 }, (_, i) => <div key={i} className="skeleton" />)}
    </div>
  );
}
