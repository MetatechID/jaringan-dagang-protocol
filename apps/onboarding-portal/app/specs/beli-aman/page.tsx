import Link from "next/link";

export const metadata = {
  title: "Beli Aman — Buyer-protection layer · Jaringan Dagang",
  description:
    "Beli Aman is the consumer-facing reference BAP on Jaringan Dagang. Drop-in escrow + Google SSO + recourse for any Indonesian DTC brand site.",
};

export default function BeliAmanSpecPage() {
  return (
    <div className="min-h-screen text-slate-200 bg-surface-900">
      <div className="mx-auto max-w-4xl px-6 py-16">
        <div className="mb-10">
          <Link href="/specs" className="text-cyan-400 text-sm">
            ← Specs
          </Link>
          <div className="mt-4 inline-flex items-center gap-2 px-3 py-1 bg-emerald-500/10 text-emerald-400 rounded-full text-xs font-semibold">
            <span>🛡️</span>
            <span>Reference Implementation · BAP</span>
          </div>
          <h1 className="mt-4 text-4xl font-bold text-white">Beli Aman</h1>
          <p className="mt-2 text-lg text-slate-400">
            The buyer-protection layer for Indonesian DTC commerce. Drop-in escrow + Google SSO +
            recourse, embeddable on any brand site via a JS SDK.
          </p>
        </div>

        <section className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-10">
          <Stat label="Take rate" value="1–2%" sub="vs 15–25% marketplace" />
          <Stat label="Trust" value="Escrow" sub="held until buyer confirms" />
          <Stat label="Migration" value="Zero" sub="additive, not replacement" />
        </section>

        <Section title="What it is">
          <p>
            Beli Aman is a single drop-in <strong>"Bayar Aman"</strong> button that any Indonesian
            DTC brand can embed on their existing checkout page. The button:
          </p>
          <ol className="list-decimal pl-5 mt-3 space-y-1.5">
            <li>Authenticates the buyer via Google SSO (no new account).</li>
            <li>Initiates a Beckn-protocol order against the brand&apos;s seller platform.</li>
            <li>Holds the buyer&apos;s payment in escrow via Xendit.</li>
            <li>Releases funds to the brand on goods-received confirmation, or D+3 auto-release.</li>
          </ol>
          <p className="mt-3 text-slate-400 text-sm">
            Think of it as the <strong>buyer-side reference implementation</strong> of Jaringan
            Dagang — what UPI/PhonePe is to ONDC, Beli Aman is to JD.
          </p>
        </Section>

        <Section title="Network identity">
          <Identifiers />
        </Section>

        <Section title="V1 scope (clickable demo)">
          <ul className="list-disc pl-5 space-y-1.5">
            <li>Three demo storefronts: <code>antarestar</code>, <code>gendes</code>, <code>yourbrand</code></li>
            <li>Real DB-backed state machine (PRE_AUTH → ESCROW_HELD → ESCROW_RELEASED)</li>
            <li>Mocked payment UI (Xendit-style) — no real money in v1</li>
            <li>Order surfaces in seller dashboard with "via Beli Aman" badge + escrow status</li>
            <li>Admin cockpit for demo lifecycle (mark-shipped / mark-delivered / elapse-D3 / refund)</li>
          </ul>
        </Section>

        <Section title="Architecture (Beckn alignment)">
          <pre className="text-xs bg-black/40 border border-cyan-900/30 rounded-lg p-4 overflow-x-auto">{`┌────────────────────┐       ┌────────────────────┐       ┌────────────────────┐
│  Consumer Browser  │       │  Brand DTC Site    │       │  Brand Backend     │
│                    │◀────▶│  (renders BA btn)   │◀────▶│  (Brand's BPP)     │
└────────┬───────────┘       └────────┬───────────┘       └────────┬───────────┘
         │ OAuth, JS SDK              │ BAP↔BPP webhooks            │ Beckn /on_*
         ▼                            ▼                              ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              BELI AMAN (BAP)                                │
│  Auth · Order state machine · Escrow ledger · Disputes · Notifications       │
└─────────────────────────────────────────────────────────────────────────────┘
         │                                                          │
         ▼                                                          ▼
┌─────────────────────────────┐                       ┌──────────────────────────┐
│  Jaringan Dagang Gateway    │                       │  Xendit                  │
│  (Beckn registry + routing) │                       │  (payment + payouts)     │
└─────────────────────────────┘                       └──────────────────────────┘`}</pre>
        </Section>

        <Section title="Live demo">
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            <DemoCard slug="antarestar" name="Antarestar" desc="Outdoor utility · Black/orange utilitarian" />
            <DemoCard slug="gendes" name="Gendes" desc="Beauty / wellness · Pink Shopify aesthetic" />
            <DemoCard slug="yourbrand" name="YourBrand" desc="Customizable · ?primary=#xxx&secondary=#yyy" />
          </div>
          <p className="mt-3 text-xs text-slate-500">
            Demo URL goes here when deployed (e.g. <code>https://beli-aman.metatech.id</code>).
          </p>
        </Section>

        <Section title="What v1 is NOT">
          <ul className="list-disc pl-5 space-y-1.5 text-slate-400">
            <li>Not a real payment integration (no live Xendit calls in v1).</li>
            <li>Not a real Beckn round-trip (BAP-shaped REST today, Beckn verbs in v2).</li>
            <li>Not a brand migration tool (real partner sites stay where they are).</li>
            <li>Not a new identity provider (we wrap Google SSO).</li>
          </ul>
        </Section>
      </div>
    </div>
  );
}

function Stat({ label, value, sub }: { label: string; value: string; sub: string }) {
  return (
    <div className="rounded-xl border border-cyan-900/30 bg-surface-800/50 p-5">
      <div className="text-[10px] text-slate-500 uppercase tracking-wider">{label}</div>
      <div className="text-2xl font-bold text-white mt-1">{value}</div>
      <div className="text-xs text-slate-400 mt-1">{sub}</div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mb-10">
      <h2 className="text-sm font-semibold text-cyan-400 uppercase tracking-wider mb-3">{title}</h2>
      <div className="rounded-xl border border-cyan-900/30 bg-surface-800/50 p-5 text-slate-300 leading-relaxed">
        {children}
      </div>
    </section>
  );
}

function Identifiers() {
  const rows: { k: string; v: string }[] = [
    { k: "subscriber_id", v: "beli-aman.jaringan-dagang.id" },
    { k: "subscriber_url", v: "http://localhost:8003/beckn (dev)" },
    { k: "type", v: "BAP" },
    { k: "domain", v: "retail" },
    { k: "city", v: "ID:JKT" },
    { k: "key algo", v: "Ed25519 (signing) · X25519 (encryption)" },
  ];
  return (
    <table className="w-full text-sm">
      <tbody>
        {rows.map((r) => (
          <tr key={r.k} className="border-b border-cyan-900/10">
            <td className="py-2 pr-4 text-xs text-slate-500 uppercase tracking-wider font-mono">{r.k}</td>
            <td className="py-2 font-mono text-slate-200">{r.v}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function DemoCard({ slug, name, desc }: { slug: string; name: string; desc: string }) {
  return (
    <a
      href={`https://beli-aman.metatech.id/${slug}`}
      className="block rounded-xl border border-cyan-900/30 bg-surface-800/50 p-4 hover:border-emerald-400/50"
    >
      <div className="font-semibold text-white">{name}</div>
      <div className="text-xs text-slate-400 mt-1">{desc}</div>
      <div className="text-xs text-emerald-400 mt-2">Open demo →</div>
    </a>
  );
}
