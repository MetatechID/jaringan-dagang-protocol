export interface Subscriber {
  subscriber_id: string;
  subscriber_url: string;
  type: "BAP" | "BPP" | "BG";
  domain: string;
  city: string;
  country: string;
  signing_public_key: string;
  encr_public_key: string;
  status: "SUBSCRIBED" | "INITIATED" | "UNDER_SUBSCRIPTION";
  created: string;
  updated: string;
  valid_from: string;
  valid_until: string;
}

export interface NetworkStats {
  total: number;
  baps: number;
  bpps: number;
  gateways: number;
  cities: string[];
  domains: string[];
}

export interface SearchFlowStep {
  id: number;
  label: string;
  description: string;
  status: "pending" | "active" | "complete" | "error";
  from?: string;
  to?: string;
}

export interface BecknAction {
  name: string;
  method: "POST";
  path: string;
  callback: string;
  description: string;
  sender: "BAP" | "BPP";
  receiver: "BPP" | "BAP";
  gatewayInvolved: boolean;
  requestExample: object;
  responseExample: object;
}

export interface IndonesiaCity {
  code: string;
  name: string;
  island: string;
  x: number;
  y: number;
}

export interface PaymentMethod {
  id: string;
  name: string;
  type: string;
  becknType: string;
  description: string;
}
