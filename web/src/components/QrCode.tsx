// A QR code drawn as one SVG path (uqr encodes it; no image is fetched and nothing leaves the page).
import { qrModules } from "../lib/mfa";

export function QrCode({ text, label, size = 200 }: { text: string; label: string; size?: number }) {
  const m = qrModules(text);
  const n = m.length;
  let d = "";
  m.forEach((row, y) => row.forEach((dark, x) => { if (dark) d += `M${x} ${y}h1v1h-1z`; }));
  return (
    <svg className="qr" role="img" aria-label={label} width={size} height={size} viewBox={`0 0 ${n} ${n}`} shapeRendering="crispEdges">
      <rect width={n} height={n} fill="#fff" />
      <path d={d} fill="#000" />
    </svg>
  );
}
