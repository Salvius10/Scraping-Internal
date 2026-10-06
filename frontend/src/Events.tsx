import { useCallback, useEffect, useState } from "react";
import CopyLink from "./CopyLink";
import DownloadIcon from "./DownloadIcon";
import RefreshButton from "./RefreshButton";
import {
  api, parseTime, type AddEventSourceResponse, type EventItem, type EventSourceInfo,
  type EventsResponse, type EventWhen,
} from "./api";
import { approxFmt, publishedFmt, showDay } from "./dates";
import { useRefreshSync } from "./useRefreshSync";

const timeFmt = new Intl.DateTimeFormat("en-IN", {
  hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Kolkata",
});

const TABS: { key: EventWhen; label: string }[] = [
  { key: "upcoming", label: "Upcoming" },
  { key: "past", label: "Past" },
];

// A Luma calendar Apify can read. One with no short name (/calendar/cal-...)
// cannot, so the server reads it as a page, like any other website.
const isLuma = (url: string) =>
  /^(https?:\/\/)?(www\.)?(lu\.ma|luma\.com)(\/|$)/i.test(url.trim())
  && !/^(https?:\/\/)?(www\.)?(lu\.ma|luma\.com)\/calendar\//i.test(url.trim());

function readAgo(iso: string | null): string {
  if (!iso) return "not read yet";
  const hours = (Date.now() - parseTime(iso).getTime()) / 3_600_000;
  if (hours < 1) return "read just now";
  if (hours < 48) return `read ${Math.round(hours)}h ago`;
  return `read ${Math.round(hours / 24)} days ago`;
}

/** Start time in IST; a day only when the source gave no time. */
function When({ event }: { event: EventItem }) {
  const start = parseTime(event.starts_at);
  if (event.date_only) {
    return <span title="The source gave the day, not the time">{approxFmt.format(start)}</span>;
  }
  const end = event.ends_at ? parseTime(event.ends_at) : null;
  const sameDay = end !== null && approxFmt.format(end) === approxFmt.format(start);
  return <>{publishedFmt.format(start)}{end && sameDay ? `–${timeFmt.format(end)}` : ""} IST</>;
}

function Where({ event }: { event: EventItem }) {
  if (event.online) return <>Online</>;
  if (!event.city && !event.venue) return <span className="ins-na">Not given</span>;
  return (
    <>
      {event.city ?? event.venue}
      {event.city && event.venue && <span className="vc-company">{event.venue}</span>}
    </>
  );
}

function costText(r: AddEventSourceResponse): string {
  return r.via === "luma"
    ? `about $${r.apify_usd.toFixed(4)} of Apify`
    : `${r.credits_used} Firecrawl credit${r.credits_used === 1 ? "" : "s"} and $${r.llm_usd.toFixed(6)}`;
}

/** Paste a website or a Luma calendar; it is saved and read straight away. */
function AddSource({
  data, defaultFirm, onAdded,
}: {
  data: EventsResponse | null;
  defaultFirm: string;
  onAdded: (message: string) => void;
}) {
  const [url, setUrl] = useState("");
  const [label, setLabel] = useState("");
  const [firm, setFirm] = useState(defaultFirm);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const luma = isLuma(url);
  const unready = data && (luma ? !data.apify_ready : !data.firecrawl_ready)
    ? luma
      ? "Luma calendars are read through Apify, which is not set up: add APIFY_API_TOKEN to .env and restart the server."
      : "Websites are read through Firecrawl, which is not set up: add FIRECRAWL_API_KEY to .env and restart the server."
    : null;

  const submit = async () => {
    if (busy || !url.trim() || unready) return;
    setBusy(true);
    setProblem(null);
    try {
      const r = await api.addEventSource(url.trim(), label.trim(), firm);
      if (!r.added) {
        setProblem(r.error ?? "Could not add that link.");
      } else {
        setUrl(""); setLabel(""); setFirm("");
        onAdded(r.error
          ? `Added. The first read failed: ${r.error} It is tried again on the next refresh.`
          : `Added. Found ${r.found} ${r.found === 1 ? "event" : "events"} (${r.new} new), for ${costText(r)}.`);
      }
    } catch {
      setProblem("Could not reach the server.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="ev-add" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
      <h3>Add a website</h3>
      <div className="ev-add-row">
        <input
          className="side-input ev-url"
          type="url"
          inputMode="url"
          value={url}
          placeholder="https://luma.com/your-calendar or a firm's events page"
          aria-label="Website or Luma calendar link"
          autoFocus
          disabled={busy}
          onChange={(e) => { setUrl(e.target.value); setProblem(null); }}
        />
        <input
          className="side-input ev-label"
          value={label}
          placeholder="Name (optional)"
          aria-label="Name for this source"
          maxLength={200}
          disabled={busy}
          onChange={(e) => setLabel(e.target.value)}
        />
        <label className="ins-date vc-select">
          Firm
          <select value={firm} disabled={busy} onChange={(e) => setFirm(e.target.value)}>
            <option value="">Not a tracked firm</option>
            {(data?.firms ?? []).map((f) => <option key={f.key} value={f.key}>{f.label}</option>)}
          </select>
        </label>
        <button type="submit" className="ins-web ev-go" disabled={busy || !url.trim() || Boolean(unready)}>
          {busy ? "Adding and reading" : "Add and read"}
        </button>
      </div>
      <p className="side-note">
        {luma
          ? "A Luma calendar: read through Apify, about $0.002 per event. Its past events are read once, then upcoming ones once a day."
          : "Any other page: Firecrawl reads it (1 credit), then gpt-oss lists the events on it (about $0.001). Read again once a day, and only re-read by the model when the page changes."}
      </p>
      {busy && (
        <div className="fc-scraping" aria-live="polite">
          <span className="pulse" />
          Reading this source now. A Luma calendar can take up to a minute.
        </div>
      )}
      {unready && url.trim() && <p className="answer-error">{unready}</p>}
      {problem && <p className="answer-error">{problem}</p>}
    </form>
  );
}

function SourceNote({
  source, confirming, onRemove,
}: {
  source: EventSourceInfo;
  confirming: boolean;
  onRemove: () => void;
}) {
  return (
    <p className="vc-firm-note">
      <a href={source.url} target="_blank" rel="noreferrer noopener">{source.label}</a>
      {" · "}{source.via_label}{source.site ? ", checked weekly" : ""}
      {source.india_only ? ", India events only" : ""}
      <span className={source.last_error ? "vc-error" : undefined}>
        {" · "}{readAgo(source.last_read)}
        {source.last_error && ` (failed: ${source.last_error})`}
      </span>
      {" · "}{source.events} {source.events === 1 ? "event" : "events"}
      {source.custom && (
        <button type="button" className="ev-remove" onClick={onRemove}>
          {confirming ? "Click again to remove" : "Remove"}
        </button>
      )}
    </p>
  );
}

export default function Events() {
  const [when, setWhen] = useState<EventWhen>("upcoming");
  const [organiser, setOrganiser] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [data, setData] = useState<EventsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [addFirm, setAddFirm] = useState("");
  const [confirming, setConfirming] = useState<number | null>(null);

  /** Open the add form, with a firm already chosen when asked from its row. */
  const openAdd = (firm = "") => {
    setAddFirm(firm);
    setAdding(true);
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const badRange = Boolean(start && end && start > end);
  const query = {
    when, organiser: organiser || undefined, start: start || undefined, end: end || undefined,
  };

  const load = useCallback(async (q: typeof query) => {
    setLoading(true);
    setError(null);
    try {
      setData(await api.events(q));
    } catch {
      setError("Could not reach the server.");
    } finally {
      setLoading(false);
    }
  }, []);

  // Any change of tab, organiser or dates reloads straight away.
  useEffect(() => {
    if (!badRange) void load(query);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load, when, organiser, start, end, badRange]);

  // Event sources are read on each scheduled refresh; follow it.
  useRefreshSync(() => {
    if (!badRange) void load(query);
    setNote("Updated with the latest events");
  });

  const remove = async (source: EventSourceInfo) => {
    if (source.id === null) return;
    if (confirming !== source.id) { setConfirming(source.id); return; }
    setConfirming(null);
    try {
      await api.removeEventSource(source.id);
      if (organiser === source.key) setOrganiser("");
      setNote(`Removed ${source.label} and its events.`);
      void load(query);
    } catch {
      setNote("Could not remove that source.");
    }
  };

  const rows = data?.events ?? [];
  const sources = data?.sources ?? [];
  const chosen = data?.organisers.find((o) => o.key === organiser);
  const firms = data?.firms ?? [];
  const sourceless = firms.filter((f) => f.sources === 0);
  const chosenFirm = firms.find((f) => f.key === organiser);
  const failing = sources.filter((s) => s.last_error).length;
  const pasted = sources.filter((s) => s.custom).length;
  const rangeText = start || end
    ? ` starting from ${start ? showDay(start) : "the beginning"} to ${end ? showDay(end) : "any date"}`
    : "";

  return (
    <section className="ins-main" aria-label="Events organised">
      <header className="ins-head">
        <div>
          <h2>Events organised</h2>
          <p>
            Demo days, open houses, meetups and summits the VC firms run, from every firm&apos;s
            website, their Luma calendars and the pages you add.
          </p>
        </div>
        <div className="ins-actions">
          <button
            type="button"
            className="ins-download ins-download--all"
            aria-pressed={adding}
            onClick={() => (adding ? setAdding(false) : openAdd())}
          >
            {adding ? "Close" : "Add a website"}
          </button>
          <RefreshButton
            scope="events"
            onDone={() => { if (!badRange) void load(query); }}
            onMessage={setNote}
          />
          <a className="ins-download" href={api.eventsExcelUrl(query)} download>
            <DownloadIcon />
            Excel: {when === "upcoming" ? "upcoming" : "past"}
          </a>
        </div>
      </header>

      {adding && (
        <AddSource
          key={addFirm}
          data={data}
          defaultFirm={addFirm}
          onAdded={(message) => { setNote(message); void load(query); }}
        />
      )}

      {data && !data.apify_ready && (
        <p className="side-note ins-pending">
          The firms&apos; Luma calendars are not being read yet: add APIFY_API_TOKEN to .env
          and restart the server.
        </p>
      )}

      <div className="ins-filter" role="group" aria-label="Filter events">
        <label className="ins-date vc-select">
          Organiser
          <select value={organiser} onChange={(e) => { setOrganiser(e.target.value); setNote(null); }}>
            <option value="">All organisers</option>
            {(data?.organisers ?? []).map((o) => {
              const firm = firms.find((f) => f.key === o.key);
              return (
                <option key={o.key} value={o.key}>
                  {o.label} ({o.count}){firm && firm.sources === 0 ? " · no source yet" : ""}
                </option>
              );
            })}
          </select>
        </label>
        <label className="ins-date">
          From
          <input type="date" value={start} max={end || undefined}
            onChange={(e) => setStart(e.target.value)} />
        </label>
        <label className="ins-date">
          To
          <input type="date" value={end} min={start || undefined}
            onChange={(e) => setEnd(e.target.value)} />
        </label>
        {(start || end) && (
          <button className="ins-clear" onClick={() => { setStart(""); setEnd(""); }}>
            Clear dates
          </button>
        )}
      </div>

      <nav className="ins-tabs" role="tablist" aria-label="Upcoming or past">
        {TABS.map((t) => (
          <button
            key={t.key}
            role="tab"
            aria-selected={when === t.key}
            onClick={() => { setWhen(t.key); setNote(null); }}
          >
            {t.label}
            <span className="ins-count">{data?.counts[t.key] ?? 0}</span>
          </button>
        ))}
      </nav>

      {note && (
        <p className="update-note" role="status">
          {note}
          <button onClick={() => setNote(null)}>Dismiss</button>
        </p>
      )}

      {badRange && <p className="answer-error ins-empty">The From date is after the To date.</p>}
      {error && <p className="answer-error ins-empty">{error}</p>}

      {!error && loading && !data && (
        <div aria-busy="true" aria-label="Loading events">
          {Array.from({ length: 5 }, (_, i) => <div key={i} className="skeleton" />)}
        </div>
      )}

      {!error && !badRange && data && rows.length === 0 && (
        chosenFirm && chosenFirm.sources === 0 ? (
          <p className="side-note ins-empty">
            {`No events source for ${chosenFirm.label} yet`}
            {chosenFirm.no_events ? `: ${chosenFirm.no_events}.` : "."}
            <button type="button" className="ev-add-for" onClick={() => openAdd(chosenFirm.key)}>
              Add one
            </button>
          </p>
        ) : (
          <p className="side-note ins-empty">
            {`No ${when} events from ${chosen?.label ?? "these organisers"}${rangeText}.`}
            {sources.some((s) => !s.last_read) && " Sources not read yet are read on the next refresh, or press Refresh now."}
          </p>
        )
      )}

      {rows.length > 0 && !badRange && (
        <div className="ins-table-wrap">
          <table className="ins-table">
            <thead>
              <tr>
                <th>When</th>
                <th>Event</th>
                {!organiser && <th>Organiser</th>}
                <th>Where</th>
                <th>Found via</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((e) => (
                <tr key={e.id}>
                  <td className="ins-time"><When event={e} /></td>
                  <td className="vc-news">
                    <a href={e.url} target="_blank" rel="noreferrer noopener">{e.title}</a>
                    {e.description && <span className="vc-company ev-desc">{e.description}</span>}
                  </td>
                  {!organiser && <td className="vc-firm">{e.organiser}</td>}
                  <td className="ev-where"><Where event={e} /></td>
                  <td className="ins-source">
                    {e.via_label}
                    <CopyLink url={e.url} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {sources.length > 0 && (
        <details className="vc-sources">
          <summary>
            Where events are read from: {sources.length} {sources.length === 1 ? "source" : "sources"}
            {pasted > 0 && ` (${pasted} added by you)`}
            {failing > 0 && `, ${failing} with a failed last read`}
          </summary>
          <ul>
            {sources.map((s) => (
              <li key={s.key}>
                <SourceNote
                  source={s}
                  confirming={confirming === s.id}
                  onRemove={() => void remove(s)}
                />
              </li>
            ))}
          </ul>
        </details>
      )}

      {sourceless.length > 0 && (
        <details className="vc-sources">
          <summary>
            No events source yet: {sourceless.length} of {firms.length} firms
          </summary>
          <p className="side-note ev-sourceless-note">
            These firms have no working website to read. Paste an events page or a Luma
            calendar for any of them with Add one.
          </p>
          <ul>
            {sourceless.map((f) => (
              <li key={f.key}>
                <p className="vc-firm-note">
                  <strong>{f.label}</strong>
                  {f.no_events && <span className="vc-no-site"> · {f.no_events}</span>}
                  <button type="button" className="ev-add-for" onClick={() => openAdd(f.key)}>
                    Add one
                  </button>
                </p>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
