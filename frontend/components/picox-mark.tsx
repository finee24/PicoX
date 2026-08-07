/** Simbolo di Picox: riquadro video + play, con il gradiente verde→viola. */
export function PicoxMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 512 512" className={className} role="img" aria-label="Picox">
      <defs>
        <linearGradient id="picox-mark" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#34d399" />
          <stop offset="100%" stopColor="#a78bfa" />
        </linearGradient>
      </defs>
      <rect width="512" height="512" rx="112" fill="#111113" />
      <rect
        x="112"
        y="136"
        width="288"
        height="240"
        rx="32"
        fill="none"
        stroke="url(#picox-mark)"
        strokeWidth="26"
      />
      <path d="M228 208 L316 256 L228 304 Z" fill="url(#picox-mark)" />
    </svg>
  );
}
