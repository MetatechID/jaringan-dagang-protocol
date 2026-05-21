"use client";

import { useState } from "react";
import { Subscriber } from "@/lib/types";
import { CITY_NAMES } from "@/lib/indonesia-data";

interface Props {
  subscribers: Subscriber[];
}

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
    <span
      className={`inline-flex px-2 py-0.5 rounded text-xs font-mono font-bold border ${colors[type] || colors.BG}`}
    >
      {type}
    </span>
  );
}

export function ParticipantTable({ subscribers }: Props) {
  const [expandedId, setExpandedId] = useState<string | null>(null);

  return (
    <div className="rounded-xl border border-cyan-900/30 bg-surface-800/50 overflow-hidden">
      <div className="px-5 py-4 border-b border-cyan-900/20 flex items-center justify-between">
        <div>
          <h3 className="text-sm font-semibold text-white">
            Network Participants
          </h3>
          <p className="text-xs text-slate-500 mt-0.5">
            {subscribers.length} registered on the network
          </p>
        </div>
        <div className="flex items-center gap-2 text-xs text-slate-500">
          <span className="flex items-center gap-1">
            <span className="w-2 h-2 rounded-full bg-cyan-400" /> BAP
          </span>
          <span className="flex items-center gap-1">
            <span className="w-2 h-2 rounded-full bg-purple-400" /> BPP
          </span>
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-cyan-900/20 text-xs text-slate-500 uppercase tracking-wider">
              <th className="text-left px-5 py-3 font-medium">Subscriber</th>
              <th className="text-left px-3 py-3 font-medium">Type</th>
              <th className="text-left px-3 py-3 font-medium">Domain</th>
              <th className="text-left px-3 py-3 font-medium">City</th>
              <th className="text-left px-3 py-3 font-medium">Status</th>
              <th className="text-left px-3 py-3 font-medium hidden lg:table-cell">
                URL
              </th>
              <th className="text-left px-3 py-3 font-medium hidden md:table-cell">
                Registered
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-cyan-900/10">
            {subscribers.map((sub) => {
              const isExpanded = expandedId === sub.subscriber_id;
              return (
                <tr key={sub.subscriber_id} className="group">
                  <td colSpan={7} className="p-0">
                    <div
                      className="cursor-pointer hover:bg-white/[0.02] transition-colors"
                      onClick={() =>
                        setExpandedId(isExpanded ? null : sub.subscriber_id)
                      }
                    >
                      <div className="flex items-center">
                        <div className="flex-1 grid grid-cols-[1fr] sm:grid-cols-[1fr_auto_auto_auto_auto] lg:grid-cols-[1fr_auto_auto_auto_auto_1fr_auto] items-center">
                          <div className="px-5 py-3">
                            <div className="flex items-center gap-2">
                              <svg
                                className={`w-3 h-3 text-slate-600 transition-transform ${isExpanded ? "rotate-90" : ""}`}
                                viewBox="0 0 12 12"
                                fill="currentColor"
                              >
                                <path d="M4 2 L9 6 L4 10 Z" />
                              </svg>
                              <span className="font-mono text-xs text-slate-300">
                                {sub.subscriber_id}
                              </span>
                            </div>
                          </div>
                          <div className="px-3 py-3 hidden sm:block">
                            <TypeBadge type={sub.type} />
                          </div>
                          <div className="px-3 py-3 hidden sm:block">
                            <span className="text-xs text-slate-400 capitalize">
                              {sub.domain}
                            </span>
                          </div>
                          <div className="px-3 py-3 hidden sm:block">
                            <span className="text-xs text-slate-400">
                              {CITY_NAMES[sub.city] || sub.city}
                            </span>
                          </div>
                          <div className="px-3 py-3 hidden sm:block">
                            <StatusBadge status={sub.status} />
                          </div>
                          <div className="px-3 py-3 hidden lg:block">
                            <span className="text-xs text-slate-600 font-mono">
                              {sub.subscriber_url}
                            </span>
                          </div>
                          <div className="px-3 py-3 hidden md:block">
                            <span className="text-xs text-slate-600">
                              {new Date(sub.created).toLocaleDateString(
                                "en-GB",
                                {
                                  day: "2-digit",
                                  month: "short",
                                  year: "numeric",
                                }
                              )}
                            </span>
                          </div>
                        </div>
                      </div>
                    </div>

                    {/* Expanded detail */}
                    {isExpanded && (
                      <div className="px-5 py-4 bg-surface-700/30 border-t border-cyan-900/10 animate-slide-in">
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                          <div>
                            <h4 className="text-xs font-semibold text-slate-400 mb-2 uppercase tracking-wider">
                              Connection Details
                            </h4>
                            <div className="space-y-2 text-xs">
                              <div className="flex justify-between">
                                <span className="text-slate-500">
                                  Subscriber URL
                                </span>
                                <span className="font-mono text-cyan-400">
                                  {sub.subscriber_url}
                                </span>
                              </div>
                              <div className="flex justify-between">
                                <span className="text-slate-500">
                                  Valid From
                                </span>
                                <span className="text-slate-300">
                                  {new Date(sub.valid_from).toLocaleString()}
                                </span>
                              </div>
                              <div className="flex justify-between">
                                <span className="text-slate-500">
                                  Valid Until
                                </span>
                                <span className="text-slate-300">
                                  {new Date(sub.valid_until).toLocaleString()}
                                </span>
                              </div>
                            </div>
                          </div>
                          <div>
                            <h4 className="text-xs font-semibold text-slate-400 mb-2 uppercase tracking-wider">
                              Public Keys
                            </h4>
                            <div className="space-y-2">
                              <div>
                                <span className="text-[10px] text-slate-500 block mb-1">
                                  Signing Key
                                </span>
                                <code className="text-[10px] font-mono text-cyan-400/60 bg-surface-900 px-2 py-1 rounded block break-all">
                                  {sub.signing_public_key}
                                </code>
                              </div>
                              <div>
                                <span className="text-[10px] text-slate-500 block mb-1">
                                  Encryption Key
                                </span>
                                <code className="text-[10px] font-mono text-cyan-400/60 bg-surface-900 px-2 py-1 rounded block break-all">
                                  {sub.encr_public_key}
                                </code>
                              </div>
                            </div>
                          </div>
                        </div>
                      </div>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
