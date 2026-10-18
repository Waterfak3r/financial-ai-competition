import type { AnnualAnalysisResponse, AnnualIssuePipeline, AnnualReportRecord } from "../types/annualReport";
import type { AnnualAnalysisJobMode } from "../types/annualAnalysisJob";
import type { AnnualDiscoveryEvidenceIndexItem, AnnualDiscoveryFinding, AnnualModelDiscovery } from "../types/discoveryEvidence";
import "../styles/annual-report-overview.css";

const HIGHLIGHT_LIMIT = 3;
const CITATION_LIMIT = 3;
const SUPPLEMENT_LIMIT = 2;
const MODEL_VIEW_TAG = "重点排查";

export interface AnnualReportOverviewProps {
  response: AnnualAnalysisResponse;
  activeMode: AnnualAnalysisJobMode | null;
  previewBusyId: string | null;
  onDiscoveryPreview: (evidenceId: string, findingId: string) => void;
  onIssueSupplementPreview: (evidenceId: string) => void;
  onLocateFinding: (findingId: string) => void;
}

interface OverviewCitation {
  evidenceId: string;
  findingId: string;
  page: number | null;
}

interface OverviewSupplement {
  evidenceId: string;
  page: number | null;
  section: string | null;
  roleLabel: string;
}

interface OverviewItem {
  key: string;
  title: string;
  observation: string | null;
  whyItMatters: string | null;
  followUp: string | null;
  explanationNature: string;
  unverifiedLabel: string;
  groupingNote: string | null;
  locateFindingId: string | null;
  citations: OverviewCitation[];
  rejectedCitationCount: number;
  supplements: OverviewSupplement[];
  extraSupplementCount: number;
}

type HighlightSource = "issues" | "pipeline_candidates" | "findings" | "none";
type MemberDisposition = "pending" | "retain" | "merge" | "exclude";
const CANDIDATE_STATUS_LABEL: Record<MemberDisposition, string> = {
  pending: "待核查",
  retain: "重点关注",
  merge: "关联事项",
  exclude: "一般披露",
};

interface LedgerStats {
  trusted: boolean;
  complete: boolean;
  byFinding: Map<string, MemberDisposition>;
  conflictingFindingIds: Set<string>;
  excludedFindingCount: number;
  excludedIssueCount: number;
  retainCount: number;
  mergeCount: number;
  pendingMemberCount: number;
  unmergedCount: number;
  memberCount: number;
}

interface HighlightSelection {
  items: OverviewItem[];
  source: HighlightSource;
  sourceTotal: number;
  presentableIssueCount: number;
  findingCount: number;
  deterministicCount: number;
  abstainedCount: number;
  pendingIssueCount: number;
  pendingFindingCount: number;
  excludedFindingCount: number;
  excludedIssueCount: number;
  retainCount: number;
  mergeCount: number;
  pendingMemberCount: number;
  unmergedCount: number;
  memberCount: number;
  ledgerTrusted: boolean;
  ledgerComplete: boolean;
  nextFindingId: string | null;
}

export function AnnualReportOverview({
  response,
  activeMode,
  previewBusyId,
  onDiscoveryPreview,
  onIssueSupplementPreview,
  onLocateFinding,
}: AnnualReportOverviewProps) {
  const report = response.report;
  const discovery = report.model_discovery ?? null;
  const pipeline = usablePipeline(report.model_issue_pipeline);
  const findings = discovery?.findings ?? [];
  const evidenceById = new Map((response.discovery_evidences?.items ?? []).map((item) => [item.evidence_id, item]));
  const findingsById = new Map(findings.map((item) => [item.finding_id, item]));
  const mode = resolveOverviewMode(activeMode, response.manifest.mode, report.m3_screening != null);
  const aiEvidence = hasAiEvidence(report, discovery);
  const selection = selectHighlights(report, pipeline, findings, findingsById, evidenceById);
  const statusNotes = buildStatusNotes(response, mode, discovery, pipeline, findings.length, selection);
  const remainingInSource = Math.max(0, selection.sourceTotal - selection.items.length);
  const remainingForLedger = remainingInSource > 0
    ? remainingInSource
    : (selection.items.length === 0 && selection.findingCount > 0 ? selection.findingCount : 0);
  const nextFindingId = selection.nextFindingId;

  return (
    <section
      id="annual-report-overview"
      className="annual-report-overview"
      aria-labelledby="annual-report-overview-title"
    >
      <header className="annual-report-overview__header">
        <div className="annual-report-overview__heading">
          <p className="annual-report-overview__eyebrow">重点财务线索与排查建议</p>
          <h3 id="annual-report-overview-title" className="annual-report-overview__title">
            重点核查事项
          </h3>
        </div>
        {selection.items.length > 0 ? (
          <p className="annual-report-overview__limit-note">
            首屏展示需重点关注的财务事项，点击卡片可查看对应成因分析、核查建议与原文出处。
          </p>
        ) : null}
      </header>

      {statusNotes.length > 0 ? (
        <ul className="annual-report-overview__status" aria-label="本次结果状态">
          {statusNotes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      ) : null}

      {selection.items.length === 0 ? (
        <p className="annual-report-overview__empty" role="status">
          {emptyMessage(selection, mode, discovery, pipeline, aiEvidence)}
        </p>
      ) : (
        <ol className={`annual-report-overview__list annual-report-overview__list--cols-${Math.min(selection.items.length, HIGHLIGHT_LIMIT)}`} aria-label="重点待核查事项">
          {selection.items.map((item, index) => (
            <li key={item.key}>
              <OverviewItemCard
                item={item}
                index={index}
                previewBusyId={previewBusyId}
                onDiscoveryPreview={onDiscoveryPreview}
                onIssueSupplementPreview={onIssueSupplementPreview}
                onLocateFinding={onLocateFinding}
              />
            </li>
          ))}
        </ol>
      )}

      <div className="annual-report-overview__footer">
        <p role="status">{footerInventory(selection, remainingInSource)}</p>
        {nextFindingId !== null && remainingForLedger > 0 ? (
          <button
            type="button"
            className="annual-report-overview__more"
            onClick={() => onLocateFinding(nextFindingId)}
            aria-label={`在下方完整列表中定位其余 ${remainingForLedger} 项中的下一项`}
          >
            在下方完整列表查看更多
          </button>
        ) : null}
      </div>
    </section>
  );
}

function OverviewItemCard({
  item,
  index,
  previewBusyId,
  onDiscoveryPreview,
  onIssueSupplementPreview,
  onLocateFinding,
}: {
  item: OverviewItem;
  index: number;
  previewBusyId: string | null;
  onDiscoveryPreview: (evidenceId: string, findingId: string) => void;
  onIssueSupplementPreview: (evidenceId: string) => void;
  onLocateFinding: (findingId: string) => void;
}) {
  const primaryCitation = item.citations[0] ?? null;
  const extraCitations = item.citations.slice(1, CITATION_LIMIT);
  const extraCitationCount = Math.max(0, item.citations.length - CITATION_LIMIT);
  const locateFindingId = item.locateFindingId;
  const hasMoreEvidence = extraCitations.length > 0
    || item.supplements.length > 0
    || item.rejectedCitationCount > 0
    || extraCitationCount > 0
    || item.extraSupplementCount > 0;
  const extraEvidenceSummary = extraEvidenceLabel(extraCitations.length, item.supplements.length, item.rejectedCitationCount);

  return (
    <article className="annual-report-overview__item" aria-labelledby={`annual-report-overview-item-${index}`}>
      <header className="annual-report-overview__item-header">
        <h4 id={`annual-report-overview-item-${index}`} className="annual-report-overview__item-title">
          {item.title}
        </h4>
        <div className="annual-report-overview__tags">
          <p className="annual-report-overview__badge">{item.unverifiedLabel}</p>
          <p className="annual-report-overview__nature">{item.explanationNature}</p>
        </div>
      </header>
      <dl className="annual-report-overview__fields">
        <div>
          <dt>现象与背景</dt>
          <dd>{item.observation ?? "暂无特别异常披露说明。"}</dd>
        </div>
      </dl>
      <div className="annual-report-overview__actions" aria-label={`${item.title} 的原文入口`}>
        {primaryCitation === null ? (
          <p className="annual-report-overview__missing">
            暂无直接原文段落引用。
          </p>
        ) : (
          <CitationButton
            itemTitle={item.title}
            citation={primaryCitation}
            previewBusyId={previewBusyId}
            onDiscoveryPreview={onDiscoveryPreview}
          />
        )}
        {locateFindingId !== null ? (
          <button
            type="button"
            className="annual-report-overview__locate"
            onClick={() => onLocateFinding(locateFindingId)}
            aria-label={`在下方列表中查看「${item.title}」`}
          >
            查看完整详情
          </button>
        ) : null}
      </div>
      <details className="annual-report-overview__fold">
        <summary>成因分析与核查建议</summary>
        <div className="annual-report-overview__fold-body">
          <dl className="annual-report-overview__fields">
            <div>
              <dt>核查原因</dt>
              <dd>{item.whyItMatters ?? "财务数据存在同比或跨表背离。"}</dd>
            </div>
            <div>
              <dt>核查建议</dt>
              <dd>{item.followUp ?? "建议核查相关科目明细账及期后凭证。"}</dd>
            </div>
          </dl>
          {item.groupingNote !== null ? <p className="annual-report-overview__item-note">{item.groupingNote}</p> : null}
        </div>
      </details>
      {hasMoreEvidence ? (
        <details className="annual-report-overview__fold">
          <summary>{extraEvidenceSummary}</summary>
          <div className="annual-report-overview__fold-body">
            <div className="annual-report-overview__actions" aria-label={`${item.title} 的更多引用及补证`}>
              {extraCitations.map((citation) => (
                <CitationButton
                  key={`${citation.evidenceId}-${citation.findingId}`}
                  itemTitle={item.title}
                  citation={citation}
                  previewBusyId={previewBusyId}
                  onDiscoveryPreview={onDiscoveryPreview}
                />
              ))}
              {item.supplements.map((supplement) => (
                <SupplementButton
                  key={supplement.evidenceId}
                  itemTitle={item.title}
                  supplement={supplement}
                  previewBusyId={previewBusyId}
                  onIssueSupplementPreview={onIssueSupplementPreview}
                />
              ))}
            </div>
            {item.rejectedCitationCount > 0 ? (
              <p className="annual-report-overview__missing">
                另有 {item.rejectedCitationCount} 处引用未通过当前来源校验，未请求页图。
              </p>
            ) : null}
            {extraCitationCount > 0 || item.extraSupplementCount > 0 ? (
              <p className="annual-report-overview__item-note">
                {extraCitationCount > 0 ? `其余 ${extraCitationCount} 处发现引用在下方完整列表。` : ""}
                {item.extraSupplementCount > 0 ? `其余 ${item.extraSupplementCount} 条同源补证在下方归并账。` : ""}
              </p>
            ) : null}
          </div>
        </details>
      ) : null}
    </article>
  );
}

function CitationButton({
  itemTitle,
  citation,
  previewBusyId,
  onDiscoveryPreview,
}: {
  itemTitle: string;
  citation: OverviewCitation;
  previewBusyId: string | null;
  onDiscoveryPreview: (evidenceId: string, findingId: string) => void;
}) {
  const busy = previewBusyId === citation.evidenceId;
  const pageText = citation.page === null ? "已校验原文" : `PDF 第 ${citation.page} 页原文`;
  return (
    <button
      type="button"
      className="secondary annual-report-overview__citation"
      onClick={() => onDiscoveryPreview(citation.evidenceId, citation.findingId)}
      disabled={busy}
      aria-label={`查看${itemTitle}的${pageText}`}
    >
      {busy ? "正在读取页图…" : `查看${pageText}`}
    </button>
  );
}

function SupplementButton({
  itemTitle,
  supplement,
  previewBusyId,
  onIssueSupplementPreview,
}: {
  itemTitle: string;
  supplement: OverviewSupplement;
  previewBusyId: string | null;
  onIssueSupplementPreview: (evidenceId: string) => void;
}) {
  const busy = previewBusyId === supplement.evidenceId;
  const pageText = supplement.page === null ? "同源补证" : `PDF 第 ${supplement.page} 页补证`;
  const sectionText = supplement.section === null ? "" : ` · ${supplement.section}`;
  return (
    <button
      type="button"
      className="secondary annual-report-overview__citation"
      onClick={() => onIssueSupplementPreview(supplement.evidenceId)}
      disabled={busy}
      aria-label={`查看${itemTitle}的${pageText}，${supplement.roleLabel}`}
    >
      {busy ? "正在读取页图…" : `查看${pageText}${sectionText}`}
      <span className="annual-report-overview__role">{supplement.roleLabel}</span>
    </button>
  );
}

function extraEvidenceLabel(
  extraCitationButtons: number,
  supplementButtons: number,
  _rejectedCitationCount: number,
): string {
  const parts: string[] = [];
  if (extraCitationButtons > 0) parts.push(`${extraCitationButtons} 处原文引用`);
  if (supplementButtons > 0) parts.push(`${supplementButtons} 条补充披露`);
  if (parts.length === 0) return "更多原文与附注依据";
  return `更多依据 · ${parts.join("、")}`;
}

function selectHighlights(
  report: AnnualAnalysisResponse["report"],
  pipeline: AnnualIssuePipeline | null,
  findings: AnnualDiscoveryFinding[],
  findingsById: Map<string, AnnualDiscoveryFinding>,
  evidenceById: Map<string, AnnualDiscoveryEvidenceIndexItem>,
): HighlightSelection {
  const deterministicCount = report.pending_review.candidate_signals.filter((item) => item.status === "candidate").length;
  const abstainedCount = report.pending_review.candidate_signals.filter((item) => item.status === "abstained").length;
  const closure = pipeline === null ? null : asRecord(pipeline.global_closure);
  const ledger = pipelineLedgerStats(pipeline, findings.length);
  const base = {
    presentableIssueCount: 0,
    findingCount: findings.length,
    deterministicCount,
    abstainedCount,
    pendingIssueCount: closure === null ? 0 : asStringList(closure.pending_issue_ids).length,
    pendingFindingCount: closure === null ? 0 : asStringList(closure.pending_finding_ids).length,
    excludedFindingCount: ledger.excludedFindingCount,
    excludedIssueCount: ledger.excludedIssueCount,
    retainCount: ledger.retainCount,
    mergeCount: ledger.mergeCount,
    pendingMemberCount: ledger.pendingMemberCount,
    unmergedCount: ledger.unmergedCount,
    memberCount: ledger.memberCount,
    ledgerTrusted: ledger.trusted,
    ledgerComplete: ledger.complete,
  };

  if (pipeline !== null) {
    const issuesById = new Map<string, AnnualReportRecord>();
    const issuesByMember = new Map<string, AnnualReportRecord>();
    for (const issue of pipeline.issues) {
      const issueId = asString(issue.issue_id);
      if (issueId !== null) issuesById.set(issueId, issue);
      for (const member of asStringList(issue.member_finding_ids)) {
        issuesByMember.set(member, issue);
      }
    }
    const presentableIssues = orderedPresentableIssues(pipeline, issuesById);
    if (presentableIssues.length > 0) {
      const remaining = presentableIssues.slice(HIGHLIGHT_LIMIT);
      let nextFindingId: string | null = null;
      for (const issue of remaining) {
        nextFindingId = representativeFindingId(issue, asStringList(issue.member_finding_ids), findingsById);
        if (nextFindingId !== null) break;
      }
      if (nextFindingId === null) nextFindingId = findings[0]?.finding_id ?? null;
      return {
        ...base,
        items: presentableIssues.slice(0, HIGHLIGHT_LIMIT).map((issue, index) =>
          itemFromIssue(issue, index, pipeline, report, findingsById, evidenceById),
        ),
        source: "issues",
        sourceTotal: presentableIssues.length,
        presentableIssueCount: presentableIssues.length,
        nextFindingId,
      };
    }

    const pipelineCandidates = orderedPipelineCandidates(pipeline, report, ledger, issuesByMember);
    if (pipelineCandidates.length > 0) {
      let nextFindingId: string | null = null;
      for (const candidate of pipelineCandidates.slice(HIGHLIGHT_LIMIT)) {
        const findingId = asString(candidate.finding_id);
        if (findingId !== null) {
          nextFindingId = findingId;
          break;
        }
      }
      if (nextFindingId === null) nextFindingId = findings[0]?.finding_id ?? null;
      return {
        ...base,
        items: pipelineCandidates.slice(0, HIGHLIGHT_LIMIT).map((candidate, index) => {
          const findingId = asString(candidate.finding_id);
          const disposition = findingId === null || ledger.conflictingFindingIds.has(findingId)
            ? null
            : (ledger.byFinding.get(findingId) ?? null);
          return itemFromPipelineCandidate(
            candidate,
            index,
            report,
            findingsById,
            evidenceById,
            disposition,
            findingId === null ? undefined : issuesByMember.get(findingId),
          );
        }),
        source: "pipeline_candidates",
        sourceTotal: pipelineCandidates.length,
        nextFindingId,
      };
    }

    return {
      ...base,
      items: [],
      source: "none",
      sourceTotal: 0,
      presentableIssueCount: 0,
      nextFindingId: findings[0]?.finding_id ?? null,
    };
  }

  if (findings.length > 0) {
    return {
      ...base,
      items: findings.slice(0, HIGHLIGHT_LIMIT).map((finding, index) => itemFromFinding(finding, index, report, evidenceById)),
      source: "findings",
      sourceTotal: findings.length,
      nextFindingId: findings[HIGHLIGHT_LIMIT]?.finding_id ?? null,
    };
  }
  const SIGNAL_LABELS: Record<string, string> = {
    profit_up_cash_down: "净利润与经营现金流背离",
    revenue_up_cash_down: "营业收入与经营现金流背离",
    consolidated_profit_up_cash_down: "合并净利润与经营现金流背离",
    deducted_parent_profit_share: "扣非净利润占归母净利润比例偏低",
    receivables_growth_gap: "应收账款增幅显著超过营业收入",
    inventory_growth_gap: "存货增幅显著超过营业成本",
  };

  const investigationItems = Array.isArray(report.model_investigation?.items)
    ? (report.model_investigation?.items as unknown as Array<Record<string, unknown>>)
    : [];
  const validInvItems = investigationItems.filter((it) => it.status === "interpretation" && it.explanation);

  if (validInvItems.length > 0) {
    const items: OverviewItem[] = validInvItems.slice(0, HIGHLIGHT_LIMIT).map((invItem, idx) => {
      const sigId = String(invItem.signal_id || "");
      const title = SIGNAL_LABELS[sigId] ?? sigId;
      const rawNids = Array.isArray(invItem.narrative_evidence_ids) ? invItem.narrative_evidence_ids : [];
      const narrativeEvs = Array.isArray(report.model_investigation?.narrative_evidences)
        ? (report.model_investigation.narrative_evidences as unknown as Array<Record<string, unknown>>)
        : [];
      const citations: OverviewCitation[] = rawNids.map((eid) => {
        const matchEv = narrativeEvs.find((ev) => ev.evidence_id === eid);
        return {
          evidenceId: String(eid),
          findingId: `lead:${sigId}`,
          page: typeof matchEv?.pdf_page === "number" ? matchEv.pdf_page : null,
        };
      });
      return {
        key: `inv-${sigId}-${idx}`,
        title,
        observation: String(invItem.explanation),
        whyItMatters: "该财务指标在报告期内存在异常或跨表背离，需核实成因与披露一致性。",
        followUp: "建议核实管理层讨论（MD&A）相关披露及期后结算凭证。",
        explanationNature: "成因分析",
        unverifiedLabel: "重点关注",
        groupingNote: null,
        locateFindingId: null,
        citations,
        rejectedCitationCount: 0,
        supplements: [],
        extraSupplementCount: 0,
      };
    });
    return {
      ...base,
      items,
      source: "findings",
      sourceTotal: validInvItems.length,
      nextFindingId: null,
    };
  }

  const candidates = report.pending_review.candidate_signals.filter((item) => item.status === "candidate");
  if (candidates.length > 0) {
    const items: OverviewItem[] = candidates.slice(0, HIGHLIGHT_LIMIT).map((cand, idx) => {
      const title = SIGNAL_LABELS[cand.signal_id] ?? cand.signal_id;
      return {
        key: `cand-${cand.signal_id}-${idx}`,
        title,
        observation: "根据已核验的财务数据，该指标在本期与比较期之间存在明显差异或跨表背离。",
        whyItMatters: "指标同向或背离变化需要核对管理层讨论（MD&A）成因披露。",
        followUp: "核查对应会计附注与现金流量表补充资料。",
        explanationNature: "财务规则筛查",
        unverifiedLabel: "待核查线索",
        groupingNote: null,
        locateFindingId: null,
        citations: [],
        rejectedCitationCount: 0,
        supplements: [],
        extraSupplementCount: 0,
      };
    });
    return {
      ...base,
      items,
      source: "pipeline_candidates",
      sourceTotal: candidates.length,
      nextFindingId: null,
    };
  }

  return {
    ...base,
    items: [],
    source: "none",
    sourceTotal: 0,
    nextFindingId: null,
  };
}

function itemFromIssue(
  issue: AnnualReportRecord,
  index: number,
  pipeline: AnnualIssuePipeline,
  report: AnnualAnalysisResponse["report"],
  findingsById: Map<string, AnnualDiscoveryFinding>,
  evidenceById: Map<string, AnnualDiscoveryEvidenceIndexItem>,
): OverviewItem {
  const issueId = asString(issue.issue_id) ?? `issue-${index}`;
  const members = asStringList(issue.member_finding_ids);
  const citationScan = collectIssueCitations(issue, members, findingsById, evidenceById, report);
  const supplements = collectIssueSupplements(issueId, pipeline, report);
  const memberCount = members.length;
  let groupingNote: string | null = null;
  if (issue.status === "pending_global_relation" || issue.status === "provisional") {
    groupingNote = "该事项的跨片关系尚未闭合，成员账仍在下方。";
  } else if (memberCount > 1) {
    groupingNote = `已归并 ${memberCount} 条发现成员；完整成员账在下方。`;
  }
  return {
    key: issueId,
    title: asString(issue.title) ?? "未命名已归并事项",
    observation: asString(issue.observation),
    whyItMatters: asString(issue.why_it_matters),
    followUp: asString(issue.follow_up),
    explanationNature: MODEL_VIEW_TAG,
    unverifiedLabel: "待核查 · 已归并事项",
    groupingNote,
    locateFindingId: representativeFindingId(issue, members, findingsById),
    citations: citationScan.accepted,
    rejectedCitationCount: citationScan.rejected,
    supplements: supplements.visible,
    extraSupplementCount: supplements.extra,
  };
}

function itemFromPipelineCandidate(
  candidate: AnnualReportRecord,
  index: number,
  report: AnnualAnalysisResponse["report"],
  findingsById: Map<string, AnnualDiscoveryFinding>,
  evidenceById: Map<string, AnnualDiscoveryEvidenceIndexItem>,
  disposition: MemberDisposition | null,
  issue: AnnualReportRecord | undefined,
): OverviewItem {
  const findingId = asString(candidate.finding_id);
  const finding = findingId === null ? undefined : findingsById.get(findingId);
  const citationScan = collectRecordCitations(asRecordList(candidate.citations), findingId, evidenceById, report);
  return {
    key: findingId ?? `candidate-${index}`,
    title: asString(candidate.title) ?? finding?.title ?? "未命名合格发现",
    observation: asString(candidate.observation) ?? finding?.observation ?? null,
    whyItMatters: asString(candidate.why_it_matters) ?? finding?.why_it_matters ?? null,
    followUp: asString(candidate.follow_up) ?? finding?.follow_up ?? null,
    explanationNature: MODEL_VIEW_TAG,
    unverifiedLabel: disposition === null ? "待核查 · 未归并发现" : CANDIDATE_STATUS_LABEL[disposition],
    groupingNote: candidateGroupingNote(disposition, issue),
    locateFindingId: finding === undefined ? null : finding.finding_id,
    citations: citationScan.accepted,
    rejectedCitationCount: citationScan.rejected,
    supplements: [],
    extraSupplementCount: 0,
  };
}

function itemFromFinding(
  finding: AnnualDiscoveryFinding,
  index: number,
  report: AnnualAnalysisResponse["report"],
  evidenceById: Map<string, AnnualDiscoveryEvidenceIndexItem>,
): OverviewItem {
  const citationScan = collectRecordCitations(asRecordList(finding.citations), finding.finding_id, evidenceById, report);
  return {
    key: finding.finding_id || `finding-${index}`,
    title: finding.title,
    observation: asString(finding.observation),
    whyItMatters: asString(finding.why_it_matters),
    followUp: asString(finding.follow_up),
    explanationNature: MODEL_VIEW_TAG,
    unverifiedLabel: "待核查 · 合格发现",
    groupingNote: null,
    locateFindingId: finding.finding_id,
    citations: citationScan.accepted,
    rejectedCitationCount: citationScan.rejected,
    supplements: [],
    extraSupplementCount: 0,
  };
}

function collectIssueCitations(
  issue: AnnualReportRecord,
  members: string[],
  findingsById: Map<string, AnnualDiscoveryFinding>,
  evidenceById: Map<string, AnnualDiscoveryEvidenceIndexItem>,
  report: AnnualAnalysisResponse["report"],
): { accepted: OverviewCitation[]; rejected: number } {
  const accepted: OverviewCitation[] = [];
  const seen = new Set<string>();
  let rejected = 0;
  for (const citation of asRecordList(issue.citations)) {
    const evidenceId = asString(citation.evidence_id);
    if (evidenceId === null) {
      rejected += 1;
      continue;
    }
    if (seen.has(evidenceId)) continue;
    seen.add(evidenceId);
    if (!sourceMatchesReport(citation, report)) {
      rejected += 1;
      continue;
    }
    const indexed = evidenceById.get(evidenceId);
    const findingId = findingIdForEvidence(evidenceId, members, findingsById, evidenceById);
    if (indexed === undefined || findingId === null || !indexed.finding_ids.includes(findingId)) {
      rejected += 1;
      continue;
    }
    accepted.push({ evidenceId, findingId, page: indexed.pdf_page });
  }
  return { accepted, rejected };
}

function collectRecordCitations(
  citations: AnnualReportRecord[],
  findingId: string | null,
  evidenceById: Map<string, AnnualDiscoveryEvidenceIndexItem>,
  report: AnnualAnalysisResponse["report"],
): { accepted: OverviewCitation[]; rejected: number } {
  const accepted: OverviewCitation[] = [];
  const seen = new Set<string>();
  let rejected = 0;
  for (const citation of citations) {
    const evidenceId = asString(citation.evidence_id);
    if (evidenceId === null) {
      rejected += 1;
      continue;
    }
    if (seen.has(evidenceId)) continue;
    seen.add(evidenceId);
    if (!sourceMatchesReport(citation, report) || findingId === null) {
      rejected += 1;
      continue;
    }
    const indexed = evidenceById.get(evidenceId);
    if (indexed === undefined || !indexed.finding_ids.includes(findingId)) {
      rejected += 1;
      continue;
    }
    accepted.push({ evidenceId, findingId, page: indexed.pdf_page });
  }
  return { accepted, rejected };
}

function collectIssueSupplements(
  issueId: string,
  pipeline: AnnualIssuePipeline,
  report: AnnualAnalysisResponse["report"],
): { visible: OverviewSupplement[]; extra: number } {
  const matched: OverviewSupplement[] = [];
  for (const supplement of pipeline.supplements) {
    const evidenceId = asString(supplement.evidence_id);
    if (evidenceId === null || !sourceMatchesReport(supplement, report)) continue;
    const roles = asRecordList(supplement.issue_roles);
    const listedIds = asStringList(supplement.issue_ids);
    if (!roles.some((role) => asString(role.issue_id) === issueId) && !listedIds.includes(issueId)) continue;
    const matchedRole = roles.find((role) => asString(role.issue_id) === issueId);
    const role = asString(matchedRole?.role) ?? asString(supplement.role);
    const page = supplement.pdf_page;
    matched.push({
      evidenceId,
      page: typeof page === "number" && Number.isInteger(page) ? page : null,
      section: asString(supplement.section),
      roleLabel: role === "context"
        ? "同源上下文，待核查，不是独立核验"
        : role === "possible_explanation"
          ? "模型认为可能解释，待核查"
          : role === "unknown"
            ? "补证角色未知，待核查"
            : supplement.role_status === "pending" && roles.length === 0
              ? "角色尚待标注，不是独立核验"
              : "同源补证，待核查，不是独立核验",
    });
  }
  return {
    visible: matched.slice(0, SUPPLEMENT_LIMIT),
    extra: Math.max(0, matched.length - SUPPLEMENT_LIMIT),
  };
}

function orderedPresentableIssues(
  pipeline: AnnualIssuePipeline,
  issuesById: Map<string, AnnualReportRecord>,
): AnnualReportRecord[] {
  const seen = new Set<string>();
  const ordered: AnnualReportRecord[] = [];
  const order = presentationOrder(pipeline);
  for (const row of order?.issue_order ?? []) {
    const issue = issuesById.get(row.issue_id);
    if (issue === undefined || issue.status === "excluded" || seen.has(row.issue_id)) continue;
    seen.add(row.issue_id);
    ordered.push(issue);
  }
  for (const issue of pipeline.issues) {
    const issueId = asString(issue.issue_id);
    if (issueId === null || issue.status === "excluded" || seen.has(issueId)) continue;
    seen.add(issueId);
    ordered.push(issue);
  }
  return ordered;
}

function orderedPipelineCandidates(
  pipeline: AnnualIssuePipeline,
  report: AnnualAnalysisResponse["report"],
  ledger: LedgerStats,
  issuesByMember: Map<string, AnnualReportRecord>,
): AnnualReportRecord[] {
  const byFinding = new Map<string, AnnualReportRecord>();
  for (const candidate of pipeline.candidates) {
    const findingId = asString(candidate.finding_id);
    const identity = asRecord(candidate.source_identity);
    if (findingId === null) continue;
    if (identity !== null && !sourceMatchesReport(identity, report)) continue;
    if (highlightBlockedByLedger(findingId, ledger, issuesByMember)) continue;
    byFinding.set(findingId, candidate);
  }
  const seen = new Set<string>();
  const ordered: AnnualReportRecord[] = [];
  const order = presentationOrder(pipeline);
  for (const row of order?.candidate_order ?? []) {
    const candidate = byFinding.get(row.finding_id);
    if (candidate === undefined || seen.has(row.finding_id)) continue;
    seen.add(row.finding_id);
    ordered.push(candidate);
  }
  for (const candidate of byFinding.values()) {
    const findingId = asString(candidate.finding_id);
    if (findingId === null || seen.has(findingId)) continue;
    seen.add(findingId);
    ordered.push(candidate);
  }
  return ordered;
}

function buildStatusNotes(
  response: AnnualAnalysisResponse,
  _mode: AnnualAnalysisJobMode | null,
  discovery: AnnualModelDiscovery | null,
  _pipeline: AnnualIssuePipeline | null,
  _findingCount: number,
  _selection: HighlightSelection,
): string[] {
  const report = response.report;
  const notes: string[] = [];
  const review = report.model_review ?? null;
  const aiEvidence = hasAiEvidence(report, discovery);

  if (response.snapshot?.is_partial === true) {
    notes.push("当前展示已保存的阶段性分析结果。");
  }
  if (!aiEvidence) {
    notes.push("当前为本地确定性财务分析，未启用 AI 深度调查。");
  }
  if (review?.status === "completed") {
    if (review.assessment === "prioritize_review") {
      notes.push("AI 综合评审建议对重点事项进行优先人工核查。");
    } else if (review.assessment === "no_priority_issue_identified_within_scope") {
      notes.push("AI 综合评审在当前核查范围内未识别出优先事项。");
    } else if (review.assessment === "insufficient_evidence") {
      notes.push("当前披露依据有限，建议结合年报附注进一步核对。");
    }
  } else if (aiEvidence && review?.status === "failed") {
    notes.push("AI 评审生成未成功，下方保留已提取的财务分析记录供查阅。");
  }
  return notes;
}

function emptyMessage(
  selection: HighlightSelection,
  _mode: AnnualAnalysisJobMode | null,
  _discovery: AnnualModelDiscovery | null,
  _pipeline: AnnualIssuePipeline | null,
  aiEvidence: boolean,
): string {
  if (selection.abstainedCount > 0 && selection.items.length === 0) {
    return "年报原文中暂未检索到相关财务指标的披露成因说明，基础财务核验数据详见下方财务概览。";
  }
  if (!aiEvidence) {
    return "当前为基础财务核验分析，未启用 AI 深度调查。基础核验数据详见下方。";
  }
  return "本次核对范围内暂未发现需要优先核查的重大异常事项。完整指标与附注记录详见下方。";
}

function footerInventory(selection: HighlightSelection, _remainingInSource: number): string {
  const count = selection.items.length;
  if (count > 0) {
    return `已列出 ${count} 个重点排查事项，点击卡片可查看对应原文段落与核查建议。`;
  }
  return "暂无首屏重点事项，完整财务指标与附注核验详见下方各区。";
}

function usablePipeline(value: AnnualIssuePipeline | null | undefined): AnnualIssuePipeline | null {
  if (value == null || value.kind !== "annual_issue_pipeline_v1" || value.source_binding_status !== "matched") {
    return null;
  }
  return value;
}

function pipelineLedgerStats(pipeline: AnnualIssuePipeline | null, findingCount: number): LedgerStats {
  const empty: LedgerStats = {
    trusted: false,
    complete: false,
    byFinding: new Map(),
    conflictingFindingIds: new Set(),
    excludedFindingCount: 0,
    excludedIssueCount: 0,
    retainCount: 0,
    mergeCount: 0,
    pendingMemberCount: 0,
    unmergedCount: 0,
    memberCount: 0,
  };
  if (pipeline === null) return empty;
  const byFinding = new Map<string, MemberDisposition>();
  const conflictingFindingIds = new Set<string>();
  for (const row of pipeline.member_decisions) {
    const findingId = asString(row.finding_id);
    const disposition = asDisposition(row.disposition);
    if (findingId === null || disposition === null) continue;
    if (byFinding.has(findingId)) conflictingFindingIds.add(findingId);
    byFinding.set(findingId, disposition);
  }
  const expectedIds = asStringList(pipeline.input.finding_ids);
  const expected = expectedIds.length > 0 ? expectedIds : pipeline.candidates.map((item) => asString(item.finding_id)).filter((id): id is string => id !== null);
  const uniqueExpected = [...new Set(expected)];
  const complete = conflictingFindingIds.size === 0
    && uniqueExpected.length === expected.length
    && uniqueExpected.every((id) => byFinding.has(id))
    && (uniqueExpected.length > 0 || findingCount === 0);
  let excludedFindingCount = 0;
  let retainCount = 0;
  let mergeCount = 0;
  let pendingMemberCount = 0;
  for (const [findingId, disposition] of byFinding) {
    if (conflictingFindingIds.has(findingId)) continue;
    if (disposition === "exclude") excludedFindingCount += 1;
    else if (disposition === "retain") retainCount += 1;
    else if (disposition === "merge") mergeCount += 1;
    else pendingMemberCount += 1;
  }
  const excludedIssueCount = pipeline.issues.filter((issue) => issue.status === "excluded").length;
  const unmergedCount = uniqueExpected.filter((id) => !byFinding.has(id) || conflictingFindingIds.has(id)).length;
  return {
    trusted: true,
    complete,
    byFinding,
    conflictingFindingIds,
    excludedFindingCount,
    excludedIssueCount,
    retainCount,
    mergeCount,
    pendingMemberCount,
    unmergedCount,
    memberCount: byFinding.size,
  };
}

function highlightBlockedByLedger(
  findingId: string,
  ledger: LedgerStats,
  issuesByMember: Map<string, AnnualReportRecord>,
): boolean {
  if (issuesByMember.get(findingId)?.status === "excluded") return true;
  if (ledger.conflictingFindingIds.has(findingId)) return false;
  return ledger.byFinding.get(findingId) === "exclude";
}

function candidateGroupingNote(disposition: MemberDisposition | null, _issue: AnnualReportRecord | undefined): string | null {
  if (disposition === "pending") return "该线索正在排查中，详情见下方。";
  if (disposition === "merge") return "该线索已与其他关联事项归并展示。";
  if (disposition === "retain") return "该线索已列入重点关注。";
  return null;
}

function presentationOrder(pipeline: AnnualIssuePipeline): AnnualIssuePipeline["presentation_order"] | null {
  const order = pipeline.presentation_order;
  if (order == null || order.schema_version !== "annual_issue_display_order_v1") return null;
  if (!Array.isArray(order.issue_order) || !Array.isArray(order.candidate_order)) return null;
  return order;
}

function representativeFindingId(
  issue: AnnualReportRecord,
  members: string[],
  findingsById: Map<string, AnnualDiscoveryFinding>,
): string | null {
  const retained = asRecordList(issue.member_sources).find((row) => row.disposition === "retain" && asString(row.finding_id) !== null);
  const retainedId = retained === undefined ? null : asString(retained.finding_id);
  if (retainedId !== null && findingsById.has(retainedId)) return retainedId;
  for (const member of members) {
    if (findingsById.has(member)) return member;
  }
  return null;
}

function findingIdForEvidence(
  evidenceId: string,
  members: string[],
  findingsById: Map<string, AnnualDiscoveryFinding>,
  evidenceById: Map<string, AnnualDiscoveryEvidenceIndexItem>,
): string | null {
  const indexed = evidenceById.get(evidenceId);
  if (indexed !== undefined) {
    const hit = indexed.finding_ids.find((id) => members.includes(id));
    if (hit !== undefined) return hit;
  }
  for (const member of members) {
    const finding = findingsById.get(member);
    if (finding === undefined) continue;
    if (finding.evidence_ids.includes(evidenceId) || finding.citations.some((citation) => citation.evidence_id === evidenceId)) {
      return member;
    }
  }
  return null;
}

function sourceMatchesReport(record: AnnualReportRecord, report: AnnualAnalysisResponse["report"]): boolean {
  const documentId = asString(record.source_document_id);
  const sha = asString(record.source_sha256);
  if (documentId !== null && documentId !== report.source_document_id) return false;
  if (sha !== null && sha.toLowerCase() !== report.source_sha256.toLowerCase()) return false;
  return true;
}

function resolveOverviewMode(
  activeMode: AnnualAnalysisJobMode | null,
  manifestMode: AnnualAnalysisResponse["manifest"]["mode"],
  hasM3Screening: boolean,
): AnnualAnalysisJobMode | null {
  if (isAnnualAnalysisJobMode(activeMode)) return activeMode;
  if (isAnnualAnalysisJobMode(manifestMode)) return manifestMode;
  if (hasM3Screening) return "m3_screening";
  return null;
}

function isAnnualAnalysisJobMode(value: unknown): value is AnnualAnalysisJobMode {
  return value === "deterministic" || value === "m3_screening"
    || value === "model_investigation" || value === "integrated_analysis";
}

function hasAiEvidence(
  report: AnnualAnalysisResponse["report"],
  discovery: AnnualModelDiscovery | null,
): boolean {
  return discovery !== null
    || report.model_issue_pipeline != null
    || report.model_called === true
    || (report.model_investigation !== undefined && report.model_investigation !== null);
}

function asDisposition(value: unknown): MemberDisposition | null {
  return value === "pending" || value === "retain" || value === "merge" || value === "exclude" ? value : null;
}

function asRecord(value: unknown): AnnualReportRecord | null {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? value as AnnualReportRecord : null;
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

function asStringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && item !== "") : [];
}

function asRecordList(value: unknown): AnnualReportRecord[] {
  return Array.isArray(value)
    ? value.map(asRecord).filter((item): item is AnnualReportRecord => item !== null)
    : [];
}
