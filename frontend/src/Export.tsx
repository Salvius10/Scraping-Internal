import { useCallback, useEffect, useState } from "react";
import CopyLink from "./CopyLink";
import DownloadIcon from "./DownloadIcon";
import {
  api, parseTime, type ExportResponse, type ExportSelection, type RoundOrigin, type StageKey,
} from "./api";
import { approxFmt, publishedFmt } from "./dates";
import { useRefreshSync } from "./useRefreshSync";

const FOUND_VIA: Record<RoundOrigin, string> = {
  feed: "News feed",
  web: "Web search",
  pasted: "Your website",
  linkedin: "LinkedIn",
};

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

/** The selections that fill the list: what was added, and taking it back. */
function Selections({
  selections, onRemove, confirming,
}: {
  selections: ExportSelection[];
  onRemove: (s: ExportSelection) => void;
  confirming: number | null;
}) {
  return (
    <nav className="ins-side" aria-label="In this export">
      <h2>In this export</h2>
      {selections.length === 0 && (
        <p className="ex-side-empty">Nothing added yet.</p>
      )}
      {selections.map((s) => (
        <div key={s.id} className="ex-sel">
          <span className="ex-sel-label">{s.label}</span>
          <span className="ex-sel-meta">
            {s.dates} · {plural(s.rounds, "round", "rounds")}
          </span>
          <button type="button" className="ev-remove ex-sel-remove" onClick={() => onRemove(s)}>
            {confirming === s.id ? "Click again to remove" : "Remove"}
          </button>
        </div>
      ))}
    </nav>
  );
}

export default function Export() {
  const [data, setData] = useState<ExportResponse | null>(null);
  const [tab, setTab] = useState<StageKey | "all">("all");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<number | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await api.exportList());
    } catch {
      setError("Could not reach the server.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  // Selections are filters, so new rounds from the scheduled refresh join the
  // list on their own; follow it.
  useRefreshSync(() => {
    void load();
    setNote("Updated with the latest rounds");
  });

  const remove = async (s: ExportSelection) => {
    if (confirming !== s.id) { setConfirming(s.id); return; }
    setConfirming(null);
    try {
      await api.removeExportSelection(s.id);
      setNote(`Removed ${s.label} from Export.`);
      void load();
    } catch {
      setNote("Could not remove that.");
    }
  };

  const clearAll = async () => {
    if (!confirmClear) { setConfirmClear(true); return; }
    setConfirmClear(false);
    try {
      await api.clearExport();
      setNote("Export is empty.");
      void load();
    } catch {
      setNote("Could not empty Export.");
    }
  };

  const selections = data?.selections ?? [];
  const all = data?.rounds ?? [];
  const stageLabel = data?.stages.find((s) => s.key === tab)?.label;
  const rows = tab === "all" ? all : all.filter((r) => r.stage === stageLabel);
  const empty = data !== null && selections.length === 0;

  return (
    <div className="ins" id="main">
      <Selections selections={selections} onRemove={(s) => void remove(s)} confirming={confirming} />

      <section className="ins-main" aria-label="Export">
        <header className="ins-head">
          <div>
            <h2>Export</h2>
            <p>
              Funding rounds added from Startup firms and LinkedIn, in one list with duplicates
              removed. It stays current: new rounds that fit what you added appear here on
              their own.
            </p>
          </div>
          <div className="ins-actions">
            {!empty && (
              <button
                type="button"
                className="ins-download ins-download--all"
                onClick={() => void clearAll()}
                onBlur={() => setConfirmClear(false)}
              >
                {confirmClear ? "Click again to empty" : "Clear all"}
              </button>
            )}
            {data && data.total > 0 ? (
              <a className="ins-download" href={api.exportExcelUrl} download>
                <DownloadIcon />
                Download Excel ({plural(data.total, "round", "rounds")})
              </a>
            ) : (
              <button type="button" className="ins-download" disabled>
                <DownloadIcon />
                Download Excel
              </button>
            )}
          </div>
        </header>

        {data && !empty && (
          <p className="ex-summary">
            {plural(data.total, "round", "rounds")} from{" "}
            {plural(selections.length, "selection", "selections")}
            {data.duplicates > 0
              ? ` · ${plural(data.duplicates, "duplicate", "duplicates")} removed`
              : " · no duplicates"}
          </p>
        )}

        {data && !empty && (
          <nav className="ins-tabs" role="tablist" aria-label="Funding stage">
            <button role="tab" aria-selected={tab === "all"} onClick={() => setTab("all")}>
              All stages
              <span className="ins-count">{data.total}</span>
            </button>
            {data.stages.map((s) => (
              <button
                key={s.key}
                role="tab"
                aria-selected={tab === s.key}
                onClick={() => setTab(s.key)}
              >
                {s.label}
                <span className="ins-count">{s.count}</span>
              </button>
            ))}
          </nav>
        )}

        {note && (
          <p className="update-note" role="status">
            {note}
            <button onClick={() => setNote(null)}>Dismiss</button>
          </p>
        )}

        {error && <p className="answer-error ins-empty">{error}</p>}

        {!error && loading && !data && (
          <div aria-busy="true" aria-label="Loading the export">
            {Array.from({ length: 5 }, (_, i) => <div key={i} className="skeleton" />)}
          </div>
        )}

        {empty && (
          <div className="ins-soon">
            <h2>Nothing to export yet</h2>
            <p>
              Open <a href="#insights">Insights › Startup firms</a> or{" "}
              <a href="#linkedin">LinkedIn</a>, choose a stage, account or dates, and click
              “Add to Export”. Each addition is merged into one list here, with duplicates
              removed, ready to download as Excel.
            </p>
          </div>
        )}

        {data && !empty && rows.length === 0 && (
          <p className="side-note ins-empty">
            No {stageLabel ?? ""} rounds in what you added.
          </p>
        )}

        {rows.length > 0 && (
          <div className="ins-table-wrap">
            <table className="ins-table">
              <thead>
                <tr>
                  <th>Company</th>
                  <th>Round</th>
                  <th>Investors</th>
                  <th>Amount</th>
                  <th>Published</th>
                  <th>Source</th>
                  <th>Found via</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id}>
                    <td className="ins-company">
                      <a href={r.url} target="_blank" rel="noreferrer noopener" title={r.headline}>
                        {r.company ?? "Unnamed company"}
                      </a>
                    </td>
                    <td>
                      {r.round ?? r.stage}
                      {tab === "all" && r.round && r.round !== r.stage && (
                        <span className="ex-stage">{r.stage}</span>
                      )}
                    </td>
                    <td className="ins-investors">
                      {r.investors.length ? r.investors.join(", ") : <span className="ins-na">Not named</span>}
                    </td>
                    <td className="ins-amount">
                      {r.amount ?? <span className="ins-na">Undisclosed</span>}
                    </td>
                    <td className="ins-time">
                      {!r.published_at
                        ? <span className="ins-na">Not given</span>
                        : r.date_approx
                          ? <span title="The site gave a relative time, like 3 days ago">
                              About {approxFmt.format(parseTime(r.published_at))}
                            </span>
                          : `${publishedFmt.format(parseTime(r.published_at))} IST`}
                    </td>
                    <td className="ins-source">
                      {r.source_label}
                      <CopyLink url={r.url} />
                    </td>
                    <td className="ex-via">
                      {[r.origin, ...r.also_via].map((o) => (
                        <span key={o} className="ex-via-tag" data-origin={o}>{FOUND_VIA[o]}</span>
                      ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
