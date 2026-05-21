"use client";

import { useState } from "react";
import { registerSubscriber } from "@/lib/api";
import { Subscriber } from "@/lib/types";
import { CITIES, DOMAINS } from "@/lib/indonesia-data";

type FormData = {
  subscriber_id: string;
  subscriber_url: string;
  type: "BAP" | "BPP";
  domain: string;
  city: string;
  signing_public_key: string;
  encr_public_key: string;
};

const INITIAL_FORM: FormData = {
  subscriber_id: "",
  subscriber_url: "",
  type: "BPP",
  domain: "retail",
  city: "std:021",
  signing_public_key: "",
  encr_public_key: "",
};

function generateMockKeypair() {
  // Generate a random base64-like string for demo purposes
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  const randomKey = (prefix: string) => {
    let key = prefix;
    for (let i = 0; i < 32; i++) {
      key += chars.charAt(Math.floor(Math.random() * chars.length));
    }
    return key + "=";
  };
  return {
    signing: randomKey("MCowBQYDK2VwAyEA"),
    encryption: randomKey("MCowBQYDK2VuAyEA"),
  };
}

export default function RegisterParticipant() {
  const [form, setForm] = useState<FormData>(INITIAL_FORM);
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<{
    success: boolean;
    data?: Subscriber;
    error?: string;
  } | null>(null);

  const updateField = <K extends keyof FormData>(
    key: K,
    value: FormData[K]
  ) => {
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  const handleGenerateKeys = () => {
    const keys = generateMockKeypair();
    setForm((prev) => ({
      ...prev,
      signing_public_key: keys.signing,
      encr_public_key: keys.encryption,
    }));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setResult(null);

    const res = await registerSubscriber(form);
    setResult(res);
    setSubmitting(false);

    if (res.success) {
      setForm(INITIAL_FORM);
    }
  };

  const isValid =
    form.subscriber_id.trim() &&
    form.subscriber_url.trim() &&
    form.signing_public_key.trim() &&
    form.encr_public_key.trim();

  return (
    <div className="grid-bg min-h-screen">
      <div className="mx-auto max-w-3xl px-4 sm:px-6 py-8">
        {/* Header */}
        <div className="mb-8">
          <h1 className="text-2xl font-bold text-white tracking-tight">
            Register Participant
          </h1>
          <p className="text-sm text-slate-400 mt-1">
            Join the Jaringan Dagang network by registering as a BAP or BPP
          </p>
        </div>

        {/* Info banner */}
        <div className="rounded-xl border border-cyan-900/30 bg-cyan-400/5 p-4 mb-8 flex items-start gap-3">
          <svg className="w-5 h-5 text-cyan-400 flex-shrink-0 mt-0.5" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
            <circle cx="10" cy="10" r="8" />
            <line x1="10" y1="6" x2="10" y2="11" />
            <circle cx="10" cy="14" r="0.5" fill="currentColor" />
          </svg>
          <div>
            <p className="text-sm text-cyan-200">
              Registering adds your application to the Beckn network registry.
            </p>
            <p className="text-xs text-slate-500 mt-1">
              BAPs (Buyer Application Platforms) are consumer-facing apps. BPPs
              (Beckn Provider Platforms) are seller/provider systems. The
              Registry verifies your keys and makes you discoverable to other
              network participants.
            </p>
          </div>
        </div>

        {/* Form */}
        <form onSubmit={handleSubmit}>
          <div className="rounded-xl border border-cyan-900/30 bg-surface-800/50 overflow-hidden">
            {/* Basic Info */}
            <div className="px-6 py-5 border-b border-cyan-900/20">
              <h2 className="text-sm font-semibold text-white mb-4">
                Basic Information
              </h2>
              <div className="space-y-4">
                <div>
                  <label className="block text-xs font-medium text-slate-400 mb-1.5">
                    Subscriber ID
                  </label>
                  <input
                    type="text"
                    value={form.subscriber_id}
                    onChange={(e) =>
                      updateField("subscriber_id", e.target.value)
                    }
                    placeholder="my-app.example.com"
                    className="w-full px-4 py-2.5 rounded-lg bg-surface-900 border border-cyan-900/30 text-sm text-white placeholder-slate-600 focus:outline-none focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500/20 font-mono"
                  />
                  <p className="text-[10px] text-slate-600 mt-1">
                    Unique identifier for your application on the network
                  </p>
                </div>

                <div>
                  <label className="block text-xs font-medium text-slate-400 mb-1.5">
                    Subscriber URL
                  </label>
                  <input
                    type="url"
                    value={form.subscriber_url}
                    onChange={(e) =>
                      updateField("subscriber_url", e.target.value)
                    }
                    placeholder="https://api.my-app.com"
                    className="w-full px-4 py-2.5 rounded-lg bg-surface-900 border border-cyan-900/30 text-sm text-white placeholder-slate-600 focus:outline-none focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500/20 font-mono"
                  />
                  <p className="text-[10px] text-slate-600 mt-1">
                    The base URL where your Beckn API endpoints are hosted
                  </p>
                </div>
              </div>
            </div>

            {/* Type */}
            <div className="px-6 py-5 border-b border-cyan-900/20">
              <h2 className="text-sm font-semibold text-white mb-4">
                Participant Type
              </h2>
              <div className="grid grid-cols-2 gap-3">
                {(["BAP", "BPP"] as const).map((type) => (
                  <button
                    key={type}
                    type="button"
                    onClick={() => updateField("type", type)}
                    className={`relative p-4 rounded-xl border text-left transition-all ${
                      form.type === type
                        ? type === "BAP"
                          ? "border-cyan-400 bg-cyan-400/5 shadow-[0_0_15px_rgba(0,240,255,0.1)]"
                          : "border-purple-400 bg-purple-400/5 shadow-[0_0_15px_rgba(168,85,247,0.1)]"
                        : "border-slate-800 bg-surface-900 hover:border-slate-700"
                    }`}
                  >
                    {form.type === type && (
                      <div
                        className={`absolute top-3 right-3 w-5 h-5 rounded-full flex items-center justify-center ${
                          type === "BAP" ? "bg-cyan-400" : "bg-purple-400"
                        }`}
                      >
                        <svg className="w-3 h-3 text-surface-900" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="2.5">
                          <path d="M2 6 L5 9 L10 3" />
                        </svg>
                      </div>
                    )}
                    <span
                      className={`text-lg font-bold font-mono ${
                        form.type === type
                          ? type === "BAP"
                            ? "text-cyan-300"
                            : "text-purple-300"
                          : "text-slate-500"
                      }`}
                    >
                      {type}
                    </span>
                    <p
                      className={`text-xs mt-1 ${
                        form.type === type ? "text-slate-300" : "text-slate-600"
                      }`}
                    >
                      {type === "BAP"
                        ? "Buyer Application Platform"
                        : "Beckn Provider Platform"}
                    </p>
                    <p className="text-[10px] text-slate-600 mt-2">
                      {type === "BAP"
                        ? "Consumer-facing apps that search, order, and track"
                        : "Seller/provider systems that fulfill orders"}
                    </p>
                  </button>
                ))}
              </div>
            </div>

            {/* Domain & City */}
            <div className="px-6 py-5 border-b border-cyan-900/20">
              <h2 className="text-sm font-semibold text-white mb-4">
                Network Configuration
              </h2>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <label className="block text-xs font-medium text-slate-400 mb-1.5">
                    Domain
                  </label>
                  <select
                    value={form.domain}
                    onChange={(e) => updateField("domain", e.target.value)}
                    className="w-full px-4 py-2.5 rounded-lg bg-surface-900 border border-cyan-900/30 text-sm text-white focus:outline-none focus:border-cyan-500"
                  >
                    {DOMAINS.map((d) => (
                      <option key={d.id} value={d.id}>
                        {d.name}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-400 mb-1.5">
                    City
                  </label>
                  <select
                    value={form.city}
                    onChange={(e) => updateField("city", e.target.value)}
                    className="w-full px-4 py-2.5 rounded-lg bg-surface-900 border border-cyan-900/30 text-sm text-white focus:outline-none focus:border-cyan-500"
                  >
                    {CITIES.map((c) => (
                      <option key={c.code} value={c.code}>
                        {c.name} ({c.code})
                      </option>
                    ))}
                  </select>
                </div>
              </div>
            </div>

            {/* Keys */}
            <div className="px-6 py-5 border-b border-cyan-900/20">
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-sm font-semibold text-white">
                  Cryptographic Keys
                </h2>
                <button
                  type="button"
                  onClick={handleGenerateKeys}
                  className="px-3 py-1.5 rounded-lg border border-cyan-900/30 text-xs font-medium text-cyan-400 hover:bg-cyan-400/5 transition-colors"
                >
                  Generate Keypair (Demo)
                </button>
              </div>
              <div className="space-y-4">
                <div>
                  <label className="block text-xs font-medium text-slate-400 mb-1.5">
                    Signing Public Key (Ed25519)
                  </label>
                  <textarea
                    value={form.signing_public_key}
                    onChange={(e) =>
                      updateField("signing_public_key", e.target.value)
                    }
                    placeholder="MCowBQYDK2VwAyEA..."
                    rows={2}
                    className="w-full px-4 py-2.5 rounded-lg bg-surface-900 border border-cyan-900/30 text-sm text-white placeholder-slate-600 focus:outline-none focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500/20 font-mono resize-none"
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-400 mb-1.5">
                    Encryption Public Key (X25519)
                  </label>
                  <textarea
                    value={form.encr_public_key}
                    onChange={(e) =>
                      updateField("encr_public_key", e.target.value)
                    }
                    placeholder="MCowBQYDK2VuAyEA..."
                    rows={2}
                    className="w-full px-4 py-2.5 rounded-lg bg-surface-900 border border-cyan-900/30 text-sm text-white placeholder-slate-600 focus:outline-none focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500/20 font-mono resize-none"
                  />
                </div>
              </div>
            </div>

            {/* Submit */}
            <div className="px-6 py-5 bg-surface-700/20 flex items-center justify-between">
              <p className="text-xs text-slate-600">
                Submits to POST /subscribe on the Registry
              </p>
              <button
                type="submit"
                disabled={!isValid || submitting}
                className="px-6 py-2.5 rounded-lg bg-gradient-to-r from-cyan-500 to-teal-500 text-sm font-semibold text-surface-900 hover:from-cyan-400 hover:to-teal-400 disabled:opacity-40 disabled:cursor-not-allowed transition-all"
              >
                {submitting ? (
                  <span className="flex items-center gap-2">
                    <span className="w-4 h-4 border-2 border-surface-900 border-t-transparent rounded-full animate-spin" />
                    Registering...
                  </span>
                ) : (
                  "Register on Network"
                )}
              </button>
            </div>
          </div>
        </form>

        {/* Result */}
        {result && (
          <div
            className={`mt-6 rounded-xl border p-5 animate-slide-in ${
              result.success
                ? "border-emerald-700/30 bg-emerald-400/5"
                : "border-red-700/30 bg-red-400/5"
            }`}
          >
            {result.success ? (
              <div>
                <div className="flex items-center gap-2 mb-3">
                  <div className="w-6 h-6 rounded-full bg-emerald-500/20 flex items-center justify-center">
                    <svg className="w-4 h-4 text-emerald-400" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2">
                      <path d="M3 8 L6 11 L13 4" />
                    </svg>
                  </div>
                  <span className="text-sm font-semibold text-emerald-300">
                    Successfully Registered
                  </span>
                </div>
                <p className="text-xs text-slate-400">
                  Your application has been submitted to the registry. Status
                  will change to SUBSCRIBED once the registry verifies your
                  credentials.
                </p>
                {result.data && (
                  <div className="mt-3 p-3 rounded-lg bg-surface-900/50">
                    <pre className="text-[11px] font-mono text-slate-400 overflow-x-auto">
                      {JSON.stringify(result.data, null, 2)}
                    </pre>
                  </div>
                )}
              </div>
            ) : (
              <div>
                <div className="flex items-center gap-2 mb-2">
                  <div className="w-6 h-6 rounded-full bg-red-500/20 flex items-center justify-center">
                    <span className="text-red-400 text-sm font-bold">!</span>
                  </div>
                  <span className="text-sm font-semibold text-red-300">
                    Registration Failed
                  </span>
                </div>
                <p className="text-xs text-red-400/80">
                  {result.error ||
                    "Could not connect to the registry. Make sure the registry service is running at localhost:3030."}
                </p>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
