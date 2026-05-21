"use client";

import { useState, useCallback } from "react";

interface FlowStep {
  id: number;
  label: string;
  detail: string;
  status: "pending" | "active" | "complete" | "error";
  from: string;
  to: string;
}

interface SearchResultItem {
  name: string;
  price: string;
  category: string;
}

interface BppResult {
  bpp: string;
  items: SearchResultItem[];
}

const INITIAL_STEPS: FlowStep[] = [
  { id: 1, label: "BAP sends /search to Gateway", detail: "POST /search with intent: query, domain, city", status: "pending", from: "BAP", to: "Gateway" },
  { id: 2, label: "Gateway multicasts to BPPs", detail: "Registry lookup + fan-out to all matching BPPs", status: "pending", from: "Gateway", to: "BPPs" },
  { id: 3, label: "BPPs process search request", detail: "Each BPP searches their catalog for matching items", status: "pending", from: "BPPs", to: "BPPs" },
  { id: 4, label: "BPPs send /on_search callback", detail: "Async callback with catalog results to BAP", status: "pending", from: "BPPs", to: "BAP" },
  { id: 5, label: "BAP receives catalog results", detail: "Aggregated catalogs from all responding BPPs", status: "pending", from: "BAP", to: "BAP" },
];

function formatPrice(value: string | number): string {
  const num = typeof value === "string" ? parseFloat(value) : value;
  if (isNaN(num)) return `Rp ${value}`;
  return `Rp ${num.toLocaleString("id-ID")}`;
}

function displayBppName(id: string): string {
  return id
    .replace(/\.jaringan-dagang\.id$/, "")
    .replace(/\.jaringan-dagang\.local$/, "")
    .replace(/\.example\.com$/, "")
    .split(/[.\-_]/)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

export function LiveSearchDemo() {
  const [query, setQuery] = useState("matcha");
  const [steps, setSteps] = useState<FlowStep[]>(INITIAL_STEPS);
  const [isRunning, setIsRunning] = useState(false);
  const [showResults, setShowResults] = useState(false);
  const [results, setResults] = useState<BppResult[]>([]);
  const [activeArrow, setActiveArrow] = useState<string | null>(null);
  const [timings, setTimings] = useState<Record<number, number>>({});
  const [error, setError] = useState<string | null>(null);

  const updateStep = (id: number, status: FlowStep["status"]) => {
    setSteps((prev) => prev.map((s) => (s.id === id ? { ...s, status } : s)));
  };

  const runSearch = useCallback(async () => {
    if (isRunning) return;
    setIsRunning(true);
    setShowResults(false);
    setResults([]);
    setError(null);
    setTimings({});
    setSteps(INITIAL_STEPS.map((s) => ({ ...s, status: "pending" })));

    const totalStart = Date.now();

    // Step 1: BAP sends search to Gateway
    setActiveArrow("BAP-Gateway");
    updateStep(1, "active");
    let sessionId: string | null = null;

    try {
      const res = await fetch("/api/bap/api/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query }),
      });
      if (!res.ok) throw new Error(`BAP returned ${res.status}`);
      const data = await res.json();
      sessionId = data.session_id;
      setTimings((t) => ({ ...t, 1: Date.now() - totalStart }));
      updateStep(1, "complete");
    } catch (e) {
      updateStep(1, "error");
      setError(e instanceof Error ? e.message : "Search failed");
      setIsRunning(false);
      setActiveArrow(null);
      return;
    }

    // Step 2: Gateway multicasts
    setActiveArrow("Gateway-BPPs");
    updateStep(2, "active");
    await new Promise((r) => setTimeout(r, 500));
    setTimings((t) => ({ ...t, 2: Date.now() - totalStart }));
    updateStep(2, "complete");

    // Step 3: BPPs process
    updateStep(3, "active");
    setActiveArrow(null);

    // Step 4: BPPs send callback
    // Poll for results
    let pollResults: BppResult[] = [];
    let attempts = 0;
    const maxAttempts = 15;

    while (attempts < maxAttempts) {
      await new Promise((r) => setTimeout(r, 2000));
      attempts++;

      try {
        const res = await fetch(`/api/bap/api/search/${sessionId}/results`);
        if (!res.ok) continue;
        const data = await res.json();

        if (data.result_count > 0 || data.status === "completed" || data.status === "expired") {
          setTimings((t) => ({ ...t, 3: Date.now() - totalStart }));
          updateStep(3, "complete");

          // Parse results
          const bppResults: BppResult[] = [];
          for (const [bppId, bppData] of Object.entries(data.results || {})) {
            const catalog = (bppData as Record<string, unknown>).catalog as Record<string, unknown> | undefined;
            const providers = (catalog?.providers || []) as Array<Record<string, unknown>>;
            const items: SearchResultItem[] = [];

            for (const provider of providers) {
              const providerItems = (provider.items || []) as Array<Record<string, unknown>>;
              for (const item of providerItems) {
                const descriptor = item.descriptor as Record<string, string> | undefined;
                const price = item.price as Record<string, string> | undefined;
                const category = item.category_id as string || "";
                items.push({
                  name: descriptor?.name || "Unknown",
                  price: formatPrice(price?.value || "0"),
                  category: category,
                });
              }
            }

            if (items.length > 0) {
              bppResults.push({ bpp: bppId, items: items.slice(0, 5) });
            }
          }

          pollResults = bppResults;
          break;
        }
      } catch {
        // Continue polling
      }
    }

    // Step 4
    setActiveArrow("BPPs-BAP");
    updateStep(4, "active");
    await new Promise((r) => setTimeout(r, 300));
    setTimings((t) => ({ ...t, 4: Date.now() - totalStart }));
    updateStep(4, "complete");

    // Step 5
    updateStep(5, "active");
    setActiveArrow(null);
    await new Promise((r) => setTimeout(r, 200));
    setTimings((t) => ({ ...t, 5: Date.now() - totalStart }));
    updateStep(5, "complete");

    if (pollResults.length > 0) {
      setResults(pollResults);
    } else {
      setError("No results found — BPPs may be cold-starting. Try again.");
    }

    setShowResults(true);
    setIsRunning(false);
  }, [isRunning, query]);

  const getStepColor = (status: FlowStep["status"]) => {
    switch (status) {
      case "active": return "border-cyan-400 bg-cyan-400/5";
      case "complete": return "border-emerald-500/30 bg-emerald-400/5";
      case "error": return "border-red-500/30 bg-red-400/5";
      default: return "border-slate-800 bg-surface-800/30";
    }
  };

  const getStepIcon = (status: FlowStep["status"]) => {
    switch (status) {
      case "active":
        return <div className="w-5 h-5 rounded-full border-2 border-cyan-400 border-t-transparent animate-spin" />;
      case "complete":
        return (
          <div className="w-5 h-5 rounded-full bg-emerald-500/20 flex items-center justify-center">
            <svg className="w-3 h-3 text-emerald-400" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M2 6 L5 9 L10 3" />
            </svg>
          </div>
        );
      case "error":
        return (
          <div className="w-5 h-5 rounded-full bg-red-500/20 flex items-center justify-center">
            <span className="text-red-400 text-xs font-bold">!</span>
          </div>
        );
      default:
        return <div className="w-5 h-5 rounded-full border border-slate-700 bg-surface-800" />;
    }
  };

  return (
    <div className="rounded-xl border border-cyan-900/30 bg-surface-800/50 overflow-hidden">
      <div className="px-5 py-4 border-b border-cyan-900/20">
        <h3 className="text-sm font-semibold text-white flex items-center gap-2">
          <svg className="w-4 h-4 text-cyan-400" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
            <circle cx="7" cy="7" r="5" />
            <line x1="11" y1="11" x2="14" y2="14" />
          </svg>
          Try a Live Beckn Search
        </h3>
        <p className="text-xs text-slate-500 mt-1">
          Real search through the Beckn protocol — multicast through the Gateway to all registered BPPs
        </p>
      </div>

      <div className="p-5">
        {/* Search Input */}
        <div className="flex gap-2 mb-6">
          <div className="relative flex-1">
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search for products..."
              disabled={isRunning}
              className="w-full px-4 py-2.5 rounded-lg bg-surface-900 border border-cyan-900/30 text-sm text-white placeholder-slate-600 focus:outline-none focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500/20 disabled:opacity-50 font-mono"
            />
            <div className="absolute right-3 top-1/2 -translate-y-1/2 text-xs text-slate-600 font-mono">
              /search
            </div>
          </div>
          <button
            onClick={runSearch}
            disabled={isRunning || !query.trim()}
            className="px-5 py-2.5 rounded-lg bg-gradient-to-r from-cyan-500 to-teal-500 text-sm font-semibold text-surface-900 hover:from-cyan-400 hover:to-teal-400 disabled:opacity-50 disabled:cursor-not-allowed transition-all whitespace-nowrap"
          >
            {isRunning ? "Searching..." : "Send Beckn Search"}
          </button>
        </div>

        {/* Mini Flow Diagram */}
        <div className="flex items-center justify-center gap-2 mb-6 py-3">
          {["BAP", "Gateway", "BPPs"].map((node, i) => (
            <div key={node} className="flex items-center gap-2">
              <div className={`px-3 py-1.5 rounded-md border text-xs font-mono font-bold transition-all ${
                activeArrow?.includes(node)
                  ? "border-cyan-400 text-cyan-300 bg-cyan-400/10 shadow-[0_0_12px_rgba(0,240,255,0.2)]"
                  : "border-slate-700 text-slate-500 bg-surface-800"
              }`}>
                {node}
              </div>
              {i < 2 && (
                <div className="flex items-center">
                  <div className={`w-8 h-0.5 transition-all ${
                    activeArrow === (i === 0 ? "BAP-Gateway" : "Gateway-BPPs")
                      ? "bg-cyan-400 shadow-[0_0_8px_rgba(0,240,255,0.5)]"
                      : "bg-slate-700"
                  }`} />
                  <svg className={`w-2 h-2 -ml-0.5 transition-colors ${
                    activeArrow === (i === 0 ? "BAP-Gateway" : "Gateway-BPPs") ? "text-cyan-400" : "text-slate-700"
                  }`} viewBox="0 0 8 8" fill="currentColor">
                    <path d="M0 0 L8 4 L0 8 Z" />
                  </svg>
                </div>
              )}
            </div>
          ))}
        </div>

        {/* Steps */}
        <div className="space-y-2">
          {steps.map((step) => (
            <div key={step.id} className={`flex items-start gap-3 p-3 rounded-lg border transition-all duration-300 ${getStepColor(step.status)}`}>
              <div className="mt-0.5">{getStepIcon(step.status)}</div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className={`text-sm font-medium ${
                    step.status === "active" ? "text-cyan-300"
                    : step.status === "complete" ? "text-emerald-300"
                    : step.status === "error" ? "text-red-300"
                    : "text-slate-400"
                  }`}>
                    {step.label}
                  </span>
                  {step.status === "active" && (
                    <span className="text-[10px] text-cyan-500 font-mono animate-pulse">processing...</span>
                  )}
                </div>
                <p className="text-xs text-slate-600 mt-0.5">{step.detail}</p>
              </div>
              {step.status === "complete" && timings[step.id] && (
                <span className="text-[10px] text-slate-600 font-mono tabular-nums">
                  {timings[step.id]}ms
                </span>
              )}
            </div>
          ))}
        </div>

        {/* Error */}
        {error && showResults && (
          <div className="mt-4 p-3 rounded-lg border border-amber-800/30 bg-amber-400/5 text-sm text-amber-300">
            {error}
          </div>
        )}

        {/* Results */}
        {showResults && results.length > 0 && (
          <div className="mt-6 animate-slide-in">
            <div className="flex items-center gap-2 mb-3">
              <div className="w-2 h-2 rounded-full bg-emerald-400" />
              <span className="text-sm font-semibold text-emerald-300">
                Catalog Results Received
              </span>
              <span className="text-xs text-slate-600">
                {results.length} BPP{results.length > 1 ? "s" : ""} responded &middot; {results.reduce((sum, r) => sum + r.items.length, 0)} items
              </span>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {results.map((result) => (
                <div key={result.bpp} className="rounded-lg border border-emerald-800/20 bg-emerald-400/5 p-4">
                  <div className="flex items-center gap-2 mb-3">
                    <span className="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-purple-400/10 text-purple-300 border border-purple-700/30">
                      BPP
                    </span>
                    <span className="text-xs font-medium text-slate-300">
                      {displayBppName(result.bpp)}
                    </span>
                  </div>
                  <div className="space-y-2">
                    {result.items.map((item, j) => (
                      <div key={j} className="flex items-center justify-between py-1.5 border-b border-slate-800/50 last:border-0">
                        <span className="text-sm text-slate-300 truncate mr-2">{item.name}</span>
                        <span className="text-sm font-semibold text-emerald-400 font-mono whitespace-nowrap">
                          {item.price}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
