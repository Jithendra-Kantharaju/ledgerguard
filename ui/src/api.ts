
export const API_URL: string =
  import.meta.env.VITE_API_URL?.replace(/\/$/, "") ||
  "http://localhost:8000";

// --------------------
// Types
// --------------------

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

export type ReviewResponse = {
  review_id: string;
  status: string;
};

export type UploadResponse = DocSummary & {
  duplicate_upload: boolean;
};

export type Analysis = {
  match?: {
    review_flags?: string[];
  };
  confidence?: {
    calibrated?: boolean;
  };
  field_agreement?: Record<string, boolean | null>;
  [key: string]: unknown;
};

export type DocDetail = DocSummary & {
  invoice: Record<string, unknown> | null;

  decision: {
    outcome: string;
    gate: string;
    reasons: string[];
    confidence: number | null;
  } | null;

  analysis: Analysis | null;
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

// --------------------
// API Client
// --------------------

export class Api {
  constructor(private readonly key: string) {}

  private async req(
    path: string,
    init: RequestInit = {}
  ): Promise<Response> {
    const headers = new Headers(init.headers);

    headers.set("X-API-Key", this.key);

    const response = await fetch(
      `${API_URL}${path}`,
      {
        ...init,
        headers,
      }
    );

    if (!response.ok) {
      let detail: string = response.statusText;

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
        // Server did not return valid JSON.
      }

      throw new Error(
        `API error ${response.status}: ${detail}`
      );
    }

    return response;
  }

  // Current authenticated user
  async me(): Promise<UserInfo> {
    const response = await this.req("/me");
    return response.json();
  }

  // List documents
  async list(
    status: string = ""
  ): Promise<DocSummary[]> {
    const query = status
      ? `?status=${encodeURIComponent(status)}`
      : "";

    const response = await this.req(
      `/documents${query}`
    );

    return response.json();
  }

  // Get document details
  async get(id: string): Promise<DocDetail> {
    const response = await this.req(
      `/documents/${encodeURIComponent(id)}`
    );

    return response.json();
  }

  // Get invoice PDF preview
  async fileUrl(id: string): Promise<string> {
    const response = await this.req(
      `/documents/${encodeURIComponent(id)}/file`
    );

    const blob = await response.blob();

    return URL.createObjectURL(blob);
  }

  // Upload invoice
  async upload(
    file: File
  ): Promise<UploadResponse> {
    const body = new FormData();

    body.append("file", file);

    const response = await this.req(
      "/documents",
      {
        method: "POST",
        body,
      }
    );

    return response.json();
  }

  // Submit human review
  async review(
    id: string,
    body: ReviewRequest
  ): Promise<ReviewResponse> {
    const response = await this.req(
      `/documents/${encodeURIComponent(id)}/review`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify(body),
      }
    );

    return response.json();
  }
}
