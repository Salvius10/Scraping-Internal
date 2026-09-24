import { useEffect, useState } from "react";

/** Copy text to the clipboard, falling back for non-secure contexts. */
async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // navigator.clipboard needs a secure context; a LAN IP over http is not one.
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  }
}

/** A small button that copies a story's link and confirms it did. */
export default function CopyLink({ url }: { url: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");

  useEffect(() => {
    if (state === "idle") return;
    const timer = setTimeout(() => setState("idle"), 1600);
    return () => clearTimeout(timer);
  }, [state]);

  const label =
    state === "copied" ? "Link copied" : state === "failed" ? "Copy failed" : "Copy link";

  return (
    <button
      type="button"
      className="copy-link"
      data-state={state}
      aria-label={label}
      title={label}
      onClick={async () => setState((await copyText(url)) ? "copied" : "failed")}
    >
      {state === "copied" ? (
        <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
          <path d="M3 8.5l3 3 7-7" fill="none" stroke="currentColor" strokeWidth="1.8"
            strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      ) : (
        <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
          <rect x="5.5" y="5.5" width="8" height="8" rx="1.5" fill="none"
            stroke="currentColor" strokeWidth="1.5" />
          <path d="M10.5 3.5v-.5A1.5 1.5 0 0 0 9 1.5H4A1.5 1.5 0 0 0 2.5 3v5A1.5 1.5 0 0 0 4 9.5h.5"
            fill="none" stroke="currentColor" strokeWidth="1.5" />
        </svg>
      )}
      <span className="copy-link-text" aria-live="polite">
        {state === "copied" ? "Copied" : state === "failed" ? "Failed" : ""}
      </span>
    </button>
  );
}
