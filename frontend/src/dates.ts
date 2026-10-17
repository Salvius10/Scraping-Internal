// Date helpers. Everything is shown in IST.

import { parseTime } from "./api";

const ZONE = "Asia/Kolkata";

export const timeFmt = new Intl.DateTimeFormat("en-IN", {
  day: "numeric", month: "short", year: "numeric",
  hour: "2-digit", minute: "2-digit", hour12: false, timeZone: ZONE,
});

export const dayFmt = new Intl.DateTimeFormat("en-IN", {
  day: "numeric", month: "short", year: "numeric", timeZone: ZONE,
});

const weekdayFmt = new Intl.DateTimeFormat("en-IN", {
  weekday: "short", day: "numeric", month: "short", timeZone: ZONE,
});

const clockFmt = new Intl.DateTimeFormat("en-IN", {
  hour: "2-digit", minute: "2-digit", hour12: false, timeZone: ZONE,
});

/** Today in India as YYYY-MM-DD, optionally some days back (negative: ahead). */
export function istDay(daysAgo = 0): string {
  const at = new Date(Date.now() - daysAgo * 86_400_000);
  return at.toLocaleDateString("en-CA", { timeZone: ZONE });
}

export const PAST_PRESETS: { label: string; days: number | null }[] = [
  { label: "Last 7 days", days: 7 },
  { label: "Last 30 days", days: 30 },
  { label: "Last 90 days", days: 90 },
  { label: "All time", days: null },
];

const ymdFmt = new Intl.DateTimeFormat("en-IN", {
  day: "numeric", month: "short", year: "numeric", timeZone: "UTC",
});
export const showDay = (ymd: string) => ymdFmt.format(new Date(`${ymd}T00:00:00Z`));

export function published(iso: string | null): string {
  return iso ? `${timeFmt.format(parseTime(iso))} IST` : "Not given";
}

/** "Sat 1 Nov, 18:30-21:00 IST", or the day alone when no time was given. */
export function eventWhen(starts: string, ends: string | null, dateOnly: boolean): string {
  const start = parseTime(starts);
  if (dateOnly) return weekdayFmt.format(start);
  let text = `${weekdayFmt.format(start)}, ${clockFmt.format(start)}`;
  if (ends) {
    const end = parseTime(ends);
    const sameDay = dayFmt.format(start) === dayFmt.format(end);
    text += sameDay ? `-${clockFmt.format(end)}` : ` to ${weekdayFmt.format(end)}, ${clockFmt.format(end)}`;
  }
  return `${text} IST`;
}

export function describeAge(hours: number | null): string {
  if (hours === null) return "Not refreshed yet";
  if (hours < 1) return "Refreshed just now";
  if (hours < 48) return `Refreshed ${Math.round(hours)}h ago`;
  return `Refreshed ${Math.round(hours / 24)} days ago`;
}
