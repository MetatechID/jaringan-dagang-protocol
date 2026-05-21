"use client";

import { useState } from "react";
import {
  CITIES,
  DOMAINS,
  PAYMENT_METHODS,
  LOGISTICS_PROVIDERS,
  CATEGORIES,
  ISLAND_GROUPS,
} from "@/lib/indonesia-data";

type Tab = "domains" | "cities" | "payments" | "logistics" | "categories";

const TABS: { id: Tab; label: string }[] = [
  { id: "domains", label: "Domains" },
  { id: "cities", label: "Cities" },
  { id: "payments", label: "Payment Methods" },
  { id: "logistics", label: "Logistics" },
  { id: "categories", label: "Categories" },
];

function DomainsTab() {
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
      {DOMAINS.map((domain) => (
        <div
          key={domain.id}
          className="rounded-xl border border-cyan-900/30 bg-surface-800/50 p-5 card-hover"
        >
          <div className="flex items-start justify-between mb-3">
            <h3 className="text-sm font-semibold text-white">{domain.name}</h3>
            <span className="px-2 py-0.5 rounded text-[10px] font-mono text-cyan-400 bg-cyan-400/10 border border-cyan-800/30">
              {domain.id}
            </span>
          </div>
          <p className="text-xs text-slate-400 mb-3">{domain.description}</p>
          <div className="mb-3">
            <span className="text-[10px] text-slate-600 uppercase tracking-wider">
              Beckn Domain Code
            </span>
            <p className="text-xs font-mono text-slate-500 mt-0.5">
              {domain.becknDomain}
            </p>
          </div>
          <div>
            <span className="text-[10px] text-slate-600 uppercase tracking-wider">
              Examples in Indonesia
            </span>
            <div className="flex flex-wrap gap-1.5 mt-1.5">
              {domain.examples.map((ex) => (
                <span
                  key={ex}
                  className="px-2 py-0.5 rounded-full text-[10px] bg-surface-900 text-slate-400 border border-slate-800"
                >
                  {ex}
                </span>
              ))}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

function CitiesTab() {
  return (
    <div>
      {/* Island map visualization */}
      <div className="rounded-xl border border-cyan-900/30 bg-surface-800/50 p-6 mb-6">
        <h3 className="text-sm font-semibold text-white mb-4">
          Network Coverage Map
        </h3>
        <div className="overflow-x-auto">
          <svg
            viewBox="0 0 100 80"
            className="w-full"
            style={{ maxHeight: "400px", minWidth: "500px" }}
          >
            {/* Simple Indonesia outline */}
            {/* Sumatra */}
            <ellipse cx="28" cy="48" rx="12" ry="18" fill="#0e7490" opacity="0.15" stroke="#0e7490" strokeWidth="0.3" />
            <text x="28" y="36" textAnchor="middle" fill="#0e7490" fontSize="2.5" opacity="0.5">Sumatra</text>

            {/* Java */}
            <ellipse cx="44" cy="64" rx="10" ry="4" fill="#06b6d4" opacity="0.15" stroke="#06b6d4" strokeWidth="0.3" />
            <text x="44" y="59" textAnchor="middle" fill="#06b6d4" fontSize="2.5" opacity="0.5">Java</text>

            {/* Kalimantan */}
            <ellipse cx="46" cy="48" rx="8" ry="10" fill="#22d3ee" opacity="0.15" stroke="#22d3ee" strokeWidth="0.3" />
            <text x="46" y="42" textAnchor="middle" fill="#22d3ee" fontSize="2.5" opacity="0.5">Kalimantan</text>

            {/* Sulawesi */}
            <ellipse cx="57" cy="50" rx="5" ry="10" fill="#67e8f9" opacity="0.15" stroke="#67e8f9" strokeWidth="0.3" />
            <text x="57" y="42" textAnchor="middle" fill="#67e8f9" fontSize="2.5" opacity="0.5">Sulawesi</text>

            {/* Bali & Nusa Tenggara */}
            <ellipse cx="53" cy="67" rx="6" ry="2.5" fill="#0891b2" opacity="0.15" stroke="#0891b2" strokeWidth="0.3" />
            <text x="53" y="72" textAnchor="middle" fill="#0891b2" fontSize="2" opacity="0.5">Bali &amp; NT</text>

            {/* Papua */}
            <ellipse cx="80" cy="52" rx="10" ry="8" fill="#155e75" opacity="0.15" stroke="#155e75" strokeWidth="0.3" />
            <text x="80" y="48" textAnchor="middle" fill="#155e75" fontSize="2.5" opacity="0.5">Papua</text>

            {/* City dots */}
            {CITIES.map((city) => (
              <g key={city.code}>
                {/* Pulse */}
                <circle cx={city.x} cy={city.y} r="1.2" fill="none" stroke="#00f0ff" strokeWidth="0.3" opacity="0.3">
                  <animate attributeName="r" values="1.2;2.5;1.2" dur="3s" repeatCount="indefinite" />
                  <animate attributeName="opacity" values="0.3;0;0.3" dur="3s" repeatCount="indefinite" />
                </circle>
                {/* Dot */}
                <circle cx={city.x} cy={city.y} r="0.8" fill="#00f0ff" opacity="0.8" />
                {/* Label */}
                <text
                  x={city.x}
                  y={city.y - 2}
                  textAnchor="middle"
                  fill="#e2e8f0"
                  fontSize="2"
                  fontWeight="500"
                >
                  {city.name}
                </text>
              </g>
            ))}
          </svg>
        </div>

        {/* Island legend */}
        <div className="flex flex-wrap gap-3 mt-4 justify-center">
          {ISLAND_GROUPS.map((ig) => {
            const count = CITIES.filter((c) => c.island === ig.name).length;
            return (
              <div key={ig.name} className="flex items-center gap-1.5 text-xs text-slate-400">
                <span
                  className="w-2.5 h-2.5 rounded-full"
                  style={{ backgroundColor: ig.color, opacity: 0.6 }}
                />
                {ig.name} ({count})
              </div>
            );
          })}
        </div>
      </div>

      {/* Cities table */}
      <div className="rounded-xl border border-cyan-900/30 bg-surface-800/50 overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-cyan-900/20 text-xs text-slate-500 uppercase tracking-wider">
              <th className="text-left px-5 py-3 font-medium">City</th>
              <th className="text-left px-3 py-3 font-medium">Code</th>
              <th className="text-left px-3 py-3 font-medium">Island Group</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-cyan-900/10">
            {CITIES.map((city) => (
              <tr key={city.code} className="hover:bg-white/[0.02]">
                <td className="px-5 py-3 text-slate-300 font-medium">
                  {city.name}
                </td>
                <td className="px-3 py-3 font-mono text-xs text-cyan-400/60">
                  {city.code}
                </td>
                <td className="px-3 py-3 text-xs text-slate-500">
                  {city.island}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function PaymentsTab() {
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
      {PAYMENT_METHODS.map((pm) => (
        <div
          key={pm.id}
          className="rounded-xl border border-cyan-900/30 bg-surface-800/50 p-5 card-hover"
        >
          <div className="flex items-start justify-between mb-2">
            <h3 className="text-sm font-semibold text-white">{pm.name}</h3>
            <div className="flex items-center gap-2">
              <span className="px-2 py-0.5 rounded text-[10px] bg-surface-900 text-slate-500 border border-slate-800">
                {pm.type}
              </span>
              <span className="px-2 py-0.5 rounded text-[10px] font-mono text-cyan-400 bg-cyan-400/10 border border-cyan-800/30">
                {pm.becknType}
              </span>
            </div>
          </div>
          <p className="text-xs text-slate-400">{pm.description}</p>
        </div>
      ))}
    </div>
  );
}

function LogisticsTab() {
  return (
    <div className="rounded-xl border border-cyan-900/30 bg-surface-800/50 overflow-hidden">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-cyan-900/20 text-xs text-slate-500 uppercase tracking-wider">
            <th className="text-left px-5 py-3 font-medium">Provider</th>
            <th className="text-left px-3 py-3 font-medium">Services</th>
            <th className="text-left px-3 py-3 font-medium">Coverage</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-cyan-900/10">
          {LOGISTICS_PROVIDERS.map((lp) => (
            <tr key={lp.id} className="hover:bg-white/[0.02]">
              <td className="px-5 py-3">
                <span className="text-slate-300 font-medium">{lp.name}</span>
              </td>
              <td className="px-3 py-3">
                <div className="flex flex-wrap gap-1.5">
                  {lp.services.map((s) => (
                    <span
                      key={s}
                      className="px-2 py-0.5 rounded text-[10px] bg-surface-900 text-slate-400 border border-slate-800"
                    >
                      {s}
                    </span>
                  ))}
                </div>
              </td>
              <td className="px-3 py-3 text-xs text-slate-500">
                {lp.coverage}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CategoriesTab() {
  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
      {CATEGORIES.map((cat) => (
        <div
          key={cat.id}
          className="rounded-xl border border-cyan-900/30 bg-surface-800/50 p-5 card-hover text-center"
        >
          <div className="w-12 h-12 rounded-xl bg-cyan-400/10 border border-cyan-800/30 flex items-center justify-center mx-auto mb-3">
            <span className="text-lg font-bold text-cyan-300">{cat.icon}</span>
          </div>
          <h3 className="text-sm font-semibold text-white">{cat.name}</h3>
          <p className="text-[10px] text-slate-600 mt-1 font-mono">{cat.id}</p>
        </div>
      ))}
    </div>
  );
}

export default function SpecsPage() {
  const [activeTab, setActiveTab] = useState<Tab>("domains");

  return (
    <div className="grid-bg min-h-screen">
      <div className="mx-auto max-w-7xl px-4 sm:px-6 py-8">
        {/* Header */}
        <div className="mb-8">
          <h1 className="text-2xl font-bold text-white tracking-tight">
            Indonesia Network Extension Specs
          </h1>
          <p className="text-sm text-slate-400 mt-1">
            Domain-specific configurations for the Jaringan Dagang network --
            cities, payment methods, logistics providers, and categories adapted
            for Indonesia
          </p>
        </div>

        {/* Tabs */}
        <div className="flex gap-1 mb-8 p-1 rounded-xl bg-surface-800/50 border border-cyan-900/30 overflow-x-auto">
          {TABS.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`px-4 py-2 rounded-lg text-sm font-medium transition-all whitespace-nowrap ${
                activeTab === tab.id
                  ? "bg-cyan-400/10 text-cyan-300 shadow-[0_0_10px_rgba(0,240,255,0.05)]"
                  : "text-slate-500 hover:text-slate-300 hover:bg-white/5"
              }`}
            >
              {tab.label}
            </button>
          ))}
        </div>

        {/* Tab content */}
        <div className="animate-fade-in">
          {activeTab === "domains" && <DomainsTab />}
          {activeTab === "cities" && <CitiesTab />}
          {activeTab === "payments" && <PaymentsTab />}
          {activeTab === "logistics" && <LogisticsTab />}
          {activeTab === "categories" && <CategoriesTab />}
        </div>
      </div>
    </div>
  );
}
