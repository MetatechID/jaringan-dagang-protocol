"use client";

import { Subscriber } from "@/lib/types";
import { CITY_NAMES } from "@/lib/indonesia-data";

interface Props {
  subscribers: Subscriber[];
}

/** Extract a display-friendly name from a subscriber_id.
 *
 * Two registration conventions are in use:
 *   - brand-first: "matchamu.jaringan-dagang.id" → "Matchamu"
 *   - role-first:  "bpp.antarestar.local"        → "Antarestar"
 */
function displayName(subscriberId: string): string {
  const segments = subscriberId.split(".");
  const head = segments[0]?.toLowerCase();
  const brand = head === "bap" || head === "bpp" ? segments[1] || segments[0] : segments[0];
  return brand
    .split("-")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

function NodeCard({
  label,
  type,
  status,
  domain,
  city,
  x,
  y,
  color,
}: {
  label: string;
  type: string;
  status: string;
  domain?: string;
  city?: string;
  x: number;
  y: number;
  color: string;
}) {
  const isActive = status === "SUBSCRIBED";
  const cityName = city ? CITY_NAMES[city] || city : "";

  return (
    <g transform={`translate(${x}, ${y})`}>
      {/* Pulse ring for active nodes */}
      {isActive && (
        <circle r="32" fill="none" stroke={color} strokeWidth="1" opacity="0.3">
          <animate
            attributeName="r"
            values="32;44;32"
            dur="3s"
            repeatCount="indefinite"
          />
          <animate
            attributeName="opacity"
            values="0.3;0;0.3"
            dur="3s"
            repeatCount="indefinite"
          />
        </circle>
      )}

      {/* Node background */}
      <rect
        x="-64"
        y="-32"
        width="128"
        height="64"
        rx="8"
        fill="#0f1520"
        stroke={color}
        strokeWidth={isActive ? "1.5" : "0.5"}
        opacity={isActive ? 1 : 0.6}
      />

      {/* Inner glow */}
      <rect
        x="-62"
        y="-30"
        width="124"
        height="60"
        rx="6"
        fill="none"
        stroke={color}
        strokeWidth="0.5"
        opacity="0.15"
      />

      {/* Type badge */}
      <rect
        x="-58"
        y="-26"
        width="36"
        height="16"
        rx="4"
        fill={color}
        opacity="0.15"
      />
      <text
        x="-40"
        y="-15"
        textAnchor="middle"
        fill={color}
        fontSize="9"
        fontWeight="700"
        fontFamily="JetBrains Mono, monospace"
      >
        {type}
      </text>

      {/* Status dot */}
      <circle
        cx="50"
        cy="-20"
        r="4"
        fill={isActive ? "#00ff88" : "#facc15"}
      >
        {isActive && (
          <animate
            attributeName="opacity"
            values="1;0.5;1"
            dur="2s"
            repeatCount="indefinite"
          />
        )}
      </circle>

      {/* Name */}
      <text
        x="0"
        y="2"
        textAnchor="middle"
        fill="#e2e8f0"
        fontSize="10"
        fontWeight="600"
      >
        {label.length > 18 ? label.slice(0, 16) + ".." : label}
      </text>

      {/* Domain + City */}
      <text
        x="0"
        y="18"
        textAnchor="middle"
        fill="#94a3b8"
        fontSize="8"
      >
        {domain || ""} {cityName ? `- ${cityName}` : ""}
      </text>
    </g>
  );
}

export function NetworkTopology({ subscribers }: Props) {
  const baps = subscribers.filter((s) => s.type === "BAP");
  const bpps = subscribers.filter((s) => s.type === "BPP");

  const width = 900;
  // Dynamically size height based on number of nodes
  const maxNodes = Math.max(baps.length, bpps.length, 1);
  const nodeSpacing = 100;
  const height = Math.max(420, maxNodes * nodeSpacing + 200);
  const centerX = width / 2;
  const centerY = height / 2;

  // Node positions
  const registryPos = { x: centerX, y: centerY - 60 };
  const gatewayPos = { x: centerX, y: centerY + 60 };

  // Center BAP/BPP nodes vertically around the center
  const bapTotalHeight = (baps.length - 1) * nodeSpacing;
  const bapStartY = centerY - bapTotalHeight / 2;
  const bapX = 130;

  const bppTotalHeight = (bpps.length - 1) * nodeSpacing;
  const bppStartY = centerY - bppTotalHeight / 2;
  const bppX = width - 130;

  return (
    <div className="relative w-full overflow-hidden rounded-xl border border-cyan-900/30 bg-surface-800/50">
      {/* Corner labels */}
      <div className="absolute top-3 left-4 text-[10px] font-mono text-cyan-600/50 uppercase tracking-widest">
        Buyer Apps (BAP)
      </div>
      <div className="absolute top-3 right-4 text-[10px] font-mono text-purple-400/50 uppercase tracking-widest">
        Provider Platforms (BPP)
      </div>

      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="w-full h-auto"
        style={{ maxHeight: `${height}px` }}
      >
        <defs>
          {/* Glow filter */}
          <filter id="glow">
            <feGaussianBlur stdDeviation="3" result="coloredBlur" />
            <feMerge>
              <feMergeNode in="coloredBlur" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>

          <filter id="glow-strong">
            <feGaussianBlur stdDeviation="6" result="coloredBlur" />
            <feMerge>
              <feMergeNode in="coloredBlur" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>

          {/* Gradient for connections */}
          <linearGradient id="grad-cyan-left" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor="#22d3ee" stopOpacity="0.6" />
            <stop offset="100%" stopColor="#22d3ee" stopOpacity="0.15" />
          </linearGradient>
          <linearGradient id="grad-cyan-right" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor="#a855f7" stopOpacity="0.15" />
            <stop offset="100%" stopColor="#a855f7" stopOpacity="0.6" />
          </linearGradient>
          <linearGradient id="grad-center" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#00f0ff" stopOpacity="0.5" />
            <stop offset="100%" stopColor="#00f0ff" stopOpacity="0.5" />
          </linearGradient>
        </defs>

        {/* Grid background */}
        <pattern
          id="grid"
          width="40"
          height="40"
          patternUnits="userSpaceOnUse"
        >
          <line
            x1="0"
            y1="0"
            x2="40"
            y2="0"
            stroke="#0f1520"
            strokeWidth="0.5"
          />
          <line
            x1="0"
            y1="0"
            x2="0"
            y2="40"
            stroke="#0f1520"
            strokeWidth="0.5"
          />
        </pattern>
        <rect width={width} height={height} fill="url(#grid)" />

        {/* Connection: Registry to Gateway */}
        <line
          x1={registryPos.x}
          y1={registryPos.y + 32}
          x2={gatewayPos.x}
          y2={gatewayPos.y - 32}
          stroke="#00f0ff"
          strokeWidth="1.5"
          strokeDasharray="6 3"
          opacity="0.4"
        >
          <animate
            attributeName="stroke-dashoffset"
            values="18;0"
            dur="1.5s"
            repeatCount="indefinite"
          />
        </line>

        {/* Connections: BAPs to Gateway */}
        {baps.map((_, i) => {
          const bapY = bapStartY + i * nodeSpacing;
          return (
            <g key={`bap-conn-${i}`}>
              <path
                d={`M ${bapX + 64} ${bapY} Q ${centerX - 60} ${bapY} ${gatewayPos.x - 64} ${gatewayPos.y}`}
                fill="none"
                stroke="#22d3ee"
                strokeWidth="1"
                strokeDasharray="6 4"
                opacity="0.3"
              >
                <animate
                  attributeName="stroke-dashoffset"
                  values="20;0"
                  dur="2s"
                  repeatCount="indefinite"
                />
              </path>
              {/* Flowing particle */}
              <circle r="2" fill="#22d3ee" opacity="0.8">
                <animateMotion
                  dur={`${2.5 + i * 0.3}s`}
                  repeatCount="indefinite"
                  path={`M ${bapX + 64} ${bapY} Q ${centerX - 60} ${bapY} ${gatewayPos.x - 64} ${gatewayPos.y}`}
                />
              </circle>
            </g>
          );
        })}

        {/* Connections: Gateway to BPPs */}
        {bpps.map((_, i) => {
          const bppY = bppStartY + i * nodeSpacing;
          return (
            <g key={`bpp-conn-${i}`}>
              <path
                d={`M ${gatewayPos.x + 64} ${gatewayPos.y} Q ${centerX + 60} ${bppY} ${bppX - 64} ${bppY}`}
                fill="none"
                stroke="#a855f7"
                strokeWidth="1"
                strokeDasharray="6 4"
                opacity="0.3"
              >
                <animate
                  attributeName="stroke-dashoffset"
                  values="20;0"
                  dur="2s"
                  repeatCount="indefinite"
                />
              </path>
              {/* Flowing particle */}
              <circle r="2" fill="#a855f7" opacity="0.8">
                <animateMotion
                  dur={`${2.5 + i * 0.3}s`}
                  repeatCount="indefinite"
                  path={`M ${gatewayPos.x + 64} ${gatewayPos.y} Q ${centerX + 60} ${bppY} ${bppX - 64} ${bppY}`}
                />
              </circle>
            </g>
          );
        })}

        {/* Registry Node (center top) */}
        <g transform={`translate(${registryPos.x}, ${registryPos.y})`}>
          {/* Glow ring */}
          <circle r="40" fill="none" stroke="#00f0ff" strokeWidth="1" opacity="0.2" filter="url(#glow)">
            <animate attributeName="r" values="40;48;40" dur="4s" repeatCount="indefinite" />
            <animate attributeName="opacity" values="0.2;0;0.2" dur="4s" repeatCount="indefinite" />
          </circle>
          <rect x="-70" y="-28" width="140" height="56" rx="8" fill="#0f1520" stroke="#00f0ff" strokeWidth="2" filter="url(#glow)" />
          <rect x="-68" y="-26" width="136" height="52" rx="6" fill="none" stroke="#00f0ff" strokeWidth="0.5" opacity="0.1" />
          <text x="0" y="-6" textAnchor="middle" fill="#00f0ff" fontSize="11" fontWeight="700" fontFamily="JetBrains Mono, monospace">
            REGISTRY
          </text>
          <text x="0" y="12" textAnchor="middle" fill="#67e8f9" fontSize="9" opacity="0.7">
            Participant Directory
          </text>
          <circle cx="56" cy="-16" r="4" fill="#00ff88">
            <animate attributeName="opacity" values="1;0.4;1" dur="2s" repeatCount="indefinite" />
          </circle>
        </g>

        {/* Gateway Node (center bottom) */}
        <g transform={`translate(${gatewayPos.x}, ${gatewayPos.y})`}>
          <circle r="40" fill="none" stroke="#00d4aa" strokeWidth="1" opacity="0.2" filter="url(#glow)">
            <animate attributeName="r" values="40;48;40" dur="4s" repeatCount="indefinite" />
            <animate attributeName="opacity" values="0.2;0;0.2" dur="4s" repeatCount="indefinite" />
          </circle>
          <rect x="-70" y="-28" width="140" height="56" rx="8" fill="#0f1520" stroke="#00d4aa" strokeWidth="2" filter="url(#glow)" />
          <text x="0" y="-6" textAnchor="middle" fill="#00d4aa" fontSize="11" fontWeight="700" fontFamily="JetBrains Mono, monospace">
            GATEWAY
          </text>
          <text x="0" y="12" textAnchor="middle" fill="#5eead4" fontSize="9" opacity="0.7">
            Search Multicast
          </text>
          <circle cx="56" cy="-16" r="4" fill="#00ff88">
            <animate attributeName="opacity" values="1;0.4;1" dur="2s" repeatCount="indefinite" />
          </circle>
        </g>

        {/* BAP Nodes */}
        {baps.map((bap, i) => (
          <NodeCard
            key={bap.subscriber_id}
            label={displayName(bap.subscriber_id)}
            type="BAP"
            status={bap.status}
            domain={bap.domain}
            city={bap.city}
            x={bapX}
            y={bapStartY + i * nodeSpacing}
            color="#22d3ee"
          />
        ))}

        {/* BPP Nodes */}
        {bpps.map((bpp, i) => (
          <NodeCard
            key={bpp.subscriber_id}
            label={displayName(bpp.subscriber_id)}
            type="BPP"
            status={bpp.status}
            domain={bpp.domain}
            city={bpp.city}
            x={bppX}
            y={bppStartY + i * nodeSpacing}
            color="#a855f7"
          />
        ))}

        {/* Protocol flow labels */}
        <text x={centerX - 100} y={gatewayPos.y + 4} textAnchor="end" fill="#22d3ee" fontSize="8" opacity="0.4" fontFamily="JetBrains Mono, monospace">
          search /
        </text>
        <text x={centerX + 100} y={gatewayPos.y + 4} textAnchor="start" fill="#a855f7" fontSize="8" opacity="0.4" fontFamily="JetBrains Mono, monospace">
          / on_search
        </text>
        <text x={centerX} y={registryPos.y + 50} textAnchor="middle" fill="#00f0ff" fontSize="8" opacity="0.3" fontFamily="JetBrains Mono, monospace">
          lookup / subscribe
        </text>
      </svg>
    </div>
  );
}
