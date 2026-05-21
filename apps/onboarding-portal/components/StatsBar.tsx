"use client";

import { NetworkStats } from "@/lib/types";

interface Props {
  stats: NetworkStats;
}

const statCards = [
  {
    key: "total" as const,
    label: "Total Participants",
    icon: (
      <svg className="w-5 h-5" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
        <circle cx="10" cy="4" r="2.5" />
        <circle cx="4" cy="16" r="2.5" />
        <circle cx="16" cy="16" r="2.5" />
        <line x1="10" y1="6.5" x2="5" y2="13.5" />
        <line x1="10" y1="6.5" x2="15" y2="13.5" />
      </svg>
    ),
    color: "cyan",
  },
  {
    key: "baps" as const,
    label: "Buyer Apps",
    icon: (
      <svg className="w-5 h-5" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
        <rect x="3" y="3" width="14" height="14" rx="2" />
        <path d="M7 10 L9 12 L13 8" />
      </svg>
    ),
    color: "blue",
  },
  {
    key: "bpps" as const,
    label: "Provider Platforms",
    icon: (
      <svg className="w-5 h-5" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
        <rect x="2" y="6" width="16" height="10" rx="1" />
        <line x1="2" y1="10" x2="18" y2="10" />
        <circle cx="10" cy="3.5" r="1.5" />
        <line x1="10" y1="5" x2="10" y2="6" />
      </svg>
    ),
    color: "purple",
  },
  {
    key: "cities" as const,
    label: "Cities Covered",
    icon: (
      <svg className="w-5 h-5" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
        <circle cx="10" cy="8" r="3" />
        <path d="M10 11 L10 17" />
        <path d="M6 17 L14 17" />
      </svg>
    ),
    color: "teal",
  },
  {
    key: "domains" as const,
    label: "Domains Active",
    icon: (
      <svg className="w-5 h-5" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
        <circle cx="10" cy="10" r="7" />
        <ellipse cx="10" cy="10" rx="3" ry="7" />
        <line x1="3" y1="10" x2="17" y2="10" />
      </svg>
    ),
    color: "green",
  },
];

const colorMap: Record<string, { bg: string; text: string; border: string; glow: string }> = {
  cyan: {
    bg: "bg-cyan-400/5",
    text: "text-cyan-300",
    border: "border-cyan-800/30",
    glow: "shadow-[0_0_15px_rgba(0,240,255,0.05)]",
  },
  blue: {
    bg: "bg-blue-400/5",
    text: "text-blue-300",
    border: "border-blue-800/30",
    glow: "shadow-[0_0_15px_rgba(59,130,246,0.05)]",
  },
  purple: {
    bg: "bg-purple-400/5",
    text: "text-purple-300",
    border: "border-purple-800/30",
    glow: "shadow-[0_0_15px_rgba(168,85,247,0.05)]",
  },
  teal: {
    bg: "bg-teal-400/5",
    text: "text-teal-300",
    border: "border-teal-800/30",
    glow: "shadow-[0_0_15px_rgba(0,212,170,0.05)]",
  },
  green: {
    bg: "bg-emerald-400/5",
    text: "text-emerald-300",
    border: "border-emerald-800/30",
    glow: "shadow-[0_0_15px_rgba(0,255,136,0.05)]",
  },
};

export function StatsBar({ stats }: Props) {
  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
      {statCards.map((card) => {
        const colors = colorMap[card.color];
        const value =
          card.key === "cities"
            ? stats.cities.length
            : card.key === "domains"
            ? stats.domains.length
            : stats[card.key];
        return (
          <div
            key={card.key}
            className={`
              relative rounded-xl border p-4 transition-all
              ${colors.bg} ${colors.border} ${colors.glow}
              hover:scale-[1.02]
            `}
          >
            <div className="flex items-center justify-between mb-2">
              <span className={`${colors.text} opacity-60`}>{card.icon}</span>
              <span
                className={`text-2xl font-bold tabular-nums ${colors.text}`}
              >
                {value}
              </span>
            </div>
            <p className="text-xs text-slate-500 font-medium">{card.label}</p>
          </div>
        );
      })}
    </div>
  );
}
