import { useRef, useState } from "react";
import type { FormEvent } from "react";
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
    } catch (error) {
      setRequestError(errorMessage(error));
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
      document.getElementById("run-id")?.focus();
      return;
    }
    requestLock.current = true;
    setRecord(null);
    setPhase("loading");
    setRequestError(null);
    try {
      setRecord(await loadAnnualPrecheck(nextRunId));
    } catch (error) {
      setRequestError(errorMessage(error));
    } finally {
      requestLock.current = false;
      setPhase("idle");
    }
  }

  return (
    <div className="page">
      <header className="page-header">
        <h1>本地年度预检</h1>
        <p className="scope">
          本页是本地年度预检，展示财务事实、同比、引用页和原文金额复核。没有用户账户，页面也不调用云端模型，结果也不是智能体作出的风险或舞弊判断。
        </p>
      </header>

      <main aria-busy={busy}>
        <form onSubmit={onCreate} noValidate>
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
              onChange={setParsedPath}
            />
            <TextField
              id="source-pdf-path"
              label="原始 PDF 相对路径（相对 data/raw）"
              value={sourcePdfPath}
              help="例如 603288/2024/cninfo-1222994233/1222994233.PDF"
              error={fieldErrors.sourcePdfPath}
              disabled={busy}
              onChange={setSourcePdfPath}
            />
            <TextField
              id="company-id"
              label="company_id"
              value={companyId}
              help="公司标识，海天样例为 603288。"
              error={fieldErrors.companyId}
              disabled={busy}
              onChange={setCompanyId}
            />
            <TextField
              id="report-year"
              label="report_year"
              value={reportYear}
              help="报告年度，1900 到 2100 的整数。"
              error={fieldErrors.reportYear}
              disabled={busy}
              inputMode="numeric"
              onChange={setReportYear}
            />
          </div>
          <div className="actions">
            <button type="button" onClick={fillHaitianSample} disabled={busy}>
              填入海天 2024 样例
            </button>
            <button type="submit" disabled={busy}>
              创建预检
            </button>
          </div>
        </form>

        <form onSubmit={onLoad} noValidate>
          <h2>按 run_id 回看</h2>
          <div className="form-grid">
            <TextField
              id="run-id"
              label="run_id"
              value={runId}
              help="已有预检目录名。创建成功后会填入本次 run_id。"
              error={fieldErrors.runId}
              disabled={busy}
              onChange={setRunId}
            />
          </div>
          <div className="actions">
            <button type="submit" disabled={busy}>
              读取运行
            </button>
          </div>
        </form>

        <p role="status" className="status-line">
          {phase === "creating"
            ? "正在创建预检。接口同步返回，页面没有单独的任务进度。"
            : phase === "loading"
              ? "正在读取该次运行。"
              : "当前没有进行中的请求。"}
        </p>
        {requestError !== null ? (
          <p role="alert" className="alert">
            {requestError}
          </p>
        ) : null}
        {record === null && phase === "idle" && requestError === null ? (
          <p className="empty">尚未创建或读取预检。填写路径后创建，或输入 run_id 回看已有结果。</p>
        ) : null}
        {record === null && requestError !== null ? (
          <p className="empty">这次请求没有得到预检记录。</p>
        ) : null}
        {record !== null ? <PrecheckResult record={record} /> : null}
      </main>
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

function PrecheckResult({ record }: { record: AnnualPrecheckRecord }) {
  const groups = groupFacts(record.facts.facts);
  const changes = record.calculation.changes;
  const verification = record.verification;
  return (
    <section className="result" aria-live="polite">
      <h2>预检结果</h2>
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
                        {group.name}
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
                <ChangeRow key={change.indicator_name} change={change} />
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h3>原文金额复核</h3>
      {verification === undefined ? (
        <p className="empty">这次运行没有记录 verification。</p>
      ) : (
        <VerificationBlock verification={verification} />
      )}

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
    </section>
  );
}

function ChangeRow({ change }: { change: AnnualChange }) {
  const approximate = approximatePercent(change.rate);
  return (
    <tr>
      <th scope="row">{change.indicator_name}</th>
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
}: {
  verification: NonNullable<AnnualPrecheckRecord["verification"]>;
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
                <VerificationRow key={`${item.indicator_name}-${item.report_year}-${item.column_role}`} item={item} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function VerificationRow({ item }: { item: SourceAmountResult }) {
  return (
    <tr>
      <th scope="row">{item.indicator_name}</th>
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
