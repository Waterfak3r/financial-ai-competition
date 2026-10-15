import type { AnnualAnalysisResponse, AnnualIssuePipeline, AnnualReportRecord } from "../types/annualReport";
import type { AnnualAnalysisJobMode } from "../types/annualAnalysisJob";
import type { AnnualDiscoveryEvidenceIndexItem, AnnualDiscoveryFinding, AnnualModelDiscovery } from "../types/discoveryEvidence";
import "../styles/annual-report-overview.css";

const HIGHLIGHT_LIMIT = 3;
const CITATION_LIMIT = 3;
const SUPPLEMENT_LIMIT = 2;
const MODEL_VIEW_NATURE = "模型观点，待核查，不是已证实原因、因果或舞弊判断。";
const MODEL_VIEW_TAG = "模型观点 · 待核查";

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
  pending: "待核查 · 待处理成员",
  retain: "待核查 · 已保留发现",
  merge: "待核查 · 已合并成员",
  exclude: "待核查 · 已排除成员",
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
          <p className="annual-report-overview__eyebrow">待核查入口 · 完整列表在下方</p>
          <h3 id="annual-report-overview-title" className="annual-report-overview__title">
            重点事项
          </h3>
        </div>
        {selection.items.length > 0 ? (
          <p className="annual-report-overview__limit-note">
            最多列出 {HIGHLIGHT_LIMIT} 项供首屏核对。{MODEL_VIEW_NATURE}限量重点不代表丢弃后部发现，次序只是核查阅读顺序，不是风险概率。
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
          <dt>观察</dt>
          <dd>{item.observation ?? "报告未记录观察，不补写。"}</dd>
        </div>
      </dl>
      <div className="annual-report-overview__actions" aria-label={`${item.title} 的原文入口`}>
        {primaryCitation === null ? (
          <p className="annual-report-overview__missing">
            没有通过当前来源校验的发现引用。{item.supplements.length > 0 ? "同源补证入口在“更多引用及补证”中，仍待核查。" : ""}
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
            aria-label={`在下方完整列表中定位「${item.title}」`}
          >
            在下方完整列表定位此项
          </button>
        ) : null}
      </div>
      <details className="annual-report-overview__fold">
        <summary>关注理由与待核查动作</summary>
        <div className="annual-report-overview__fold-body">
          <dl className="annual-report-overview__fields">
            <div>
              <dt>关注理由</dt>
              <dd>{item.whyItMatters ?? "原因尚未说明。"}</dd>
            </div>
            <div>
              <dt>待核查动作</dt>
              <dd>{item.followUp ?? "报告未记录待核查动作，不补写。"}</dd>
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
  rejectedCitationCount: number,
): string {
  const parts: string[] = [];
  if (extraCitationButtons > 0) parts.push(`${extraCitationButtons} 处引用`);
  if (supplementButtons > 0) parts.push(`${supplementButtons} 条补证`);
  if (rejectedCitationCount > 0) parts.push(`${rejectedCitationCount} 处未通过校验`);
  if (parts.length === 0) return "更多引用及补证";
  return `更多引用及补证 · ${parts.join("、")}`;
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
  mode: AnnualAnalysisJobMode | null,
  discovery: AnnualModelDiscovery | null,
  pipeline: AnnualIssuePipeline | null,
  findingCount: number,
  selection: HighlightSelection,
): string[] {
  const report = response.report;
  const notes: string[] = [];
  const coverage = discovery?.coverage ?? null;
  const review = report.model_review ?? null;
  const historicInvestigation = report.model_investigation !== undefined && report.model_investigation !== null;
  const aiEvidence = hasAiEvidence(report, discovery);
  const localOnly = !aiEvidence && mode !== "model_investigation" && mode !== "integrated_analysis";
  const aiExpected = mode === "model_investigation" || mode === "integrated_analysis" || aiEvidence;

  if (response.snapshot?.is_partial === true) {
    notes.push(`当前读取的是部分结果快照（序号 ${response.snapshot.sequence}），不表示任务已成功完成。`);
  }
  if (localOnly) {
    notes.push(
      mode === "m3_screening"
        ? "本次为本地四规则试行，未运行云端 AI 发现，不能声称做过公司特有问题扫描。四规则计分不是风险概率。"
        : "本次仅为本地基础数据核验，未运行云端 AI 发现，不能声称做过公司特有问题扫描。",
    );
  }
  if (discovery === null && (historicInvestigation || report.model_called === true || mode === "model_investigation" || mode === "integrated_analysis")) {
    notes.push("旧归档缺少全文发现层，不能视为已扫描整份年报。");
  }
  if (discovery !== null && pipeline === null) {
    notes.push("本归档有全文发现，但缺少可用的问题归并层；下方仍保留完整发现账。");
  }
  if (report.model_issue_pipeline != null && report.model_issue_pipeline.source_binding_status === "mismatch") {
    notes.push("问题归并层未通过来源绑定，本区不使用该层结论。");
  }
  if (discovery !== null) {
    if (coverage?.source_status === "unavailable") {
      notes.push("年报文本解析不可用，未规划扫描批次。模型尝试次数不表示文字已覆盖，也不表示语义质量。");
    } else if (discovery.status === "not_called") {
      notes.push("全文发现未调用。");
    } else if (discovery.status === "failed") {
      notes.push("全文扫描失败。已保存线索仍可查看；覆盖计数不是语义质量。");
    } else if (coverage?.complete === true && discovery.status === "completed") {
      notes.push("已覆盖本次可提取文字。覆盖完整只说明扫描范围，不是语义质量、无风险或舞弊判断。");
    } else {
      const planned = coverage?.planned_batches;
      const processed = coverage?.processed_batches;
      const progress = typeof planned === "number" && typeof processed === "number"
        ? `已结束 ${processed}/${planned} 批。`
        : "存在未完成批次或页面。";
      notes.push(`扫描未完整。${progress}批次数和字符计数不是语义质量。`);
    }
    if (findingCount === 0 && discovery.status !== "not_called") {
      notes.push("本次没有提出带有效原文引用的具体待核查线索；这不代表整份年报没有风险。");
    }
  }
  if (pipeline !== null) {
    const closed = asRecord(pipeline.global_closure)?.closed === true;
    if (pipeline.status === "failed") {
      notes.push("问题归并失败；下方仍保留已有合格发现与处理账。");
    } else if (pipeline.status === "abstained") {
      notes.push("问题归并已中止；下方仍保留已有候选账。");
    } else if (!closed) {
      notes.push("问题归并或跨片关系尚未闭合。本区不把未完成归并当作整份结果。");
    } else if (pipeline.status === "pending" || pipeline.status === "running" || pipeline.status === "partial") {
      notes.push("当前已知问题关系已闭合，但问题处理仍有覆盖或补证缺口。闭合不代表全文或全部处理已完成。");
    }
    if (selection.ledgerTrusted && selection.excludedFindingCount > 0) {
      notes.push(`已排除发现成员 ${selection.excludedFindingCount} 条${selection.excludedIssueCount > 0 ? `、已排除问题 ${selection.excludedIssueCount} 个` : ""}，仅保留在下方完整处理账，不作为首屏待核查候选。排除不是无风险。`);
    }
    if (selection.pendingFindingCount > 0 || selection.pendingIssueCount > 0 || selection.pendingMemberCount > 0) {
      const pendingMembers = Math.max(selection.pendingFindingCount, selection.pendingMemberCount);
      notes.push(`待处理成员 ${pendingMembers} 条、待处理问题 ${selection.pendingIssueCount} 个，完整账在下方。`);
    }
  }
  if (aiExpected && (review === null || review.status !== "completed")) {
    if (review?.status === "failed") notes.push("最终评审未完成（失败）。已有发现仍待核查，评审缺失不能解释为无风险。");
    else if (review?.status === "abstained") notes.push("最终评审已弃权。已有发现仍待核查。");
    else if (review?.status === "not_called") notes.push("最终评审未调用。");
    else notes.push("最终评审未完成。已有线索仍可在下方查看。");
  }
  if (review?.status === "completed" && review.assessment === "no_priority_issue_identified_within_scope") {
    notes.push("最终评审记录了范围内未识别优先事项。这是待核查观点，不能理解为无风险。");
  }
  if (pipeline?.verification_notice) notes.push(pipeline.verification_notice);
  return notes;
}

function emptyMessage(
  selection: HighlightSelection,
  mode: AnnualAnalysisJobMode | null,
  discovery: AnnualModelDiscovery | null,
  pipeline: AnnualIssuePipeline | null,
  aiEvidence: boolean,
): string {
  if (selection.abstainedCount > 0 && selection.deterministicCount === 0 && selection.findingCount === 0) {
    return `没有可展示的重点事项。基础年度比较有 ${selection.abstainedCount} 项因缺少前提而弃权；弃权不是无风险。请先核对其后已核验基础对照。`;
  }
  if (discovery !== null && selection.findingCount === 0) {
    return "没有带有效原文引用的待核查事项可列入首屏；这不代表整份年报没有风险。完整扫描与缺口说明仍在下方。";
  }
  if (selection.ledgerTrusted && selection.findingCount > 0 && selection.items.length === 0) {
    const parts = ["当前没有可列入首屏的待核查事项"];
    if (selection.ledgerComplete) {
      if (selection.excludedFindingCount > 0) {
        parts.push(`处理账已排除 ${selection.excludedFindingCount} 条发现成员`);
      }
      if (selection.pendingMemberCount > 0) {
        parts.push(`另有 ${selection.pendingMemberCount} 条成员待处理`);
      }
      if (selection.unmergedCount > 0) {
        parts.push(`另有 ${selection.unmergedCount} 条尚未计入处理账`);
      }
    } else {
      parts.push("当前处理账不完整，本区不把缺失处置冒充为已排除或无风险");
    }
    parts.push("排除或待处理不是公司无风险");
    parts.push(`下方完整列表仍保留发现 ${selection.findingCount} 条及完整成员账`);
    return `${parts.join("。")}。`;
  }
  if (!aiEvidence) {
    if (mode === "m3_screening") {
      return "本次为四规则试行，没有公司特有发现可列作重点事项。请先核对其后已核验基础对照；四规则计分不是风险概率。";
    }
    return "本次未运行云端 AI 发现，没有公司特有事项可列作重点。请先核对其后已核验基础对照与证据入口。";
  }
  if (discovery === null && pipeline === null) {
    return "本归档没有全文发现或问题归并层，不能视为已扫描整份年报。请先核对其后已核验基础对照；完整核验记录仍在下方，空缺不是无风险。";
  }
  return "当前没有可列入首屏的重点事项；完整发现、归并账或基础比较仍在下方。空缺不是无风险。";
}

function footerInventory(selection: HighlightSelection, remainingInSource: number): string {
  const parts = [
    `本区列出 ${selection.items.length} 项`,
    `下方完整列表仍保留发现 ${selection.findingCount} 条`,
  ];
  if (selection.presentableIssueCount > 0) parts.push(`已归并可展示问题 ${selection.presentableIssueCount} 个`);
  if (selection.ledgerTrusted && selection.memberCount > 0) {
    parts.push(`完整成员 ${selection.memberCount} 条（保留 ${selection.retainCount}、合并 ${selection.mergeCount}、待处理 ${selection.pendingMemberCount}、排除 ${selection.excludedFindingCount}）`);
  }
  if (selection.deterministicCount > 0) parts.push(`基础比较候选 ${selection.deterministicCount} 项`);
  if (selection.abstainedCount > 0) parts.push(`基础比较弃权 ${selection.abstainedCount} 项`);
  if (remainingInSource > 0) parts.push(`本源其余 ${remainingInSource} 项未丢弃`);
  if (selection.items.length === 0 && selection.findingCount > 0) parts.push("长尾发现仍在下方完整详情");
  if (selection.source === "findings" || selection.source === "pipeline_candidates") {
    parts.push("所列发现不是完整归并结果");
  }
  return `${parts.join("；")}。`;
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

function candidateGroupingNote(disposition: MemberDisposition | null, issue: AnnualReportRecord | undefined): string | null {
  if (disposition === "pending") return "该发现成员尚未完成归并处理，完整账在下方。";
  if (disposition === "merge") return "该发现已合并到其他事项成员账，完整账在下方。";
  if (disposition === "retain" && (issue?.status === "pending_global_relation" || issue?.status === "provisional")) {
    return "该发现已保留，但事项关系尚未闭合，完整账在下方。";
  }
  if (disposition === null) return "该发现尚未计入可信处理账，不把缺失处置写成已排除。";
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
