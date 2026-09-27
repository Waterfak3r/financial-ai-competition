import { useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { AnnualReportApiError, loadAnnualAnalysis, loadVerificationEvidencePreview } from "../api/annualReports";
import type {
  AnnualAnalysisResponse,
  AnnualReportRecord,
  CandidateSignal,
  ConfirmedCalculation,
  ConfirmedMetric,
  VerificationEvidence,
  VerifiedClaim,
} from "../types/annualReport";

const SAMPLE_RUN_ID = "annual-analysis-ffcd6078-a1df-416b-88b8-6774ebe66d42";
const RUN_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$/;

interface Preview {
  evidenceId: string;
  pageNumber: number;
  objectUrl: string;
}

export function AnnualAnalysisPage() {
  const [runId, setRunId] = useState(SAMPLE_RUN_ID);
  const [response, setResponse] = useState<AnnualAnalysisResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [runIdError, setRunIdError] = useState<string | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [previewBusyId, setPreviewBusyId] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const requestLock = useRef(false);
  const previewRequest = useRef(0);

  useEffect(
    () => () => {
      if (preview !== null) {
        URL.revokeObjectURL(preview.objectUrl);
      }
    },
    [preview],
  );
  useEffect(
    () => () => {
      previewRequest.current += 1;
    },
    [],
  );

  async function onLoad(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (requestLock.current) {
      return;
    }
    const checkedRunId = runId.trim();
    setRunId(checkedRunId);
    if (!RUN_ID_PATTERN.test(checkedRunId)) {
      setRunIdError("run_id 只能包含字母、数字、点、下划线和短横线，长度不超过 121 个字符。");
      return;
    }
    setRunIdError(null);
    setLoadError(null);
    setPreviewError(null);
    setPreview(null);
    setPreviewBusyId(null);
    previewRequest.current += 1;
    requestLock.current = true;
    setLoading(true);
    try {
      const loaded = await loadAnnualAnalysis(checkedRunId);
      setResponse(loaded);
    } catch (error) {
      setLoadError(errorMessage(error));
      setResponse(null);
    } finally {
      requestLock.current = false;
      setLoading(false);
    }
  }

  async function onPreview(evidenceId: string) {
    if (response === null || requestLock.current) {
      return;
    }
    const evidence = response.verification_evidences.find((item) => item.evidence_id === evidenceId);
    if (evidence === undefined) {
      setPreviewError("该证据不在服务端返回的独立核验证据索引中，未请求页图。");
      return;
    }
    const requestId = previewRequest.current + 1;
    previewRequest.current = requestId;
    setPreview(null);
    setPreviewError(null);
    setPreviewBusyId(evidenceId);
    try {
      const loaded = await loadVerificationEvidencePreview(
        response.report.run_id,
        evidence,
        response.manifest.report_sha256,
      );
      if (previewRequest.current !== requestId) {
        return;
      }
      setPreview({
        evidenceId,
        pageNumber: loaded.pageNumber,
        objectUrl: URL.createObjectURL(loaded.blob),
      });
    } catch (error) {
      if (previewRequest.current === requestId) {
        setPreviewError(errorMessage(error));
      }
    } finally {
      if (previewRequest.current === requestId) {
        setPreviewBusyId(null);
      }
    }
  }

  return (
    <>
      <section className="annual-analysis-intro">
        <div>
          <p className="eyebrow">FINTRACE / 年度分析</p>
          <h1>年度报告与证据</h1>
          <p className="lede">
            按 run_id 回看已归档的确定性年度分析，核对来源绑定、事实、计算与报告缺口。候选线索仅供进一步核查，不代表已确认异常或舞弊。
          </p>
        </div>
        <form className="annual-analysis-lookup" onSubmit={onLoad} noValidate>
          <label htmlFor="annual-analysis-run-id">年度分析 run_id</label>
          <div className="annual-analysis-lookup-row">
            <input
              id="annual-analysis-run-id"
              value={runId}
              autoComplete="off"
              spellCheck={false}
              aria-invalid={runIdError !== null}
              aria-describedby={runIdError === null ? undefined : "annual-analysis-run-id-error"}
              onChange={(event) => setRunId(event.target.value)}
            />
            <button type="submit" className="primary" disabled={loading}>
              {loading ? "读取中…" : "读取报告"}
            </button>
          </div>
          <p className="help">示例：海天味业（603288）2024 年正式运行。</p>
          {runIdError !== null ? (
            <p id="annual-analysis-run-id-error" className="field-error">
              {runIdError}
            </p>
          ) : null}
        </form>
      </section>

      {loading ? (
        <p className="status-line" role="status">
          正在校验并读取年度报告归档…
        </p>
      ) : null}
      {loadError !== null ? (
        <p className="alert" role="alert">
          {loadError}
        </p>
      ) : null}

      {response === null && !loading && loadError === null ? (
        <section className="panel annual-empty">
          <h2>读取归档报告</h2>
          <p className="empty">
            输入正式年度分析 run_id 后加载对应报告。页面只读取既有归档，不会重新分析、修改原始年报或调用云端模型。
          </p>
        </section>
      ) : null}

      {response !== null ? (
        <AnnualReportView
          response={response}
          preview={preview}
          previewBusyId={previewBusyId}
          previewError={previewError}
          onPreview={onPreview}
        />
      ) : null}
    </>
  );
}

function AnnualReportView({
  response,
  preview,
  previewBusyId,
  previewError,
  onPreview,
}: {
  response: AnnualAnalysisResponse;
  preview: Preview | null;
  previewBusyId: string | null;
  previewError: string | null;
  onPreview: (evidenceId: string) => void;
}) {
  const { report, manifest, verification_evidences: evidenceIndex } = response;
  const verifiedMetricCount = report.confirmed.metrics.filter(isIndependentlyVerifiedMetric).length;
  const verifiedCalculationCount = report.confirmed.analyses.filter(isIndependentlyVerifiedCalculation).length;
  const evidenceById = new Map(evidenceIndex.map((evidence) => [evidence.evidence_id, evidence]));
  const reportModelInvestigation = report.model_investigation;

  return (
    <div className="annual-report" aria-label="年度分析报告">
      <section className="annual-report-heading">
        <div>
          <p className="eyebrow">
            {report.company_id} · {report.report_year} 年度 · {manifestStatusText(manifest.status)}
          </p>
          <h2>{report.title ?? "FINTRACE 年度财报分析报告"}</h2>
          <p className="annual-run-id">
            run_id <code>{report.run_id}</code>
          </p>
        </div>
        <span className="annual-status-chip">{report.model_called ? "报告记录：调用过模型" : "报告记录：未调用模型"}</span>
      </section>

      <section className="annual-summary-grid" aria-label="报告对象数量">
        <SummaryTile label="报告事实" value={report.confirmed.metrics.length} note={verifiedMetricCount + " 项独立核验通过"} />
        <SummaryTile
          label="报告计算"
          value={report.confirmed.analyses.length}
          note={verifiedCalculationCount + " 项独立计算核验通过"}
        />
        <SummaryTile label="核验后主张" value={report.verified_claims.length} note="以报告记录的状态展示" />
        <SummaryTile label="待核查候选" value={report.pending_review.candidate_signals.length} note="不作为确认结论" />
      </section>

      <section className="panel annual-source-panel">
        <div className="annual-section-heading">
          <div>
            <p className="eyebrow">ARCHIVE BINDING</p>
            <h3>来源与归档绑定</h3>
          </div>
          <span className={report.source_sha256 === manifest.source_pdf_sha256 ? "verification-chip" : "review-chip"}>
            {report.source_sha256 === manifest.source_pdf_sha256 ? "报告与原文哈希一致" : "来源哈希不一致"}
          </span>
        </div>
        <dl className="annual-meta-grid">
          <dt>公司 / 报告期</dt>
          <dd>{report.company_id} · {report.report_year} 年</dd>
          <dt>来源文档</dt>
          <dd><code>{manifest.document_id}</code></dd>
          <dt>原文 SHA256</dt>
          <dd><code>{manifest.source_pdf_sha256}</code></dd>
          <dt>报告 SHA256</dt>
          <dd><code>{manifest.report_sha256}</code></dd>
          <dt>归档状态</dt>
          <dd>{manifestStatusText(manifest.status)}（{manifest.status}）</dd>
          <dt>期间可比性</dt>
          <dd>
            {report.comparability === null
              ? "报告未提供可比性结果"
              : comparabilityStatusText(report.comparability.status) +
                " · 追溯状态：" +
                restatementStatusText(report.comparability.restatement_status)}
          </dd>
        </dl>
        <p className="annual-source-note">
          报告内容和 PDF 页图均来自此 run_id 的本地归档；页图入口只针对服务端返回的独立核验证据。
        </p>
      </section>

      <section className="panel annual-section">
        <div className="annual-section-heading">
          <div>
            <p className="eyebrow">CONFIRMED FACTS</p>
            <h3>报告事实与原值</h3>
          </div>
          <span className="sub">{report.confirmed.metrics.length} 项</span>
        </div>
        {report.confirmed.metrics.length === 0 ? (
          <p className="empty">报告没有 confirmed 区事实。</p>
        ) : (
          <div className="annual-record-grid">
            {report.confirmed.metrics.map((metric) => (
              <MetricCard
                key={metric.fact_id}
                metric={metric}
                evidenceById={evidenceById}
                previewBusyId={previewBusyId}
                onPreview={onPreview}
              />
            ))}
          </div>
        )}
      </section>

      {preview !== null ? (
        <section className="panel annual-evidence-preview" aria-live="polite">
          <div className="annual-section-heading">
            <div>
              <p className="eyebrow">VERIFICATION EVIDENCE</p>
              <h3>独立核验证据 · 第 {preview.pageNumber} 页</h3>
            </div>
            <code>{preview.evidenceId}</code>
          </div>
          <p className="help">
            此页图由服务端根据独立核验证据生成，黄色框标出该证据的数值区域；它是核验材料，不表示异常或舞弊结论。
          </p>
          <img
            src={preview.objectUrl}
            alt={"独立核验证据 PDF 页图，第 " + preview.pageNumber + " 页"}
            className="annual-evidence-image"
          />
        </section>
      ) : null}
      {previewError !== null ? (
        <p className="alert" role="alert">
          {previewError}
        </p>
      ) : null}

      <section className="panel annual-section">
        <div className="annual-section-heading">
          <div>
            <p className="eyebrow">CONFIRMED CALCULATIONS</p>
            <h3>公式计算与独立重算</h3>
          </div>
          <span className="sub">{report.confirmed.analyses.length} 项</span>
        </div>
        {report.confirmed.analyses.length === 0 ? (
          <p className="empty">报告没有 confirmed 区计算。</p>
        ) : (
          <div className="annual-record-grid">
            {report.confirmed.analyses.map((calculation) => (
              <CalculationCard key={calculation.calculation_id} calculation={calculation} />
            ))}
          </div>
        )}
      </section>

      <section className="panel annual-section">
        <div className="annual-section-heading">
          <div>
            <p className="eyebrow">VERIFIED CLAIMS</p>
            <h3>报告记录的核验后主张</h3>
          </div>
          <span className="sub">{report.verified_claims.length} 项</span>
        </div>
        {report.verified_claims.length === 0 ? (
          <p className="empty">报告没有核验后主张。</p>
        ) : (
          <div className="annual-claim-list">
            {report.verified_claims.map((claim) => (
              <ClaimCard key={claim.claim_id} claim={claim} />
            ))}
          </div>
        )}
      </section>

      <section className="panel annual-section">
        <div className="annual-section-heading">
          <div>
            <p className="eyebrow">PENDING REVIEW</p>
            <h3>候选线索、弃权与待核查</h3>
          </div>
          <span className="review-chip">候选不等于已确认异常</span>
        </div>
        <p className="annual-candidate-warning">
          本区只呈现报告中的候选或待核查对象。它们不构成已确认异常、舞弊认定或审计意见。
        </p>
        {report.pending_review.candidate_signals.length === 0 ? (
          <p className="empty">报告未记录候选线索。</p>
        ) : (
          <div className="annual-record-grid">
            {report.pending_review.candidate_signals.map((candidate) => (
              <CandidateCard key={candidate.signal_id} candidate={candidate} />
            ))}
          </div>
        )}
        <PendingItems pending={report.pending_review} />
      </section>

      <section className="panel annual-section">
        <div className="annual-section-heading">
          <div>
            <p className="eyebrow">SCOPE & LIMITATIONS</p>
            <h3>分析范围与报告缺口</h3>
          </div>
        </div>
        <p>{report.scope.note}</p>
        <GapList title="报告缺口" items={report.scope.gaps} emptyText="报告没有单列缺口。" />
        {report.comparability !== null ? (
          <GapList title="可比性限制" items={report.comparability.limitations} emptyText="报告没有记录可比性限制。" />
        ) : null}
        <GapList title="报告局限" items={report.limitations} emptyText="报告没有记录其他局限。" />
      </section>

      {reportModelInvestigation !== undefined && reportModelInvestigation !== null ? (
        <ModelInvestigationBlock investigation={reportModelInvestigation} />
      ) : null}
    </div>
  );
}

function SummaryTile({ label, value, note }: { label: string; value: number; note: string }) {
  return (
    <article className="annual-summary-tile">
      <p>{label}</p>
      <strong>{value}</strong>
      <span>{note}</span>
    </article>
  );
}

function MetricCard({
  metric,
  evidenceById,
  previewBusyId,
  onPreview,
}: {
  metric: ConfirmedMetric;
  evidenceById: Map<string, VerificationEvidence>;
  previewBusyId: string | null;
  onPreview: (evidenceId: string) => void;
}) {
  const verified =
    metric.verifications.some(
      (item) =>
        item.target_type === "financial_fact" &&
        item.target_id === metric.fact_id &&
        item.status === "verified",
    );
  const verifiedIds = new Set(
    metric.verifications
      .filter(
        (item) =>
          item.target_type === "financial_fact" &&
          item.target_id === metric.fact_id &&
          item.status === "verified",
      )
      .flatMap((item) => item.evidence_ids),
  );
  const allowedEvidences = metric.verification_evidences.filter((item) => {
    const indexed = evidenceById.get(item.evidence_id);
    return (
      verifiedIds.has(item.evidence_id) &&
      indexed !== undefined &&
      indexed.document_id === metric.source_document_id &&
      indexed.source_sha256 === metric.source_sha256 &&
      indexed.value_region.page === indexed.pdf_page
    );
  });
  const unit = metric.unit ?? allowedEvidences.find((item) => item.unit)?.unit ?? "报告未记录单位";

  return (
    <article className="annual-record-card">
      <div className="annual-record-heading">
        <div>
          <h4>{metric.label_raw}</h4>
          <p className="sub">
            <code>{metric.metric_id ?? metric.fact_id}</code>
          </p>
        </div>
        <span className={verified ? "verification-chip" : "review-chip"}>
          {verified ? "独立核验通过" : "未标记为独立核验通过"}
        </span>
      </div>
      <p className="annual-value">{metric.normalized_value}</p>
      <dl className="annual-detail-grid">
        <dt>原文数值</dt><dd>{metric.raw_value}</dd>
        <dt>报告年度 / 所属期间</dt>
        <dd>
          {metric.report_year} 年报告 · {metric.period_start ?? "期间未记录"} 至 {metric.period_end ?? "期间未记录"} ·{" "}
          {comparisonRoleText(metric.comparison_role)}
        </dd>
        <dt>币种与单位</dt><dd>{metric.currency} · {unit} · 单位倍率 {metric.unit_multiplier}</dd>
        <dt>报表口径</dt>
        <dd>{statementTypeText(metric.statement_type)} · {scopeText(metric.scope)} · {metric.period_type ?? "期间类型未记录"}</dd>
        <dt>追溯状态</dt><dd>{restatementStatusText(metric.restatement_status)}</dd>
      </dl>
      {allowedEvidences.length > 0 ? (
        <div className="annual-evidence-buttons">
          <h5>可查看的独立核验证据</h5>
          {allowedEvidences.map((evidence) => (
            <button
              key={evidence.evidence_id}
              type="button"
              className="secondary annual-evidence-button"
              onClick={() => onPreview(evidence.evidence_id)}
              disabled={previewBusyId !== null}
            >
              {previewBusyId === evidence.evidence_id ? "读取证据页…" : "查看第 " + evidence.pdf_page + " 页核验证据"}
            </button>
          ))}
        </div>
      ) : (
        <p className="help annual-no-evidence">
          没有与该事实的通过核验记录及服务端核验证据索引同时匹配的页图证据。
        </p>
      )}
      {metric.limitations.length > 0 ? <GapList title="事实限制" items={metric.limitations} /> : null}
    </article>
  );
}

function CalculationCard({ calculation }: { calculation: ConfirmedCalculation }) {
  const independentlyVerified = isIndependentlyVerifiedCalculation(calculation);
  const value = calculation.output_value === null ? "未形成结果" : String(calculation.output_value);
  const recomputed = calculation.independent_verification?.recomputed_value;
  return (
    <article className="annual-record-card">
      <div className="annual-record-heading">
        <div>
          <h4>{calculation.formula_id}</h4>
          <p className="sub"><code>{calculation.calculation_id}</code></p>
        </div>
        <span className={independentlyVerified ? "verification-chip" : "review-chip"}>
          {independentlyVerified ? "独立重算核验通过" : "未通过独立重算核验"}
        </span>
      </div>
      <dl className="annual-detail-grid">
        <dt>公式</dt><dd><code>{calculation.formula_expression}</code></dd>
        <dt>输入事实</dt>
        <dd>{calculation.input_fact_ids.length === 0 ? "未记录" : calculation.input_fact_ids.map((id) => <code key={id}>{id}</code>)}</dd>
        <dt>报告结果</dt><dd>{value} {calculation.unit ?? ""}</dd>
        <dt>独立重算值</dt><dd>{recomputed === undefined || recomputed === null ? "未记录" : String(recomputed) + " " + (calculation.unit ?? "")}</dd>
        <dt>计算状态</dt><dd>{calculationStatusText(calculation.status)}（{calculation.status}）</dd>
      </dl>
      {calculation.failure_reason !== null ? <p className="annual-reason">失败原因：{calculation.failure_reason}</p> : null}
      {calculation.independent_verification?.reason ? (
        <p className="annual-reason">独立核验说明：{calculation.independent_verification.reason}</p>
      ) : null}
    </article>
  );
}

function ClaimCard({ claim }: { claim: VerifiedClaim }) {
  return (
    <details className="annual-claim">
      <summary>
        <span className="annual-claim-status">{verificationStatusText(claim.verification_status)}</span>
        <span>{claim.text}</span>
      </summary>
      <dl className="annual-detail-grid">
        <dt>主张类型</dt><dd>{claimTypeText(claim.claim_type)}</dd>
        <dt>主张 ID</dt><dd><code>{claim.claim_id}</code></dd>
        <dt>支持事实</dt><dd>{idList(claim.supporting_fact_ids)}</dd>
        <dt>支持计算</dt><dd>{idList(claim.calculation_ids)}</dd>
        <dt>支持证据 ID</dt><dd>{idList(claim.supporting_evidence_ids)}</dd>
        <dt>核验状态</dt><dd>{verificationStatusText(claim.verification_status)}</dd>
      </dl>
      {claim.limitations.length > 0 ? <GapList title="主张限制" items={claim.limitations} /> : null}
      {claim.alternative_explanations.length > 0 ? <GapList title="替代解释" items={claim.alternative_explanations} /> : null}
      {claim.follow_up_items.length > 0 ? <GapList title="后续核查" items={claim.follow_up_items} /> : null}
      {claim.independent_verification?.reason ? (
        <p className="annual-reason">核验说明：{claim.independent_verification.reason}</p>
      ) : null}
    </details>
  );
}

function CandidateCard({ candidate }: { candidate: CandidateSignal }) {
  const status = candidateStatusText(candidate.status);
  return (
    <article className="annual-record-card annual-candidate-card">
      <div className="annual-record-heading">
        <div>
          <h4>{signalTitle(candidate.signal_id)}</h4>
          <p className="sub"><code>{candidate.signal_id}</code></p>
        </div>
        <span className="review-chip">{status}</span>
      </div>
      <dl className="annual-detail-grid">
        <dt>候选状态</dt><dd>{status}</dd>
        <dt>线索说明</dt>
        <dd>{nonEmpty(candidate.reason) ?? (candidate.status === "abstained" ? "报告未记录单独的弃权理由。" : "报告未记录单独的线索理由。")}</dd>
        <dt>相关事实 ID</dt><dd>{idList(candidate.input_fact_ids)}</dd>
        <dt>相关计算 ID</dt><dd>{idList(candidate.calculation_ids)}</dd>
        <dt>左侧差额</dt><dd>{candidate.left_difference ?? "报告未记录"}</dd>
        <dt>右侧差额</dt><dd>{candidate.right_difference ?? "报告未记录"}</dd>
        <dt>舞弊结论</dt><dd>{candidate.fraud_conclusion ?? "报告未给出舞弊结论。"}</dd>
      </dl>
      {candidate.placement_reasons.length > 0 ? <GapList title="进入待核查区的原因" items={candidate.placement_reasons} /> : null}
    </article>
  );
}

function PendingItems({
  pending,
}: {
  pending: AnnualAnalysisResponse["report"]["pending_review"];
}) {
  const groups: Array<[string, AnnualReportRecord[]]> = [
    ["待核查事实", pending.facts],
    ["待核查计算", pending.calculations],
    ["待核查主张", pending.claims],
    ["未被主张引用的证据", pending.uncited_evidences],
  ];
  const needsReason = groups.flatMap(([label, items]) =>
    items
      .filter(hasExceptionStatus)
      .map((item, index) => ({ label, item, key: recordIdentity(item) + "-" + index })),
  );
  const nonAbstained = groups.flatMap(([label, items]) =>
    items
      .filter((item) => !hasExceptionStatus(item))
      .map((item, index) => ({ label, item, key: recordIdentity(item) + "-" + index })),
  );

  return (
    <>
      <div className="annual-pending-grid">
        <div>
          <h4>弃权、证据不足与核验冲突</h4>
          {needsReason.length === 0 ? (
            <p className="empty">报告未记录单独列出的弃权对象。</p>
          ) : (
            <ul className="annual-note-list">
              {needsReason.map(({ label, item, key }) => (
                <li key={key}>
                  <strong>{label}：</strong>
                  <PendingItemSummary item={item} />
                </li>
              ))}
            </ul>
          )}
        </div>
        <div>
          <h4>其他待核查对象</h4>
          {nonAbstained.length === 0 ? (
            <p className="empty">报告未记录其他待核查对象。</p>
          ) : (
            <ul className="annual-note-list">
              {nonAbstained.map(({ label, item, key }) => (
                <li key={key}>
                  <strong>{label}：</strong>
                  <PendingItemSummary item={item} />
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </>
  );
}

function ModelInvestigationBlock({ investigation }: { investigation: AnnualReportRecord }) {
  const entries = Object.entries(investigation);
  const knownFields = new Set(["status", "model_called", "reason", "reason_detail", "items"]);
  const otherEntries = entries.filter(([key]) => !knownFields.has(key));
  const status = stringValue(investigation.status);
  const items = Array.isArray(investigation.items)
    ? investigation.items.map(record).filter((item): item is AnnualReportRecord => item !== null)
    : [];
  const reason = stringValue(investigation.reason);
  const reasonDetail = stringValue(investigation.reason_detail);
  const modelCalled = typeof investigation.model_called === "boolean" ? investigation.model_called : null;
  return (
    <section className="panel annual-model-investigation">
      <div className="annual-section-heading">
        <div>
          <p className="eyebrow">MODEL INVESTIGATION</p>
          <h3>模型解释与弃权记录</h3>
        </div>
        <span className="review-chip">{investigationStatusText(status)}</span>
      </div>
      <p className="annual-candidate-warning">
        以下内容来自报告中的可选模型调查字段，解释和弃权均未独立核实，不作为已核验事实或确认结论。
      </p>
      <dl className="annual-model-overview">
        <dt>调查状态</dt>
        <dd>{status === null ? "未记录状态" : <>{investigationStatusText(status)} <code>{status}</code></>}</dd>
        {modelCalled !== null ? <><dt>实际模型调用</dt><dd>{modelCalled ? "是" : "否"}</dd></> : null}
      </dl>
      {reason !== null || reasonDetail !== null ? (
        <div className="annual-model-reason" role="note">
          {reason !== null ? (
            <p><strong>状态说明：</strong>{investigationReasonText(reason)}{investigationReasonText(reason) !== reason ? <> <code>{reason}</code></> : null}</p>
          ) : null}
          {reasonDetail !== null ? <p><strong>补充详情：</strong>{reasonDetail}</p> : null}
        </div>
      ) : null}
      {items.length === 0 ? (
        <p className="empty">没有可显示的解释条目。{reason === null && reasonDetail === null ? "报告也未记录额外的状态原因。" : "上方列出了本次状态说明。"}</p>
      ) : (
        <div className="annual-model-items">
          {items.map((item, index) => <ModelInvestigationItem key={(stringValue(item.signal_id) ?? "item") + "-" + index} item={item} />)}
        </div>
      )}
      {otherEntries.length > 0 ? (
        <details className="annual-other-model-fields">
          <summary>展开来源绑定、证据编号与审计记录等技术字段（{otherEntries.length} 项）</summary>
          <div className="annual-model-fields">
            {otherEntries.map(([key, value]) => (
              <section className="annual-model-field" key={key}>
                <h4>{modelFieldLabel(key)}</h4>
                <ModelValue value={value} depth={0} />
              </section>
            ))}
          </div>
        </details>
      ) : null}
    </section>
  );
}

function ModelInvestigationItem({ item }: { item: AnnualReportRecord }) {
  const signalId = stringValue(item.signal_id);
  const status = stringValue(item.status);
  const explanation = stringValue(item.explanation);
  const reason = stringValue(item.reason);
  const alternatives = stringArray(item.alternative_explanations);
  const limitations = stringArray(item.limitations);
  const narrativeEvidenceIds = stringArray(item.narrative_evidence_ids);

  return (
    <article className="annual-model-item">
      <div className="annual-record-heading">
        <div>
          <h4>{signalId === null ? "模型调查条目" : modelSignalTitle(signalId)}</h4>
          {signalId !== null ? <p className="sub"><code>{signalId}</code></p> : null}
        </div>
        <span className="review-chip">{investigationItemStatusText(status)}</span>
      </div>
      {status === "interpretation" ? (
        <p className="annual-model-explanation"><strong>未核实模型解释：</strong>{explanation ?? "报告未提供解释文字。"}</p>
      ) : status === "abstained" ? (
        <p className="annual-model-explanation"><strong>弃权原因：</strong>{reason === null ? "未提供具体原因。" : investigationReasonText(reason)}{reason !== null && investigationReasonText(reason) !== reason ? <> <code>{reason}</code></> : null}</p>
      ) : explanation !== null ? (
        <p className="annual-model-explanation"><strong>未核实说明：</strong>{explanation}</p>
      ) : null}
      {reason !== null && status !== "abstained" ? (
        <p className="annual-model-explanation"><strong>条目原因：</strong>{investigationReasonText(reason)}{investigationReasonText(reason) !== reason ? <> <code>{reason}</code></> : null}</p>
      ) : null}
      {alternatives.length > 0 ? <GapList title="替代解释" items={alternatives} /> : null}
      {limitations.length > 0 ? <GapList title="限制" items={limitations} /> : null}
      {narrativeEvidenceIds.length > 0 ? (
        <details className="annual-model-evidence-ids">
          <summary>查看模型引用的叙述证据编号（未核验）</summary>
          <ul>{narrativeEvidenceIds.map((id, index) => <li key={id + "-" + index}><code>{id}</code></li>)}</ul>
        </details>
      ) : null}
    </article>
  );
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && item.trim() !== "") : [];
}

function investigationStatusText(status: string | null): string {
  const labels: Record<string, string> = {
    completed: "调查已完成 · 内容未核实",
    abstained: "调查弃权",
    failed: "调查失败",
  };
  return status === null ? "未核实" : labels[status] ?? "调查状态待确认";
}

function investigationItemStatusText(status: string | null): string {
  if (status === "interpretation") return "模型解释 · 未核实";
  if (status === "abstained") return "模型弃权";
  return "状态待确认 · 未核实";
}

function investigationReasonText(reason: string): string {
  const labels: Record<string, string> = {
    insufficient_evidence: "可用证据不足，模型选择弃权。",
    model_configuration_unavailable: "未提供可用的模型配置。",
    model_configuration_load_failed: "模型配置读取失败。",
    model_requires_repository_artifacts_root: "调查归档位置不符合要求，未继续调用模型。",
    source_record_unreadable: "来源记录无法读取。",
    source_record_invalid_json: "来源记录格式无效。",
    source_record_invalid_shape: "来源记录结构不完整。",
    source_record_identity_mismatch: "来源记录与报告身份不匹配。",
    source_record_pdf_path_missing: "来源记录没有提供原始 PDF 路径。",
    source_record_pdf_path_mismatch: "来源记录中的 PDF 路径不匹配。",
    source_pdf_unreadable: "原始 PDF 无法读取。",
    source_pdf_hash_mismatch: "原始 PDF 哈希与来源记录不一致。",
    langgraph_unavailable: "运行调查所需组件不可用。",
    annual_investigation_failed: "调查流程运行失败。",
  };
  return labels[reason] ?? reason;
}

function modelSignalTitle(signalId: string): string {
  const labels: Record<string, string> = {
    profit_up_cash_down: "归母净利润与经营现金流差额方向相反",
    revenue_up_cash_down: "营业收入与经营现金流差额方向相反",
  };
  return labels[signalId] ?? "模型调查条目";
}

function ModelValue({ value, depth }: { value: unknown; depth: number }): ReactNode {
  if (value === null || typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return <span>{String(value)}</span>;
  }
  if (depth >= 3) {
    return <span>包含更深层结构的报告内容</span>;
  }
  if (Array.isArray(value)) {
    if (value.length === 0) {
      return <p className="empty">无记录。</p>;
    }
    return (
      <ul className="annual-note-list">
        {value.map((item, index) => (
          <li key={index}><ModelValue value={item} depth={depth + 1} /></li>
        ))}
      </ul>
    );
  }
  const object = record(value);
  if (object === null) {
    return <span>报告记录了无法显示的内容。</span>;
  }
  const allEntries = Object.entries(object);
  const displayEntries = allEntries.filter(([key]) => !isTechnicalModelField(key));
  const fields = displayEntries.slice(0, 12);
  const shownKeys = new Set(fields.map(([key]) => key));
  const hiddenEntries = allEntries.filter(([key]) => !shownKeys.has(key));
  return (
    <>
      <dl className="annual-detail-grid">
        {fields.map(([key, child]) => (
          <div className="annual-model-entry" key={key}>
            <dt>{modelFieldLabel(key)}</dt>
            <dd><ModelValue value={child} depth={depth + 1} /></dd>
          </div>
        ))}
      </dl>
      {hiddenEntries.length > 0 ? (
        <details className="annual-other-model-fields">
          <summary>展开其他记录字段（{hiddenEntries.length} 项）</summary>
          <dl className="annual-detail-grid">
            {hiddenEntries.map(([key, child]) => (
              <div className="annual-model-entry" key={key}>
                <dt>{modelFieldLabel(key)}</dt>
                <dd><ModelValue value={child} depth={depth + 1} /></dd>
              </div>
            ))}
          </dl>
        </details>
      ) : null}
    </>
  );
}

function GapList({ title, items, emptyText }: { title: string; items: string[]; emptyText?: string }) {
  return (
    <div className="annual-gap-list">
      <h4>{title}</h4>
      {items.length === 0 ? (
        emptyText === undefined ? null : <p className="empty">{emptyText}</p>
      ) : (
        <ul>
          {items.map((item, index) => <li key={title + "-" + index}>{item}</li>)}
        </ul>
      )}
    </div>
  );
}

function isIndependentlyVerifiedMetric(metric: ConfirmedMetric): boolean {
  return metric.verifications.some(
    (item) => item.target_type === "financial_fact" && item.target_id === metric.fact_id && item.status === "verified",
  );
}

function isIndependentlyVerifiedCalculation(calculation: ConfirmedCalculation): boolean {
  return calculation.independent_verification?.status === "verified";
}

function hasExceptionStatus(item: AnnualReportRecord): boolean {
  const independent = record(item.independent_verification);
  const verification = record(item.verification);
  const verificationRecords = Array.isArray(item.verifications)
    ? item.verifications.map(record).filter((value): value is AnnualReportRecord => value !== null)
    : [];
  const statuses = [
    stringValue(item.status),
    stringValue(item.verification_status),
    stringValue(independent?.status),
    stringValue(verification?.status),
    ...verificationRecords.map((verification) => stringValue(verification.status)),
  ].filter((value): value is string => value !== null);
  const hasConflicts =
    flattenReason(item.conflicts).length > 0 ||
    flattenReason(verification?.conflicts).length > 0 ||
    verificationRecords.some((itemVerification) => flattenReason(itemVerification.conflicts).length > 0);
  return hasConflicts || statuses.some((status) =>
    ["abstained", "insufficient_evidence", "failed", "conflict", "conflicted", "unsupported"].includes(status),
  );
}

function PendingItemSummary({ item }: { item: AnnualReportRecord }) {
  const id =
    stringValue(item.fact_id) ??
    stringValue(item.calculation_id) ??
    stringValue(item.claim_id) ??
    stringValue(item.evidence_id) ??
    "未提供对象 ID";
  const verificationRecords = Array.isArray(item.verifications)
    ? item.verifications.map(record).filter((value): value is AnnualReportRecord => value !== null)
    : [];
  const statusRecords = [
    stringValue(item.status),
    stringValue(item.verification_status),
    stringValue(record(item.independent_verification)?.status),
    stringValue(record(item.verification)?.status),
    ...verificationRecords.map((verification) => stringValue(verification.status)),
  ].filter((value): value is string => value !== null);
  const reasons: string[] = [];
  appendReason(reasons, "原因", item.reason);
  appendReason(reasons, "失败原因", item.failure_reason);
  appendReason(reasons, "放置原因", item.placement_reasons);
  appendReason(reasons, "限制", item.limitations);
  appendReason(reasons, "核验说明", record(item.independent_verification)?.reason);
  appendReason(reasons, "核验说明", record(item.verification)?.reason);
  appendReason(reasons, "核验限制", record(item.verification)?.limitations);
  appendReason(reasons, "核验冲突", record(item.verification)?.conflicts);
  for (const verification of verificationRecords) {
    appendReason(reasons, "核验限制", verification.limitations);
    appendReason(reasons, "核验冲突", verification.conflicts);
    appendReason(reasons, "核验说明", verification.reason);
  }
  const uniqueReasons = Array.from(new Set(reasons));
  const statuses = Array.from(new Set(statusRecords.map(pendingStatusText)));
  return (
    <span className="annual-pending-summary">
      <code>{id}</code>
      {statuses.length > 0 ? <strong>{statuses.join(" / ")}</strong> : null}
      {uniqueReasons.length > 0 ? (
        <ul>
          {uniqueReasons.map((reason, index) => <li key={index}>{reason}</li>)}
        </ul>
      ) : (
        <span className="sub">报告未记录具体原因。</span>
      )}
    </span>
  );
}

function appendReason(target: string[], label: string, value: unknown): void {
  for (const message of flattenReason(value)) {
    target.push(label + "：" + message);
  }
}

function flattenReason(value: unknown): string[] {
  if (typeof value === "string") {
    return value.trim() === "" ? [] : [value.trim()];
  }
  if (Array.isArray(value)) {
    return value.flatMap(flattenReason);
  }
  const object = record(value);
  if (object === null) {
    return [];
  }
  const keys = ["reason", "message", "detail", "description", "conflict", "conflicts", "expected", "actual"];
  return keys.flatMap((key) => flattenReason(object[key]));
}

function pendingStatusText(status: string): string {
  const labels: Record<string, string> = {
    abstained: "弃权",
    insufficient_evidence: "证据不足",
    failed: "失败",
    conflict: "存在冲突",
    conflicted: "存在冲突",
    unsupported: "缺少支持依据",
    pending_review: "待核查",
    unverified: "未核验",
    unknown: "状态未知",
  };
  return labels[status] ?? status;
}

function recordIdentity(item: AnnualReportRecord): string {
  return (
    stringValue(item.fact_id) ??
    stringValue(item.calculation_id) ??
    stringValue(item.claim_id) ??
    stringValue(item.evidence_id) ??
    "item"
  );
}

function record(value: unknown): AnnualReportRecord | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as AnnualReportRecord)
    : null;
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

function nonEmpty(value: string | null): string | null {
  return value !== null && value.trim() !== "" ? value : null;
}

function idList(ids: string[]): React.ReactNode {
  if (ids.length === 0) {
    return "无";
  }
  return ids.map((id) => <code className="annual-id-chip" key={id}>{id}</code>);
}

function manifestStatusText(status: string): string {
  return status === "completed"
    ? "已完成"
    : status === "completed_with_issues"
      ? "完成但有问题"
      : "状态未知";
}

function comparabilityStatusText(status: string): string {
  return status === "verified" ? "可比性检查通过" : status === "abstained" ? "可比性弃权" : "可比性未确认";
}

function restatementStatusText(status: string): string {
  const labels: Record<string, string> = {
    restated: "已追溯调整",
    not_restated: "报告称未追溯调整",
    unknown: "未知",
    not_applicable: "不适用",
  };
  return labels[status] ?? status;
}

function comparisonRoleText(role: string): string {
  return role === "current" ? "本期" : role === "comparative" ? "比较期" : "口径：" + role;
}

function statementTypeText(type: string | undefined): string {
  if (type === "income_statement") {
    return "利润表";
  }
  if (type === "balance_sheet") {
    return "资产负债表";
  }
  if (type === "cash_flow_statement") {
    return "现金流量表";
  }
  return type ?? "报表类型未记录";
}

function scopeText(scope: string): string {
  return scope === "consolidated" ? "合并口径" : scope === "parent" ? "母公司口径" : scope;
}

function calculationStatusText(status: string): string {
  return status === "succeeded" ? "计算完成" : status === "failed" ? "计算失败" : status;
}

function verificationStatusText(status: string): string {
  return status === "verified" ? "已核验" : status === "failed" ? "核验失败" : status === "abstained" ? "弃权" : "待核验";
}

function claimTypeText(type: string): string {
  const labels: Record<string, string> = {
    fact: "事实主张",
    calculation: "计算主张",
    inference: "推论",
    hypothesis: "假设",
  };
  return labels[type] ?? type;
}

function candidateStatusText(status: string): string {
  if (status === "candidate") {
    return "候选待核查";
  }
  if (status === "abstained") {
    return "弃权";
  }
  if (status === "not_triggered") {
    return "规则未触发";
  }
  return status;
}

function signalTitle(signalId: string): string {
  const labels: Record<string, string> = {
    profit_up_cash_down: "归母净利润与经营现金流差额方向相反",
    revenue_up_cash_down: "营业收入与经营现金流差额方向相反",
  };
  return labels[signalId] ?? "待核查候选线索";
}

function modelFieldLabel(key: string): string {
  const labels: Record<string, string> = {
    status: "记录状态",
    summary: "摘要",
    explanation: "解释",
    interpretation: "解释",
    interpretations: "解释内容",
    abstention: "弃权说明",
    abstentions: "弃权记录",
    abstention_reason: "弃权理由",
    abstention_reasons: "弃权理由",
    items: "解释与弃权条目",
    reason: "理由",
    reason_detail: "补充详情",
    limitations: "限制",
    follow_up_items: "后续核查",
    model_called: "实际模型调用",
    model_call_count: "模型调用次数",
    model_call_attempt_count: "模型调用尝试次数",
    model_name: "模型名称",
    source_binding: "来源绑定",
    audit_artifacts: "调用审计记录",
    archive_path: "调查归档路径",
    narrative_evidence_ids: "叙述证据编号",
    title: "标题",
    text: "内容",
    claim_id: "主张 ID",
    fact_id: "事实 ID",
    calculation_id: "计算 ID",
    verification_status: "核验状态",
    verification_evidences: "核验证据",
    evidence_ids: "证据 ID",
    placement_reasons: "放置原因",
    abstention_status: "弃权状态",
    topic: "主题",
    confidence: "模型置信度",
    rationale: "理由",
  };
  return labels[key] ?? key;
}

function isTechnicalModelField(key: string): boolean {
  return /(^|_)(path|trace|request|response|payload|prompt|token|usage|debug|raw)(_|$)/i.test(key);
}

function errorMessage(error: unknown): string {
  if (error instanceof AnnualReportApiError) {
    return error.message;
  }
  if (error instanceof Error && error.message.trim() !== "") {
    return error.message;
  }
  return "年度报告请求未完成。";
}
