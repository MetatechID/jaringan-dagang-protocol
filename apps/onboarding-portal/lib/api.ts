import { Subscriber, NetworkStats } from "./types";

const REGISTRY_URL = "/api/registry";
const BAP_URL = "/api/bap";

export async function fetchSubscribers(): Promise<Subscriber[]> {
  const res = await fetch(`${REGISTRY_URL}/subscribers`, {
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`Registry responded ${res.status}`);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const raw: any[] = await res.json();
  // Map API field names to our Subscriber type
  return raw.map((r) => ({
    subscriber_id: r.subscriber_id,
    subscriber_url: r.subscriber_url,
    type: r.type,
    domain: r.domain,
    city: r.city,
    country: r.country || "IDN",
    signing_public_key: r.signing_public_key,
    encr_public_key: r.encryption_public_key || r.encr_public_key || "",
    status: r.status,
    created: r.created_at || r.created,
    updated: r.updated_at || r.updated,
    valid_from: r.valid_from,
    valid_until: r.valid_until,
  }));
}

export async function fetchHealth(
  service: "registry" | "gateway" | "bap" | "bpp"
): Promise<boolean> {
  const urls: Record<string, string> = {
    registry: `${REGISTRY_URL}/health`,
    gateway: "/api/gateway/health",
    bap: `${BAP_URL}/health`,
    bpp: "/api/bpp/health",
  };
  try {
    const res = await fetch(urls[service], { cache: "no-store" });
    return res.ok;
  } catch {
    return false;
  }
}

export async function registerSubscriber(data: {
  subscriber_id: string;
  subscriber_url: string;
  type: string;
  domain: string;
  city: string;
  signing_public_key: string;
  encr_public_key: string;
}): Promise<{ success: boolean; data?: Subscriber; error?: string }> {
  try {
    const res = await fetch(`${REGISTRY_URL}/subscribe`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...data,
        country: "IDN",
        valid_from: new Date().toISOString(),
        valid_until: new Date(
          Date.now() + 365 * 24 * 60 * 60 * 1000
        ).toISOString(),
      }),
    });
    if (!res.ok) {
      const err = await res.text();
      return { success: false, error: err };
    }
    const result = await res.json();
    return { success: true, data: result };
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : "Unknown error";
    return { success: false, error: msg };
  }
}

export async function searchProducts(
  query: string
): Promise<{ searchId: string } | null> {
  try {
    const res = await fetch(`${BAP_URL}/api/search`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query,
        domain: "retail",
        city: "std:062",
      }),
    });
    if (!res.ok) return null;
    return res.json();
  } catch {
    return null;
  }
}

export async function pollSearchResults(
  searchId: string
): Promise<{ results: unknown[]; complete: boolean }> {
  try {
    const res = await fetch(`${BAP_URL}/api/search/${searchId}/results`);
    if (!res.ok) return { results: [], complete: false };
    return res.json();
  } catch {
    return { results: [], complete: false };
  }
}

export function computeStats(subscribers: Subscriber[]): NetworkStats {
  const cities = Array.from(new Set(subscribers.map((s) => s.city)));
  const domains = Array.from(new Set(subscribers.map((s) => s.domain)));
  return {
    total: subscribers.length,
    baps: subscribers.filter((s) => s.type === "BAP").length,
    bpps: subscribers.filter((s) => s.type === "BPP").length,
    gateways: subscribers.filter((s) => s.type === "BG").length,
    cities,
    domains,
  };
}

