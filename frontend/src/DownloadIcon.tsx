// Phosphor Icons (MIT), regular weight: "download-simple".
const DOWNLOAD =
  "M224,144v64a8,8,0,0,1-8,8H40a8,8,0,0,1-8-8V144a8,8,0,0,1,16,0v56H208V144a8,8,0,0,1,16,0Zm-101.66,5.66a8,8,0,0,0,11.32,0l40-40a8,8,0,0,0-11.32-11.32L136,124.69V32a8,8,0,0,0-16,0v92.69L93.66,98.34a8,8,0,0,0-11.32,11.32Z";

export default function DownloadIcon() {
  return (
    <svg viewBox="0 0 256 256" width="15" height="15" fill="currentColor" aria-hidden="true">
      <path d={DOWNLOAD} />
    </svg>
  );
}
