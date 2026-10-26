
import { cognitoEnabled } from "./auth";

export const API_URL: string =
  import.meta.env.VITE_API_URL?.replace(/\/$/, "") ||
  "http://localhost:8000";

export type UserInfo = {
  id: string;
  tenant_id: string;
  email: string;
  role: string;
  approval_limit: string;
};

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
  updated_at?: string;
};

export type ReviewRequest = {
  action: "approve" | "reject" | "correct";
  expected_version: number;
  fields?: Record<string, unknown>;
  comment?: string;
  exception_codes?: string[];
  exception_reason?: string;
};

export type DocDetail = DocSummary & {
  invoice: Record<string, unknown> | null;

  decision: {
    outcome: string;
    gate: string;
    reasons: string[];
    confidence: number | null;
  } | null;

  analysis: {
    match?: {
      review_flags?: string[];
    };
    confidence?: {
      calibrated?: boolean;
    };
    field_agreement?: Record<string, boolean | null>;
    [key: string]: unknown;
  } | null;

  error: string | null;

  reviews: {
    id: string;
    user: string;
    action: string;
    document_version: number;
    comment: string | null;
    outcome: string | null;
    created_at: string;
  }[];

  audit: {
    actor: string;
    event: string;
    data: Record<string, unknown>;
    created_at: string;
  }[];

  outbox: {
    idempotency_key: string;
    status: string;
    attempts: number;
    ap_reference: string | null;
    last_error: string | null;
  }[];
};

export class Api {
  constructor(
    private readonly credential: string
  ) {}

  private async req(
    path: string,
    init: RequestInit = {}
  ): Promise<Response> {
    const headers = new Headers(init.headers);

    if (cognitoEnabled) {
      headers.set(
        "Authorization",
        `Bearer ${this.credential}`
      );
    } else {
      headers.set(
        "X-API-Key",
        this.credential
      );
    }

    const response = await fetch(
      `${API_URL}${path}`,
      {
        ...init,
        headers
      }
    );

    if (!response.ok) {
      let detail = response.statusText;

      try {
        const data: unknown = await response.json();

        if (
          typeof data === "object" &&
          data !== null &&
          "detail" in data
        ) {
          const value = data.detail;

          detail =
            typeof value === "string"
              ? value
              : JSON.stringify(value);
        }
      } catch {
        // Server did not return JSON.
      }

      throw new Error(
        `${response.status}: ${detail}`
      );
    }

    return response;
  }

  async me(): Promise<UserInfo> {
    return (await this.req("/me")).json();
  }

  async list(
    status = ""
  ): Promise<DocSummary[]> {
    const query = status
      ? `?status=${encodeURIComponent(status)}`
      : "";

    return (
      await this.req(`/documents${query}`)
    ).json();
  }

  async get(
    id: string
  ): Promise<DocDetail> {
    return (
      await this.req(
        `/documents/${encodeURIComponent(id)}`
      )
    ).json();
  }

  async fileUrl(
    id: string
  ): Promise<string> {
    const response = await this.req(
      `/documents/${encodeURIComponent(id)}/file`
    );

    return URL.createObjectURL(
      await response.blob()
    );
  }

  async upload(file: File) {
    const body = new FormData();

    body.append("file", file);

    return (
      await this.req("/documents", {
        method: "POST",
        body
      })
    ).json();
  }

  async review(
    id: string,
    body: ReviewRequest
  ) {
    return (
      await this.req(
        `/documents/${encodeURIComponent(id)}/review`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json"
          },
          body: JSON.stringify(body)
        }
      )
    ).json();
  }
}
