import { IndonesiaCity, PaymentMethod } from "./types";

export const CITIES: IndonesiaCity[] = [
  { code: "std:021", name: "Jakarta", island: "Java", x: 42, y: 62 },
  { code: "std:022", name: "Bandung", island: "Java", x: 40, y: 64 },
  { code: "std:031", name: "Surabaya", island: "Java", x: 48, y: 64 },
  { code: "std:024", name: "Semarang", island: "Java", x: 44, y: 63 },
  { code: "std:0274", name: "Yogyakarta", island: "Java", x: 44, y: 65 },
  { code: "std:061", name: "Medan", island: "Sumatra", x: 28, y: 38 },
  { code: "std:062", name: "Padang", island: "Sumatra", x: 26, y: 50 },
  { code: "std:0711", name: "Palembang", island: "Sumatra", x: 33, y: 56 },
  { code: "std:0361", name: "Denpasar", island: "Bali & Nusa Tenggara", x: 52, y: 66 },
  { code: "std:0411", name: "Makassar", island: "Sulawesi", x: 55, y: 58 },
  { code: "std:0561", name: "Pontianak", island: "Kalimantan", x: 43, y: 50 },
  { code: "std:0511", name: "Banjarmasin", island: "Kalimantan", x: 48, y: 56 },
  { code: "std:0967", name: "Jayapura", island: "Papua", x: 82, y: 56 },
  { code: "std:0431", name: "Manado", island: "Sulawesi", x: 58, y: 44 },
  { code: "std:0370", name: "Mataram", island: "Bali & Nusa Tenggara", x: 54, y: 66 },
];

export const CITY_NAMES: Record<string, string> = {};
CITIES.forEach((c) => {
  CITY_NAMES[c.code] = c.name;
});

export const DOMAINS = [
  {
    id: "retail",
    name: "Retail",
    description: "General merchandise, electronics, fashion, home goods",
    becknDomain: "nic2004:52110",
    examples: ["Tokopedia sellers", "Shopee merchants", "Warung digital"],
  },
  {
    id: "food-beverage",
    name: "Food & Beverage",
    description: "Restaurants, warungs, cloud kitchens, beverages",
    becknDomain: "nic2004:55204",
    examples: ["GoFood restaurants", "GrabFood merchants", "Warung makan"],
  },
  {
    id: "logistics",
    name: "Logistics",
    description: "Package delivery, freight, last-mile delivery",
    becknDomain: "nic2004:60232",
    examples: ["JNE", "J&T Express", "SiCepat", "Anteraja"],
  },
  {
    id: "mobility",
    name: "Mobility",
    description: "Ride-hailing, car rental, bike sharing",
    becknDomain: "nic2004:60221",
    examples: ["Gojek drivers", "Grab rides", "BlueBird"],
  },
  {
    id: "healthcare",
    name: "Healthcare",
    description: "Telemedicine, pharmacies, lab bookings",
    becknDomain: "nic2004:85110",
    examples: ["Halodoc", "Alodokter", "K-24 pharmacies"],
  },
];

export const PAYMENT_METHODS: PaymentMethod[] = [
  {
    id: "qris",
    name: "QRIS",
    type: "QR Payment",
    becknType: "ON-ORDER",
    description:
      "Bank Indonesia's Quick Response Code Indonesian Standard. Universal QR payment accepted across all banks and e-wallets.",
  },
  {
    id: "va",
    name: "Virtual Account",
    type: "Bank Transfer",
    becknType: "PRE-FULFILLMENT",
    description:
      "Bank virtual account for direct transfers. Supported by BCA, BNI, BRI, Mandiri, and others.",
  },
  {
    id: "gopay",
    name: "GoPay",
    type: "E-Wallet",
    becknType: "ON-ORDER",
    description:
      "Gojek's e-wallet. One of Indonesia's most popular digital payment methods.",
  },
  {
    id: "ovo",
    name: "OVO",
    type: "E-Wallet",
    becknType: "ON-ORDER",
    description:
      "OVO e-wallet. Widely accepted in retail, F&B, and ride-hailing.",
  },
  {
    id: "dana",
    name: "DANA",
    type: "E-Wallet",
    becknType: "ON-ORDER",
    description:
      "DANA digital wallet. Popular for online and offline payments.",
  },
  {
    id: "shopeepay",
    name: "ShopeePay",
    type: "E-Wallet",
    becknType: "ON-ORDER",
    description:
      "Shopee's payment platform. Integrated with Shopee marketplace.",
  },
  {
    id: "cod",
    name: "Cash on Delivery",
    type: "Cash",
    becknType: "ON-FULFILLMENT",
    description:
      "Pay cash when order is delivered. Still very popular in Indonesia, especially outside Java.",
  },
  {
    id: "cc",
    name: "Credit/Debit Card",
    type: "Card",
    becknType: "ON-ORDER",
    description:
      "Visa, Mastercard, and JCB credit/debit cards via payment gateway.",
  },
];

export const LOGISTICS_PROVIDERS = [
  { id: "jne", name: "JNE", services: ["REG", "YES", "OKE"], coverage: "Nationwide" },
  { id: "jnt", name: "J&T Express", services: ["EZ", "J&T Super"], coverage: "Nationwide" },
  { id: "sicepat", name: "SiCepat", services: ["REG", "BEST", "GOKIL"], coverage: "Nationwide" },
  { id: "anteraja", name: "AnterAja", services: ["Regular", "Next Day", "Same Day"], coverage: "Java & major cities" },
  { id: "gosend", name: "GoSend", services: ["Instant", "Same Day"], coverage: "Major cities" },
  { id: "grabexpress", name: "GrabExpress", services: ["Instant", "Same Day"], coverage: "Major cities" },
  { id: "pos", name: "Pos Indonesia", services: ["Regular", "Express", "Same Day"], coverage: "Nationwide + rural" },
  { id: "tiki", name: "TIKI", services: ["REG", "ONS", "SDS"], coverage: "Nationwide" },
];

export const CATEGORIES = [
  { id: "fashion", name: "Fashion & Apparel", icon: "F", count: 0 },
  { id: "electronics", name: "Electronics & Gadgets", icon: "E", count: 0 },
  { id: "grocery", name: "Grocery & Fresh", icon: "G", count: 0 },
  { id: "fnb", name: "Food & Beverage", icon: "R", count: 0 },
  { id: "health", name: "Health & Beauty", icon: "H", count: 0 },
  { id: "home", name: "Home & Living", icon: "L", count: 0 },
  { id: "automotive", name: "Automotive Parts", icon: "A", count: 0 },
  { id: "books", name: "Books & Stationery", icon: "B", count: 0 },
];

export const ISLAND_GROUPS = [
  { name: "Sumatra", color: "#06b6d4" },
  { name: "Java", color: "#22d3ee" },
  { name: "Kalimantan", color: "#67e8f9" },
  { name: "Sulawesi", color: "#a5f3fc" },
  { name: "Bali & Nusa Tenggara", color: "#0891b2" },
  { name: "Papua", color: "#0e7490" },
];
