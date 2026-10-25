
export const API_URL: string =
  import.meta.env.VITE_API_URL ??
  "http://localhost:8000";

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

export type ReviewRequest = {
  action: "approve" | "reject" | "correct";
  expected_version: number;
  fields?: Record<string, unknown>;
  comment?: string;

  // Human-approved matching exceptions.
  exception_codes?: string[];
  exception_reason?: string;
};

export type DocDetail = DocSummary & {
  invoice: Record<string, unknown> | null;

  decision: {
    outcome: string;
    gate: string;
    reasons: string[];
    confidence: number;
  } | null;

  analysis: any;

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
    data: any;
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
    private key: string
  ) {}

  private async req(
    path: string,
    init: RequestInit = {}
  ): Promise<Response> {

    const response = await fetch(
      API_URL + path,
      {
        ...init,
        headers: {
          "X-API-Key": this.key,
          ...(init.headers ?? {}),
        },
      }
    );

    if (!response.ok) {
      let detail = response.statusText;

      try {
        const data = await response.json();

        detail = data.detail ?? detail;

      } catch {
        // Response did not contain JSON.
      }

      throw new Error(
        `${response.status}: ${
          typeof detail === "string"
            ? detail
            : JSON.stringify(detail)
        }`
      );
    }

    return response;
  }

  me = async () =>
    (
      await this.req("/me")
    ).json();

  list = async (
    status: string
  ): Promise<DocSummary[]> =>
    (
      await this.req(
        `/documents${
          status
            ? `?status=${encodeURIComponent(status)}`
            : ""
        }`
      )
    ).json();

  get = async (
    id: string
  ): Promise<DocDetail> =>
    (
      await this.req(
        `/documents/${id}`
      )
    ).json();

  fileUrl = async (
    id: string
  ): Promise<string> => {

    const response = await this.req(
      `/documents/${id}/file`
    );

    const blob = await response.blob();

    return URL.createObjectURL(blob);
  };

  upload = async (
    file: File
  ) => {

    const body = new FormData();

    body.append(
      "file",
      file
    );

    const response = await this.req(
      "/documents",
      {
        method: "POST",
        body,
      }
    );

    return response.json();
  };

  review = async (
    id: string,
    body: ReviewRequest
  ) => {

    const response = await this.req(
      `/documents/${id}/review`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify(body),
      }
    );

    return response.json();
  };
}
