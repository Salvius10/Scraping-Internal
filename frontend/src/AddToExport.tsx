import { useState } from "react";
import { api, type AddToExport, type AddToExportResponse } from "./api";

// Phosphor Icons (MIT), regular weight: "plus".
const PLUS =
  "M224,128a8,8,0,0,1-8,8H136v80a8,8,0,0,1-16,0V136H40a8,8,0,0,1,0-16h80V40a8,8,0,0,1,16,0v80h80A8,8,0,0,1,224,128Z";

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

function addedText(r: AddToExportResponse): string {
  const repeats = r.duplicates
    ? `${r.duplicates} ${r.duplicates === 1 ? "was" : "were"} already there and merged`
    : "no duplicates";
  return `Added ${r.label} to Export: ${plural(r.rounds, "round", "rounds")}, `
    + `${r.new} new, ${repeats}. Export now holds ${plural(r.total, "round", "rounds")}; `
    + "download it from the Export tab.";
}

/** Saves what the page is showing as a selection on the Export page. The
    selection is a filter, not a copy, so it stays current as rounds arrive. */
export default function AddToExportButton({
  selection, label, quiet = false, disabled = false, onMessage,
}: {
  selection: AddToExport;
  label: string;
  quiet?: boolean;
  disabled?: boolean;
  onMessage: (text: string) => void;
}) {
  const [busy, setBusy] = useState(false);

  const add = async () => {
    if (busy || disabled) return;
    setBusy(true);
    try {
      const r = await api.addToExport(selection);
      onMessage(r.added ? addedText(r) : (r.error ?? "Could not add that to Export."));
    } catch {
      onMessage("Could not reach the server.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <button
      type="button"
      className={`ins-download${quiet ? " ins-download--all" : ""}`}
      disabled={busy || disabled}
      onClick={() => void add()}
    >
      <svg viewBox="0 0 256 256" width="15" height="15" fill="currentColor" aria-hidden="true">
        <path d={PLUS} />
      </svg>
      {busy ? "Adding" : label}
    </button>
  );
}
