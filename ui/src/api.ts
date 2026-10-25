export const API_URL: string = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

export type DocSummary = {
  id: string;
  filename: string;
  status: string;
  version: number;
  invoice_number: string | null;
  vendor_name: string | null;
  total: string | null;
  currency: string | null;
  outcome: string | null;
  reasons: string[];
  confidence: number | null;
  created_at: string;
};

export type DocDetail = DocSummary & {
  invoice: Record<string, unknown> | null;
  decision: { outcome: string; gate: string; reasons: string[]; confidence: number } | null;
  analysis: any;
  error: string | null;
  reviews: { id: string; user: string; action: string; document_version: number; comment: string | null; outcome: string | null; created_at: string }[];
  audit: { actor: string; event: string; data: any; created_at: string }[];
  outbox: { idempotency_key: string; status: string; attempts: number; ap_reference: string | null; last_error: string | null }[];
};

export class Api {
  constructor(private key: string) {}

  private async req(path: string, init: RequestInit = {}): Promise<Response> {
    const res = await fetch(API_URL + path, {
      ...init,
      headers: { "X-API-Key": this.key, ...(init.headers ?? {}) },
    });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        detail = (await res.json()).detail ?? detail;
      } catch {
        /* not json */
      }
      throw new Error(`${res.status}: ${typeof detail === "string" ? detail : JSON.stringify(detail)}`);
    }
    return res;
  }

  me = async () => (await this.req("/me")).json();
  list = async (status: string): Promise<DocSummary[]> =>
    (await this.req(`/documents${status ? `?status=${encodeURIComponent(status)}` : ""}`)).json();
  get = async (id: string): Promise<DocDetail> => (await this.req(`/documents/${id}`)).json();
  fileUrl = async (id: string): Promise<string> => URL.createObjectURL(await (await this.req(`/documents/${id}/file`)).blob());
  upload = async (file: File) => {
    const body = new FormData();
    body.append("file", file);
    return (await this.req("/documents", { method: "POST", body })).json();
  };
  review = async (id: string, body: { action: string; expected_version: number; fields?: Record<string, unknown>; comment?: string }) =>
    (await this.req(`/documents/${id}/review`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
}
