import { useCallback, useEffect, useState } from "react";
import { Api, DocDetail, DocSummary } from "./api";

const STATUSES = ["awaiting_review", "", "queued", "processing", "executing", "executed", "shadow_approved", "rejected", "failed"];
const FIELDS = ["invoice_number", "vendor_name", "po_number", "invoice_date", "currency", "subtotal", "tax", "total"];

function loadKey(): string {
  try {
    return localStorage.getItem("ap-api-key") ?? "";
  } catch {
    return "";
  }
}

function saveKey(k: string) {
  try {
    localStorage.setItem("ap-api-key", k);
  } catch {
    /* storage unavailable */
  }
}

export default function App() {
  const [key, setKey] = useState(loadKey());
  const [user, setUser] = useState<{ email: string; role: string; approval_limit: string } | null>(null);
  const [status, setStatus] = useState("awaiting_review");
  const [docs, setDocs] = useState<DocSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const api = new Api(key);

  const refresh = useCallback(async () => {
    if (!key) return;
    try {
      setUser(await new Api(key).me());
      setDocs(await new Api(key).list(status));
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, [key, status]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, [refresh]);

  return (
    <div className="app">
      <header>
        <h1>AP Review</h1>
        <input
          type="password"
          placeholder="X-API-Key (e.g. dev-acme-approver)"
          value={key}
          onChange={(e) => {
            setKey(e.target.value);
            saveKey(e.target.value);
          }}
        />
        {user && (
          <span className="who">
            {user.email} · {user.role} · limit {user.approval_limit}
          </span>
        )}
        <label className="upload">
          Upload PDF
          <input
            type="file"
            accept="application/pdf"
            onChange={async (e) => {
              const f = e.target.files?.[0];
              if (!f) return;
              try {
                await api.upload(f);
                refresh();
              } catch (err) {
                setError(String(err));
              }
              e.target.value = "";
            }}
          />
        </label>
      </header>
      {error && <div className="error">{error}</div>}
      <main>
        <aside>
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s || "all"}
              </option>
            ))}
          </select>
          <ul>
            {docs.map((d) => (
              <li key={d.id} className={d.id === selected ? "active" : ""} onClick={() => setSelected(d.id)}>
                <div className="row">
                  <strong>{d.vendor_name ?? d.filename}</strong>
                  <span className={`badge ${d.status}`}>{d.status}</span>
                </div>
                <div className="muted">
                  {d.invoice_number ?? "—"} · {d.total ?? "?"} {d.currency ?? ""}
                </div>
                <div className="muted small">{d.reasons.slice(0, 2).join("; ")}</div>
              </li>
            ))}
            {docs.length === 0 && <li className="muted">Nothing here.</li>}
          </ul>
        </aside>
        <section>{selected ? <Detail api={api} id={selected} onDone={refresh} /> : <p className="muted">Select a document.</p>}</section>
      </main>
    </div>
  );
}

function Detail({ api, id, onDone }: { api: Api; id: string; onDone: () => void }) {
  const [doc, setDoc] = useState<DocDetail | null>(null);
  const [pdf, setPdf] = useState<string | null>(null);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [comment, setComment] = useState("");
  const [msg, setMsg] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setDoc(await api.get(id));
    } catch (e) {
      setMsg(String(e));
    }
  }, [api, id]);

  useEffect(() => {
    setEdits({});
    setMsg(null);
    load();
    let url: string | null = null;
    api.fileUrl(id).then((u) => {
      url = u;
      setPdf(u);
    }).catch(() => setPdf(null));
    return () => {
      if (url) URL.revokeObjectURL(url);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  if (!doc) return <p className="muted">Loading…</p>;
  const inv = (doc.invoice ?? {}) as Record<string, unknown>;
  const awaiting = doc.status === "awaiting_review";

  async function act(action: string) {
    if (!doc) return;
    try {
      const body: { action: string; expected_version: number; fields?: Record<string, unknown>; comment?: string } = {
        action,
        expected_version: doc.version,
        comment: comment || undefined,
      };
      if (action === "correct") body.fields = edits;
      await api.review(doc.id, body);
      setMsg(`${action} submitted; the worker revalidates before anything happens.`);
      setEdits({});
      setTimeout(() => {
        load();
        onDone();
      }, 1500);
    } catch (e) {
      setMsg(String(e));
    }
  }

  return (
    <div className="detail">
      <div className="pdf">{pdf ? <iframe title="invoice" src={pdf} /> : <p className="muted">No preview.</p>}</div>
      <div className="side">
        <h2>
          {doc.filename} <span className={`badge ${doc.status}`}>{doc.status}</span>
        </h2>
        <p className="muted small">
          version {doc.version} · confidence {doc.confidence?.toFixed(2) ?? "—"}
          {doc.analysis?.confidence?.calibrated === false && " (uncalibrated)"} · gate {doc.decision?.gate ?? "—"}
        </p>
        {doc.error && <div className="error">{doc.error}</div>}
        <h3>Why</h3>
        <ul className="reasons">
          {(doc.decision?.reasons ?? []).map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
        <h3>Fields</h3>
        <table>
          <tbody>
            {FIELDS.map((f) => {
              const agreed = doc.analysis?.field_agreement?.[f];
              return (
                <tr key={f}>
                  <td className={agreed === false ? "warn" : ""} title={agreed === false ? "the two extraction passes disagree" : ""}>
                    {f}
                  </td>
                  <td>
                    <input
                      disabled={!awaiting}
                      value={edits[f] ?? String(inv[f] ?? "")}
                      onChange={(e) => setEdits({ ...edits, [f]: e.target.value })}
                    />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {awaiting && (
          <div className="actions">
            <input placeholder="comment (optional)" value={comment} onChange={(e) => setComment(e.target.value)} />
            <button onClick={() => act("approve")} disabled={Object.keys(edits).length > 0} title="approver role required">
              Approve
            </button>
            <button onClick={() => act("correct")} disabled={Object.keys(edits).length === 0}>
              Save corrections
            </button>
            <button className="danger" onClick={() => act("reject")}>
              Reject
            </button>
          </div>
        )}
        {msg && <div className="note">{msg}</div>}
        <h3>Execution</h3>
        {doc.outbox.length === 0 ? (
          <p className="muted small">Not sent to AP.</p>
        ) : (
          doc.outbox.map((o) => (
            <p key={o.idempotency_key} className="small">
              {o.idempotency_key} · {o.status} · attempts {o.attempts} {o.ap_reference && `· ${o.ap_reference}`} {o.last_error && `· ${o.last_error}`}
            </p>
          ))
        )}
        <h3>Audit trail</h3>
        <ol className="audit">
          {doc.audit.map((e, i) => (
            <li key={i}>
              <span className="muted small">{new Date(e.created_at).toLocaleString()}</span> <strong>{e.event}</strong>{" "}
              <span className="muted small">by {e.actor}</span>
            </li>
          ))}
        </ol>
      </div>
    </div>
  );
}
