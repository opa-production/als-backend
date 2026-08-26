import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { AlertTriangle, FileText, HardDrive, Layers, MessagesSquare } from "lucide-react";

import { ShareBar } from "../components/charts.jsx";
import {
  Badge,
  Card,
  Empty,
  ErrorState,
  Pagination,
  Select,
  Stat,
  Table,
} from "../components/ui.jsx";
import { useApi } from "../lib/useApi.js";
import { bytes, compact, dateTime, extractionTone, humanise, number } from "../lib/format.js";

const STATUSES = [
  { value: "done", label: "Done" },
  { value: "pending", label: "Pending" },
  { value: "running", label: "Running" },
  { value: "failed", label: "Failed" },
  { value: "skipped", label: "Skipped" },
];

const KINDS = [
  { value: "pdf", label: "PDF" },
  { value: "note", label: "Note" },
  { value: "image", label: "Image" },
  { value: "link", label: "Link" },
];

const LIMIT = 25;

export default function Content() {
  const [params, setParams] = useSearchParams();
  const [status, setStatus] = useState(params.get("extraction_status") ?? "");
  const [kind, setKind] = useState("");
  const [offset, setOffset] = useState(0);

  const stats = useApi("/content/stats");

  useEffect(() => {
    setOffset(0);
  }, [status, kind]);

  useEffect(() => {
    setParams(status ? { extraction_status: status } : {}, { replace: true });
  }, [status, setParams]);

  const materials = useApi("/content/materials", {
    extraction_status: status,
    kind,
    limit: LIMIT,
    offset,
  });

  if (stats.error) return <ErrorState error={stats.error} onRetry={stats.reload} />;

  const data = stats.data;
  const extraction = data?.extraction ?? {};

  return (
    <div className="stack-24 content-narrow">
      <div className="grid grid-4">
        <Stat
          icon={FileText}
          label="Materials filed"
          value={stats.loading ? "—" : number(data.materials)}
          hint={stats.loading ? "" : `across ${number(data.units)} units`}
        />
        <Stat
          icon={Layers}
          label="Searchable passages"
          value={stats.loading ? "—" : compact(data.material_chunks)}
          hint="Extracted text, in Postgres"
        />
        <Stat
          icon={HardDrive}
          label="Stored in the bucket"
          value={stats.loading ? "—" : bytes(data.storage_bytes)}
          hint="Supabase Storage, private"
        />
        <Stat
          icon={MessagesSquare}
          label="Tutor answers"
          value={stats.loading ? "—" : compact(data.tutor_answers)}
          hint={stats.loading ? "" : `${compact(data.prompt_tokens)} prompt tokens`}
        />
      </div>

      {data?.extraction_stalled > 0 && (
        <div className="attention warn">
          <AlertTriangle size={15} strokeWidth={2} />
          <span>
            <strong>{data.extraction_stalled}</strong> material
            {data.extraction_stalled === 1 ? " has" : "s have"} been waiting over an hour for
            text extraction. The worker is either down or wedged — nothing else in the system
            will say so.
          </span>
          <button
            className="attention-link"
            onClick={() => setStatus("pending")}
            style={{ background: "none", border: 0, cursor: "pointer" }}
          >
            Show the queue
          </button>
        </div>
      )}

      <div className="grid grid-2">
        <Card
          title="Extraction pipeline"
          note="A PDF goes to the bucket; the text pulled out of it goes to Postgres. This is that second step."
        >
          {stats.loading ? (
            <div className="skeleton" style={{ height: 140 }} />
          ) : (
            <ShareBar
              formatValue={number}
              items={Object.entries(extraction)
                .map(([key, value]) => ({ label: humanise(key), value }))
                .sort((a, b) => b.value - a.value)}
            />
          )}
        </Card>

        <Card title="What students keep" note="Rows in Postgres, not files.">
          {stats.loading ? (
            <div className="skeleton" style={{ height: 140 }} />
          ) : (
            <div className="dl">
              {[
                ["Units", data.units],
                ["Class sessions", data.class_sessions],
                ["Events and deadlines", data.events],
                ["Chats", data.chats],
                ["Messages", data.messages],
              ].map(([label, value]) => (
                <Row key={label} label={label} value={number(value)} />
              ))}
            </div>
          )}
        </Card>
      </div>

      <div>
        <div className="filters">
          <Select
            value={status}
            onChange={(event) => setStatus(event.target.value)}
            options={STATUSES}
            placeholder="Any extraction status"
            aria-label="Filter by extraction status"
          />
          <Select
            value={kind}
            onChange={(event) => setKind(event.target.value)}
            options={KINDS}
            placeholder="Any kind"
            aria-label="Filter by kind"
          />
        </div>

        <Card
          title="Materials"
          note="Titles and page counts only. This console does not open a student's coursework."
          flush
        >
          <div className={materials.refetching ? "is-refetching" : ""}>
            <Table
              loading={materials.loading}
              rows={materials.data?.items ?? []}
              rowKey={(row) => row.id}
              empty={<Empty icon={FileText} title="No materials match that" />}
              columns={[
                {
                  key: "title",
                  header: "Material",
                  render: (row) => (
                    <div>
                      <div className="cell-primary">{row.title}</div>
                      <div className="cell-sub">
                        {humanise(row.kind)}
                        {row.byte_size ? ` · ${bytes(row.byte_size)}` : ""}
                        {row.page_count ? ` · ${row.page_count} pages` : ""}
                      </div>
                    </div>
                  ),
                },
                {
                  key: "owner",
                  header: "Owner",
                  render: (row) => (
                    <Link to={`/users/${row.user_id}`} className="mono" style={{ fontSize: 12 }}>
                      {row.user_id.slice(0, 8)}
                    </Link>
                  ),
                },
                {
                  key: "status",
                  header: "Extraction",
                  render: (row) => (
                    <div>
                      <Badge tone={extractionTone(row.extraction_status)}>
                        {humanise(row.extraction_status)}
                      </Badge>
                      {row.extraction_error && (
                        <div className="cell-sub" style={{ maxWidth: 320 }}>
                          {row.extraction_error}
                        </div>
                      )}
                    </div>
                  ),
                },
                {
                  key: "created",
                  header: "Filed",
                  align: "right",
                  render: (row) => <span className="dim">{dateTime(row.created_at)}</span>,
                },
              ]}
            />
          </div>

          {materials.data && materials.data.total > 0 && (
            <div className="card-foot">
              <Pagination
                total={materials.data.total}
                limit={LIMIT}
                offset={offset}
                onChange={setOffset}
              />
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}

function Row({ label, value }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </>
  );
}
