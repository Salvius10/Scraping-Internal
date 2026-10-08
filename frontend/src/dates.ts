// Date helpers shared by the Insights pages. Everything is shown in IST.

import { parseTime } from "./api";

export const publishedFmt = new Intl.DateTimeFormat("en-IN", {
  day: "numeric", month: "short", year: "numeric",
  hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Kolkata",
});

export const approxFmt = new Intl.DateTimeFormat("en-IN", {
  day: "numeric", month: "short", year: "numeric", timeZone: "Asia/Kolkata",
});

/** Today in India as YYYY-MM-DD, optionally some days back. */
export function istDay(daysAgo = 0): string {
  const at = new Date(Date.now() - daysAgo * 86_400_000);
  return at.toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
}

export const PRESETS: { label: string; days: number | null }[] = [
  { label: "Last 7 days", days: 7 },
  { label: "Last 30 days", days: 30 },
  { label: "Last 90 days", days: 90 },
  { label: "All time", days: null },
];

const dayFmt = new Intl.DateTimeFormat("en-IN", {
  day: "numeric", month: "short", year: "numeric", timeZone: "UTC",
});
export const showDay = (ymd: string) => dayFmt.format(new Date(`${ymd}T00:00:00Z`));

/** How long ago a source was last read, for its health line. */
export function readAgo(iso: string | null): string {
  if (!iso) return "not read yet";
  const hours = (Date.now() - parseTime(iso).getTime()) / 3_600_000;
  if (hours < 1) return "read just now";
  if (hours < 48) return `read ${Math.round(hours)}h ago`;
  return `read ${Math.round(hours / 24)} days ago`;
}
