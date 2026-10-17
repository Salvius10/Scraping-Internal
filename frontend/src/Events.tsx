import { useCallback, useEffect, useState } from "react";
import { api, safeHref, type EventsPage, type EventsQuery, type When } from "./api";
import { DateInputs, ExportLink, SearchBox, Skeleton, Sources } from "./controls";
import { eventWhen } from "./dates";

const WHEN: { key: When; label: string }[] = [
  { key: "upcoming", label: "Upcoming" },
  { key: "past", label: "Past" },
  { key: "all", label: "All" },
];

/** India startup events feed: upcoming soonest first, past most recent first. */
export default function Events({ reloadKey }: { reloadKey: number }) {
  const [when, setWhen] = useState<When>("upcoming");
  const [city, setCity] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [q, setQ] = useState("");
  const [data, setData] = useState<EventsPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const badRange = Boolean(start && end && start > end);
  const filters: EventsQuery = {
    when, city: city || undefined, start: start || undefined, end: end || undefined,
    q: q || undefined,
  };
  const key = JSON.stringify(filters);

  const load = useCallback(async (f: EventsQuery) => {
    setLoading(true);
    setError(null);
    try {
      setData(await api.events(f));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not reach the server.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!badRange) void load(JSON.parse(key) as EventsQuery);
  }, [key, badRange, load, reloadKey]);

  const counts: Record<When, number | undefined> = {
    upcoming: data?.upcoming, past: data?.past,
    all: data ? data.upcoming + data.past : undefined,
  };

  return (
    <section className="view" aria-labelledby="events-title">
      <header className="view-head">
        <div>
          <h2 id="events-title">Startup events in India</h2>
          <p className="muted">
            Founder meetups, pitch and demo days, conferences and programmes, from Luma and
            Eventbrite. The same event listed twice is shown once.
          </p>
        </div>
        <ExportLink href={api.eventsExcel(filters)} disabled={badRange || !data?.total} />
      </header>

      <nav className="tabs" role="tablist" aria-label="When">
        {WHEN.map((w) => (
          <button key={w.key} role="tab" aria-selected={when === w.key}
                  onClick={() => setWhen(w.key)}>
            {w.label} {counts[w.key] !== undefined && <span className="count">{counts[w.key]}</span>}
          </button>
        ))}
      </nav>

      <div className="filters">
        <div className="presets" role="group" aria-label="City">
          <button aria-pressed={city === ""} onClick={() => setCity("")}>All cities</button>
          {(data?.cities ?? []).map((c) => (
            <button key={c.name} aria-pressed={city === c.name} onClick={() => setCity(c.name)}>
              {c.name} <span className="count">{c.count}</span>
            </button>
          ))}
        </div>
        <DateInputs start={start} end={end} onStart={setStart} onEnd={setEnd}
                    label="Starting between" />
        <SearchBox value={q} onChange={setQ} placeholder="Search event, organiser, venue" />
      </div>

      {badRange && <p className="error">The From date is after the To date.</p>}
      {error && <p className="error">{error}</p>}
      {!error && loading && !data && <Skeleton />}
      {!error && !badRange && data && data.rows.length === 0 && (
        <p className="empty">
          No {when === "all" ? "" : when} events{city ? ` in ${city}` : ""}{q ? ` matching "${q}"` : ""}.
          New events appear after each refresh.
        </p>
      )}

      {!badRange && data && data.rows.length > 0 && (
        <div className="table-wrap" aria-busy={loading}>
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Event</th>
                <th>Where</th>
                <th>Organiser</th>
                <th>Price</th>
                <th>Source</th>
              </tr>
            </thead>
            <tbody>
              {data.rows.map((e) => (
                <tr key={e.id}>
                  <td className="when">{eventWhen(e.starts_at, e.ends_at, e.date_only)}</td>
                  <td className="lead">
                    <a href={safeHref(e.url)} target="_blank" rel="noreferrer noopener">{e.title}</a>
                    {e.summary && <p className="sub">{e.summary}</p>}
                    {e.category && <p className="tags"><span className="tag">{e.category}</span></p>}
                  </td>
                  <td>
                    {e.online ? "Online" : (e.city ?? <span className="muted">Not given</span>)}
                    {e.venue && <p className="sub">{e.venue}</p>}
                  </td>
                  <td>{e.organiser ?? <span className="muted">Not given</span>}</td>
                  <td>{e.price ?? <span className="muted">Not given</span>}</td>
                  <td><Sources url={e.source_url} label={e.source_label} type={e.source_type}
                               others={e.sources} /></td>
                </tr>
              ))}
            </tbody>
          </table>
          {data.total > data.rows.length && (
            <p className="more muted">
              Showing {data.rows.length} of {data.total}. Export to Excel for all of them.
            </p>
          )}
        </div>
      )}
    </section>
  );
}
