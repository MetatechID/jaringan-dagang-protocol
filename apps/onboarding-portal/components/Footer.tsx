export function Footer() {
  return (
    <footer className="border-t border-cyan-900/20 bg-surface-900/50">
      <div className="mx-auto max-w-7xl px-4 sm:px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-2 text-xs text-slate-500">
          <svg className="w-4 h-4" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
            <circle cx="8" cy="8" r="6" />
            <path d="M4 8 Q8 4 12 8 Q8 12 4 8" />
          </svg>
          <span>Powered by Beckn Protocol</span>
          <span className="text-slate-700">|</span>
          <span>Jaringan Dagang - Indonesia Open Commerce Network</span>
        </div>
        <div className="text-xs text-slate-600">
          v0.1.0
        </div>
      </div>
    </footer>
  );
}
