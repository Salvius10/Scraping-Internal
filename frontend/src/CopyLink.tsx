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

// Phosphor Icons (MIT), regular weight: "copy" and "check".
const COPY =
  "M216,32H88a8,8,0,0,0-8,8V80H40a8,8,0,0,0-8,8V216a8,8,0,0,0,8,8H168a8,8,0,0,0,8-8V176h40a8,8,0,0,0,8-8V40A8,8,0,0,0,216,32ZM160,208H48V96H160Zm48-48H176V88a8,8,0,0,0-8-8H96V48H208Z";
const CHECK =
  "M229.66,77.66l-128,128a8,8,0,0,1-11.32,0l-56-56a8,8,0,0,1,11.32-11.32L96,188.69,218.34,66.34a8,8,0,0,1,11.32,11.32Z";

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
      <svg viewBox="0 0 256 256" width="14" height="14" fill="currentColor" aria-hidden="true">
        <path d={state === "copied" ? CHECK : COPY} />
      </svg>
      <span className="copy-link-text" aria-live="polite">
        {state === "copied" ? "Copied" : state === "failed" ? "Failed" : ""}
      </span>
    </button>
  );
}
