"use client";

import { useState, useEffect, useMemo } from "react";
import { Subscriber } from "@/lib/types";
import { fetchSubscribers, computeStats } from "@/lib/api";
import { CITY_NAMES } from "@/lib/indonesia-data";

function StatusBadge({ status }: { status: string }) {
  if (status === "SUBSCRIBED") {
    return (
      <span className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium status-subscribed">
        <span className="w-1.5 h-1.5 rounded-full bg-green-400 animate-pulse" />
        Subscribed
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium status-initiated">
      <span className="w-1.5 h-1.5 rounded-full bg-yellow-400" />
      Initiated
    </span>
  );
}

function TypeBadge({ type }: { type: string }) {
  const colors: Record<string, string> = {
    BAP: "bg-cyan-400/10 text-cyan-300 border-cyan-700/30",
    BPP: "bg-purple-400/10 text-purple-300 border-purple-700/30",
    BG: "bg-teal-400/10 text-teal-300 border-teal-700/30",
  };
  return (
    <span className={`inline-flex px-2 py-0.5 rounded text-xs font-mono font-bold border ${colors[type] || colors.BG}`}>
      {type}
    </span>
  );
}

export default function RegistryExplorer() {
  const [subscribers, setSubscribers] = useState<Subscriber[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [filterType, setFilterType] = useState<string>("all");
  const [filterDomain, setFilterDomain] = useState<string>("all");
  const [filterCity, setFilterCity] = useState<string>("all");
  const [selectedSub, setSelectedSub] = useState<Subscriber | null>(null);

  useEffect(() => {
    async function load() {
      try {
        const data = await fetchSubscribers();
        setSubscribers(data);
        setError(null);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to fetch subscribers");
      } finally {
        setLoading(false);
      }
    }
    load();
  }, []);

  const stats = computeStats(subscribers);

  const filtered = useMemo(() => {
    return subscribers.filter((s) => {
      if (filterType !== "all" && s.type !== filterType) return false;
      if (filterDomain !== "all" && s.domain !== filterDomain) return false;
      if (filterCity !== "all" && s.city !== filterCity) return false;
      if (
        search &&
        !s.subscriber_id.toLowerCase().includes(search.toLowerCase()) &&
        !s.subscriber_url.toLowerCase().includes(search.toLowerCase())
      )
        return false;
      return true;
    });
  }, [subscribers, filterType, filterDomain, filterCity, search]);

  const uniqueDomains = Array.from(new Set(subscribers.map((s) => s.domain)));
  const uniqueCities = Array.from(new Set(subscribers.map((s) => s.city)));

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="w-10 h-10 rounded-full border-2 border-cyan-400 border-t-transparent animate-spin" />
      </div>
    );
  }

  if (error && subscribers.length === 0) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-center max-w-md">
          <p className="text-sm text-red-400 mb-2">Failed to connect to registry</p>
          <p className="text-xs text-slate-500 mb-4">{error}</p>
          <button
            onClick={() => window.location.reload()}
            className="px-4 py-2 rounded-lg border border-cyan-900/30 text-sm text-cyan-400 hover:bg-cyan-400/5 transition-colors"
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="grid-bg min-h-screen">
      <div className="mx-auto max-w-7xl px-4 sm:px-6 py-8">
        {/* Header */}
        <div className="mb-8">
          <h1 className="text-2xl font-bold text-white tracking-tight">
            Registry Explorer
          </h1>
          <p className="text-sm text-slate-400 mt-1">
            Browse and search all participants registered on the Jaringan Dagang
            network
          </p>
        </div>

        {/* Stats cards */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-8">
          <div className="rounded-xl border border-cyan-900/30 bg-surface-800/50 p-4">
            <div className="text-2xl font-bold text-cyan-300">{stats.total}</div>
            <div className="text-xs text-slate-500">Total Participants</div>
          </div>
          <div className="rounded-xl border border-cyan-900/30 bg-surface-800/50 p-4">
            <div className="text-2xl font-bold text-blue-300">{stats.baps}</div>
            <div className="text-xs text-slate-500">Buyer Apps (BAP)</div>
          </div>
          <div className="rounded-xl border border-cyan-900/30 bg-surface-800/50 p-4">
            <div className="text-2xl font-bold text-purple-300">{stats.bpps}</div>
            <div className="text-xs text-slate-500">Provider Platforms (BPP)</div>
          </div>
          <div className="rounded-xl border border-cyan-900/30 bg-surface-800/50 p-4">
            <div className="text-2xl font-bold text-emerald-300">
              {subscribers.filter((s) => s.status === "SUBSCRIBED").length}
            </div>
            <div className="text-xs text-slate-500">Active (Subscribed)</div>
          </div>
        </div>

        {/* Breakdown by Type and City */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-8">
          {/* By Type */}
          <div className="rounded-xl border border-cyan-900/30 bg-surface-800/50 p-5">
            <h3 className="text-sm font-semibold text-white mb-4">
              Registrations by Type
            </h3>
            <div className="space-y-3">
              {[
                { type: "BAP", label: "Buyer Application Platform", color: "bg-cyan-400" },
                { type: "BPP", label: "Beckn Provider Platform", color: "bg-purple-400" },
                { type: "BG", label: "Beckn Gateway", color: "bg-teal-400" },
              ].map(({ type, label, color }) => {
                const count = subscribers.filter((s) => s.type === type).length;
                const pct = stats.total > 0 ? (count / stats.total) * 100 : 0;
                return (
                  <div key={type}>
                    <div className="flex items-center justify-between mb-1">
                      <div className="flex items-center gap-2">
                        <span className={`w-2 h-2 rounded-full ${color}`} />
                        <span className="text-xs text-slate-400">{label}</span>
                      </div>
                      <span className="text-xs font-bold text-slate-300 tabular-nums">{count}</span>
                    </div>
                    <div className="h-1.5 rounded-full bg-surface-900 overflow-hidden">
                      <div
                        className={`h-full rounded-full ${color} opacity-60 transition-all duration-500`}
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          {/* By City */}
          <div className="rounded-xl border border-cyan-900/30 bg-surface-800/50 p-5">
            <h3 className="text-sm font-semibold text-white mb-4">
              Registrations by City
            </h3>
            <div className="space-y-3">
              {uniqueCities.map((city) => {
                const count = subscribers.filter((s) => s.city === city).length;
                const pct = stats.total > 0 ? (count / stats.total) * 100 : 0;
                return (
                  <div key={city}>
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-xs text-slate-400">
                        {CITY_NAMES[city] || city}
                      </span>
                      <span className="text-xs font-bold text-slate-300 tabular-nums">{count}</span>
                    </div>
                    <div className="h-1.5 rounded-full bg-surface-900 overflow-hidden">
                      <div
                        className="h-full rounded-full bg-cyan-400 opacity-50 transition-all duration-500"
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        </div>

        {/* Filters */}
        <div className="rounded-xl border border-cyan-900/30 bg-surface-800/50 p-4 mb-6">
          <div className="flex flex-wrap items-center gap-3">
            {/* Search */}
            <div className="relative flex-1 min-w-[200px]">
              <svg className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-600" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
                <circle cx="7" cy="7" r="5" />
                <line x1="11" y1="11" x2="14" y2="14" />
              </svg>
              <input
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search by subscriber ID or URL..."
                className="w-full pl-10 pr-4 py-2 rounded-lg bg-surface-900 border border-cyan-900/30 text-sm text-white placeholder-slate-600 focus:outline-none focus:border-cyan-500"
              />
            </div>

            {/* Type filter */}
            <select
              value={filterType}
              onChange={(e) => setFilterType(e.target.value)}
              className="px-3 py-2 rounded-lg bg-surface-900 border border-cyan-900/30 text-sm text-slate-300 focus:outline-none focus:border-cyan-500"
            >
              <option value="all">All Types</option>
              <option value="BAP">BAP</option>
              <option value="BPP">BPP</option>
              <option value="BG">Gateway</option>
            </select>

            {/* Domain filter */}
            <select
              value={filterDomain}
              onChange={(e) => setFilterDomain(e.target.value)}
              className="px-3 py-2 rounded-lg bg-surface-900 border border-cyan-900/30 text-sm text-slate-300 focus:outline-none focus:border-cyan-500"
            >
              <option value="all">All Domains</option>
              {uniqueDomains.map((d) => (
                <option key={d} value={d}>
                  {d}
                </option>
              ))}
            </select>

            {/* City filter */}
            <select
              value={filterCity}
              onChange={(e) => setFilterCity(e.target.value)}
              className="px-3 py-2 rounded-lg bg-surface-900 border border-cyan-900/30 text-sm text-slate-300 focus:outline-none focus:border-cyan-500"
            >
              <option value="all">All Cities</option>
              {uniqueCities.map((c) => (
                <option key={c} value={c}>
                  {CITY_NAMES[c] || c}
                </option>
              ))}
            </select>

            <span className="text-xs text-slate-600">
              {filtered.length} of {subscribers.length}
            </span>
          </div>
        </div>

        {/* Results table */}
        <div className="rounded-xl border border-cyan-900/30 bg-surface-800/50 overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-cyan-900/20 text-xs text-slate-500 uppercase tracking-wider">
                  <th className="text-left px-5 py-3 font-medium">Subscriber ID</th>
                  <th className="text-left px-3 py-3 font-medium">Type</th>
                  <th className="text-left px-3 py-3 font-medium">Domain</th>
                  <th className="text-left px-3 py-3 font-medium">City</th>
                  <th className="text-left px-3 py-3 font-medium">Status</th>
                  <th className="text-left px-3 py-3 font-medium hidden lg:table-cell">URL</th>
                  <th className="text-left px-3 py-3 font-medium">Registered</th>
                  <th className="text-left px-3 py-3 font-medium"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-cyan-900/10">
                {filtered.map((sub) => (
                  <tr
                    key={sub.subscriber_id}
                    className="hover:bg-white/[0.02] transition-colors cursor-pointer"
                    onClick={() => setSelectedSub(sub)}
                  >
                    <td className="px-5 py-3">
                      <span className="font-mono text-xs text-slate-300">
                        {sub.subscriber_id}
                      </span>
                    </td>
                    <td className="px-3 py-3">
                      <TypeBadge type={sub.type} />
                    </td>
                    <td className="px-3 py-3 text-xs text-slate-400 capitalize">
                      {sub.domain}
                    </td>
                    <td className="px-3 py-3 text-xs text-slate-400">
                      {CITY_NAMES[sub.city] || sub.city}
                    </td>
                    <td className="px-3 py-3">
                      <StatusBadge status={sub.status} />
                    </td>
                    <td className="px-3 py-3 text-xs text-slate-600 font-mono hidden lg:table-cell">
                      {sub.subscriber_url}
                    </td>
                    <td className="px-3 py-3 text-xs text-slate-600">
                      {new Date(sub.created).toLocaleDateString("en-GB", {
                        day: "2-digit",
                        month: "short",
                        year: "numeric",
                      })}
                    </td>
                    <td className="px-3 py-3">
                      <button className="text-xs text-cyan-500 hover:text-cyan-400">
                        View
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {filtered.length === 0 && (
            <div className="text-center py-12">
              <p className="text-sm text-slate-500">No participants match your filters</p>
            </div>
          )}
        </div>
      </div>

      {/* Detail modal */}
      {selectedSub && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
          onClick={() => setSelectedSub(null)}
        >
          <div
            className="w-full max-w-lg mx-4 rounded-2xl border border-cyan-900/30 bg-surface-800 shadow-2xl animate-slide-in"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between px-6 py-4 border-b border-cyan-900/20">
              <div>
                <h3 className="text-sm font-semibold text-white">
                  Participant Details
                </h3>
                <p className="text-xs text-slate-500 font-mono mt-0.5">
                  {selectedSub.subscriber_id}
                </p>
              </div>
              <button
                onClick={() => setSelectedSub(null)}
                className="p-1 rounded-lg hover:bg-white/5"
              >
                <svg className="w-5 h-5 text-slate-500" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
                  <line x1="5" y1="5" x2="15" y2="15" />
                  <line x1="15" y1="5" x2="5" y2="15" />
                </svg>
              </button>
            </div>

            <div className="px-6 py-5 space-y-5">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <span className="text-[10px] text-slate-500 uppercase tracking-wider">Type</span>
                  <div className="mt-1">
                    <TypeBadge type={selectedSub.type} />
                  </div>
                </div>
                <div>
                  <span className="text-[10px] text-slate-500 uppercase tracking-wider">Status</span>
                  <div className="mt-1">
                    <StatusBadge status={selectedSub.status} />
                  </div>
                </div>
                <div>
                  <span className="text-[10px] text-slate-500 uppercase tracking-wider">Domain</span>
                  <p className="text-sm text-slate-300 mt-1 capitalize">{selectedSub.domain}</p>
                </div>
                <div>
                  <span className="text-[10px] text-slate-500 uppercase tracking-wider">City</span>
                  <p className="text-sm text-slate-300 mt-1">
                    {CITY_NAMES[selectedSub.city] || selectedSub.city}
                  </p>
                </div>
              </div>

              <div>
                <span className="text-[10px] text-slate-500 uppercase tracking-wider">URL</span>
                <p className="text-sm text-cyan-400 font-mono mt-1">
                  {selectedSub.subscriber_url}
                </p>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <span className="text-[10px] text-slate-500 uppercase tracking-wider">Valid From</span>
                  <p className="text-xs text-slate-400 mt-1">
                    {new Date(selectedSub.valid_from).toLocaleString()}
                  </p>
                </div>
                <div>
                  <span className="text-[10px] text-slate-500 uppercase tracking-wider">Valid Until</span>
                  <p className="text-xs text-slate-400 mt-1">
                    {new Date(selectedSub.valid_until).toLocaleString()}
                  </p>
                </div>
              </div>

              <div>
                <span className="text-[10px] text-slate-500 uppercase tracking-wider">Signing Public Key</span>
                <code className="block text-[11px] font-mono text-cyan-400/60 bg-surface-900 px-3 py-2 rounded-lg mt-1 break-all">
                  {selectedSub.signing_public_key}
                </code>
              </div>

              <div>
                <span className="text-[10px] text-slate-500 uppercase tracking-wider">Encryption Public Key</span>
                <code className="block text-[11px] font-mono text-cyan-400/60 bg-surface-900 px-3 py-2 rounded-lg mt-1 break-all">
                  {selectedSub.encr_public_key}
                </code>
              </div>
            </div>

            <div className="px-6 py-4 border-t border-cyan-900/20 flex justify-end">
              <button
                onClick={() => setSelectedSub(null)}
                className="px-4 py-2 rounded-lg text-sm font-medium text-slate-400 hover:text-white hover:bg-white/5 transition-colors"
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
