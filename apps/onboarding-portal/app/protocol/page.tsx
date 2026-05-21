"use client";

import { useState } from "react";

interface BecknAction {
  name: string;
  callback: string;
  description: string;
  sender: "BAP" | "BPP";
  receiver: "BPP" | "BAP";
  gatewayInvolved: boolean;
  category: "discovery" | "order" | "fulfillment" | "post-fulfillment";
  requestExample: object;
  responseExample: object;
}

const ACTIONS: BecknAction[] = [
  {
    name: "search",
    callback: "on_search",
    description:
      "Discover products/services across the network. The only action that goes through the Gateway for multicast discovery.",
    sender: "BAP",
    receiver: "BPP",
    gatewayInvolved: true,
    category: "discovery",
    requestExample: {
      context: {
        domain: "retail",
        action: "search",
        country: "IDN",
        city: "std:021",
        bap_id: "buyer-app.example.com",
        bap_uri: "https://buyer-app.example.com/beckn",
        transaction_id: "txn_abc123",
        message_id: "msg_001",
        timestamp: "2026-04-05T10:00:00Z",
      },
      message: {
        intent: {
          item: { descriptor: { name: "indomie" } },
          fulfillment: { type: "Delivery" },
        },
      },
    },
    responseExample: {
      context: { action: "on_search", bpp_id: "seller.example.com" },
      message: {
        catalog: {
          "bpp/descriptor": { name: "Warung Pintar" },
          "bpp/providers": [
            {
              id: "provider_1",
              items: [
                {
                  id: "item_1",
                  descriptor: { name: "Indomie Mi Goreng" },
                  price: { currency: "IDR", value: "3500" },
                },
              ],
            },
          ],
        },
      },
    },
  },
  {
    name: "select",
    callback: "on_select",
    description:
      "Select specific items from a provider's catalog to get a quote with pricing and availability.",
    sender: "BAP",
    receiver: "BPP",
    gatewayInvolved: false,
    category: "order",
    requestExample: {
      context: { action: "select", bpp_id: "seller.example.com" },
      message: {
        order: {
          provider: { id: "provider_1" },
          items: [{ id: "item_1", quantity: { count: 5 } }],
        },
      },
    },
    responseExample: {
      context: { action: "on_select" },
      message: {
        order: {
          provider: { id: "provider_1" },
          items: [{ id: "item_1", quantity: { count: 5 } }],
          quote: {
            price: { currency: "IDR", value: "17500" },
            breakup: [
              { title: "Item price", price: { value: "17500" } },
              { title: "Delivery", price: { value: "8000" } },
            ],
          },
        },
      },
    },
  },
  {
    name: "init",
    callback: "on_init",
    description:
      "Initialize an order with billing details and delivery address. Provider returns payment terms.",
    sender: "BAP",
    receiver: "BPP",
    gatewayInvolved: false,
    category: "order",
    requestExample: {
      context: { action: "init" },
      message: {
        order: {
          provider: { id: "provider_1" },
          items: [{ id: "item_1", quantity: { count: 5 } }],
          billing: { name: "Budi Santoso", phone: "+62812345678" },
          fulfillment: {
            end: {
              location: { address: { door: "Jl. Sudirman No. 1, Jakarta" } },
            },
          },
        },
      },
    },
    responseExample: {
      context: { action: "on_init" },
      message: {
        order: {
          provider: { id: "provider_1" },
          payment: {
            type: "ON-ORDER",
            uri: "https://payment.example.com/qris",
            params: { method: "QRIS", amount: "25500" },
          },
        },
      },
    },
  },
  {
    name: "confirm",
    callback: "on_confirm",
    description:
      "Confirm the order after payment. This creates the order on the provider side.",
    sender: "BAP",
    receiver: "BPP",
    gatewayInvolved: false,
    category: "order",
    requestExample: {
      context: { action: "confirm" },
      message: {
        order: {
          provider: { id: "provider_1" },
          items: [{ id: "item_1", quantity: { count: 5 } }],
          payment: { status: "PAID", transaction_id: "qris_txn_456" },
        },
      },
    },
    responseExample: {
      context: { action: "on_confirm" },
      message: {
        order: {
          id: "order_789",
          state: "Accepted",
          provider: { id: "provider_1" },
          items: [{ id: "item_1", quantity: { count: 5 } }],
        },
      },
    },
  },
  {
    name: "status",
    callback: "on_status",
    description:
      "Check the current status of an active order.",
    sender: "BAP",
    receiver: "BPP",
    gatewayInvolved: false,
    category: "fulfillment",
    requestExample: {
      context: { action: "status" },
      message: { order_id: "order_789" },
    },
    responseExample: {
      context: { action: "on_status" },
      message: {
        order: {
          id: "order_789",
          state: "In-progress",
          fulfillment: {
            state: { descriptor: { name: "Order picked up" } },
            agent: { name: "Driver Andi" },
          },
        },
      },
    },
  },
  {
    name: "track",
    callback: "on_track",
    description:
      "Get real-time tracking information for an order in fulfillment.",
    sender: "BAP",
    receiver: "BPP",
    gatewayInvolved: false,
    category: "fulfillment",
    requestExample: {
      context: { action: "track" },
      message: { order_id: "order_789" },
    },
    responseExample: {
      context: { action: "on_track" },
      message: {
        tracking: {
          url: "https://track.logistics.example.com/order_789",
          status: "in-transit",
          location: { gps: "-6.2088,106.8456" },
        },
      },
    },
  },
  {
    name: "cancel",
    callback: "on_cancel",
    description:
      "Cancel an active order. Subject to the provider's cancellation policy.",
    sender: "BAP",
    receiver: "BPP",
    gatewayInvolved: false,
    category: "post-fulfillment",
    requestExample: {
      context: { action: "cancel" },
      message: {
        order_id: "order_789",
        cancellation_reason_id: "buyer_changed_mind",
      },
    },
    responseExample: {
      context: { action: "on_cancel" },
      message: {
        order: {
          id: "order_789",
          state: "Cancelled",
          tags: { refund_status: "initiated" },
        },
      },
    },
  },
  {
    name: "update",
    callback: "on_update",
    description:
      "Update an active order (e.g., change delivery address or modify items).",
    sender: "BAP",
    receiver: "BPP",
    gatewayInvolved: false,
    category: "post-fulfillment",
    requestExample: {
      context: { action: "update" },
      message: {
        order: {
          id: "order_789",
          fulfillment: {
            end: {
              location: { address: { door: "Jl. Thamrin No. 5, Jakarta" } },
            },
          },
        },
      },
    },
    responseExample: {
      context: { action: "on_update" },
      message: { order: { id: "order_789", state: "Accepted" } },
    },
  },
  {
    name: "rating",
    callback: "on_rating",
    description:
      "Rate an order, item, fulfillment, or provider after completion.",
    sender: "BAP",
    receiver: "BPP",
    gatewayInvolved: false,
    category: "post-fulfillment",
    requestExample: {
      context: { action: "rating" },
      message: {
        ratings: [
          {
            id: "order_789",
            rating_category: "order",
            value: "4",
          },
        ],
      },
    },
    responseExample: {
      context: { action: "on_rating" },
      message: { feedback_form: { url: "https://feedback.example.com/form" } },
    },
  },
  {
    name: "support",
    callback: "on_support",
    description:
      "Request support contact information for an order or provider.",
    sender: "BAP",
    receiver: "BPP",
    gatewayInvolved: false,
    category: "post-fulfillment",
    requestExample: {
      context: { action: "support" },
      message: { ref_id: "order_789" },
    },
    responseExample: {
      context: { action: "on_support" },
      message: {
        phone: "+62215551234",
        email: "support@seller.example.com",
        uri: "https://wa.me/62215551234",
      },
    },
  },
];

const CATEGORIES = [
  { id: "discovery", label: "Discovery", color: "cyan" },
  { id: "order", label: "Order", color: "blue" },
  { id: "fulfillment", label: "Fulfillment", color: "purple" },
  { id: "post-fulfillment", label: "Post-Fulfillment", color: "teal" },
];

const PAYMENT_MAPPING = [
  { beckn: "ON-ORDER", indo: "QRIS, GoPay, OVO, DANA", description: "Payment collected at order time" },
  { beckn: "PRE-FULFILLMENT", indo: "Virtual Account (BCA/BNI/BRI)", description: "Payment before fulfillment begins" },
  { beckn: "ON-FULFILLMENT", indo: "Cash on Delivery (COD)", description: "Payment at time of delivery" },
  { beckn: "POST-FULFILLMENT", indo: "Credit / Pay Later (Kredivo)", description: "Payment after service completion" },
];

function JsonViewer({ data }: { data: object }) {
  const json = JSON.stringify(data, null, 2);
  // Simple syntax highlighting
  const highlighted = json
    .replace(/"([^"]+)":/g, '<span class="json-key">"$1"</span>:')
    .replace(/: "([^"]+)"/g, ': <span class="json-string">"$1"</span>')
    .replace(/: (\d+)/g, ': <span class="json-number">$1</span>')
    .replace(/: (true|false)/g, ': <span class="json-bool">$1</span>');

  return (
    <div className="json-block p-4 overflow-x-auto">
      <pre dangerouslySetInnerHTML={{ __html: highlighted }} />
    </div>
  );
}

export default function ProtocolPage() {
  const [selectedAction, setSelectedAction] = useState<string>("search");
  const [showRequest, setShowRequest] = useState(true);

  const action = ACTIONS.find((a) => a.name === selectedAction)!;

  return (
    <div className="grid-bg min-h-screen">
      <div className="mx-auto max-w-7xl px-4 sm:px-6 py-8">
        {/* Header */}
        <div className="mb-8">
          <h1 className="text-2xl font-bold text-white tracking-tight">
            Beckn Protocol Flow
          </h1>
          <p className="text-sm text-slate-400 mt-1">
            Interactive visualization of the 10 Beckn API actions and how they
            flow through the network
          </p>
        </div>

        {/* Visual Flow Diagram */}
        <div className="rounded-xl border border-cyan-900/30 bg-surface-800/50 p-6 mb-8">
          <h2 className="text-sm font-semibold text-white mb-4">
            Protocol Flow Diagram
          </h2>

          <div className="overflow-x-auto">
            <svg viewBox="0 0 800 200" className="w-full" style={{ minWidth: "600px", maxHeight: "200px" }}>
              <defs>
                <filter id="flow-glow">
                  <feGaussianBlur stdDeviation="3" result="coloredBlur" />
                  <feMerge>
                    <feMergeNode in="coloredBlur" />
                    <feMergeNode in="SourceGraphic" />
                  </feMerge>
                </filter>
              </defs>

              {/* BAP */}
              <rect x="20" y="60" width="140" height="80" rx="8" fill="#0f1520" stroke="#22d3ee" strokeWidth="1.5" />
              <text x="90" y="95" textAnchor="middle" fill="#22d3ee" fontSize="14" fontWeight="700" fontFamily="JetBrains Mono, monospace">BAP</text>
              <text x="90" y="115" textAnchor="middle" fill="#94a3b8" fontSize="9">Buyer App</text>

              {/* Gateway */}
              <rect x="280" y="60" width="140" height="80" rx="8" fill="#0f1520" stroke="#00d4aa" strokeWidth="1.5" />
              <text x="350" y="95" textAnchor="middle" fill="#00d4aa" fontSize="14" fontWeight="700" fontFamily="JetBrains Mono, monospace">GATEWAY</text>
              <text x="350" y="115" textAnchor="middle" fill="#94a3b8" fontSize="9">Multicast (search only)</text>

              {/* BPP */}
              <rect x="540" y="60" width="140" height="80" rx="8" fill="#0f1520" stroke="#a855f7" strokeWidth="1.5" />
              <text x="610" y="95" textAnchor="middle" fill="#a855f7" fontSize="14" fontWeight="700" fontFamily="JetBrains Mono, monospace">BPP</text>
              <text x="610" y="115" textAnchor="middle" fill="#94a3b8" fontSize="9">Provider Platform</text>

              {/* Registry */}
              <rect x="280" y="10" width="140" height="36" rx="6" fill="#0f1520" stroke="#00f0ff" strokeWidth="1" opacity="0.6" />
              <text x="350" y="33" textAnchor="middle" fill="#00f0ff" fontSize="10" fontWeight="600" fontFamily="JetBrains Mono, monospace">REGISTRY</text>

              {/* Registry connections */}
              <line x1="350" y1="46" x2="350" y2="60" stroke="#00f0ff" strokeWidth="0.5" strokeDasharray="3 3" opacity="0.3" />

              {/* Search flow (through gateway) */}
              <line x1="160" y1="85" x2="280" y2="85" stroke="#22d3ee" strokeWidth="1.5" strokeDasharray="6 3" opacity="0.5">
                <animate attributeName="stroke-dashoffset" values="18;0" dur="1.5s" repeatCount="indefinite" />
              </line>
              <text x="220" y="78" textAnchor="middle" fill="#22d3ee" fontSize="8" fontFamily="JetBrains Mono, monospace">search</text>

              <line x1="420" y1="85" x2="540" y2="85" stroke="#00d4aa" strokeWidth="1.5" strokeDasharray="6 3" opacity="0.5">
                <animate attributeName="stroke-dashoffset" values="18;0" dur="1.5s" repeatCount="indefinite" />
              </line>
              <text x="480" y="78" textAnchor="middle" fill="#00d4aa" fontSize="8" fontFamily="JetBrains Mono, monospace">multicast</text>

              {/* Callback flow */}
              <line x1="540" y1="115" x2="160" y2="115" stroke="#a855f7" strokeWidth="1.5" strokeDasharray="6 3" opacity="0.5">
                <animate attributeName="stroke-dashoffset" values="0;18" dur="1.5s" repeatCount="indefinite" />
              </line>
              <text x="350" y="130" textAnchor="middle" fill="#a855f7" fontSize="8" fontFamily="JetBrains Mono, monospace">on_search / on_select / on_init / on_confirm / ...</text>

              {/* Direct flow label */}
              <line x1="160" y1="155" x2="540" y2="155" stroke="#64748b" strokeWidth="0.5" strokeDasharray="2 4" opacity="0.3" />
              <text x="350" y="170" textAnchor="middle" fill="#64748b" fontSize="8">select, init, confirm, status, track, cancel, update, rating, support (direct BAP to BPP)</text>

              {/* Highlight selected action */}
              {action.gatewayInvolved ? (
                <rect x="15" y="55" width="670" height="90" rx="10" fill="none" stroke="#00f0ff" strokeWidth="0.5" strokeDasharray="4 4" opacity="0.2" />
              ) : (
                <g>
                  <line x1="160" y1="100" x2="540" y2="100" stroke={`${action.sender === "BAP" ? "#22d3ee" : "#a855f7"}`} strokeWidth="2" opacity="0.3" filter="url(#flow-glow)" />
                </g>
              )}
            </svg>
          </div>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-[280px_1fr] gap-6">
          {/* Action list sidebar */}
          <div className="rounded-xl border border-cyan-900/30 bg-surface-800/50 overflow-hidden">
            <div className="px-4 py-3 border-b border-cyan-900/20">
              <h3 className="text-xs font-semibold text-slate-400 uppercase tracking-wider">
                10 Beckn Actions
              </h3>
            </div>
            <div className="p-2">
              {CATEGORIES.map((cat) => (
                <div key={cat.id} className="mb-2">
                  <div className="px-3 py-1.5 text-[10px] font-bold text-slate-600 uppercase tracking-wider">
                    {cat.label}
                  </div>
                  {ACTIONS.filter((a) => a.category === cat.id).map((a) => (
                    <button
                      key={a.name}
                      onClick={() => setSelectedAction(a.name)}
                      className={`w-full text-left px-3 py-2 rounded-lg text-sm transition-all flex items-center justify-between ${
                        selectedAction === a.name
                          ? "bg-cyan-400/10 text-cyan-300"
                          : "text-slate-400 hover:text-slate-200 hover:bg-white/5"
                      }`}
                    >
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-xs font-bold">
                          /{a.name}
                        </span>
                        {a.gatewayInvolved && (
                          <span className="px-1.5 py-0.5 rounded text-[9px] font-bold bg-teal-400/10 text-teal-400 border border-teal-700/30">
                            GW
                          </span>
                        )}
                      </div>
                      <svg
                        className={`w-3 h-3 ${selectedAction === a.name ? "text-cyan-400" : "text-transparent"}`}
                        viewBox="0 0 12 12"
                        fill="currentColor"
                      >
                        <path d="M4 2 L9 6 L4 10 Z" />
                      </svg>
                    </button>
                  ))}
                </div>
              ))}
            </div>
          </div>

          {/* Action detail */}
          <div className="space-y-6">
            <div className="rounded-xl border border-cyan-900/30 bg-surface-800/50 p-6">
              <div className="flex items-start justify-between mb-4">
                <div>
                  <div className="flex items-center gap-3 mb-1">
                    <h2 className="text-xl font-bold text-white font-mono">
                      /{action.name}
                    </h2>
                    <svg className="w-4 h-4 text-slate-600" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
                      <line x1="3" y1="8" x2="13" y2="8" />
                      <path d="M9 4 L13 8 L9 12" />
                    </svg>
                    <span className="text-sm font-mono text-slate-500">
                      /{action.callback}
                    </span>
                  </div>
                  <p className="text-sm text-slate-400">{action.description}</p>
                </div>
              </div>

              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-6">
                <div className="rounded-lg bg-surface-900 p-3">
                  <span className="text-[10px] text-slate-600 uppercase tracking-wider">
                    Sender
                  </span>
                  <div className={`text-sm font-mono font-bold mt-1 ${action.sender === "BAP" ? "text-cyan-300" : "text-purple-300"}`}>
                    {action.sender}
                  </div>
                </div>
                <div className="rounded-lg bg-surface-900 p-3">
                  <span className="text-[10px] text-slate-600 uppercase tracking-wider">
                    Receiver
                  </span>
                  <div className={`text-sm font-mono font-bold mt-1 ${action.receiver === "BAP" ? "text-cyan-300" : "text-purple-300"}`}>
                    {action.receiver}
                  </div>
                </div>
                <div className="rounded-lg bg-surface-900 p-3">
                  <span className="text-[10px] text-slate-600 uppercase tracking-wider">
                    Gateway
                  </span>
                  <div className={`text-sm font-bold mt-1 ${action.gatewayInvolved ? "text-teal-300" : "text-slate-600"}`}>
                    {action.gatewayInvolved ? "Yes" : "No"}
                  </div>
                </div>
                <div className="rounded-lg bg-surface-900 p-3">
                  <span className="text-[10px] text-slate-600 uppercase tracking-wider">
                    Category
                  </span>
                  <div className="text-sm font-medium mt-1 text-slate-300 capitalize">
                    {action.category}
                  </div>
                </div>
              </div>

              {/* Request / Response toggle */}
              <div className="flex gap-2 mb-4">
                <button
                  onClick={() => setShowRequest(true)}
                  className={`px-4 py-1.5 rounded-lg text-xs font-medium transition-all ${
                    showRequest
                      ? "bg-cyan-400/10 text-cyan-300 border border-cyan-700/30"
                      : "text-slate-500 hover:text-slate-300"
                  }`}
                >
                  Request (/{action.name})
                </button>
                <button
                  onClick={() => setShowRequest(false)}
                  className={`px-4 py-1.5 rounded-lg text-xs font-medium transition-all ${
                    !showRequest
                      ? "bg-purple-400/10 text-purple-300 border border-purple-700/30"
                      : "text-slate-500 hover:text-slate-300"
                  }`}
                >
                  Callback (/{action.callback})
                </button>
              </div>

              <JsonViewer
                data={
                  showRequest
                    ? action.requestExample
                    : action.responseExample
                }
              />
            </div>

            {/* Indonesia Payment Mapping */}
            <div className="rounded-xl border border-cyan-900/30 bg-surface-800/50 p-6">
              <h3 className="text-sm font-semibold text-white mb-1">
                Indonesia Payment Mapping
              </h3>
              <p className="text-xs text-slate-500 mb-4">
                How Indonesian payment methods map to Beckn payment types
              </p>
              <div className="space-y-2">
                {PAYMENT_MAPPING.map((pm) => (
                  <div
                    key={pm.beckn}
                    className="flex items-center gap-4 p-3 rounded-lg bg-surface-900/50 border border-slate-800/50"
                  >
                    <span className="px-2 py-1 rounded text-[10px] font-mono font-bold bg-cyan-400/10 text-cyan-300 border border-cyan-800/30 whitespace-nowrap">
                      {pm.beckn}
                    </span>
                    <div className="flex-1">
                      <span className="text-sm text-white">{pm.indo}</span>
                      <p className="text-[10px] text-slate-600">
                        {pm.description}
                      </p>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
