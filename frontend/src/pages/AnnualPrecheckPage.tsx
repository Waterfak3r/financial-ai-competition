import { useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { createAnnualPrecheck, loadAnnualPrecheck, PrecheckApiError } from "../api/prechecks";
import type {
  AnnualChange,
  AnnualPrecheckRecord,
  FactColumnRole,
  FinancialFact,
  PrecheckIssue,
  SourceAmountEvidence,
  SourceAmountResult,
} from "../types/precheck";

const HAITIAN_2024 = {
  parsedPath: "603288/2024/cninfo-1222994233/text_pdf.json",
  sourcePdfPath: "603288/2024/cninfo-1222994233/1222994233.PDF",
  companyId: "603288",
  reportYear: "2024",
} as const;

const RUN_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$/;

interface FieldErrors {
  parsedPath?: string;
  sourcePdfPath?: string;
  companyId?: string;
  reportYear?: string;
  runId?: string;
}

type RequestPhase = "idle" | "creating" | "loading";
type AppView = "home" | "create" | "result" | "report";
type ReportSection = "status" | "scope" | "record";

export function AnnualPrecheckPage() {
  const [parsedPath, setParsedPath] = useState("");
  const [sourcePdfPath, setSourcePdfPath] = useState("");
  const [companyId, setCompanyId] = useState("");
  const [reportYear, setReportYear] = useState("");
  const [runId, setRunId] = useState("");
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [phase, setPhase] = useState<RequestPhase>("idle");
  const [requestError, setRequestError] = useState<string | null>(null);
  const [record, setRecord] = useState<AnnualPrecheckRecord | null>(null);
  const [view, setView] = useState<AppView>("home");
  const [selectedIndicator, setSelectedIndicator] = useState<string | null>(null);
  const [reportSection, setReportSection] = useState<ReportSection>("status");
  const requestLock = useRef(false);
  const busy = phase !== "idle" || requestLock.current;

  function fillHaitianSample() {
    setParsedPath(HAITIAN_2024.parsedPath);
    setSourcePdfPath(HAITIAN_2024.sourcePdfPath);
    setCompanyId(HAITIAN_2024.companyId);
    setReportYear(HAITIAN_2024.reportYear);
    setFieldErrors((current) => ({
      ...current,
      parsedPath: undefined,
      sourcePdfPath: undefined,
      companyId: undefined,
      reportYear: undefined,
    }));
  }

  async function onCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (requestLock.current) {
      return;
    }
    const nextParsedPath = parsedPath.trim();
    const nextSourcePdfPath = sourcePdfPath.trim();
    const nextCompanyId = companyId.trim();
    const nextReportYear = reportYear.trim();
    setParsedPath(nextParsedPath);
    setSourcePdfPath(nextSourcePdfPath);
    setCompanyId(nextCompanyId);
    setReportYear(nextReportYear);
    const errors = validateCreate(nextParsedPath, nextSourcePdfPath, nextCompanyId, nextReportYear);
    setFieldErrors((current) => ({ ...current, ...errors, runId: current.runId }));
    if (hasFieldErrors(errors)) {
      setView("create");
      focusField(errors);
      return;
    }
    const year = Number(nextReportYear);
    requestLock.current = true;
    setRecord(null);
    setPhase("creating");
    setRequestError(null);
    try {
      const created = await createAnnualPrecheck({
        parsed_path: nextParsedPath,
        source_pdf_path: nextSourcePdfPath,
        company_id: nextCompanyId,
        report_year: year,
      });
      setRecord(created);
      setRunId(created.run_id);
      setSelectedIndicator(created.facts.facts[0]?.indicator_name ?? null);
      setView("result");
    } catch (error) {
      setRequestError(errorMessage(error));
      setView("create");
    } finally {
      requestLock.current = false;
      setPhase("idle");
    }
  }

  async function onLoad(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (requestLock.current) {
      return;
    }
    const nextRunId = runId.trim();
    setRunId(nextRunId);
    const runError = validateRunId(nextRunId);
    setFieldErrors((current) => ({ ...current, runId: runError }));
    if (runError !== undefined) {
      setView("create");
      document.getElementById("run-id")?.focus();
      return;
    }
    requestLock.current = true;
    setRecord(null);
    setPhase("loading");
    setRequestError(null);
    try {
      const loaded = await loadAnnualPrecheck(nextRunId);
      setRecord(loaded);
      setSelectedIndicator(loaded.facts.facts[0]?.indicator_name ?? null);
      setView("result");
    } catch (error) {
      setRequestError(errorMessage(error));
      setView("result");
    } finally {
      requestLock.current = false;
      setPhase("idle");
    }
  }

  const summary = summarize(record);

  return (
    <div className="shell">
      <aside className="sidebar">
        <p className="brand">
          <span className="brand-mark" aria-hidden="true" />
          财务智析
        </p>
        <nav aria-label="预检功能">
          <ul className="nav-list">
            <li>
              <NavButton view="home" current={view} onSelect={setView} icon={<HomeIcon />} label="首页" />
            </li>
            <li>
              <NavButton view="create" current={view} onSelect={setView} icon={<FormIcon />} label="创建预检" />
            </li>
            <li>
              <NavButton view="result" current={view} onSelect={setView} icon={<TableIcon />} label="预检结果" />
            </li>
            <li>
              <NavButton view="report" current={view} onSelect={setView} icon={<DocIcon />} label="报告" />
            </li>
          </ul>
        </nav>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <form className="lookup" onSubmit={onLoad} noValidate>
            <label htmlFor="run-id">运行 ID</label>
            <input
              id="run-id"
              value={runId}
              disabled={busy}
              autoComplete="off"
              spellCheck={false}
              aria-invalid={fieldErrors.runId !== undefined}
              aria-describedby={fieldErrors.runId === undefined ? undefined : "run-id-help"}
              placeholder="输入 run_id 回看"
              onChange={(event) => setRunId(event.target.value)}
            />
            <button type="submit" disabled={busy}>
              回看
            </button>
          </form>
          <p className="top-note">本地预检 · 无账户</p>
        </header>
        <main className="content" aria-busy={busy}>
          {phase === "idle" ? null : (
            <p role="status" className="status-line">
              {phase === "creating"
                ? "正在创建预检。接口同步返回，页面没有单独的任务进度。"
                : "正在读取该次运行。"}
            </p>
          )}
          {requestError !== null ? (
            <p role="alert" className="alert">
              {requestError}
            </p>
          ) : null}
          {fieldErrors.runId !== undefined ? (
            <p id="run-id-help" className="field-error">
              {fieldErrors.runId}
            </p>
          ) : null}
          {view === "home" ? (
            <HomeView
              summary={summary}
              record={record}
              onCreate={() => setView("create")}
              onResult={() => setView("result")}
            />
          ) : null}
          {view === "create" ? (
            <CreateView
              parsedPath={parsedPath}
              sourcePdfPath={sourcePdfPath}
              companyId={companyId}
              reportYear={reportYear}
              fieldErrors={fieldErrors}
              busy={busy}
              onParsedPath={setParsedPath}
              onSourcePdfPath={setSourcePdfPath}
              onCompanyId={setCompanyId}
              onReportYear={setReportYear}
              onFill={fillHaitianSample}
              onSubmit={onCreate}
            />
          ) : null}
          {view === "result" ? (
            <ResultView
              record={record}
              phase={phase}
              requestError={requestError}
              selectedIndicator={selectedIndicator}
              onSelectIndicator={setSelectedIndicator}
              onCreate={() => setView("create")}
            />
          ) : null}
          {view === "report" ? (
            <ReportView
              record={record}
              section={reportSection}
              onSection={setReportSection}
              onResult={() => setView("result")}
            />
          ) : null}
        </main>
      </div>
    </div>
  );
}

function NavButton({
  view,
  current,
  onSelect,
  icon,
  label,
}: {
  view: AppView;
  current: AppView;
  onSelect: (view: AppView) => void;
  icon: ReactNode;
  label: string;
}) {
  return (
    <button
      type="button"
      className="nav-item"
      aria-current={current === view ? "page" : undefined}
      onClick={() => onSelect(view)}
    >
      {icon}
      {label}
    </button>
  );
}

function HomeView({
  summary,
  record,
  onCreate,
  onResult,
}: {
  summary: Summary;
  record: AnnualPrecheckRecord | null;
  onCreate: () => void;
  onResult: () => void;
}) {
  return (
    <>
      <section className="hero">
        <div className="hero-copy">
          <h1>让财务预检更清晰</h1>
          <p className="lede">
            这是本地年度预检。页面展示已加载记录中的财务事实、同比、引用页和原文金额复核，不调用云端模型，也不给出风险或舞弊结论。
          </p>
          <div className="hero-actions">
            <button type="button" className="primary hero-cta" onClick={onCreate}>
              去创建
            </button>
            <button type="button" className="secondary hero-cta" onClick={onResult}>
              去回看
            </button>
          </div>
        </div>
        <HeroArt />
      </section>
      <section className="card-grid" aria-label="当前记录摘要">
        <SummaryCard tone="facts" label="事实条数" value={summary.facts} icon={<TableIcon />} />
        <SummaryCard tone="changes" label="同比组数" value={summary.changes} icon={<BarsIcon />} />
        <SummaryCard tone="passed" label="金额复核通过数" value={summary.passed} icon={<CheckIcon />} />
        <SummaryCard tone="issues" label="问题条数" value={summary.issues} icon={<AlertIcon />} />
      </section>
      <section className="panel">
        <h2>最近一次运行</h2>
        {record === null ? (
          <p className="empty">尚未加载预检记录。创建或按运行 ID 回看后，这里显示该次 run_id 与状态。</p>
        ) : (
          <>
            <dl className="meta">
              <dt>run_id</dt>
              <dd>
                <code>{record.run_id}</code>
              </dd>
              <dt>状态</dt>
              <dd>
                {statusText(record.status)}（{record.status}）
              </dd>
            </dl>
            <div className="inline-actions">
              <button type="button" className="secondary" onClick={onResult}>
                打开这次结果
              </button>
            </div>
          </>
        )}
      </section>
    </>
  );
}

function SummaryCard({
  tone,
  label,
  value,
  icon,
}: {
  tone: "facts" | "changes" | "passed" | "issues";
  label: string;
  value: string;
  icon: ReactNode;
}) {
  return (
    <article className={`card card-${tone}`}>
      <div className="card-head">
        <span className="card-icon" aria-hidden="true">
          {icon}
        </span>
        <p className="kicker">{label}</p>
      </div>
      <p className="metric">{value}</p>
      <span className="card-bar" aria-hidden="true" />
    </article>
  );
}

function CreateView({
  parsedPath,
  sourcePdfPath,
  companyId,
  reportYear,
  fieldErrors,
  busy,
  onParsedPath,
  onSourcePdfPath,
  onCompanyId,
  onReportYear,
  onFill,
  onSubmit,
}: {
  parsedPath: string;
  sourcePdfPath: string;
  companyId: string;
  reportYear: string;
  fieldErrors: FieldErrors;
  busy: boolean;
  onParsedPath: (value: string) => void;
  onSourcePdfPath: (value: string) => void;
  onCompanyId: (value: string) => void;
  onReportYear: (value: string) => void;
  onFill: () => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
}) {
  return (
    <form className="panel" onSubmit={onSubmit} noValidate>
      <h2>创建预检</h2>
      <p id="sample-help">
        一键填充写入海天味业（603288）2024
        年公开年报的相对路径。使用前，本机 data/processed 与 data/raw 中需要已经有这些文件。
      </p>
      <div className="form-grid">
        <TextField
          id="parsed-path"
          label="解析结果相对路径（相对 data/processed）"
          value={parsedPath}
          help="例如 603288/2024/cninfo-1222994233/text_pdf.json"
          error={fieldErrors.parsedPath}
          disabled={busy}
          onChange={onParsedPath}
        />
        <TextField
          id="source-pdf-path"
          label="原始 PDF 相对路径（相对 data/raw）"
          value={sourcePdfPath}
          help="例如 603288/2024/cninfo-1222994233/1222994233.PDF"
          error={fieldErrors.sourcePdfPath}
          disabled={busy}
          onChange={onSourcePdfPath}
        />
        <TextField
          id="company-id"
          label="company_id"
          value={companyId}
          help="公司标识，海天样例为 603288。"
          error={fieldErrors.companyId}
          disabled={busy}
          onChange={onCompanyId}
        />
        <TextField
          id="report-year"
          label="report_year"
          value={reportYear}
          help="报告年度，1900 到 2100 的整数。"
          error={fieldErrors.reportYear}
          disabled={busy}
          inputMode="numeric"
          onChange={onReportYear}
        />
      </div>
      <div className="actions">
        <button type="button" className="secondary" onClick={onFill} disabled={busy}>
          填入海天 2024 样例
        </button>
        <button type="submit" className="primary" disabled={busy}>
          创建预检
        </button>
      </div>
    </form>
  );
}

function ResultView({
  record,
  phase,
  requestError,
  selectedIndicator,
  onSelectIndicator,
  onCreate,
}: {
  record: AnnualPrecheckRecord | null;
  phase: RequestPhase;
  requestError: string | null;
  selectedIndicator: string | null;
  onSelectIndicator: (name: string) => void;
  onCreate: () => void;
}) {
  if (record === null) {
    return (
      <section className="panel">
        <h2>预检结果</h2>
        <p className="empty">
          {requestError !== null
            ? "这次请求没有得到预检记录。"
            : phase === "idle"
              ? "尚未创建或读取预检。到创建页填写路径，或在顶栏输入 run_id 回看。"
              : "正在等待预检记录。"}
        </p>
        <button type="button" className="secondary" onClick={onCreate}>
          去创建
        </button>
      </section>
    );
  }
  return <PrecheckResult record={record} selectedIndicator={selectedIndicator} onSelectIndicator={onSelectIndicator} />;
}

function ReportView({
  record,
  section,
  onSection,
  onResult,
}: {
  record: AnnualPrecheckRecord | null;
  section: ReportSection;
  onSection: (section: ReportSection) => void;
  onResult: () => void;
}) {
  return (
    <div className="report-layout">
      <nav className="panel toc" aria-label="报告目录">
        <h2>目录</h2>
        <button type="button" aria-current={section === "status" ? "true" : undefined} onClick={() => onSection("status")}>
          生成状态
        </button>
        <button type="button" aria-current={section === "scope" ? "true" : undefined} onClick={() => onSection("scope")}>
          未实现范围
        </button>
        <button type="button" aria-current={section === "record" ? "true" : undefined} onClick={() => onSection("record")}>
          已加载记录
        </button>
      </nav>
      <section className="panel report-body">
        <p className="lede">报告生成与导出尚未实现。本页不编写分析结论。</p>
        <div>
          {section === "status" ? (
            <>
              <h3 className="section-title">生成状态</h3>
              <p>报告生成与导出尚未实现。这里没有报告正文，也没有下载文件。</p>
            </>
          ) : null}
          {section === "scope" ? (
            <>
              <h3 className="section-title">未实现范围</h3>
              <p>上传、任务进度、模型分析、LangGraph 编排、完整核验和报告导出都还未接到这个页面。</p>
            </>
          ) : null}
          {section === "record" ? (
            <>
              <h3 className="section-title">已加载记录</h3>
              {record === null ? (
                <p>当前没有已加载的预检记录。这不是一份生成的报告。</p>
              ) : (
                <>
                  <p>
                    当前只保留已加载预检的标识，供对照。run_id <code>{record.run_id}</code>，状态{" "}
                    {statusText(record.status)}（{record.status}）。
                  </p>
                  <button type="button" className="secondary" onClick={onResult}>
                    查看预检结果
                  </button>
                </>
              )}
            </>
          ) : null}
        </div>
      </section>
    </div>
  );
}

function HeroArt() {
  return (
    <div className="hero-art" aria-hidden="true">
      <svg className="hero-sheet" viewBox="0 0 220 250" fill="none">
        <rect x="28" y="18" width="150" height="196" rx="12" fill="#ffffff" stroke="#b9d4f2" />
        <path d="M48 52h92M48 74h70M48 96h84" stroke="#c5daf0" strokeWidth="6" strokeLinecap="round" />
        <rect x="48" y="122" width="22" height="58" rx="4" fill="#d7e9fb" />
        <rect x="80" y="100" width="22" height="80" rx="4" fill="#8ebcf2" />
        <rect x="112" y="78" width="22" height="102" rx="4" fill="#1d6adf" />
        <circle cx="168" cy="176" r="28" fill="#e8f3ff" stroke="#1d6adf" strokeWidth="3" />
        <circle cx="168" cy="176" r="12" stroke="#1d6adf" strokeWidth="3" />
        <path d="M186 194l16 16" stroke="#1d6adf" strokeWidth="4" strokeLinecap="round" />
      </svg>
    </div>
  );
}

function TextField({
  id,
  label,
  value,
  help,
  error,
  disabled,
  inputMode = "text",
  onChange,
}: {
  id: string;
  label: string;
  value: string;
  help: string;
  error?: string;
  disabled: boolean;
  inputMode?: "text" | "numeric";
  onChange: (value: string) => void;
}) {
  const helpId = `${id}-help`;
  const errorId = `${id}-error`;
  const describedBy = error === undefined ? helpId : `${helpId} ${errorId}`;
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        value={value}
        disabled={disabled}
        inputMode={inputMode}
        autoComplete="off"
        spellCheck={false}
        aria-invalid={error !== undefined}
        aria-describedby={describedBy}
        onChange={(event) => onChange(event.target.value)}
      />
      <p id={helpId} className="help">
        {help}
      </p>
      {error !== undefined ? (
        <p id={errorId} className="field-error">
          {error}
        </p>
      ) : null}
    </div>
  );
}

function PrecheckResult({
  record,
  selectedIndicator,
  onSelectIndicator,
}: {
  record: AnnualPrecheckRecord;
  selectedIndicator: string | null;
  onSelectIndicator: (name: string) => void;
}) {
  const groups = groupFacts(record.facts.facts);
  const changes = record.calculation.changes;
  const verification = record.verification;
  const summary = summarize(record);
  const activeName = selectedIndicator ?? groups[0]?.name ?? null;
  const activeFacts = record.facts.facts.filter((fact) => fact.indicator_name === activeName);
  const activeChange = changes.find((change) => change.indicator_name === activeName) ?? null;
  const activeChecks =
    verification?.results.filter((item) => item.indicator_name === activeName) ?? [];
  return (
    <section className="result" aria-live="polite">
      <h2 className="section-title">预检结果</h2>
      <div className="card-grid">
        <SummaryCard tone="facts" label="事实条数" value={summary.facts} icon={<TableIcon />} />
        <SummaryCard tone="changes" label="同比组数" value={summary.changes} icon={<BarsIcon />} />
        <SummaryCard tone="passed" label="金额复核通过数" value={summary.passed} icon={<CheckIcon />} />
        <SummaryCard tone="issues" label="问题条数" value={summary.issues} icon={<AlertIcon />} />
      </div>
      <div className="split">
        <div className="detail-pane">
          <h3>指标</h3>
          {groups.length === 0 ? (
            <p className="empty">这次预检没有提取到财务事实。</p>
          ) : (
            <ul className="indicator-list">
              {groups.map((group) => (
                <li key={group.name}>
                  <button
                    type="button"
                    aria-pressed={group.name === activeName}
                    onClick={() => onSelectIndicator(group.name)}
                  >
                    {group.name}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="detail-pane">
          <h3>证据详情</h3>
          {activeName === null ? (
            <p className="empty">选择左侧指标后，这里显示该指标的事实、同比和复核证据。</p>
          ) : (
            <IndicatorDetail name={activeName} facts={activeFacts} change={activeChange} checks={activeChecks} />
          )}
        </div>
      </div>

      <div className="panel">
        <h3>运行信息</h3>
        <dl className="meta">
          <dt>run_id</dt>
          <dd>
            <code>{record.run_id}</code>
          </dd>
          <dt>status</dt>
          <dd>
            {statusText(record.status)}（{record.status}）
          </dd>
          <dt>创建时间</dt>
          <dd>{record.created_at}</dd>
          <dt>company_id</dt>
          <dd>{record.inputs.company_id}</dd>
          <dt>report_year</dt>
          <dd>{record.inputs.report_year}</dd>
          <dt>document_id</dt>
          <dd>{record.inputs.document_id}</dd>
          <dt>解析结果路径</dt>
          <dd>{record.inputs.parsed_path}</dd>
          <dt>原始 PDF 路径</dt>
          <dd>{record.inputs.source_pdf_path}</dd>
          <dt>原始 PDF SHA256</dt>
          <dd>
            <code>{record.inputs.source_pdf_sha256}</code>
          </dd>
          <dt>解析记录 source_sha256</dt>
          <dd>
            <code>{record.inputs.parsed_source_sha256}</code>
          </dd>
          <dt>原始 SHA 匹配</dt>
          <dd>{record.inputs.hashes_match ? "一致" : "不一致"}</dd>
          <dt>model_called</dt>
          <dd>{record.model_called ? "true" : "false"}</dd>
          <dt>independently_verified</dt>
          <dd>{record.independently_verified ? "true" : "false"}</dd>
        </dl>
      </div>

      <div className="panel">
        <h3>财务事实</h3>
        {groups.length === 0 ? (
          <p className="empty">这次预检没有提取到财务事实。</p>
        ) : (
          <div className="table-wrap">
            <table>
              <caption>按指标和年度列出的财务事实。规范值是按单位倍率换算后的币种基本单位，以十进制字符串保存。</caption>
              <thead>
                <tr>
                  <th scope="col">指标</th>
                  <th scope="col">年度</th>
                  <th scope="col">列</th>
                  <th scope="col">原始金额</th>
                  <th scope="col">规范值（换算后的币种基本单位）</th>
                  <th scope="col">口径</th>
                  <th scope="col">引用页</th>
                  <th scope="col">局限</th>
                </tr>
              </thead>
              {groups.map((group) => (
                <tbody key={group.name}>
                  {group.facts.map((fact, index) => (
                    <tr key={`${fact.indicator_name}-${fact.report_year}-${fact.column_role}`}>
                      {index === 0 ? (
                        <th scope="row" rowSpan={group.facts.length}>
                          <button type="button" className="row-button" onClick={() => onSelectIndicator(group.name)}>
                            {group.name}
                          </button>
                          <span className="sub">{fact.table_name}</span>
                        </th>
                      ) : null}
                      <td>
                        {fact.report_year}
                        <span className="sub">{fact.period_label}</span>
                      </td>
                      <td>{columnRoleText(fact.column_role)}</td>
                      <td className="num">{fact.raw_value}</td>
                      <td className="num">
                        <DecimalValue value={fact.normalized_value} />
                      </td>
                      <td>
                        {fact.statement_scope}
                        <span className="sub">
                          {fact.currency}，倍率 {fact.unit_multiplier}，重述状态 {restatementText(fact.restatement_status)}
                        </span>
                      </td>
                      <td>{pageList(fact)}</td>
                      <td>{fact.limitations.length === 0 ? "无" : fact.limitations.join("；")}</td>
                    </tr>
                  ))}
                </tbody>
              ))}
            </table>
          </div>
        )}
      </div>

      <div className="panel">
        <h3>同比</h3>
        {changes.length === 0 ? (
          <p className="empty">这次预检没有同比结果。</p>
        ) : (
          <div className="table-wrap">
            <table>
              <caption>
                本次返回 {changes.length} 组同比。四项指标都能配对时为 4
                组。差额是规范值相减。同比率是近似 Decimal，旁边的百分比只按约两位换算，供浏览。
                {record.formula === undefined ? "" : ` 公式：${record.formula}`}
              </caption>
              <thead>
                <tr>
                  <th scope="col">指标</th>
                  <th scope="col">报告年</th>
                  <th scope="col">比较年</th>
                  <th scope="col">本期规范值</th>
                  <th scope="col">上期规范值</th>
                  <th scope="col">差额</th>
                  <th scope="col">同比率</th>
                </tr>
              </thead>
              <tbody>
                {changes.map((change) => (
                  <ChangeRow key={change.indicator_name} change={change} onSelect={onSelectIndicator} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="panel">
        <h3>原文金额复核</h3>
        {verification === undefined ? (
          <p className="empty">这次运行没有记录 verification。</p>
        ) : (
          <VerificationBlock verification={verification} onSelect={onSelectIndicator} />
        )}
      </div>

      <div className="panel">
        <h3>问题与局限</h3>
        <h4>提取问题</h4>
        <IssueList items={record.issues.extraction} empty="没有提取问题。" />
        <h4>同比问题</h4>
        <IssueList items={record.issues.calculation} empty="没有同比问题。" />
        {record.note === undefined ? null : (
          <>
            <h4>预检说明</h4>
            <p>{record.note}</p>
          </>
        )}
        {verification === undefined ? null : (
          <>
            <h4>复核范围</h4>
            <p>{verification.scope_note}</p>
          </>
        )}
      </div>
    </section>
  );
}

function IndicatorDetail({
  name,
  facts,
  change,
  checks,
}: {
  name: string;
  facts: FinancialFact[];
  change: AnnualChange | null;
  checks: SourceAmountResult[];
}) {
  return (
    <>
      <p>
        <strong>{name}</strong>
      </p>
      {facts.length === 0 ? (
        <p>没有该指标的财务事实。</p>
      ) : (
        <ul>
          {facts.map((fact) => (
            <li key={`${fact.report_year}-${fact.column_role}`}>
              {fact.report_year} {columnRoleText(fact.column_role)}：原始金额 {fact.raw_value}，规范值{" "}
              {fact.normalized_value}。引用页 {pageList(fact)}。
            </li>
          ))}
        </ul>
      )}
      {change === null ? (
        <p>没有该指标的同比。</p>
      ) : (
        <p>
          同比 {change.report_year} 对比 {change.prior_year}：差额 {change.difference}，同比率 {change.rate}
          {approximatePercent(change.rate) === null ? "" : `（约 ${approximatePercent(change.rate)}）`}。
        </p>
      )}
      {checks.length === 0 ? (
        <p>没有该指标的逐条复核证据。</p>
      ) : (
        checks.map((item) => (
          <article key={`${item.report_year}-${item.column_role}`}>
            <p>
              {item.report_year} {columnRoleText(item.column_role)}：{statusText(item.status)}（{item.status}）
              。原文金额{item.amount_located ? "已定位" : "未定位"}，数值复核 {calculationText(item.calculation_ok)}。
            </p>
            {item.reason === null ? null : <p className="sub">{item.reason}</p>}
            {item.amount_match === null ? null : (
              <p className="snippet-label">
                匹配文本 <code>{item.amount_match.matched_text}</code>
              </p>
            )}
            {item.evidence.length === 0 ? <p className="snippet-label">无证据片段。</p> : null}
            {item.evidence.map((evidence) => (
              <EvidenceSnippet
                key={`${evidence.page_number}-${evidence.block_index}-${evidence.x0}-${evidence.y0}`}
                evidence={evidence}
              />
            ))}
          </article>
        ))
      )}
    </>
  );
}

function ChangeRow({ change, onSelect }: { change: AnnualChange; onSelect: (name: string) => void }) {
  const approximate = approximatePercent(change.rate);
  return (
    <tr>
      <th scope="row">
        <button type="button" className="row-button" onClick={() => onSelect(change.indicator_name)}>
          {change.indicator_name}
        </button>
      </th>
      <td>{change.report_year}</td>
      <td>{change.prior_year}</td>
      <td className="num">
        <DecimalValue value={change.current_value} />
      </td>
      <td className="num">
        <DecimalValue value={change.prior_value} />
      </td>
      <td className="num">
        <DecimalValue value={change.difference} />
      </td>
      <td className="num">
        <DecimalValue value={change.rate} />
        {approximate === null ? null : <span className="sub">约 {approximate}</span>}
      </td>
    </tr>
  );
}

function VerificationBlock({
  verification,
  onSelect,
}: {
  verification: NonNullable<AnnualPrecheckRecord["verification"]>;
  onSelect: (name: string) => void;
}) {
  const located = verification.results.filter((item) => item.amount_located).length;
  const calculationOk = verification.results.filter((item) => item.calculation_ok === true).length;
  const calculationFailed = verification.results.filter((item) => item.calculation_ok === false).length;
  const calculationUnknown = verification.results.filter((item) => item.calculation_ok === null).length;
  return (
    <>
      <p>
        {verificationKindText(verification.kind)}。复核状态 {statusText(verification.status)}（
        {verification.status}）。通过 {verification.passed_count}，未通过 {verification.failed_count}，弃权{" "}
        {verification.abstained_count}。原文金额定位 {located} / {verification.results.length}
        ，数值复核通过 {calculationOk}，未通过 {calculationFailed}，未形成结论 {calculationUnknown}。
      </p>
      {verification.results.length === 0 ? (
        <p className="empty">没有逐条复核结果。</p>
      ) : (
        <div className="table-wrap">
          <table>
            <caption>逐条事实的原文金额定位、数值复核和证据片段。</caption>
            <thead>
              <tr>
                <th scope="col">指标</th>
                <th scope="col">年度</th>
                <th scope="col">列</th>
                <th scope="col">状态</th>
                <th scope="col">原文金额</th>
                <th scope="col">数值复核</th>
                <th scope="col">证据片段</th>
              </tr>
            </thead>
            <tbody>
              {verification.results.map((item) => (
                <VerificationRow
                  key={`${item.indicator_name}-${item.report_year}-${item.column_role}`}
                  item={item}
                  onSelect={onSelect}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function VerificationRow({ item, onSelect }: { item: SourceAmountResult; onSelect: (name: string) => void }) {
  return (
    <tr>
      <th scope="row">
        <button type="button" className="row-button" onClick={() => onSelect(item.indicator_name)}>
          {item.indicator_name}
        </button>
      </th>
      <td>{item.report_year}</td>
      <td>{columnRoleText(item.column_role)}</td>
      <td>
        {statusText(item.status)}（{item.status}）
        {item.reason === null ? null : <span className="sub">{item.reason}</span>}
      </td>
      <td>{item.amount_located ? "已定位" : "未定位"}</td>
      <td>{calculationText(item.calculation_ok)}</td>
      <td>
        {item.amount_match === null ? null : (
          <p className="snippet-label">
            匹配文本 <code>{item.amount_match.matched_text}</code>
          </p>
        )}
        {item.evidence.length === 0 ? <p className="snippet-label">无证据片段。</p> : null}
        {item.evidence.map((evidence) => (
          <EvidenceSnippet
            key={`${evidence.page_number}-${evidence.block_index}-${evidence.x0}-${evidence.y0}`}
            evidence={evidence}
          />
        ))}
      </td>
    </tr>
  );
}

function EvidenceSnippet({ evidence }: { evidence: SourceAmountEvidence }) {
  return (
    <figure className="snippet">
      <figcaption>
        第 {evidence.page_number} 页，块 {evidence.block_index}
        {evidence.amount_in_this_clip ? "，此片段含金额" : "，此片段未单独含金额"}
        {evidence.problem === null ? "" : `。${evidence.problem}`}
      </figcaption>
      <pre>{evidence.extracted_text === "" ? "（空片段）" : evidence.extracted_text}</pre>
    </figure>
  );
}

function IssueList({ items, empty }: { items: PrecheckIssue[]; empty: string }) {
  if (items.length === 0) {
    return <p>{empty}</p>;
  }
  return (
    <ul>
      {items.map((issue, index) => (
        <li key={`${issue.code ?? "issue"}-${issue.indicator_name ?? "none"}-${index}`}>
          {issue.indicator_name === null ? "" : `${issue.indicator_name}：`}
          {issue.message ?? "没有说明。"}
          {issue.code === null ? "" : `（${issue.code}）`}
        </li>
      ))}
    </ul>
  );
}

function DecimalValue({ value }: { value: string }) {
  const grouped = groupDecimal(value);
  return <data value={value}>{grouped}</data>;
}

interface Summary {
  facts: string;
  changes: string;
  passed: string;
  issues: string;
}

function summarize(record: AnnualPrecheckRecord | null): Summary {
  if (record === null) {
    return { facts: "—", changes: "—", passed: "—", issues: "—" };
  }
  const issueCount = record.issues.extraction.length + record.issues.calculation.length;
  return {
    facts: String(record.facts.facts.length),
    changes: String(record.calculation.changes.length),
    passed: record.verification === undefined ? "—" : String(record.verification.passed_count),
    issues: String(issueCount),
  };
}

function groupFacts(facts: FinancialFact[]): { name: string; facts: FinancialFact[] }[] {
  const names: string[] = [];
  const grouped = new Map<string, FinancialFact[]>();
  for (const fact of facts) {
    const existing = grouped.get(fact.indicator_name);
    if (existing === undefined) {
      names.push(fact.indicator_name);
      grouped.set(fact.indicator_name, [fact]);
    } else {
      existing.push(fact);
    }
  }
  return names.map((name) => ({
    name,
    facts: [...(grouped.get(name) ?? [])].sort(compareFacts),
  }));
}

function compareFacts(left: FinancialFact, right: FinancialFact): number {
  if (left.column_role !== right.column_role) {
    return left.column_role === "current" ? -1 : 1;
  }
  return right.report_year - left.report_year;
}

function pageList(fact: FinancialFact): string {
  if (fact.hits.length === 0) {
    return "无引用页";
  }
  return fact.hits
    .map((hit) => `第 ${hit.page_number} 页（块 ${hit.block_index}）`)
    .filter((label, index, labels) => labels.indexOf(label) === index)
    .join("、");
}

function columnRoleText(role: FactColumnRole): string {
  return role === "current" ? "报告年" : "比较年";
}

function restatementText(status: string): string {
  return status === "unknown" ? "尚未确认（unknown）" : status;
}

function statusText(status: string): string {
  switch (status) {
    case "completed":
      return "完成";
    case "completed_with_issues":
      return "完成，但存在问题或弃权";
    case "verification_failed":
      return "原文金额或数值复核未通过";
    case "passed":
      return "通过";
    case "failed":
      return "未通过";
    case "abstained":
      return "弃权";
    default:
      return status;
  }
}

function verificationKindText(kind: string): string {
  if (kind === "pdf_clip_amount_and_normalization") {
    return "引用坐标内的原文金额与单位换算";
  }
  return kind;
}

function calculationText(value: boolean | null): string {
  if (value === true) {
    return "通过";
  }
  if (value === false) {
    return "未通过";
  }
  return "未形成结论";
}

function groupDecimal(value: string): string {
  const match = /^(-?)(\d+)(\.\d+)?$/.exec(value);
  if (match === null) {
    return value;
  }
  const sign = match[1] ?? "";
  const whole = match[2] ?? value;
  const fraction = match[3] ?? "";
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${sign}${grouped}${fraction}`;
}

function approximatePercent(rate: string): string | null {
  if (!/^-?\d+(\.\d+)?$/.test(rate)) {
    return null;
  }
  const value = Number(rate);
  if (!Number.isFinite(value)) {
    return null;
  }
  return `${(value * 100).toFixed(2)}%`;
}

function validateCreate(
  parsedPath: string,
  sourcePdfPath: string,
  companyId: string,
  reportYear: string,
): FieldErrors {
  return {
    parsedPath: validateRelativePath(parsedPath, "解析结果相对路径"),
    sourcePdfPath: validateRelativePath(sourcePdfPath, "原始 PDF 相对路径"),
    companyId: validateCompanyId(companyId),
    reportYear: validateReportYear(reportYear),
  };
}

function validateRelativePath(value: string, label: string): string | undefined {
  if (value === "") {
    return `请填写${label}。`;
  }
  if (value.startsWith("/") || value.startsWith("\\") || /^[A-Za-z]:/.test(value)) {
    return `请把${label}写成数据目录内的相对路径。`;
  }
  const parts = value.split(/[\\/]+/);
  if (parts.some((part) => part === "" || part === "." || part === "..")) {
    return `请去掉${label}中的 . 或 .. 。`;
  }
  return undefined;
}

function validateCompanyId(value: string): string | undefined {
  if (value === "" || value === "." || value === "..") {
    return "请填写 company_id。";
  }
  if (value.includes("/") || value.includes("\\")) {
    return "company_id 不能包含斜杠。";
  }
  return undefined;
}

function validateReportYear(value: string): string | undefined {
  if (!/^\d+$/.test(value)) {
    return "report_year 需要是 1900 到 2100 的整数。";
  }
  const year = Number(value);
  if (!Number.isInteger(year) || year < 1900 || year > 2100) {
    return "report_year 需要是 1900 到 2100 的整数。";
  }
  return undefined;
}

function validateRunId(value: string): string | undefined {
  if (value === "") {
    return "请填写 run_id。";
  }
  if (!RUN_ID_PATTERN.test(value) || value.includes("..")) {
    return "run_id 需以字母或数字开头，后续只能包含字母、数字、点、下划线或连字符。";
  }
  return undefined;
}

function hasFieldErrors(errors: FieldErrors): boolean {
  return Object.values(errors).some((item) => item !== undefined);
}

function focusField(errors: FieldErrors) {
  const fieldId =
    errors.parsedPath !== undefined
      ? "parsed-path"
      : errors.sourcePdfPath !== undefined
        ? "source-pdf-path"
        : errors.companyId !== undefined
          ? "company-id"
          : errors.reportYear !== undefined
            ? "report-year"
            : null;
  if (fieldId !== null) {
    document.getElementById(fieldId)?.focus();
  }
}

function errorMessage(error: unknown): string {
  if (error instanceof PrecheckApiError) {
    const details = [
      error.status === null ? null : `HTTP ${error.status}`,
      error.code === null ? null : error.code,
    ].filter((item): item is string => item !== null);
    return details.length === 0 ? error.message : `${error.message}（${details.join("，")}）`;
  }
  return "预检请求没有完成。";
}

function HomeIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
      <path d="M4 10.5 12 4l8 6.5V20a1 1 0 0 1-1 1h-5v-6H10v6H5a1 1 0 0 1-1-1z" />
    </svg>
  );
}

function FormIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
      <rect x="5" y="3.5" width="14" height="17" rx="2" />
      <path d="M8 8h8M8 12h8M8 16h5" />
    </svg>
  );
}

function TableIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
      <rect x="4" y="4" width="16" height="16" rx="2" />
      <path d="M4 9h16M4 14h16M10 4v16" />
    </svg>
  );
}

function BarsIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
      <path d="M5 19V11M12 19V6M19 19v-8" strokeLinecap="round" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
      <circle cx="12" cy="12" r="8" />
      <path d="m8.5 12.2 2.4 2.4 4.6-5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function AlertIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
      <path d="M12 4.5 20 19H4z" strokeLinejoin="round" />
      <path d="M12 10v4.2" strokeLinecap="round" />
      <circle cx="12" cy="16.6" r="0.7" fill="currentColor" stroke="none" />
    </svg>
  );
}

function DocIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
      <path d="M7 3.5h7l5 5V20a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4.5a1 1 0 0 1 1-1z" />
      <path d="M14 3.5V9h5.5M8.5 13h7M8.5 16.5h7" />
    </svg>
  );
}
