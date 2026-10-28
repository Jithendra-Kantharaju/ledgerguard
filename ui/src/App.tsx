
import {
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";

import { Api } from "./api";

import type {
  DocDetail,
  DocSummary,
  ReviewRequest,
  UserInfo,
} from "./api";

import {
  cognitoEnabled,
  initializeAuth,
  login,
  logout,
} from "./auth";


// --------------------------------------------------
// Constants
// --------------------------------------------------

const STATUSES = [
  "awaiting_review",
  "",
  "queued",
  "processing",
  "executing",
  "executed",
  "shadow_approved",
  "rejected",
  "failed",
];

const FIELDS = [
  "invoice_number",
  "vendor_name",
  "po_number",
  "invoice_date",
  "currency",
  "subtotal",
  "tax",
  "total",
];

const REQUIRED_APPROVAL_FIELDS = [
  "invoice_number",
  "vendor_name",
  "po_number",
  "invoice_date",
  "currency",
  "total",
];

type ReviewAction =
  | "approve"
  | "correct"
  | "reject";


// --------------------------------------------------
// Local authentication helpers
// --------------------------------------------------

function loadKey(): string {
  if (cognitoEnabled) {
    return "";
  }

  try {
    return (
      localStorage.getItem("ap-api-key") ?? ""
    );
  } catch {
    return "";
  }
}


function saveKey(key: string): void {
  if (cognitoEnabled) {
    return;
  }

  try {
    localStorage.setItem(
      "ap-api-key",
      key
    );
  } catch {
    // Local browser storage unavailable.
  }
}


// --------------------------------------------------
// Main application
// --------------------------------------------------

export default function App() {
  const [key, setKey] = useState<string>(
    loadKey
  );

  const [authReady, setAuthReady] =
    useState<boolean>(!cognitoEnabled);

  const [user, setUser] =
    useState<UserInfo | null>(null);

  const [status, setStatus] =
    useState<string>("awaiting_review");

  const [docs, setDocs] =
    useState<DocSummary[]>([]);

  const [selected, setSelected] =
    useState<string | null>(null);

  const [error, setError] =
    useState<string | null>(null);

  const api = useMemo(
    () => new Api(key),
    [key]
  );

  // ------------------------------------------------
  // Initialize Cognito authentication
  // ------------------------------------------------

  useEffect(() => {
    if (!cognitoEnabled) {
      setAuthReady(true);
      return;
    }

    let active = true;

    void initializeAuth()
      .then((token: string) => {
        if (!active) return;

        setKey(token);
        setAuthReady(true);
        setError(null);
      })
      .catch((err: unknown) => {
        if (!active) return;

        setKey("");
        setAuthReady(true);
        setError(String(err));
      });

    return () => {
      active = false;
    };
  }, []);

  // ------------------------------------------------
  // Refresh user and documents
  // ------------------------------------------------

  const refresh = useCallback(
    async (): Promise<void> => {
      if (!authReady || !key) {
        setUser(null);
        setDocs([]);
        return;
      }

      try {
        const [currentUser, documents] =
          await Promise.all([
            api.me(),
            api.list(status),
          ]);

        setUser(currentUser);
        setDocs(documents);
        setError(null);
      } catch (err) {
        const message = String(err);

        setError(message);

        // An expired Cognito access token
        // requires another login.
        if (
          cognitoEnabled &&
          (
            message.includes("401") ||
            message.includes("403")
          )
        ) {
          setKey("");
          setUser(null);
          setDocs([]);
          setSelected(null);
        }
      }
    },
    [api, authReady, key, status]
  );

  useEffect(() => {
    void refresh();

    if (!key || !authReady) {
      return;
    }

    const timer = window.setInterval(
      () => {
        void refresh();
      },
      5000
    );

    return () => {
      window.clearInterval(timer);
    };
  }, [refresh, key, authReady]);

  // ------------------------------------------------
  // Upload PDF
  // ------------------------------------------------

  async function handleUpload(
    file: File
  ): Promise<void> {
    if (!key) {
      setError("Sign in before uploading.");
      return;
    }

    try {
      await api.upload(file);

      await refresh();

      setError(null);
    } catch (err) {
      setError(String(err));
    }
  }

  // ------------------------------------------------
  // Authentication controls
  // ------------------------------------------------

  function renderAuthControls() {
    if (cognitoEnabled) {
      if (!authReady) {
        return (
          <span className="muted">
            Checking sign-in...
          </span>
        );
      }

      if (key) {
        return (
          <button
            type="button"
            onClick={() => {
              logout();
            }}
          >
            Sign out
          </button>
        );
      }

      return (
        <button
          type="button"
          onClick={() => {
            void login().catch(
              (err: unknown) => {
                setError(String(err));
              }
            );
          }}
        >
          Sign in with Cognito
        </button>
      );
    }

    return (
      <input
        type="password"
        placeholder="X-API-Key"
        value={key}
        onChange={(event) => {
          const value = event.target.value;

          setKey(value);
          saveKey(value);
        }}
      />
    );
  }

  // ------------------------------------------------
  // Main UI
  // ------------------------------------------------

  return (
    <div className="app">
      <header>
        <h1>LedgerGuard AP Review</h1>

        {renderAuthControls()}

        {user && (
          <span className="who">
            {user.email}
            {" · "}
            {user.role}
            {" · "}
            limit {user.approval_limit}
          </span>
        )}

        {key && (
          <label className="upload">
            Upload PDF

            <input
              type="file"
              accept="application/pdf"
              onChange={(event) => {
                const file =
                  event.target.files?.[0];

                if (!file) return;

                void handleUpload(file);

                event.target.value = "";
              }}
            />
          </label>
        )}
      </header>

      {error && (
        <div className="error">
          {error}
        </div>
      )}

      {!authReady ? (
        <main>
          <section>
            <p className="muted">
              Initializing authentication...
            </p>
          </section>
        </main>
      ) : !key ? (
        <main>
          <section>
            <p className="muted">
              {cognitoEnabled
                ? "Sign in with Cognito to access LedgerGuard."
                : "Enter your local API key to continue."}
            </p>
          </section>
        </main>
      ) : (
        <main>
          <aside>
            <select
              value={status}
              onChange={(event) => {
                setStatus(event.target.value);
              }}
            >
              {STATUSES.map((item) => (
                <option
                  key={item}
                  value={item}
                >
                  {item || "all"}
                </option>
              ))}
            </select>

            <ul>
              {docs.map((document) => (
                <li
                  key={document.id}
                  className={
                    document.id === selected
                      ? "active"
                      : ""
                  }
                  onClick={() => {
                    setSelected(document.id);
                  }}
                >
                  <div className="row">
                    <strong>
                      {document.vendor_name ??
                        document.filename}
                    </strong>

                    <span
                      className={
                        `badge ${document.status}`
                      }
                    >
                      {document.status}
                    </span>
                  </div>

                  <div className="muted">
                    {document.invoice_number ?? "—"}
                    {" · "}
                    {document.total ?? "?"}
                    {" "}
                    {document.currency ?? ""}
                  </div>

                  <div className="muted small">
                    {document.reasons
                      .slice(0, 2)
                      .join("; ")}
                  </div>
                </li>
              ))}

              {docs.length === 0 && (
                <li className="muted">
                  Nothing here.
                </li>
              )}
            </ul>
          </aside>

          <section>
            {selected ? (
              <Detail
                api={api}
                id={selected}
                role={user?.role}
                onDone={refresh}
              />
            ) : (
              <p className="muted">
                Select a document.
              </p>
            )}
          </section>
        </main>
      )}
    </div>
  );
}


// --------------------------------------------------
// Document Detail Component
// --------------------------------------------------

type DetailProps = {
  api: Api;
  id: string;
  role?: string;
  onDone: () => Promise<void>;
};


function Detail({
  api,
  id,
  role,
  onDone,
}: DetailProps) {
  const [doc, setDoc] =
    useState<DocDetail | null>(null);

  const [pdf, setPdf] =
    useState<string | null>(null);

  const [edits, setEdits] =
    useState<Record<string, string>>({});

  const [comment, setComment] =
    useState<string>("");

  const [msg, setMsg] =
    useState<string | null>(null);

  const [
    authorizeException,
    setAuthorizeException,
  ] = useState<boolean>(false);

  const [
    exceptionReason,
    setExceptionReason,
  ] = useState<string>("");

  // ------------------------------------------------
  // Load current document
  // ------------------------------------------------

  const load = useCallback(
    async (): Promise<void> => {
      try {
        const document = await api.get(id);

        setDoc(document);
      } catch (err) {
        setMsg(String(err));
      }
    },
    [api, id]
  );

  // ------------------------------------------------
  // Load document and PDF preview
  // ------------------------------------------------

  useEffect(() => {
    setDoc(null);
    setPdf(null);
    setEdits({});
    setComment("");
    setMsg(null);

    setAuthorizeException(false);
    setExceptionReason("");

    void load();

    let url: string | null = null;
    let cancelled = false;

    void api
      .fileUrl(id)
      .then((newUrl: string) => {
        if (cancelled) {
          URL.revokeObjectURL(newUrl);
          return;
        }

        url = newUrl;
        setPdf(newUrl);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setPdf(null);
          setMsg(String(err));
        }
      });

    return () => {
      cancelled = true;

      if (url) {
        URL.revokeObjectURL(url);
      }
    };
  }, [api, id, load]);

  if (!doc) {
    return (
      <p className="muted">
        Loading...
      </p>
    );
  }

  // ------------------------------------------------
  // Current invoice and approval checks
  // ------------------------------------------------

  const invoice: Record<string, unknown> =
    doc.invoice ?? {};

  const awaiting =
    doc.status === "awaiting_review";

  const matchFlags: string[] =
    doc.analysis?.match?.review_flags ?? [];

  const hasChanges =
    Object.keys(edits).length > 0;

  const needsException =
    matchFlags.length > 0;

  const canUserApprove =
    role === "approver" ||
    role === "admin";

  const missingRequiredFields =
    REQUIRED_APPROVAL_FIELDS.filter(
      (field) => {
        const value = invoice[field];

        return (
          value === null ||
          value === undefined ||
          value === ""
        );
      }
    );

  const exceptionValid =
    !needsException ||
    (
      authorizeException &&
      exceptionReason.trim().length >= 12
    );

  const canApprove =
    awaiting &&
    canUserApprove &&
    !hasChanges &&
    exceptionValid &&
    missingRequiredFields.length === 0;

  // ------------------------------------------------
  // Human review actions
  // ------------------------------------------------

  async function act(
    action: ReviewAction
  ): Promise<void> {
    if (!doc) return;

    if (action === "approve" && !canApprove) {
      setMsg(
        "Approval is not permitted until required " +
        "fields and matching exceptions are resolved."
      );

      return;
    }

    try {
      const body: ReviewRequest = {
        action,
        expected_version: doc.version,
        comment: comment || undefined,
      };

      if (action === "correct") {
        body.fields = edits;
      }

      if (
        action === "approve" &&
        needsException &&
        authorizeException
      ) {
        body.exception_codes = matchFlags;

        body.exception_reason =
          exceptionReason.trim();
      }

      await api.review(
        doc.id,
        body
      );

      setMsg(
        `${action} submitted. Worker revalidation is pending.`
      );

      setEdits({});
      setAuthorizeException(false);
      setExceptionReason("");

      window.setTimeout(() => {
        void load();
        void onDone();
      }, 1500);
    } catch (err) {
      setMsg(String(err));
    }
  }

  // ------------------------------------------------
  // Document UI
  // ------------------------------------------------

  return (
    <div className="detail">
      <div className="pdf">
        {pdf ? (
          <iframe
            title="Invoice preview"
            src={pdf}
          />
        ) : (
          <p className="muted">
            No preview available.
          </p>
        )}
      </div>

      <div className="side">
        <h2>
          {doc.filename}
          {" "}
          <span
            className={`badge ${doc.status}`}
          >
            {doc.status}
          </span>
        </h2>

        <p className="muted small">
          Version {doc.version}
          {" · "}
          Confidence{" "}
          {doc.confidence?.toFixed(2) ?? "—"}

          {doc.analysis?.confidence?.calibrated ===
            false && " (uncalibrated)"}

          {" · "}
          Gate {doc.decision?.gate ?? "—"}
        </p>

        {doc.error && (
          <div className="error">
            {doc.error}
          </div>
        )}

        <h3>Why</h3>

        <ul className="reasons">
          {(doc.decision?.reasons ?? []).map(
            (reason) => (
              <li key={reason}>
                {reason}
              </li>
            )
          )}
        </ul>

        <h3>Fields</h3>

        <table>
          <tbody>
            {FIELDS.map((field) => {
              const agreed =
                doc.analysis?.field_agreement?.[
                  field
                ];

              return (
                <tr key={field}>
                  <td
                    className={
                      agreed === false
                        ? "warn"
                        : ""
                    }
                    title={
                      agreed === false
                        ? "Extraction passes disagree"
                        : ""
                    }
                  >
                    {field}
                  </td>

                  <td>
                    <input
                      disabled={!awaiting}
                      value={
                        edits[field] ??
                        String(
                          invoice[field] ?? ""
                        )
                      }
                      onChange={(event) => {
                        setEdits({
                          ...edits,
                          [field]: event.target.value,
                        });
                      }}
                    />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>

        {awaiting && (
          <div className="actions">
            <input
              placeholder="Comment (optional)"
              value={comment}
              onChange={(event) => {
                setComment(event.target.value);
              }}
            />

            {missingRequiredFields.length > 0 && (
              <div className="exception-review">
                <strong>
                  Corrections required
                </strong>

                <p className="small">
                  Complete these fields before approval:
                  {" "}
                  {missingRequiredFields.join(", ")}
                </p>
              </div>
            )}

            {needsException && (
              <div className="exception-review">
                <strong>
                  Unresolved PO / receipt issues
                </strong>

                <ul>
                  {matchFlags.map((flag) => (
                    <li key={flag}>
                      {flag}
                    </li>
                  ))}
                </ul>

                <label>
                  <input
                    type="checkbox"
                    checked={authorizeException}
                    onChange={(event) => {
                      setAuthorizeException(
                        event.target.checked
                      );
                    }}
                  />

                  I explicitly authorize these
                  matching exceptions.
                </label>

                <input
                  placeholder={
                    "Exception reason " +
                    "(at least 12 characters)"
                  }
                  value={exceptionReason}
                  onChange={(event) => {
                    setExceptionReason(
                      event.target.value
                    );
                  }}
                />
              </div>
            )}

            <button
              type="button"
              onClick={() => {
                void act("approve");
              }}
              disabled={!canApprove}
              title={
                canUserApprove
                  ? "Approve after validation"
                  : "Approver role required"
              }
            >
              Approve
            </button>

            <button
              type="button"
              onClick={() => {
                void act("correct");
              }}
              disabled={!hasChanges}
            >
              Save corrections
            </button>

            <button
              type="button"
              className="danger"
              onClick={() => {
                void act("reject");
              }}
            >
              Reject
            </button>
          </div>
        )}

        {msg && (
          <div className="note">
            {msg}
          </div>
        )}

        <h3>Execution</h3>

        {doc.outbox.length === 0 ? (
          <p className="muted small">
            Not sent to AP.
          </p>
        ) : (
          doc.outbox.map((item) => (
            <p
              key={item.idempotency_key}
              className="small"
            >
              {item.idempotency_key}
              {" · "}
              {item.status}
              {" · attempts "}
              {item.attempts}

              {item.ap_reference &&
                ` · ${item.ap_reference}`}

              {item.last_error &&
                ` · ${item.last_error}`}
            </p>
          ))
        )}

        <h3>Audit trail</h3>

        <ol className="audit">
          {doc.audit.map((event, index) => (
            <li key={index}>
              <span className="muted small">
                {new Date(
                  event.created_at
                ).toLocaleString()}
              </span>
              {" "}
              <strong>
                {event.event}
              </strong>
              {" "}
              <span className="muted small">
                by {event.actor}
              </span>
            </li>
          ))}
        </ol>
      </div>
    </div>
  );
}
