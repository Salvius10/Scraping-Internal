import { useState } from "react";
import { api, type EnrichmentPreview, type FundingQuery } from "./api";

const ROLES = "co-founders, CEO, CTO, COO, VP Engineering, Head of Engineering and Head of Product";

const companies = (n: number) => `${n} ${n === 1 ? "company" : "companies"}`;

/** "Send data for enrichment", confirmed: what would be sent from the view,
 *  what it may cost in SignalHire credits, and what is skipped. */
export default function EnrichPanel({ preview, filters, onSent, onOpen, onClose }: {
  preview: EnrichmentPreview;
  filters: FundingQuery;
  onSent: (message: string) => void;          // sent: show the Enrichment section
  onOpen: () => void;                          // nothing new to send: show it anyway
  onClose: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const send = async () => {
    if (busy) return;
    setBusy(true);
    setMessage(null);
    try {
      const answer = await api.enrich(filters);
      if (answer.sent) onSent(answer.message);
      else setMessage(answer.message);
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "Could not reach the server.");
    } finally {
      setBusy(false);
    }
  };

  const p = preview;
  const short = p.credits_left !== null && p.max_credits > p.credits_left;
  return (
    <section className="sources-panel enrich-panel" aria-labelledby="enrich-title">
      <header className="sources-head">
        <h3 id="enrich-title">Send for enrichment</h3>
        <button className="btn btn--ghost" onClick={onClose}>Cancel</button>
      </header>
      <p className="sub">
        {companies(p.companies)} in this view. SignalHire looks for their {ROLES}, with each
        one's email, phone and LinkedIn.
      </p>

      <ul className="enrich-facts">
        {p.new > 0 && (
          <li>
            <strong>{companies(p.new)}</strong> will be sent: up to <strong>{p.max_credits} SignalHire
            credits</strong>, at most {p.per_company} people per company. Only people found are charged.
            {p.credits_left !== null && <> {p.credits_left} credits left.</>}
          </li>
        )}
        {p.enriched > 0 && <li>{companies(p.enriched)} enriched before: not sent or charged again.</li>}
        {p.waiting > 0 && <li>{companies(p.waiting)} already waiting to be enriched.</li>}
        {p.over_cap > 0 && (
          <li>{companies(p.over_cap)} left for a later send: at most {p.new} go at a time.</li>
        )}
      </ul>

      {!p.ready && (
        <p className="sub warn">SIGNALHIRE_API_KEY is not set on the server, so nothing can be
          sent yet.</p>
      )}
      {p.ready && short && (
        <p className="sub warn">That may be more than the credits left. Enrichment stops when they
          run out, and resumes after a top-up.</p>
      )}
      {message && <p className="sub warn" role="status">{message}</p>}

      <div className="enrich-actions">
        {p.new > 0 || p.waiting > 0 ? (
          <button className="btn btn--primary" onClick={() => void send()}
                  disabled={busy || !p.ready} aria-busy={busy}>
            {busy ? "Sending" : p.new > 0 ? `Send ${companies(p.new)}` : "Resume enrichment"}
          </button>
        ) : (
          <button className="btn btn--primary" onClick={onOpen}>Open Enrichment</button>
        )}
        <button className="btn btn--ghost" onClick={onClose}>Cancel</button>
      </div>
    </section>
  );
}
