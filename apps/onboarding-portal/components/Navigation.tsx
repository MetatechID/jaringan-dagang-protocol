"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const navLinks = [
  { href: "/", label: "Network", icon: "topology" },
  { href: "/registry", label: "Registry", icon: "list" },
  { href: "/register", label: "Register", icon: "plus" },
  { href: "/protocol", label: "Protocol", icon: "flow" },
  { href: "/specs", label: "Specs", icon: "doc" },
];

function NavIcon({ icon }: { icon: string }) {
  switch (icon) {
    case "topology":
      return (
        <svg className="w-4 h-4" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
          <circle cx="8" cy="3" r="2" />
          <circle cx="3" cy="13" r="2" />
          <circle cx="13" cy="13" r="2" />
          <line x1="8" y1="5" x2="4" y2="11" />
          <line x1="8" y1="5" x2="12" y2="11" />
        </svg>
      );
    case "list":
      return (
        <svg className="w-4 h-4" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
          <line x1="5" y1="3" x2="14" y2="3" />
          <line x1="5" y1="8" x2="14" y2="8" />
          <line x1="5" y1="13" x2="14" y2="13" />
          <circle cx="2" cy="3" r="1" fill="currentColor" />
          <circle cx="2" cy="8" r="1" fill="currentColor" />
          <circle cx="2" cy="13" r="1" fill="currentColor" />
        </svg>
      );
    case "plus":
      return (
        <svg className="w-4 h-4" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
          <circle cx="8" cy="8" r="6" />
          <line x1="8" y1="5" x2="8" y2="11" />
          <line x1="5" y1="8" x2="11" y2="8" />
        </svg>
      );
    case "flow":
      return (
        <svg className="w-4 h-4" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
          <rect x="1" y="1" width="4" height="4" rx="1" />
          <rect x="11" y="1" width="4" height="4" rx="1" />
          <rect x="6" y="11" width="4" height="4" rx="1" />
          <path d="M5 3 L11 3" />
          <path d="M3 5 L8 11" />
          <path d="M13 5 L8 11" />
        </svg>
      );
    case "doc":
      return (
        <svg className="w-4 h-4" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
          <rect x="2" y="1" width="12" height="14" rx="1" />
          <line x1="5" y1="5" x2="11" y2="5" />
          <line x1="5" y1="8" x2="11" y2="8" />
          <line x1="5" y1="11" x2="8" y2="11" />
        </svg>
      );
    default:
      return null;
  }
}

export function Navigation() {
  const pathname = usePathname();

  return (
    <nav className="sticky top-0 z-50 border-b border-cyan-900/30 bg-surface-900/80 backdrop-blur-xl">
      <div className="mx-auto max-w-7xl px-4 sm:px-6">
        <div className="flex h-16 items-center justify-between">
          <Link href="/" className="flex items-center gap-3 group">
            <div className="relative">
              <div className="w-9 h-9 rounded-lg bg-gradient-to-br from-cyan-400 to-teal-500 flex items-center justify-center">
                <svg className="w-5 h-5 text-surface-900" viewBox="0 0 20 20" fill="currentColor">
                  <circle cx="10" cy="5" r="2" />
                  <circle cx="4" cy="15" r="2" />
                  <circle cx="16" cy="15" r="2" />
                  <line x1="10" y1="7" x2="5" y2="13" stroke="currentColor" strokeWidth="1.5" />
                  <line x1="10" y1="7" x2="15" y2="13" stroke="currentColor" strokeWidth="1.5" />
                  <line x1="6" y1="15" x2="14" y2="15" stroke="currentColor" strokeWidth="1.5" />
                </svg>
              </div>
              <div className="absolute -inset-1 rounded-xl bg-cyan-400/20 opacity-0 group-hover:opacity-100 transition-opacity blur-sm" />
            </div>
            <div>
              <span className="text-base font-bold text-white tracking-tight">
                Jaringan Dagang
              </span>
              <span className="hidden sm:block text-[10px] font-medium text-cyan-400/70 tracking-widest uppercase">
                Network Dashboard
              </span>
            </div>
          </Link>

          <div className="flex items-center gap-1">
            {navLinks.map((link) => {
              const isActive =
                link.href === "/"
                  ? pathname === "/"
                  : pathname.startsWith(link.href);
              return (
                <Link
                  key={link.href}
                  href={link.href}
                  className={`
                    relative flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium transition-all
                    ${
                      isActive
                        ? "text-cyan-300 bg-cyan-400/10"
                        : "text-slate-400 hover:text-slate-200 hover:bg-white/5"
                    }
                  `}
                >
                  <NavIcon icon={link.icon} />
                  <span className="hidden md:inline">{link.label}</span>
                  {isActive && (
                    <div className="absolute bottom-0 left-2 right-2 h-0.5 bg-cyan-400 rounded-full shadow-[0_0_8px_rgba(0,240,255,0.5)]" />
                  )}
                </Link>
              );
            })}
          </div>

          <div className="flex items-center gap-2">
            <div className="hidden sm:flex items-center gap-2 px-3 py-1.5 rounded-full bg-surface-700/50 border border-cyan-900/20">
              <div className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
              <span className="text-xs font-medium text-emerald-400">
                Network Live
              </span>
            </div>
          </div>
        </div>
      </div>
    </nav>
  );
}
