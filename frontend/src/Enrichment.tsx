import { useCallback, useEffect, useRef, useState } from "react";
import {
  api, mailHref, parseTime, safeHref, telHref, type CompanyStatus, type ContactRow,
  type ContactsPage, type ContactsQuery, type DesignationKey, type EnrichedCompany,
  type EnrichmentStatus,
} from "./api";
import { ExportLink, SearchBox, Skeleton } from "./controls";
import { timeFmt } from "./dates";

// How often a running enrichment is checked on. A database read: free.
const POLL_MS = 3000;

const STATUS_LABEL: Record<CompanyStatus, string> = {
  queued: "Waiting",
  working: "Enriching",
  done: "Enriched",
  no_match: "No leaders found",
  failed: "Failed",
};

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** Where the job is, and what can be done about a stopped or failed one. */
function Progress({ status, busy, onResume, onRetry }: {
  status: EnrichmentStatus;
  busy: boolean;
  onResume: () => void;
  onRetry: () => void;
}) {
  const s = status;
  const finished = s.done + s.no_match + s.failed;
  const percent = s.companies ? Math.round((finished / s.companies) * 100) : 0;
  return (
    <div className="enrich-bar" aria-live="polite">
      {!s.ready && (
        <p className="sub warn">SIGNALHIRE_API_KEY is not set on the server, so nothing can be
          enriched yet.</p>
      )}
      {s.running && (
        <div className="enrich-progress">
          <p className="sub">
            <strong>Enriching{s.current ? ` ${s.current}` : ""}</strong>: {finished} of{" "}
            {plural(s.companies, "company", "companies")} done. Leaders appear below as each
            one finishes.
          </p>
          <div className="progress" role="progressbar" aria-valuemin={0} aria-valuemax={100}
               aria-valuenow={percent} aria-label="Companies enriched">
            <span style={{ width: `${percent}%` }} />
          </div>
        </div>
      )}
      {!s.running && s.error && <p className="sub warn">Stopped: {s.error}</p>}
      {!s.running && (s.waiting > 0 || s.failed > 0) && (
        <div className="enrich-actions">
          {s.waiting > 0 && (
            <button className="btn btn--primary" onClick={onResume} disabled={busy || !s.ready}>
              Resume: {plural(s.waiting, "company", "companies")} waiting
            </button>
          )}
          {s.failed > 0 && (
            <button className="btn btn--secondary" onClick={onRetry} disabled={busy || !s.ready}>
              Retry {plural(s.failed, "failed company", "failed companies")}
            </button>
          )}
        </div>
      )}
      {s.companies > 0 && (
        <p className="sub">
          {plural(s.companies, "company", "companies")} sent · {s.done} enriched
          {s.no_match > 0 && <> · {s.no_match} with no leaders found</>}
        </p>
      )}
    </div>
  );
}

/** Every company sent, and how its enrichment went. Loaded when opened. */
function Companies({ count, progress }: { count: number; progress: number }) {
  const [open, setOpen] = useState(false);
  const [rows, setRows] = useState<EnrichedCompany[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    let stale = false;
    api.enrichmentCompanies()
      .then((r) => { if (!stale) { setRows(r); setError(null); } })
      .catch((e) => { if (!stale) setError(e instanceof Error ? e.message : "Could not load."); });
    return () => { stale = true; };
  }, [open, progress]);

  if (count === 0) return null;
  return (
    <details className="enrich-companies" onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>Companies sent <span className="count">{count}</span></summary>
      {error && <p className="sub warn">{error}</p>}
      {open && !rows && !error && <p className="sub">Loading…</p>}
      {rows && (
        <ul className="source-list">
          {rows.map((c) => (
            <li key={c.id} className="source-row">
              <div className="source-main">
                <strong>{c.company}</strong>
                <span className={`state state--${c.status}`}>{STATUS_LABEL[c.status]}</span>
                {c.stage && <span className="tag tag--plain">{c.stage}</span>}
                {c.note && <p className={`sub ${c.status === "failed" ? "warn" : ""}`}>{c.note}</p>}
              </div>
              <span className="muted source-how">
                {plural(c.people_found, "leader")} · {plural(c.credits_used, "credit")}
              </span>
            </li>
          ))}
        </ul>
      )}
    </details>
  );
}

function Contact({ value, href, others }: {
  value: string | null;
  href: string | undefined;
  others: string[];
}) {
  if (!value) return null;
  return (
    <div className="contact">
      {href ? <a href={href}>{value}</a> : value}
      {others.length > 0 && (
        <details className="also">
          <summary>+{others.length} more</summary>
          <ul>{others.map((o) => <li key={o}>{o}</li>)}</ul>
        </details>
      )}
    </div>
  );
}

function missing(row: ContactRow): string {
  return row.lookup === "success" ? "Not found" : "Not available";
}

/** "getkarta.ai", or "kartashop (LinkedIn)" when only the company's page is known. */
function companyLink(row: ContactRow): { href: string; label: string } | null {
  const site = safeHref(row.company_site);
  if (site) return { href: site, label: new URL(site).hostname.replace(/^www\./, "") };
  const page = safeHref(row.company_page);
  if (!page) return null;
  const slug = new URL(page).pathname.replace(/\/+$/, "").split("/").pop();
  return { href: page, label: slug ? `${decodeURIComponent(slug)} (LinkedIn)` : "LinkedIn page" };
}

function ContactTable({ rows }: { rows: ContactRow[] }) {
  return (
    <table>
      <thead>
        <tr>
          <th>Name</th>
          <th>Designation</th>
          <th>Company</th>
          <th>Email</th>
          <th>Phone</th>
          <th>LinkedIn</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => {
          const linkedin = safeHref(r.linkedin);
          const works = companyLink(r);
          return (
            <tr key={r.id}>
              <td className="lead">
                <strong>{r.full_name}</strong>
                {r.title && <p className="sub">{r.title}</p>}
                {r.location && <p className="sub muted">{r.location}</p>}
              </td>
              <td>
                <p className="tags tags--tight">
                  {r.designation.split(", ").map((d) => <span key={d} className="tag">{d}</span>)}
                </p>
              </td>
              <td>
                {r.company}
                {(r.stage || r.sector) && (
                  <p className="sub">{[r.stage, r.sector].filter(Boolean).join(" · ")}</p>
                )}
                {works && (
                  <p className="sub contact">
                    Works at <a href={works.href} target="_blank" rel="noreferrer noopener">
                      {works.label}</a>
                  </p>
                )}
                {r.maybe_other && (
                  <p className="tags">
                    <span className="tag tag--warn"
                          title="SignalHire knows another company by this name, and this person's company page is not the one that matches it. Check before reaching out.">
                      Maybe another company
                    </span>
                  </p>
                )}
              </td>
              <td>
                {r.email ? <Contact value={r.email} href={mailHref(r.email)} others={r.other_emails} />
                  : <span className="muted">{missing(r)}</span>}
              </td>
              <td className="when">
                {r.phone ? <Contact value={r.phone} href={telHref(r.phone)} others={r.other_phones} />
                  : <span className="muted">{missing(r)}</span>}
              </td>
              <td>
                {linkedin
                  ? <a className="contact-link" href={linkedin} target="_blank"
                       rel="noreferrer noopener">Profile</a>
                  : <span className="muted">{missing(r)}</span>}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/** Enrichment: the leaders of the funded startups sent here, with their contacts. */
export default function Enrichment({ reloadKey, onChanged, onMessage }: {
  reloadKey: number;
  onChanged: () => void;                      // a job finished: refresh the counts
  onMessage: (text: string) => void;
}) {
  const [designation, setDesignation] = useState<DesignationKey | "">("");
  const [q, setQ] = useState("");
  const [data, setData] = useState<ContactsPage | null>(null);
  const [status, setStatus] = useState<EnrichmentStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const [progress, setProgress] = useState(0);     // bumps when the job finished more
  const [pollKey, setPollKey] = useState(0);       // bumps when a job is (re)started here
  const callbacks = useRef({ onChanged, onMessage });
  callbacks.current = { onChanged, onMessage };

  const filters: ContactsQuery = { designation: designation || undefined, q: q || undefined };
  const key = JSON.stringify(filters);

  const load = useCallback(async (f: ContactsQuery) => {
    setLoading(true);
    setError(null);
    try {
      setData(await api.enrichment(f));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not reach the server.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(JSON.parse(key) as ContactsQuery);
  }, [key, load, reloadKey, progress]);

  // Follow the job: every few seconds while it runs, then once more when it ends.
  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    let wasRunning = false;
    let mark: string | null = null;
    const check = async () => {
      let next: EnrichmentStatus | null = null;
      try {
        next = await api.enrichmentStatus();
      } catch {
        /* tried again below while a job is running */
      }
      if (stopped) return;
      if (next) {
        setStatus(next);
        const now = `${next.contacts}/${next.done}/${next.no_match}/${next.failed}/${next.waiting}`;
        if (mark !== null && now !== mark) setProgress((n) => n + 1);
        mark = now;
        if (wasRunning && !next.running) {
          callbacks.current.onChanged();
          if (next.message) callbacks.current.onMessage(next.message);
        }
        wasRunning = next.running;
      }
      if (next ? next.running : wasRunning) timer = window.setTimeout(check, POLL_MS);
    };
    void check();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [pollKey, reloadKey]);

  const run = async (call: () => ReturnType<typeof api.resumeEnrichment>) => {
    if (busy) return;
    setBusy(true);
    try {
      const answer = await call();
      setStatus(answer.status);
      callbacks.current.onMessage(answer.message);
      setPollKey((n) => n + 1);
    } catch (e) {
      callbacks.current.onMessage(e instanceof Error ? e.message : "Could not reach the server.");
    } finally {
      setBusy(false);
    }
  };

  const clearList = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const answer = await api.clearEnrichment();
      setStatus(answer.status);
      callbacks.current.onMessage(answer.message);
      if (answer.cleared) {
        setConfirmClear(false);
        setProgress((n) => n + 1);                 // reload the list, now empty
        callbacks.current.onChanged();             // and the count in the masthead
      }
    } catch (e) {
      callbacks.current.onMessage(e instanceof Error ? e.message : "Could not reach the server.");
    } finally {
      setBusy(false);
    }
  };

  const nothingYet = status !== null && status.companies === 0;
  const canClear = Boolean(status && status.companies > 0 && !status.running);

  return (
    <section className="view" aria-labelledby="enrichment-title">
      <header className="view-head">
        <div>
          <h2 id="enrichment-title">Enrichment</h2>
          <p className="muted">
            Leaders of the funded startups you send here: co-founders, CEO, CTO, COO, VP
            Engineering, Head of Engineering and Head of Product, with the email, phone and
            LinkedIn SignalHire has for each.
          </p>
        </div>
        <div className="head-actions">
          <button className="btn btn--ghost" aria-expanded={confirmClear}
                  aria-controls="clear-confirm" disabled={!canClear || busy}
                  title={status?.running ? "Clear the list once enrichment is done" : undefined}
                  onClick={() => setConfirmClear(!confirmClear)}>
            Clear list
          </button>
          {/* The file lists every company sent, so it is useful before any leader is found. */}
          <ExportLink href={api.enrichmentExcel(filters)}
                      disabled={!data?.total && !status?.companies} />
        </div>
      </header>

      {confirmClear && status && canClear && (
        <div id="clear-confirm" className="sources-panel clear-confirm" role="group"
             aria-label="Clear the list">
          <p className="sub">
            Clear all {plural(status.companies, "company", "companies")} and{" "}
            {plural(status.contacts, "leader")} from this list? The contacts already found are
            kept out of sight, so sending one of these companies again costs no credits for them.
          </p>
          <div className="enrich-actions">
            <button className="btn btn--danger" onClick={() => void clearList()} disabled={busy}>
              {busy ? "Clearing" : "Clear list"}
            </button>
            <button className="btn btn--ghost" onClick={() => setConfirmClear(false)}>Keep</button>
          </div>
        </div>
      )}

      {status && (
        <Progress status={status} busy={busy}
                  onResume={() => void run(api.resumeEnrichment)}
                  onRetry={() => void run(api.retryEnrichment)} />
      )}
      {status && <Companies count={status.companies} progress={progress} />}

      <div className="filters">
        <SearchBox value={q} onChange={setQ} placeholder="Search name, company, email" />
      </div>

      <nav className="tabs" role="tablist" aria-label="Designation">
        <button role="tab" aria-selected={designation === ""} onClick={() => setDesignation("")}>
          All designations <span className="count">{data?.all_designations ?? 0}</span>
        </button>
        {(data?.designations ?? []).map((d) => (
          <button key={d.key} role="tab" aria-selected={designation === d.key}
                  onClick={() => setDesignation(d.key)}>
            {d.label} <span className="count">{d.count}</span>
          </button>
        ))}
      </nav>

      {error && <p className="error">{error}</p>}
      {!error && loading && !data && <Skeleton />}
      {!error && data && status && data.rows.length === 0 && (
        <p className="empty">
          {nothingYet
            ? <>Nothing sent yet. In Funded startups, filter to the companies you want, then
                press <strong>Send data for enrichment</strong>.</>
            : status?.running && !q && !designation
              ? "Leaders appear here as each company is enriched."
              : `No leaders found${designation ? " with that designation" : ""}${q ? ` matching "${q}"` : ""}.`}
        </p>
      )}

      {data && data.rows.length > 0 && (
        <div className="table-wrap" aria-busy={loading}>
          <ContactTable rows={data.rows} />
          {data.total > data.rows.length && (
            <p className="more muted">
              Showing {data.rows.length} of {data.total}. Export to Excel for all of them.
            </p>
          )}
          {status?.finished_at && !status.running && (
            <p className="more muted">
              Last enrichment finished {timeFmt.format(parseTime(status.finished_at))} IST.
            </p>
          )}
        </div>
      )}
    </section>
  );
}
