import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent, MouseEvent, ReactNode, RefObject } from "react";
import { loadAnnualAnalysisCapabilities } from "../api/annualAnalysisCapabilities";
import { AnnualReportApiError, downloadAnnualAnalysisExport, loadAnnualAnalysis, loadVerificationEvidencePreview } from "../api/annualReports";
import { loadAnnualAnalysisHistory } from "../api/annualAnalysisHistory";
import { createAnnualAnalysisJob, loadAnnualAnalysisJob } from "../api/annualAnalysisJobs";
import { loadDiscoveryEvidencePreview } from "../api/discoveryEvidences";
import { loadNarrativeEvidences, loadNarrativeEvidencePreview } from "../api/narrativeEvidences";
import { textPdfUploadIssues, uploadTextPdf } from "../api/prechecks";
import type {
  AnnualAnalysisResponse,
  AnnualAnalysisModelReview,
  AnnualReportRecord,
  CandidateSignal,
  ConfirmedCalculation,
  ConfirmedMetric,
  PdfBox,
  M3AnnualScreening,
  VerificationEvidence,
  VerifiedClaim,
} from "../types/annualReport";
import type { AnnualDiscoveryEvidenceIndexItem, AnnualModelDiscovery } from "../types/discoveryEvidence";
import type { AnnualAnalysisHistoryItem } from "../types/annualAnalysisHistory";
import { NARRATIVE_EVIDENCE_INTERPRETATION_LABEL } from "../types/narrativeEvidence";
import type { NarrativeEvidence, NarrativeEvidenceResponse } from "../types/narrativeEvidence";
import type { AnnualAnalysisJob, AnnualAnalysisJobMode, AnnualAnalysisJobRequest } from "../types/annualAnalysisJob";
import type { TextPdfUploadReceipt } from "../types/precheck";

const SAMPLE_RUN_ID = "annual-analysis-21d8da07-097b-4cdf-97ce-6e109c8cc1b8";
const RUN_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$/;
const JOB_ID_PATTERN = /^annual-job-[0-9a-f]{32}$/;
const ACTIVE_JOB_STORAGE_KEY = "fintrace.activeAnnualAnalysisJobId.v1";
const POLL_INTERVAL_MS = 2000;
const HAITIAN_SAMPLE_REQUEST = {
  company_id: "603288",
  report_year: 2024,
  document_id: "cninfo-1222994233",
  source_pdf_path: "603288/2024/cninfo-1222994233/1222994233.PDF",
  sha256: "5a97b13534438f5e85249752ef492fbd9e23af73e67845e47bad1ab7d92e20ee",
} as const;
const CORE_METRICS = [
  { metricId: "revenue", label: "营业收入", tone: "blue" },
  { metricId: "net_profit_parent", label: "归属于母公司股东的净利润", tone: "green" },
  { metricId: "operating_cash_flow", label: "经营活动产生的现金流量净额", tone: "orange" },
] as const;

type JobAction = "uploading" | "starting" | "retrying" | "refreshing" | null;
interface JobFormErrors {
  file?: string;
  companyId?: string;
  reportYear?: string;
}

interface UploadedFileContext {
  file: File;
  companyId: string;
  reportYear: string;
  receipt: TextPdfUploadReceipt;
}

interface Preview {
  kind: "verified_financial_value" | "narrative_context" | "annual_discovery";
  evidenceId: string;
  pageNumber: number;
  objectUrl: string;
  mode: "focus" | "full_page";
  region: PdfBox;
}

type AnnualSourceChoice = "sample" | "upload";
type UrlSelection = { kind: "run"; id: string } | { kind: "job"; id: string } | null;
type NarrativeLoadState = { runId: string; value: NarrativeEvidenceResponse | null; error: string | null } | null;
type PreviewTarget =
  | { kind: "verified_financial_value"; evidenceId: string }
  | { kind: "narrative_context"; evidenceId: string }
  | { kind: "annual_discovery"; evidenceId: string; findingId: string };

interface SourcePage {
  pageNumber: number;
  evidenceIds: string[];
}

interface SourceLocation {
  key: string;
  metricLabel: string;
  metricKey: string;
  periodYear: string | null;
  source: "verified" | "pending";
  pages: SourcePage[];
}

interface SourceLocationResult {
  locations: SourceLocation[];
  unlocatedCount: number;
}

export function AnnualAnalysisPage({ onOpenSettings }: { onOpenSettings: () => void }) {
  const [runId, setRunId] = useState(SAMPLE_RUN_ID);
  const [sourceChoice, setSourceChoice] = useState<AnnualSourceChoice>("sample");
  const [companyId, setCompanyId] = useState("");
  const [reportYear, setReportYear] = useState("");
  const [pdfFile, setPdfFile] = useState<File | null>(null);
  const [mode, setMode] = useState<AnnualAnalysisJobMode>("deterministic");
  const [modelCapability, setModelCapability] = useState<"ready" | "not_configured" | "dependency_unavailable" | null>(null);
  const [modelDiscoverySupported, setModelDiscoverySupported] = useState<boolean | null>(null);
  const [modelCapabilityLoading, setModelCapabilityLoading] = useState(false);
  const [modelCapabilityError, setModelCapabilityError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<JobFormErrors>({});
  const [uploadReceipt, setUploadReceipt] = useState<TextPdfUploadReceipt | null>(null);
  const [uploadedFileContext, setUploadedFileContext] = useState<UploadedFileContext | null>(null);
  const [job, setJob] = useState<AnnualAnalysisJob | null>(null);
  const [jobAction, setJobAction] = useState<JobAction>(null);
  const [jobError, setJobError] = useState<string | null>(null);
  const [restoreError, setRestoreError] = useState<string | null>(null);
  const [restoreBusy, setRestoreBusy] = useState(false);
  const [response, setResponse] = useState<AnnualAnalysisResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [runIdError, setRunIdError] = useState<string | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [previewTarget, setPreviewTarget] = useState<PreviewTarget | null>(null);
  const [previewBusyId, setPreviewBusyId] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewZoom, setPreviewZoom] = useState(100);
  const [narrativeState, setNarrativeState] = useState<NarrativeLoadState>(null);
  const [history, setHistory] = useState<Awaited<ReturnType<typeof loadAnnualAnalysisHistory>> | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [historyOffset, setHistoryOffset] = useState(0);
  const [historyRetry, setHistoryRetry] = useState(0);
  const [selectedHistoryItem, setSelectedHistoryItem] = useState<AnnualAnalysisHistoryItem | null>(null);
  const [resultOrigin, setResultOrigin] = useState<"new" | "history">("history");
  const urlSelectionOnMount = useRef<UrlSelection>(readUrlSelection());
  const initialSelectionHandled = useRef(false);
  const historyRequestSequence = useRef(0);
  const narrativeRequestSequence = useRef(0);
  const createdJobIds = useRef(new Set<string>());
  const evidenceDrawerRef = useRef<HTMLElement>(null);
  const evidenceDrawerCloseRef = useRef<HTMLButtonElement>(null);
  const evidenceReturnFocus = useRef<HTMLElement | null>(null);
  const requestLock = useRef(false);
  const jobSubmitLock = useRef(false);
  const jobRefreshLock = useRef(false);
  const jobReadSequence = useRef(0);
  const reportRequest = useRef(0);
  const taskGeneration = useRef(0);
  const previewRequest = useRef(0);

  useEffect(() => {
    const requestOffset = historyOffset;
    const requestSequence = historyRequestSequence.current + 1;
    historyRequestSequence.current = requestSequence;
    setHistoryLoading(true);
    setHistoryError(null);
    loadAnnualAnalysisHistory(20, requestOffset)
      .then((loaded) => {
        if (historyRequestSequence.current !== requestSequence) return;
        setHistory(loaded);
      })
      .catch((error: unknown) => {
        if (historyRequestSequence.current === requestSequence) setHistoryError(errorMessage(error));
      })
      .finally(() => {
        if (historyRequestSequence.current === requestSequence) setHistoryLoading(false);
      });
    return () => {
      if (historyRequestSequence.current === requestSequence) historyRequestSequence.current += 1;
    };
  }, [historyOffset, historyRetry]);

  useEffect(() => {
    if (history === null) return;
    const selection = readUrlSelection();
    if (selection === null) return;
    const match = history.items.find((item) =>
      selection.kind === "run" ? item.run_id === selection.id : item.job_id === selection.id,
    ) ?? null;
    setSelectedHistoryItem(match);
  }, [history]);

  const refreshModelCapability = useCallback(async () => {
    setModelCapabilityLoading(true);
    setModelCapabilityError(null);
    try {
      const capabilities = await loadAnnualAnalysisCapabilities();
      setModelCapability(capabilities.model_investigation.status);
      setModelDiscoverySupported(capabilities.model_investigation.annual_discovery_supported === true);
    } catch {
      setModelCapability(null);
      setModelDiscoverySupported(null);
      setModelCapabilityError("暂时无法确认 AI 服务状态。确定性分析仍可使用。");
    } finally {
      setModelCapabilityLoading(false);
    }
  }, []);

  useEffect(() => {
    void refreshModelCapability();
  }, [refreshModelCapability]);

  const loadReportForJob = useCallback(async (completedJob: AnnualAnalysisJob, generation: number) => {
    if (!isSuccessfulJob(completedJob) || completedJob.run_id === null) {
      return;
    }
    const requestId = reportRequest.current + 1;
    reportRequest.current = requestId;
    requestLock.current = true;
    setLoading(true);
    setLoadError(null);
    setPreviewError(null);
    setPreview(null);
    setPreviewBusyId(null);
    previewRequest.current += 1;
    try {
      const loaded = await loadAnnualAnalysis(completedJob.run_id);
      if (taskGeneration.current !== generation || reportRequest.current !== requestId) {
        return;
      }
      setResponse(loaded);
      setRunId(loaded.report.run_id);
      setResultOrigin(createdJobIds.current.has(completedJob.job_id) ? "new" : "history");
      const requiresDiscoveryLayer =
        createdJobIds.current.has(completedJob.job_id) || completedJob.discovery_progress !== undefined;
      const missingLayers = missingRequestedReportLayers(completedJob.request.mode, loaded.report, requiresDiscoveryLayer);
      if (missingLayers.length > 0) {
        setJobError(`任务已完成，但报告缺少所选的${missingLayers.join("、")}；请核对归档后再重试。`);
      }
    } catch (error) {
      if (taskGeneration.current === generation && reportRequest.current === requestId) {
        setLoadError(errorMessage(error));
        setJobError("任务已完成，但同一 run_id 的报告读取失败。可重新读取报告或重试状态查询。");
      }
    } finally {
      if (taskGeneration.current === generation && reportRequest.current === requestId) {
        requestLock.current = false;
        setLoading(false);
      }
    }
  }, []);

  useEffect(
    () => () => {
      if (preview !== null) {
        URL.revokeObjectURL(preview.objectUrl);
      }
    },
    [preview],
  );

  useEffect(() => {
    if (previewTarget === null) {
      return;
    }
    evidenceDrawerCloseRef.current?.focus({ preventScroll: true });
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeEvidenceDrawer();
        return;
      }
      if (event.key !== "Tab") return;
      const drawer = evidenceDrawerRef.current;
      if (drawer === null) return;
      const focusable = Array.from(drawer.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [href], [tabindex]:not([tabindex="-1"])',
      )).filter((element) => {
        if (element.getAttribute("aria-hidden") === "true" || element.closest("[hidden], [inert]")) return false;
        const closedDetails = element.closest("details:not([open])");
        if (closedDetails !== null && element !== closedDetails.querySelector(":scope > summary")) return false;
        const style = window.getComputedStyle(element);
        return style.display !== "none" && style.visibility !== "hidden" && element.getClientRects().length > 0;
      });
      if (focusable.length === 0) {
        event.preventDefault();
        drawer.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [previewTarget]);

  const previousDrawerOpen = useRef(false);
  useEffect(() => {
    if (previewTarget !== null) {
      previousDrawerOpen.current = true;
      return;
    }
    if (previousDrawerOpen.current) {
      previousDrawerOpen.current = false;
      if (evidenceReturnFocus.current?.isConnected) evidenceReturnFocus.current.focus({ preventScroll: true });
      else document.getElementById("annual-report-heading")?.focus({ preventScroll: true });
      evidenceReturnFocus.current = null;
    }
  }, [previewTarget]);
  useEffect(
    () => () => {
      previewRequest.current += 1;
    },
    [],
  );

  useEffect(() => {
    if (response === null) {
      return;
    }
    const heading = document.getElementById("annual-report-heading");
    if (heading === null) {
      return;
    }
    heading.focus({ preventScroll: true });
    heading.scrollIntoView({
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
      block: "start",
    });
  }, [response]);

  useEffect(() => {
    if (response === null) {
      setNarrativeState(null);
      narrativeRequestSequence.current += 1;
      return;
    }
    const requestSequence = narrativeRequestSequence.current + 1;
    narrativeRequestSequence.current = requestSequence;
    setNarrativeState({ runId: response.report.run_id, value: null, error: null });
    loadNarrativeEvidences(
      response.report.run_id,
      response.report.source_document_id,
      response.report.source_sha256,
    )
      .then((loaded) => {
        if (narrativeRequestSequence.current === requestSequence) setNarrativeState({ runId: loaded.run_id, value: loaded, error: null });
      })
      .catch((error: unknown) => {
        if (narrativeRequestSequence.current === requestSequence) {
          setNarrativeState({ runId: response.report.run_id, value: null, error: errorMessage(error) });
        }
      });
    return () => {
      if (narrativeRequestSequence.current === requestSequence) narrativeRequestSequence.current += 1;
    };
  }, [response]);

  useEffect(() => {
    if (initialSelectionHandled.current) return;
    initialSelectionHandled.current = true;
    const initialSelection = urlSelectionOnMount.current;
    if (initialSelection !== null) {
      void navigateToSelection(initialSelection, false);
      return;
    }
    const savedJobId = readStoredJobId();
    void navigateToSelection(savedJobId === null ? null : { kind: "job", id: savedJobId }, false);
  }, [loadReportForJob]);

  useEffect(() => {
    const onPopState = () => void navigateToSelection(readUrlSelection(), false);
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [history, loadReportForJob]);

  useEffect(() => {
    if (job === null || !isActiveJob(job)) {
      return;
    }
    const jobId = job.job_id;
    const generation = taskGeneration.current;
    let active = true;
    let timer = 0;
    const poll = async () => {
      if (jobRefreshLock.current) {
        timer = window.setTimeout(poll, POLL_INTERVAL_MS);
        return;
      }
      const readSequence = jobReadSequence.current + 1;
      jobReadSequence.current = readSequence;
      try {
        const current = await loadAnnualAnalysisJob(jobId);
        if (!active || taskGeneration.current !== generation) {
          return;
        }
        if (jobReadSequence.current !== readSequence) {
          timer = window.setTimeout(poll, POLL_INTERVAL_MS);
          return;
        }
        setJob(current);
        setJobError(null);
        if (isSuccessfulJob(current)) {
          void loadReportForJob(current, generation);
          return;
        }
        if (isFailedJob(current)) {
          return;
        }
        timer = window.setTimeout(poll, POLL_INTERVAL_MS);
      } catch (error) {
        if (!active || taskGeneration.current !== generation) {
          return;
        }
        if (jobReadSequence.current !== readSequence) {
          timer = window.setTimeout(poll, POLL_INTERVAL_MS);
          return;
        }
        setJobError("状态暂时无法读取：" + errorMessage(error));
        timer = window.setTimeout(poll, POLL_INTERVAL_MS * 3);
      }
    };
    timer = window.setTimeout(poll, POLL_INTERVAL_MS);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [job?.job_id, job?.status, loadReportForJob]);

  async function submitJob(
    requestFactory: () => Promise<AnnualAnalysisJobRequest> | AnnualAnalysisJobRequest,
    action: Exclude<JobAction, null>,
    requestedMode: AnnualAnalysisJobRequest["mode"] = mode,
  ) {
    if (jobSubmitLock.current || jobRefreshLock.current) {
      return;
    }
    if (modeUsesModel(requestedMode) && modelDiscoverySupported !== true) {
      setJobError("AI 全文分析不可用：当前后端尚未加载全文分析版本，请重启后端。标准分析和本地试行筛查仍可使用。");
      return;
    }
    if (modeUsesModel(requestedMode) && modelCapability !== "ready") {
      setJobError(
        modelCapability === "not_configured"
          ? "AI 分析不可用：后端尚未配置云端模型。标准分析仍可单独运行。"
          : modelCapability === "dependency_unavailable"
            ? "AI 分析不可用：后端缺少所需依赖。标准分析仍可单独运行。"
            : "暂时无法确认 AI 服务状态。确定性分析仍可使用。",
      );
      return;
    }
    jobSubmitLock.current = true;
    const previousJob = job;
    const generation = taskGeneration.current + 1;
    taskGeneration.current = generation;
    reportRequest.current += 1;
    requestLock.current = false;
    setLoading(false);
    setResponse(null);
    setLoadError(null);
    setNarrativeState(null);
    setPreview(null);
    setPreviewTarget(null);
    setPreviewError(null);
    setPreviewBusyId(null);
    previewRequest.current += 1;
    setJob(null);
    setJobAction(action);
    setJobError(null);
    setRestoreError(null);
    try {
      const request = await requestFactory();
      if (taskGeneration.current !== generation) {
        return;
      }
      setJobAction(action === "retrying" ? "retrying" : "starting");
      const created = await createAnnualAnalysisJob(request);
      if (taskGeneration.current !== generation) {
        return;
      }
      setJob(created);
      createdJobIds.current.add(created.job_id);
      setResultOrigin("new");
      setSelectedHistoryItem(null);
      writeStoredJobId(created.job_id);
      writeUrlSelection({ kind: "job", id: created.job_id }, true);
      if (created.run_id !== null) {
        setRunId(created.run_id);
      }
      if (isSuccessfulJob(created)) {
        void loadReportForJob(created, generation);
      }
    } catch (error) {
      if (taskGeneration.current === generation) {
        setJob(previousJob);
        setJobError(errorMessage(error));
      }
    } finally {
      if (taskGeneration.current === generation) {
        setJobAction(null);
      }
      jobSubmitLock.current = false;
    }
  }

  async function onUploadAndStart(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const nextCompanyId = companyId.trim();
    const nextReportYear = reportYear.trim();
    const issues = textPdfUploadIssues(pdfFile, nextCompanyId, nextReportYear);
    setCompanyId(nextCompanyId);
    setReportYear(nextReportYear);
    setFieldErrors({ file: issues.file, companyId: issues.companyId, reportYear: issues.reportYear });
    if (issues.file !== undefined || issues.companyId !== undefined || issues.reportYear !== undefined || pdfFile === null) {
      return;
    }
    const selectedFile = pdfFile;
    const reusableUpload =
      uploadedFileContext !== null &&
      uploadedFileContext.file === selectedFile &&
      uploadedFileContext.companyId === nextCompanyId &&
      uploadedFileContext.reportYear === nextReportYear
        ? uploadedFileContext
        : null;
    await submitJob(async () => {
      const receipt =
        reusableUpload?.receipt ?? (await uploadTextPdf(selectedFile, nextCompanyId, nextReportYear));
      setUploadReceipt(receipt);
      setUploadedFileContext({ file: selectedFile, companyId: nextCompanyId, reportYear: nextReportYear, receipt });
      return {
        company_id: nextCompanyId,
        report_year: Number(nextReportYear),
        document_id: receipt.document_id,
        source_pdf_path: receipt.source_pdf_path,
        sha256: receipt.sha256,
        mode,
      };
    }, reusableUpload === null ? "uploading" : "starting");
  }

  async function onRunHaitianSample() {
    await submitJob(
      () => ({ ...HAITIAN_SAMPLE_REQUEST, mode }),
      "starting",
    );
  }

  async function onStartSelectedSource(event: FormEvent<HTMLFormElement>) {
    if (sourceChoice === "sample") {
      event.preventDefault();
      await onRunHaitianSample();
      return;
    }
    await onUploadAndStart(event);
  }

  async function onRetryJob() {
    if (job === null || !canReanalyzeJob(job)) {
      return;
    }
    await submitJob(() => job.request, "retrying", job.request.mode);
  }

  async function onRefreshJob() {
    if (jobAction !== null || jobRefreshLock.current || jobSubmitLock.current) {
      return;
    }
    const jobId = job?.job_id ?? readStoredJobId();
    if (jobId === null) {
      setRestoreError("没有可读取的任务编号。请重新提交分析，或清除此任务记录。");
      return;
    }
    jobRefreshLock.current = true;
    const readSequence = jobReadSequence.current + 1;
    jobReadSequence.current = readSequence;
    const generation = taskGeneration.current;
    setJobAction("refreshing");
    setJobError(null);
    setRestoreError(null);
    try {
      const refreshed = await loadAnnualAnalysisJob(jobId);
      if (taskGeneration.current !== generation) {
        return;
      }
      if (jobReadSequence.current !== readSequence) {
        return;
      }
      setJob(refreshed);
      writeStoredJobId(refreshed.job_id);
      if (refreshed.run_id !== null) {
        setRunId(refreshed.run_id);
      }
      if (isSuccessfulJob(refreshed)) {
        void loadReportForJob(refreshed, generation);
      }
    } catch (error) {
      if (taskGeneration.current === generation) {
        if (job === null) {
          setRestoreError(errorMessage(error));
        } else {
          setJobError("状态读取失败：" + errorMessage(error));
        }
      }
    } finally {
      if (taskGeneration.current === generation) {
        setJobAction(null);
      }
      jobRefreshLock.current = false;
    }
  }

  async function loadReportByRunId(value: string, updateUrl = true) {
    const checkedRunId = value.trim();
    setRunId(checkedRunId);
    if (!RUN_ID_PATTERN.test(checkedRunId)) {
      setRunIdError("run_id 只能包含字母、数字、点、下划线和短横线，长度不超过 121 个字符。");
      return;
    }
    setRunIdError(null);
    await navigateToSelection({ kind: "run", id: checkedRunId }, updateUrl);
  }

  async function onLoad(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await loadReportByRunId(runId);
  }

  async function navigateToSelection(selection: UrlSelection, updateUrl: boolean) {
    if (updateUrl) writeUrlSelection(selection, true);
    const generation = taskGeneration.current + 1;
    taskGeneration.current = generation;
    reportRequest.current += 1;
    requestLock.current = false;
    jobReadSequence.current += 1;
    setLoading(false);
    setResponse(null);
    setLoadError(null);
    setRunIdError(null);
    setPreview(null);
    setPreviewTarget(null);
    setPreviewError(null);
    setPreviewBusyId(null);
    setPreviewZoom(100);
    previewRequest.current += 1;
    setNarrativeState(null);
    narrativeRequestSequence.current += 1;
    setJob(null);
    setJobError(null);
    setRestoreError(null);
    setRestoreBusy(false);
    jobRefreshLock.current = false;
    if (selection === null || selection.kind === "run") removeStoredJobId();
    const selected = selection === null
      ? null
      : history?.items.find((item) => selection.kind === "run" ? item.run_id === selection.id : item.job_id === selection.id) ?? null;
    setSelectedHistoryItem(selected);
    if (selection === null) {
      setRunId(SAMPLE_RUN_ID);
      setResultOrigin("history");
      return;
    }
    if (selection.kind === "run") {
      setRunId(selection.id);
      if (!RUN_ID_PATTERN.test(selection.id)) {
        setRunIdError("URL 中的 run_id 格式无效，请检查归档编号。");
        return;
      }
      const requestId = reportRequest.current + 1;
      reportRequest.current = requestId;
      requestLock.current = true;
      setLoading(true);
      setResultOrigin("history");
      try {
        const loaded = await loadAnnualAnalysis(selection.id);
        if (taskGeneration.current === generation && reportRequest.current === requestId) {
          setResponse(loaded);
          setRunId(loaded.report.run_id);
        }
      } catch (error) {
        if (taskGeneration.current === generation && reportRequest.current === requestId) {
          setLoadError(errorMessage(error));
        }
      } finally {
        if (taskGeneration.current === generation && reportRequest.current === requestId) {
          requestLock.current = false;
          setLoading(false);
        }
      }
      return;
    }
    if (!JOB_ID_PATTERN.test(selection.id)) {
      setRestoreError("URL 中的 job_id 格式无效，请检查任务编号。");
      return;
    }
    setRestoreBusy(true);
    try {
      const restored = await loadAnnualAnalysisJob(selection.id);
      if (taskGeneration.current !== generation) return;
      setJob(restored);
      setResultOrigin("history");
      writeStoredJobId(restored.job_id);
      if (restored.run_id !== null) setRunId(restored.run_id);
      if (isSuccessfulJob(restored)) void loadReportForJob(restored, generation);
    } catch (error) {
      if (taskGeneration.current === generation) setRestoreError(errorMessage(error));
    } finally {
      if (taskGeneration.current === generation) setRestoreBusy(false);
    }
  }

  function onOpenHistoryItem(item: AnnualAnalysisHistoryItem) {
    setSelectedHistoryItem(item);
    if (item.run_id !== null && item.result_available) {
      void navigateToSelection({ kind: "run", id: item.run_id }, true);
      return;
    }
    if (item.job_id !== null) {
      void navigateToSelection({ kind: "job", id: item.job_id }, true);
      return;
    }
    setHistoryError(item.issues.map((issue) => issue.message).join("；") || "此历史条目没有可读取的运行报告或任务状态。");
  }

  function onPreview(evidenceId: string) {
    if (response === null || requestLock.current) return;
    evidenceReturnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const evidence = response.verification_evidences.find((item) => item.evidence_id === evidenceId);
    if (evidence === undefined) {
      setPreviewTarget({ kind: "verified_financial_value", evidenceId });
      setPreviewError("该证据不在服务端返回的独立核验证据索引中，未请求页图。");
      return;
    }
    setPreviewTarget({ kind: "verified_financial_value", evidenceId });
    void loadEvidencePreview({ kind: "verified_financial_value", evidenceId }, true);
  }

  function onNarrativePreview(evidenceId: string) {
    if (response === null || requestLock.current) return;
    evidenceReturnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const evidence = narrativeState?.value?.items.find((item) => item.evidence_id === evidenceId);
    if (evidence === undefined) {
      setPreviewTarget({ kind: "narrative_context", evidenceId });
      setPreviewError("该叙述片段不在本次报告绑定的原文定位清单中，未请求页图。");
      return;
    }
    setPreviewTarget({ kind: "narrative_context", evidenceId });
    void loadEvidencePreview({ kind: "narrative_context", evidenceId }, true);
  }

  function onDiscoveryPreview(evidenceId: string, findingId: string) {
    if (response === null || requestLock.current) return;
    evidenceReturnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const evidence = response.discovery_evidences?.items.find((item) => item.evidence_id === evidenceId);
    if (evidence === undefined) {
      setPreviewTarget({ kind: "annual_discovery", evidenceId, findingId });
      setPreviewError("该待核查引用不在当前报告重新核对的原文索引中，未请求页图。");
      return;
    }
    setPreviewTarget({ kind: "annual_discovery", evidenceId, findingId });
    void loadEvidencePreview({ kind: "annual_discovery", evidenceId, findingId }, true);
  }

  async function loadEvidencePreview(target: PreviewTarget, focus: boolean) {
    if (response === null) return;
    const requestId = previewRequest.current + 1;
    previewRequest.current = requestId;
    setPreview(null);
    setPreviewError(null);
    setPreviewBusyId(target.evidenceId);
    setPreviewZoom(100);
    try {
      const loaded = target.kind === "verified_financial_value"
        ? await (async () => {
            const evidence = response.verification_evidences.find((item) => item.evidence_id === target.evidenceId);
            if (evidence === undefined) throw new AnnualReportApiError("该证据不在独立核验证据索引中。");
            return await loadVerificationEvidencePreview(response.report.run_id, evidence, response.manifest.report_sha256, focus);
          })()
        : target.kind === "narrative_context"
          ? await (async () => {
            const evidence = narrativeState?.value?.items.find((item) => item.evidence_id === target.evidenceId);
            if (evidence === undefined) throw new AnnualReportApiError("该片段不在本次报告绑定的原文定位清单中。");
            return await loadNarrativeEvidencePreview(response.report.run_id, evidence, response.manifest.report_sha256, focus);
          })()
          : await (async () => {
              const evidence = response.discovery_evidences?.items.find((item) => item.evidence_id === target.evidenceId);
              if (evidence === undefined) throw new AnnualReportApiError("该发现引用不在当前报告绑定的原文定位清单中。");
              return await loadDiscoveryEvidencePreview(response.report.run_id, evidence, response.manifest.report_sha256, focus);
            })();
      if (previewRequest.current !== requestId) return;
      setPreview({
        kind: target.kind,
        evidenceId: target.evidenceId,
        pageNumber: loaded.pageNumber,
        objectUrl: URL.createObjectURL(loaded.blob),
        mode: loaded.mode,
        region: loaded.region.bbox,
      });
    } catch (error) {
      if (previewRequest.current === requestId) setPreviewError(errorMessage(error));
    } finally {
      if (previewRequest.current === requestId) setPreviewBusyId(null);
    }
  }

  function closeEvidenceDrawer() {
    previewRequest.current += 1;
    setPreviewTarget(null);
    setPreview(null);
    setPreviewError(null);
    setPreviewBusyId(null);
  }

  function togglePreviewMode() {
    if (previewTarget === null) return;
    const currentlyFocused = preview?.mode === "focus" || preview === null;
    void loadEvidencePreview(previewTarget, !currentlyFocused);
  }

  return (
    <>
      {response === null ? (
        <header className="annual-home-heading">
          <p className="eyebrow">FINTRACE / 本机年度分析</p>
          <h1 id="annual-analysis-title">分析年报</h1>
          <p>逐项核对年度数据与出处；异常线索仍需继续查证。</p>
        </header>
      ) : null}
      {runIdError !== null ? (
        <p id="annual-analysis-run-id-error" className="field-error" role="alert">
          {runIdError}
        </p>
      ) : null}

      <details className="annual-start-another" open={response === null}>
        <summary>{response === null ? "选择材料并分析" : "分析另一份年报"}</summary>
        <section id="annual-job-create" className="panel annual-job-create" aria-labelledby="annual-job-create-title">
          <div className="annual-section-heading">
            <div>
              <p className="eyebrow">年度分析 · 当前仅支持年报</p>
              <h2 id="annual-job-create-title">选择已有材料或上传 PDF</h2>
            </div>
          </div>
          <form className="annual-job-form annual-start-form" onSubmit={onStartSelectedSource} noValidate>
            <fieldset className="annual-source-choice">
              <legend>材料来源</legend>
              <label className={`annual-source-option${sourceChoice === "sample" ? " selected" : ""}`}>
                <input
                  type="radio"
                  name="annual-source-choice"
                  value="sample"
                  checked={sourceChoice === "sample"}
                  onChange={() => setSourceChoice("sample")}
                />
                <span><strong>已有材料</strong><small>海天味业 · 603288 · 2024 年报</small></span>
              </label>
              <label id="annual-source-choice-upload" className={`annual-source-option${sourceChoice === "upload" ? " selected" : ""}`}>
                <input
                  id="annual-source-choice-upload-radio"
                  type="radio"
                  name="annual-source-choice"
                  value="upload"
                  checked={sourceChoice === "upload"}
                  onChange={() => setSourceChoice("upload")}
                />
                <span><strong>上传年报</strong><small>选择可读取文字的 PDF</small></span>
              </label>
            </fieldset>
            {sourceChoice === "sample" ? (
              <div className="annual-selected-source">
                <strong>海天味业 2024 年报</strong>
                <span>603288 · 文档 cninfo-1222994233 · 使用本机已保存文件</span>
              </div>
            ) : (
              <div className="annual-job-fields">
            <label>
              公司代码
              <input
                id="annual-job-company-id"
                value={companyId}
                placeholder="例如 603288"
                autoComplete="off"
                aria-invalid={fieldErrors.companyId !== undefined}
                aria-describedby={fieldErrors.companyId === undefined ? undefined : "annual-job-company-error"}
                onChange={(event) => {
                  setCompanyId(event.target.value);
                  setUploadReceipt(null);
                  setUploadedFileContext(null);
                }}
              />
              {fieldErrors.companyId === undefined ? null : <span id="annual-job-company-error" className="field-error">{fieldErrors.companyId}</span>}
            </label>
            <label>
              年报年份
              <input
                id="annual-job-report-year"
                inputMode="numeric"
                value={reportYear}
                placeholder="例如 2024"
                aria-invalid={fieldErrors.reportYear !== undefined}
                aria-describedby={fieldErrors.reportYear === undefined ? undefined : "annual-job-year-error"}
                onChange={(event) => {
                  setReportYear(event.target.value);
                  setUploadReceipt(null);
                  setUploadedFileContext(null);
                }}
              />
              {fieldErrors.reportYear === undefined ? null : <span id="annual-job-year-error" className="field-error">{fieldErrors.reportYear}</span>}
            </label>
            <div className="annual-job-file-field">
              <label className="annual-job-file-title" id="annual-job-file-title" htmlFor="annual-job-pdf-file">
              年报 PDF 文件
              </label>
              <div className="annual-file-picker">
                <input
                  id="annual-job-pdf-file"
                  type="file"
                  className="annual-file-input"
                  accept="application/pdf,.pdf"
                  aria-invalid={fieldErrors.file !== undefined}
                  aria-labelledby="annual-job-file-title"
                  aria-describedby={fieldErrors.file === undefined ? "annual-job-file-help" : "annual-job-file-error"}
                  onChange={(event) => {
                    setPdfFile(event.target.files?.[0] ?? null);
                    setUploadReceipt(null);
                    setUploadedFileContext(null);
                    setFieldErrors((current) => ({ ...current, file: undefined }));
                  }}
                />
                <label className="annual-file-picker-button" htmlFor="annual-job-pdf-file">选择 PDF 文件</label>
                <span className="annual-file-name" aria-live="polite">{pdfFile?.name ?? "未选择文件"}</span>
              </div>
              {fieldErrors.file === undefined ? (
              <span id="annual-job-file-help" className="help">仅支持可读取文字的 PDF，文件不超过 32 MiB；扫描版图片 PDF 暂不支持。</span>
              ) : (
                <span id="annual-job-file-error" className="field-error">{fieldErrors.file}</span>
              )}
            </div>
              </div>
            )}

          <fieldset className="annual-job-mode">
            <legend>分析方式</legend>
            <p className="annual-ai-consent-note">
              AI 全文发现与评审会把所选 PDF 的全部可提取文字分批发送给已配置服务；年报越长，等待时间和服务费用可能越高。图像内容不做 OCR。默认使用本地标准分析。
            </p>
            <div className="annual-job-mode-options">
              <label className={`annual-job-mode-option${mode === "deterministic" ? " selected" : ""}`}>
                <input
                  type="radio"
                  name="annual-analysis-mode"
                  value="deterministic"
                  checked={mode === "deterministic"}
                  onChange={() => setMode("deterministic")}
                />
                <span><strong>标准分析</strong><small>本地统计与核验，不调用模型。</small></span>
              </label>
              <label className={`annual-job-mode-option annual-job-mode-experimental${mode === "m3_screening" ? " selected" : ""}`}>
                <input
                  type="radio"
                  name="annual-analysis-mode"
                  value="m3_screening"
                  checked={mode === "m3_screening"}
                  onChange={() => setMode("m3_screening")}
                />
                <span><strong>标准分析 + 四规则试行</strong><small>不调用模型；规则阈值未校准，分数不是风险概率。</small></span>
              </label>
              <label className={`annual-job-mode-option annual-job-mode-ai${mode === "model_investigation" ? " selected" : ""}${modelCapability !== "ready" || modelDiscoverySupported !== true ? " is-unavailable" : ""}`}>
                <input
                  type="radio"
                  name="annual-analysis-mode"
                  value="model_investigation"
                  checked={mode === "model_investigation"}
                  disabled={modelCapability !== "ready" || modelDiscoverySupported !== true}
                  onChange={() => setMode("model_investigation")}
                />
                <span><strong>AI 全文发现与评审</strong><small>扫描所选 PDF 全部可提取文字并分批调用配置服务；AI 观点未经独立语义核验。</small></span>
              </label>
              <label className={`annual-job-mode-option annual-job-mode-ai${mode === "integrated_analysis" ? " selected" : ""}${modelCapability !== "ready" || modelDiscoverySupported !== true ? " is-unavailable" : ""}`}>
                <input
                  type="radio"
                  name="annual-analysis-mode"
                  value="integrated_analysis"
                  checked={mode === "integrated_analysis"}
                  disabled={modelCapability !== "ready" || modelDiscoverySupported !== true}
                  onChange={() => setMode("integrated_analysis")}
                />
                <span><strong>四规则试行 + AI 全文发现与评审</strong><small>M3 与全文模型发现分别归档；模型发现不参与基础事实核验。</small></span>
              </label>
            </div>
            <p className={`annual-job-capability${modelCapability === "ready" ? " is-ready" : ""}`} role="status">
              {modelCapabilityLoading
                ? "正在检查 AI 服务状态…"
                : modelDiscoverySupported === false
                  ? "当前后端尚未加载全文分析版本，请重启后端；本地标准分析和四规则试行仍可使用。"
                : modelCapability === "ready" && modelDiscoverySupported === true
                  ? "全文分析版本已加载。选择 AI 模式后会把全部可提取文字分批发送给已配置服务。"
                  : modelCapability === "not_configured"
                    ? "当前不可用：后端尚未配置云端模型。标准分析和试行筛查仍可使用。"
                    : modelCapability === "dependency_unavailable"
                      ? "当前不可用：后端缺少 AI 分析所需依赖。标准分析和试行筛查仍可使用。"
                    : modelCapabilityError ?? "正在确认 AI 服务与全文分析版本；确认前不能启动 AI 全文分析。"}
              {modelCapabilityError !== null ? (
                <button type="button" className="text-button annual-capability-retry" onClick={() => void refreshModelCapability()} disabled={modelCapabilityLoading}>
                  {modelCapabilityLoading ? "检测中…" : "重新检测"}
                </button>
              ) : null}
              {modelCapability === "not_configured" ? (
                <button type="button" className="text-button annual-capability-settings" onClick={onOpenSettings}>
                  去设置
                </button>
              ) : null}
            </p>
          </fieldset>

            <div className="annual-job-actions">
              <button type="submit" className="primary" disabled={jobAction !== null || loading}>
                {jobAction === "uploading" ? "正在上传并分析…" : jobAction !== null ? "正在启动…" : "分析年报"}
              </button>
              <span className="help">分析结果保存在本机；文本型 PDF 暂不支持扫描件 OCR。</span>
            </div>
          </form>
          {uploadReceipt !== null ? (
            <details className="annual-upload-receipt">
              <summary>PDF 已上传并生成来源记录</summary>
              <dl>
                <dt>文档标识</dt><dd><code>{uploadReceipt.document_id}</code></dd>
                <dt>页数</dt><dd>{uploadReceipt.page_count}</dd>
                <dt>来源路径</dt><dd><code>{uploadReceipt.source_pdf_path}</code></dd>
                <dt>SHA256</dt><dd><code>{uploadReceipt.sha256}</code></dd>
              </dl>
              <p className="help">如果任务启动请求失败，再次提交相同 PDF、公司代码和年度时会复用本次上传。</p>
            </details>
          ) : null}
        </section>
      </details>

      <section id="annual-history-list" className="panel annual-history-list" aria-labelledby="annual-history-heading" tabIndex={-1}>
        <div className="annual-section-heading">
          <div>
            <p className="eyebrow">本机归档</p>
            <h2 id="annual-history-heading">已有分析</h2>
          </div>
          {history === null || history.total === 0 ? null : <span className="sub">共 {history.total} 条</span>}
        </div>
        {historyLoading ? <p className="status-line" role="status">正在读取本机历史…</p> : null}
        {historyError !== null ? (
          <div className="alert annual-history-error" role="alert">
            <span>{historyError}</span>
            <button type="button" className="secondary" onClick={() => setHistoryRetry((value) => value + 1)}>重新读取</button>
          </div>
        ) : null}
        {!historyLoading && historyError === null && history?.items.length === 0 ? (
          <p className="empty annual-history-empty">
            {history.total === 0
              ? "本机还没有可列出的年度分析。上传年报或选择已有材料后开始分析。"
              : "这一页没有历史条目；可以返回上一页或重新读取本机历史。"}
          </p>
        ) : null}
        {history !== null && history.items.length > 0 ? (
          <ol className="annual-history-items">
            {history.items.map((item) => {
              const canOpenReport = item.run_id !== null && item.result_available;
              const canOpenJob = item.job_id !== null;
              const actionText = canOpenReport ? "打开报告" : canOpenJob ? "查看任务状态" : "报告暂不可读";
              return (
                <li className="annual-history-item" key={item.item_id}>
                  <div className="annual-history-main">
                    <strong>{item.company_code} · {item.year} 年报</strong>
                    <span>{item.source_name ?? item.source_document_id}</span>
                    <small>{historyTimeText(item.created_at)} · {item.replay ? "历史归档" : "本机任务"}</small>
                  </div>
                  <div className="annual-history-statuses" aria-label="任务与报告状态">
                    {item.job_id === null ? null : <span>任务：{historyStatusText(item.job_status)}</span>}
                    {item.run_id === null ? null : <span>报告：{historyStatusText(item.run_status)}</span>}
                    <span>任务模式：{historyModeText(item.mode)}</span>
                    {item.run_mode !== null && item.run_mode !== item.mode ? <span>报告模式：{historyModeText(item.run_mode)}</span> : null}
                  </div>
                  <div className="annual-history-action">
                    <button
                      type="button"
                      className={canOpenReport || canOpenJob ? "text-button" : "text-button is-disabled"}
                      disabled={!canOpenReport && !canOpenJob}
                      onClick={() => onOpenHistoryItem(item)}
                    >
                      {actionText}
                    </button>
                  </div>
                  {item.issues.length > 0 ? (
                    <ul className="annual-history-issues">
                      {item.issues.map((issue) => <li key={issue.code}>{issue.message}</li>)}
                    </ul>
                  ) : null}
                </li>
              );
            })}
          </ol>
        ) : null}
        {history !== null && (history.total > history.limit || historyOffset > 0) ? (
          <nav className="annual-history-pagination" aria-label="历史列表分页">
            <button type="button" className="secondary" disabled={history.offset === 0 || historyLoading || historyError !== null} onClick={() => setHistoryOffset((offset) => Math.max(0, offset - history.limit))}>上一页</button>
            <span>{history.items.length === 0 ? `${Math.min(history.offset, history.total)} / ${history.total} 条` : `${Math.min(history.offset + 1, history.total)}–${Math.min(history.offset + history.items.length, history.total)} / ${history.total}`}</span>
            <button type="button" className="secondary" disabled={history.offset + history.limit >= history.total || historyLoading || historyError !== null} onClick={() => setHistoryOffset((offset) => offset + history.limit)}>下一页</button>
          </nav>
        ) : null}
      </section>

      <details className="annual-archive-lookup">
        <summary>高级：回看已有分析报告</summary>
        <p className="help">输入此前保存的分析编号后，读取对应结果和可查看的核验证据。</p>
        <form className="annual-analysis-lookup" onSubmit={onLoad} noValidate>
          <label htmlFor="annual-analysis-run-id">归档分析编号</label>
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
              {loading ? "读取中…" : "读取已归档分析"}
            </button>
          </div>
          <p className="help">示例：海天味业（603288）2024 年正式运行。</p>
        </form>
      </details>

      {restoreBusy ? <p className="status-line" role="status">正在恢复上次的年度分析任务…</p> : null}
      {restoreError !== null ? (
        <div className="alert annual-job-alert" role="alert">
          <span>无法恢复上次的任务状态：{restoreError}</span>
          <button type="button" className="secondary" onClick={onRefreshJob}>重试读取</button>
          <button type="button" className="text-button" onClick={() => { removeStoredJobId(); setRestoreError(null); }}>清除此任务记录</button>
        </div>
      ) : null}
      {job !== null ? (
        <JobStatusPanel
          job={job}
          action={jobAction}
          error={jobError}
          onRetry={onRetryJob}
          onRefresh={onRefreshJob}
          onDismissError={() => setJobError(null)}
        />
      ) : null}
      {job === null && jobError !== null ? (
        <p className="alert annual-job-alert" role="alert">{jobError} 修正来源信息后可再次提交任务。</p>
      ) : null}

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

      {response !== null ? (
        <>
          {job !== null && job.run_id === response.report.run_id && missingRequestedReportLayers(
            job.request.mode,
            response.report,
            createdJobIds.current.has(job.job_id) || job.discovery_progress !== undefined,
          ).length > 0 ? (
            <p className="alert" role="alert">本次任务选择的{missingRequestedReportLayers(
              job.request.mode,
              response.report,
              createdJobIds.current.has(job.job_id) || job.discovery_progress !== undefined,
            ).join("、")}没有完整归档。当前报告仍可查看；请核对任务归档后再重试。</p>
          ) : null}
          <AnnualReportView
            response={response}
            previewBusyId={previewBusyId}
            onPreview={onPreview}
            narrativeState={narrativeState?.runId === response.report.run_id ? narrativeState : null}
            onNarrativePreview={onNarrativePreview}
            onDiscoveryPreview={onDiscoveryPreview}
            resultOrigin={resultOrigin}
            selectedHistoryItem={selectedHistoryItem}
            activeMode={job?.run_id === response.report.run_id ? job.request.mode : null}
          />
        </>
      ) : null}
      {previewTarget !== null && response !== null ? (
        <EvidenceDrawer
          target={previewTarget}
          report={response.report}
          evidence={previewTarget.kind === "verified_financial_value"
            ? response.verification_evidences.find((item) => item.evidence_id === previewTarget.evidenceId) ?? null
            : null}
          metric={previewTarget.kind === "verified_financial_value"
            ? response.report.confirmed.metrics.find((metric) => metric.verification_evidences.some((item) => item.evidence_id === previewTarget.evidenceId)) ?? null
            : null}
          narrative={previewTarget.kind === "narrative_context"
            ? narrativeState?.value?.items.find((item) => item.evidence_id === previewTarget.evidenceId) ?? null
            : null}
          discoveryEvidence={previewTarget.kind === "annual_discovery"
            ? response.discovery_evidences?.items.find((item) => item.evidence_id === previewTarget.evidenceId) ?? null
            : null}
          preview={preview}
          busy={previewBusyId !== null}
          error={previewError}
          zoom={previewZoom}
          drawerRef={evidenceDrawerRef}
          closeRef={evidenceDrawerCloseRef}
          onClose={closeEvidenceDrawer}
          onToggleMode={togglePreviewMode}
          onZoom={setPreviewZoom}
        />
      ) : null}
    </>
  );
}

type CoreMetricSpec = typeof CORE_METRICS[number];
type AnnualReport = AnnualAnalysisResponse["report"];

type CoreMetricRow = CoreMetricSpec & {
  currentMetric: ConfirmedMetric | undefined;
  comparativeMetric: ConfirmedMetric | undefined;
  currentEvidence: VerificationEvidence | null;
  comparativeEvidence: VerificationEvidence | null;
  difference: ConfirmedCalculation | null;
  yoy: ConfirmedCalculation | null;
  comparisonReason: string;
  yoyReason: string;
};

function trustedCompanyName(companyId: string, documentId: string, sourceSha256: string): string {
  return companyId === HAITIAN_SAMPLE_REQUEST.company_id &&
    documentId === HAITIAN_SAMPLE_REQUEST.document_id &&
    sourceSha256.toLowerCase() === HAITIAN_SAMPLE_REQUEST.sha256
    ? "海天味业"
    : companyId;
}

function buildCoreMetricRow(
  spec: CoreMetricSpec,
  report: AnnualReport,
  evidenceById: Map<string, VerificationEvidence>,
): CoreMetricRow {
  const currentMetric = findCoreFact(spec.metricId, "current", report, evidenceById);
  const comparativeMetric = findCoreFact(spec.metricId, "comparative", report, evidenceById);
  const currentEvidence = currentMetric === undefined ? null : verifiedMetricEvidences(currentMetric, evidenceById)[0] ?? null;
  const comparativeEvidence = comparativeMetric === undefined ? null : verifiedMetricEvidences(comparativeMetric, evidenceById)[0] ?? null;
  const inputGate = coreComparisonInputReason(currentMetric, comparativeMetric, report, evidenceById);
  const differenceResult = inputGate === null
    ? verifiedComparisonCalculation(`calc:annual_difference:${spec.metricId}`, "annual_difference", currentMetric!, comparativeMetric!, report, evidenceById)
    : { value: null, reason: inputGate };
  const yoyBase = comparativeMetric === undefined ? null : normalizeExactDecimal(comparativeMetric.normalized_value);
  const yoyBaseIsPositive = yoyBase !== null && isPositiveExactDecimal(yoyBase);
  const yoyResult = inputGate !== null
    ? { value: null, reason: inputGate }
    : !yoyBaseIsPositive
      ? { value: null, reason: "比较期基数为零、负数或未确认，同比弃权" }
      : verifiedComparisonCalculation(`calc:annual_yoy_rate:${spec.metricId}`, "annual_yoy_rate", currentMetric!, comparativeMetric!, report, evidenceById);
  return {
    ...spec,
    currentMetric,
    comparativeMetric,
    currentEvidence,
    comparativeEvidence,
    difference: differenceResult.value,
    yoy: yoyResult.value,
    comparisonReason: differenceResult.reason,
    yoyReason: yoyResult.reason,
  };
}

function findCoreFact(
  metricId: string,
  role: "current" | "comparative",
  report: AnnualReport,
  evidenceById: Map<string, VerificationEvidence>,
): ConfirmedMetric | undefined {
  const year = role === "current" ? report.report_year : report.report_year - 1;
  const matches = report.confirmed.metrics.filter((metric) =>
    metric.metric_id === metricId && metric.comparison_role === role && metric.period_end === `${year}-12-31`,
  );
  if (matches.length !== 1) return undefined;
  const metric = matches[0];
  if (
    metric.report_year !== report.report_year ||
    (metric.company_id !== undefined && metric.company_id !== report.company_id) ||
    metric.source_document_id !== report.source_document_id ||
    metric.source_sha256.toLowerCase() !== report.source_sha256.toLowerCase() ||
    metric.period_start !== `${year}-01-01` ||
    metric.period_end !== `${year}-12-31` ||
    !isIndependentlyVerifiedMetric(metric) ||
    verifiedMetricEvidences(metric, evidenceById).length === 0
  ) return undefined;
  return metric;
}

function coreComparisonInputReason(
  current: ConfirmedMetric | undefined,
  comparative: ConfirmedMetric | undefined,
  report: AnnualReport,
  evidenceById: Map<string, VerificationEvidence>,
): string | null {
  if (current === undefined || comparative === undefined) return "本期或比较期事实缺少匹配的独立核验证据";
  const comparability = report.comparability;
  if (
    comparability === null || comparability.status !== "verified" ||
    comparability.document_id !== report.source_document_id ||
    comparability.source_sha256?.toLowerCase() !== report.source_sha256.toLowerCase() ||
    comparability.report_year !== report.report_year ||
    comparability.current_year !== report.report_year ||
    comparability.comparative_year !== report.report_year - 1
  ) return "报告可比性身份或期间依据不完整，年度比较暂不能确认";
  if (
    current.scope.trim() === "" || comparative.scope.trim() === "" ||
    current.scope === "unknown" || comparative.scope === "unknown" ||
    current.scope !== comparative.scope
  ) return "两期合并或母公司口径不一致或未确认";
  if (
    current.statement_type === undefined || comparative.statement_type === undefined ||
    current.statement_type === "" || current.statement_type !== comparative.statement_type
  ) return "两期报表类别缺失或不一致";
  if (current.period_type !== "duration" || comparative.period_type !== "duration") {
    return "两期事实必须明确为期间金额，才能进行完整年度比较";
  }
  const currentEvidence = verifiedMetricEvidences(current, evidenceById);
  const comparativeEvidence = verifiedMetricEvidences(comparative, evidenceById);
  const currentUnits = uniqueKnown(currentEvidence.map((item) => item.unit));
  const comparativeUnits = uniqueKnown(comparativeEvidence.map((item) => item.unit));
  if (currentUnits.length !== 1 || comparativeUnits.length !== 1 || currentUnits[0] !== "元" || comparativeUnits[0] !== "元") {
    return "两期金额单位未能由匹配证据确认一致";
  }
  const currentCurrencies = uniqueKnown(currentEvidence.map((item) => item.currency).map(normalizeCurrency));
  const comparativeCurrencies = uniqueKnown(comparativeEvidence.map((item) => item.currency).map(normalizeCurrency));
  if (
    currentCurrencies.length !== 1 || comparativeCurrencies.length !== 1 ||
    currentCurrencies[0] !== "RMB" || comparativeCurrencies[0] !== "RMB" ||
    normalizeCurrency(current.currency) !== "RMB" || normalizeCurrency(comparative.currency) !== "RMB"
  ) return "币种未能由两期匹配证据确认一致";
  return null;
}

function verifiedComparisonCalculation(
  calculationId: string,
  formulaId: "annual_difference" | "annual_yoy_rate",
  current: ConfirmedMetric,
  comparative: ConfirmedMetric,
  report: AnnualReport,
  evidenceById: Map<string, VerificationEvidence>,
): { value: ConfirmedCalculation | null; reason: string } {
  const calculation = report.confirmed.analyses.find((item) => item.calculation_id === calculationId);
  if (
    calculation === undefined || calculation.formula_id !== formulaId || calculation.status !== "succeeded" ||
    typeof calculation.output_value !== "string" || calculation.independent_verification?.status !== "verified" ||
    calculation.independent_verification.calculation_id !== calculation.calculation_id ||
    normalizeExactDecimal(calculation.output_value) === null ||
    normalizeExactDecimal(calculation.output_value) !== normalizeExactDecimal(calculation.independent_verification.recomputed_value) ||
    calculation.input_fact_ids.length !== 2 ||
    !calculation.input_fact_ids.includes(current.fact_id) || !calculation.input_fact_ids.includes(comparative.fact_id) ||
    new Set(calculation.input_fact_ids).size !== 2 ||
    (formulaId === "annual_difference" && calculation.unit !== null && calculation.unit !== "元") ||
    (formulaId === "annual_yoy_rate" && calculation.unit !== null && calculation.unit !== "ratio")
  ) return { value: null, reason: "归档计算、输入事实或独立重算未通过绑定核对" };
  const currentEvidenceIds = new Set(verifiedMetricEvidences(current, evidenceById).map((item) => item.evidence_id));
  const comparativeEvidenceIds = new Set(verifiedMetricEvidences(comparative, evidenceById).map((item) => item.evidence_id));
  const claim = report.verified_claims.find((item) => item.claim_id === `claim:calculation:${calculationId}`);
  if (
    claim === undefined || claim.verification_status !== "verified" || claim.independent_verification?.status !== "verified" ||
    claim.independent_verification.claim_id !== claim.claim_id ||
    !claim.calculation_ids.includes(calculationId) ||
    !claim.supporting_fact_ids.includes(current.fact_id) || !claim.supporting_fact_ids.includes(comparative.fact_id) ||
    !Array.from(currentEvidenceIds).some((id) => claim.supporting_evidence_ids.includes(id)) ||
    !Array.from(comparativeEvidenceIds).some((id) => claim.supporting_evidence_ids.includes(id))
  ) return { value: null, reason: "对应核验主张缺失或未绑定两侧事实与出处" };
  return { value: calculation, reason: "比较依据尚未满足展示条件" };
}

function uniqueKnown(values: Array<string | null | undefined>): string[] {
  return [...new Set(values.filter((value): value is string => typeof value === "string" && value.trim() !== ""))];
}

function normalizeCurrency(value: string | null | undefined): string | null {
  if (value === "人民币" || value === "RMB" || value === "CNY") return "RMB";
  return null;
}

function isPositiveExactDecimal(value: string): boolean {
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(value);
  return match !== null && match[1] !== "-" && BigInt(`${match[2]}${match[3] ?? ""}`) > 0n;
}

function MetricAmount({ metric, evidence, evidenceById }: {
  metric: ConfirmedMetric;
  evidence: VerificationEvidence | null;
  evidenceById: Map<string, VerificationEvidence>;
}) {
  const validEvidences = verifiedMetricEvidences(metric, evidenceById);
  const units = uniqueKnown(validEvidences.map((item) => item.unit));
  const unit = units.length === 1 ? units[0] : null;
  const amount = unit === "元" ? formatInHundredMillions(metric.normalized_value) : null;
  const displayValue = amount === null ? formatExactFinancialValue(metric.normalized_value) : amount;
  const displayUnit = unit === "元" && amount !== null ? "亿元" : unit ?? "单位待定位";
  const displayCurrency = evidence?.currency ? currencyText(evidence.currency) : "币种待定位";
  return (
    <>
      <strong>{displayValue}</strong>
      <small>{displayCurrency} · {displayUnit} · {scopeText(metric.scope)}</small>
    </>
  );
}

function formatSignedAmount(value: string | number | null): string {
  if (typeof value !== "string") return "差额待核实";
  const normalized = normalizeExactDecimal(value);
  if (normalized === null) return "差额待核实";
  const isNegative = normalized.startsWith("-");
  const magnitude = normalized.replace(/^-/, "");
  const formatted = formatInHundredMillions(magnitude);
  if (formatted === null) return "差额待核实";
  const isZero = /^0(?:\.0+)?$/.test(magnitude);
  return `${isZero ? "" : isNegative ? "−" : "+"}${formatted} 亿元`;
}

function formatSignedPercent(value: string | number | null): string {
  if (typeof value !== "string") return "同比待核实";
  const normalized = normalizeExactDecimal(value);
  if (normalized === null) return "同比待核实";
  const isNegative = normalized.startsWith("-");
  const [whole, fraction = ""] = normalized.replace(/^-/, "").split(".");
  const scale = 10n ** BigInt(fraction.length);
  const digits = BigInt(`${whole}${fraction}`);
  const hundredths = (digits * 10000n * 2n + scale) / (scale * 2n);
  const fixed = hundredths.toString().padStart(3, "0");
  const integer = fixed.slice(0, -2).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const sign = hundredths === 0n ? "" : isNegative ? "−" : "+";
  return `${sign}${integer}.${fixed.slice(-2)}%`;
}

function NarrativeEvidenceSection({ state, previewBusyId, onPreview }: {
  state: NarrativeLoadState;
  previewBusyId: string | null;
  onPreview: (evidenceId: string) => void;
}) {
  const value = state?.value ?? null;
  return (
    <section className="panel annual-narrative-evidence" aria-labelledby="annual-narrative-title">
      <div className="annual-section-heading">
        <div>
          <p className="eyebrow">年报原文线索</p>
          <h3 id="annual-narrative-title">原文内容与出处</h3>
        </div>
      </div>
      <p className="annual-narrative-disclaimer">原文可回到年报核对；引用本身不证明经营变化原因。</p>
      {state === null || (value === null && state.error === null) ? <p className="status-line" role="status">正在读取原文片段定位…</p> : null}
      {state?.error !== null && state?.error !== undefined ? <p className="alert" role="alert">原文片段定位暂不可用：{state.error}</p> : null}
      {value !== null && value.items.length === 0 ? <p className="empty">这份报告没有经过来源校验的叙述片段定位记录。</p> : null}
      {value !== null && value.items.length > 0 ? (
        <div className="annual-narrative-items">
          {value.items.map((item: NarrativeEvidence) => (
            <article className="annual-narrative-item" key={item.evidence_id}>
              <div className="annual-narrative-item-heading">
                <strong>{narrativeSectionLabel(item.section_label)}</strong>
                <span>PDF 第 {item.pdf_page} 页</span>
              </div>
              <blockquote className="annual-narrative-reading-text">
                {item.reading_context?.text ?? item.raw_text}
              </blockquote>
              <p className="annual-narrative-context-note">{narrativeContextNote(item)}</p>
              <p className="annual-narrative-why"><strong>为什么显示这段：</strong>{narrativeWhyShown(item)}</p>
              <details className="annual-narrative-raw-details">
                <summary>原始引用与定位详情</summary>
                <blockquote>{item.raw_text}</blockquote>
                <dl>
                  <dt>原始定位原因</dt><dd>{item.selection_reason.trim() || "报告未记录"}</dd>
                  <dt>关联线索</dt><dd>{item.cited_by_signal_ids.length ? item.cited_by_signal_ids.map(signalTitle).join("、") : "报告未记录"}</dd>
                  <dt>原始引用区域</dt><dd><code>{JSON.stringify(item.bbox)}</code></dd>
                  {item.reading_context !== undefined ? (
                    <>
                      <dt>阅读范围状态</dt><dd>{narrativeContextStatus(item)}</dd>
                      <dt>上下文覆盖区域</dt><dd><code>{JSON.stringify(item.reading_context.bbox)}</code></dd>
                      <dt>来源文本块编号</dt><dd>{item.reading_context.source_blocks.map((block) => block.block_index).join("、")}</dd>
                    </>
                  ) : null}
                </dl>
                {item.reading_context !== undefined ? (
                  <details className="annual-narrative-source-blocks">
                    <summary>展开 PDF 原始文本块与坐标</summary>
                    {item.reading_context.source_blocks.map((block) => (
                      <div className="annual-narrative-source-block" key={block.block_index}>
                        <p>文本块 {block.block_index} · <code>{JSON.stringify(block.bbox)}</code></p>
                        <pre>{block.raw_text}</pre>
                      </div>
                    ))}
                  </details>
                ) : null}
              </details>
              <button type="button" className="secondary" onClick={() => onPreview(item.evidence_id)} disabled={previewBusyId === item.evidence_id}>
                {previewBusyId === item.evidence_id ? "正在读取页图…" : "查看原文页图"}
              </button>
            </article>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function EvidenceDrawer({
  target,
  evidence,
  metric,
  narrative,
  discoveryEvidence,
  report,
  preview,
  busy,
  error,
  zoom,
  drawerRef,
  closeRef,
  onClose,
  onToggleMode,
  onZoom,
}: {
  target: PreviewTarget;
  evidence: VerificationEvidence | null;
  metric: ConfirmedMetric | null;
  narrative: NarrativeEvidence | null;
  discoveryEvidence: AnnualDiscoveryEvidenceIndexItem | null;
  report: AnnualReport;
  preview: Preview | null;
  busy: boolean;
  error: string | null;
  zoom: number;
  drawerRef: RefObject<HTMLElement | null>;
  closeRef: RefObject<HTMLButtonElement | null>;
  onClose: () => void;
  onToggleMode: () => void;
  onZoom: (zoom: number) => void;
}) {
  const isNarrative = target.kind === "narrative_context";
  const isDiscovery = target.kind === "annual_discovery";
  const pageNumber = evidence?.pdf_page ?? narrative?.pdf_page ?? discoveryEvidence?.pdf_page ?? preview?.pageNumber ?? null;
  const title = isDiscovery ? "待核查线索原文定位" : isNarrative ? "原文片段定位" : "独立核验金额证据";
  const displayRow = evidence?.row_label?.trim() || "待定位";
  return (
    <div
      className="annual-evidence-backdrop"
      onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}
    >
      <section
        className={`annual-evidence-drawer${isDiscovery ? " is-discovery" : isNarrative ? " is-narrative" : " is-financial-value"}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby="annual-evidence-drawer-title"
        tabIndex={-1}
        ref={drawerRef}
      >
        <header className="annual-evidence-drawer-heading">
          <div>
            <p className="eyebrow">{isDiscovery ? "未核验模型观点的原文片段" : isNarrative ? "叙述片段" : "独立核验白名单"} · {pageNumber === null ? "页码待定位" : `PDF 第 ${pageNumber} 页`}</p>
            <h2 id="annual-evidence-drawer-title">{title}</h2>
          </div>
          <button ref={closeRef} type="button" className="secondary" onClick={onClose}>关闭</button>
        </header>

        {isDiscovery ? (
          <p className="annual-evidence-interpretation">年报原文定位，模型发现未经独立语义核验。高亮仅覆盖文字块范围，不表示逐字定位或核验结论。</p>
        ) : isNarrative ? (
          <p className="annual-evidence-interpretation">{NARRATIVE_EVIDENCE_INTERPRETATION_LABEL}。</p>
        ) : (
          <p className="annual-evidence-interpretation">页图只对应已独立核验的财务事实。来源页本身不构成异常或舞弊结论。</p>
        )}

        <div className="annual-evidence-toolbar" aria-label="页图显示设置">
          <button type="button" className="secondary" onClick={onToggleMode} disabled={busy || error !== null}>
            {preview?.mode === "full_page" ? "聚焦证据区域" : "查看整页"}
          </button>
          <div className="annual-evidence-zoom" aria-label={`缩放 ${zoom}%`}>
            <button type="button" className="secondary" onClick={() => onZoom(Math.max(75, zoom - 25))} disabled={zoom <= 75} aria-label="缩小页图">−</button>
            <span>{zoom}%</span>
            <button type="button" className="secondary" onClick={() => onZoom(Math.min(200, zoom + 25))} disabled={zoom >= 200} aria-label="放大页图">+</button>
          </div>
        </div>

        {busy ? <p className="status-line" role="status">正在读取并校验 PDF 页图…</p> : null}
        {error !== null ? <p className="alert annual-evidence-error" role="alert">{error}</p> : null}
        {preview !== null ? (
          <div className="annual-evidence-image-viewport">
            <img
              src={preview.objectUrl}
              alt={`${isDiscovery ? "待核查线索原文定位" : isNarrative ? "原文片段定位" : "独立核验金额证据"}，PDF 第 ${preview.pageNumber} 页，${preview.mode === "focus" ? "文字块区域" : "整页"}`}
              className="annual-evidence-image"
              style={{ width: `${zoom}%` }}
            />
          </div>
        ) : null}

        <section className="annual-evidence-source-detail" aria-labelledby="annual-evidence-source-title">
          <h3 id="annual-evidence-source-title">来源与定位详情</h3>
          {isDiscovery && discoveryEvidence !== null ? (
            <>
              <p className="annual-evidence-pending">引用原文及相邻内容只用于定位阅读；页图高亮原始文字块 bbox，不表示引用中的字符位置，也不验证模型观点。</p>
              <blockquote className="annual-evidence-raw-text annual-discovery-drawer-quote">
                {discoveryEvidence.citations.find((citation) => citation.finding_id === (target.kind === "annual_discovery" ? target.findingId : ""))?.quote ?? "引用文字未记录。"}
              </blockquote>
              <p className="annual-discovery-fragment-label">被引用文本片段（完整片段，不代表完整段落）</p>
              <pre className="annual-evidence-raw-text annual-discovery-fragment">{discoveryEvidence.raw_text}</pre>
              <dl className="annual-evidence-details">
                <dt>PDF 页码</dt><dd>{discoveryEvidence.pdf_page}</dd>
                <dt>来源文字块</dt><dd>{discoveryEvidence.block_index}</dd>
                <dt>字符起点</dt><dd>{discoveryEvidence.start_char}</dd>
                <dt>来源文档</dt><dd><code>{discoveryEvidence.source_document_id}</code></dd>
              </dl>
              <details className="annual-technical-record annual-evidence-technical">
                <summary>归档引用与来源校验</summary>
                <dl>
                  <dt>证据编号</dt><dd><code>{discoveryEvidence.evidence_id}</code></dd>
                  <dt>关联发现编号</dt><dd><code>{target.kind === "annual_discovery" ? target.findingId : ""}</code></dd>
                  <dt>来源 SHA256</dt><dd><code>{discoveryEvidence.source_sha256}</code></dd>
                  <dt>PDF 区域</dt><dd><code>{JSON.stringify(discoveryEvidence.bbox)}</code></dd>
                </dl>
              </details>
            </>
          ) : isNarrative && narrative !== null ? (
            <>
              <p className="annual-evidence-pending">高亮位置对应报告引用的文字；可切换到整页查看前后文。</p>
              <details className="annual-narrative-raw-details">
                <summary>原始引用与定位详情</summary>
              <blockquote className="annual-evidence-raw-text">{narrative.raw_text}</blockquote>
              <dl className="annual-evidence-details">
                <dt>章节</dt><dd>{narrativeSectionLabel(narrative.section_label)}</dd>
                <dt>定位原因</dt><dd>{narrative.selection_reason.trim() || "报告未记录"}</dd>
                <dt>引用线索</dt><dd>{narrative.cited_by_signal_ids.length ? narrative.cited_by_signal_ids.map(signalTitle).join("、") : "报告未记录"}</dd>
                <dt>页码</dt><dd>{narrative.pdf_page}</dd>
                <dt>来源文档</dt><dd><code>{narrative.source_document_id}</code></dd>
              </dl>
              <details className="annual-technical-record annual-evidence-technical">
                <summary>归档引用与来源校验</summary>
                <dl>
                  <dt>证据编号</dt><dd><code>{narrative.evidence_id}</code></dd>
                  <dt>来源 SHA256</dt><dd><code>{narrative.source_sha256}</code></dd>
                  <dt>原始章节标识</dt><dd><code>{narrative.section_label || "未记录"}</code></dd>
                  <dt>引用线索编号</dt><dd><code>{narrative.cited_by_signal_ids.join("、") || "未记录"}</code></dd>
                  <dt>PDF 区域</dt><dd><code>{JSON.stringify(narrative.bbox)}</code></dd>
                  <dt>阅读上下文区域</dt><dd><code>{JSON.stringify(narrative.reading_context?.bbox ?? narrative.bbox)}</code></dd>
                  <dt>阅读上下文块编号</dt><dd><code>{narrative.reading_context?.source_blocks.map((block) => block.block_index).join("、") ?? "旧接口仅提供原始引用"}</code></dd>
                </dl>
              </details>
              </details>
            </>
          ) : isNarrative ? <p className="annual-evidence-pending">原文片段不在已核对的定位清单中，详情待定位。</p> : (
            <>
              <dl className="annual-evidence-details">
                <dt>表名</dt><dd>{evidence?.table_title?.trim() || "待定位"}</dd>
                <dt>行</dt><dd>{displayRow}</dd>
                <dt>列</dt><dd>{evidence?.column_label?.trim() || "待定位"}</dd>
                <dt>年报原值</dt><dd>{evidence?.value_raw?.trim() || "待定位"}</dd>
                <dt>标准值</dt><dd>{evidence?.value_normalized?.trim() || metric?.normalized_value || "待定位"}</dd>
                <dt>单位 / 币种</dt><dd>{evidence?.unit?.trim() || "待定位"} · {evidence?.currency?.trim() || "待定位"}</dd>
                <dt>期间</dt><dd>{metric?.period_start && metric.period_end ? `${metric.period_start} 至 ${metric.period_end}` : "待定位"}</dd>
                <dt>口径</dt><dd>{metric === null ? "待定位" : scopeText(metric.scope)}</dd>
                <dt>页码</dt><dd>{evidence?.pdf_page ?? "待定位"}</dd>
                <dt>来源文档</dt><dd><code>{evidence?.document_id ?? report.source_document_id}</code></dd>
              </dl>
              <details className="annual-technical-record annual-evidence-technical">
                <summary>核验证据编号与区域坐标</summary>
                <dl>
                  <dt>证据编号</dt><dd><code>{evidence?.evidence_id ?? target.evidenceId}</code></dd>
                  <dt>来源 SHA256</dt><dd><code>{evidence?.source_sha256 ?? report.source_sha256}</code></dd>
                  <dt>PDF 区域</dt><dd><code>{evidence ? JSON.stringify(evidence.value_region.bbox) : "待定位"}</code></dd>
                </dl>
              </details>
            </>
          )}
        </section>
      </section>
    </div>
  );
}

function JobStatusPanel({
  job,
  action,
  error,
  onRetry,
  onRefresh,
  onDismissError,
}: {
  job: AnnualAnalysisJob;
  action: JobAction;
  error: string | null;
  onRetry: () => void;
  onRefresh: () => void;
  onDismissError: () => void;
}) {
  const terminal = !isActiveJob(job);
  const statusClass = "annual-job-status annual-job-status-" + job.status;
  return (
    <section className="panel annual-job-status-panel" aria-live="polite" aria-labelledby="annual-job-status-title">
      <div className="annual-section-heading">
        <div>
          <p className="eyebrow">年报处理进度</p>
          <h2 id="annual-job-status-title">本次分析</h2>
        </div>
        <span className={statusClass}>{jobStatusText(job.status)}</span>
      </div>
      <p className="annual-job-stage">{jobStageText(job.stage, job.status)}</p>
      <p className="annual-job-status-note">
        {job.request.mode === "m3_screening"
          ? "本次运行标准分析和四项试行筛查；规则只提示可继续核对的数据组合。"
          : job.request.mode === "integrated_analysis"
            ? "本次选择四项基础筛查与 AI 分析模式；实际执行的是候选调查还是全文发现，以及调用和可提取文字覆盖情况，以对应报告归档为准。开放式发现不参与基础事实核验，M3 分数也不是模型结论或风险概率。"
          : job.request.mode === "model_investigation"
            ? "本次选择 AI 分析模式；实际执行的是候选调查还是全文发现，以及调用和可提取文字覆盖情况，以对应报告归档为准。模型观点未经独立语义核验。"
            : "本次使用标准分析，在本机核对年报数据、计算和引用。"}
        {` 公司代码 ${job.request.company_id}，报告年份 ${job.request.report_year}。`}
        {terminal ? " 处理状态已稳定。" : " 页面会自动更新处理状态。"}
      </p>
      {job.status === "completed_with_issues" ? (
        <p className="annual-job-reanalysis-note" role="note">
          点击“重新分析这份报告”会用同一来源创建新任务，当前报告归档会保留；这不是续跑失败批次。AI 模式会重新发送整份年报中可提取的文字，并可能产生费用。
        </p>
      ) : null}
      {job.discovery_progress === undefined ? null : (
        <div className="annual-discovery-progress" role="status" aria-live="polite">
          <strong>全文发现扫描进度</strong>
          <p>
            已结束 {job.discovery_progress.processed_batches}/{job.discovery_progress.planned_batches} 批；
            完整通过 {job.discovery_progress.successful_batches} 批、部分有效 {job.discovery_progress.partial_batches} 批、失败 {job.discovery_progress.failed_batches} 批。
            {job.discovery_progress.total_chars > 0 ? ` 原文共 ${formatInteger(job.discovery_progress.total_chars)} 个字符。` : ""}
          </p>
          <p>{discoveryProgressStatusText(job.discovery_progress.status, job.discovery_progress.event, job.status)}</p>
          <small>批次进度只表示全文扫描阶段；最终评审与报告归档以本任务终态为准。</small>
        </div>
      )}
      {job.error !== null ? (
        <div className="annual-job-error" role="alert">
          <strong>{job.error.message}</strong>
          <details className="annual-technical-record">
            <summary>技术记录：错误编号</summary>
            <code>{job.error.code}</code>
          </details>
        </div>
      ) : null}
      {error !== null ? (
        <div className="annual-job-error annual-job-poll-error" role="alert">
          <span>{error}</span>
          <button type="button" className="text-button" onClick={onDismissError}>关闭提示</button>
        </div>
      ) : null}
      <div className="annual-job-actions annual-job-status-actions">
        {canReanalyzeJob(job) ? (
          <button type="button" className="primary" disabled={action !== null} onClick={onRetry}>
            {action === "retrying"
              ? job.status === "completed_with_issues" ? "正在创建新分析任务…" : "正在重新提交…"
              : job.status === "completed_with_issues" ? "重新分析这份报告" : "重新提交同一来源"}
          </button>
        ) : null}
        <button type="button" className="secondary" disabled={action !== null} onClick={onRefresh}>
          {action === "refreshing" ? "正在读取…" : isActiveJob(job) ? "立即更新状态" : "重新读取任务与报告"}
        </button>
      </div>
      <details className="annual-job-source-details">
        <summary>技术记录：来源文件与任务编号</summary>
        <dl>
          <dt>任务编号</dt><dd><code>{job.job_id}</code></dd>
          {job.run_id === null ? null : <><dt>分析编号</dt><dd><code>{job.run_id}</code></dd></>}
          <dt>公司 / 报告年度</dt><dd>{job.request.company_id} · {job.request.report_year}</dd>
          <dt>文档标识</dt><dd><code>{job.request.document_id}</code></dd>
          <dt>来源路径</dt><dd><code>{job.request.source_pdf_path}</code></dd>
          <dt>SHA256</dt><dd><code>{job.request.sha256}</code></dd>
          <dt>创建时间</dt><dd>{formatTimestamp(job.created_at)}</dd>
          <dt>最近更新</dt><dd>{formatTimestamp(job.updated_at)}</dd>
        </dl>
      </details>
    </section>
  );
}

function AnnualDiscoverySection({
  discovery,
  evidenceIndex,
  activeMode,
  modelCalled,
  legacyInvestigationRecorded,
  modelReview,
  previewBusyId,
  query,
  priority,
  expandedFindings,
  onQueryChange,
  onPriorityChange,
  onToggleFinding,
  onPreview,
}: {
  discovery: AnnualModelDiscovery | null;
  evidenceIndex: AnnualDiscoveryEvidenceIndexItem[];
  activeMode: string | null;
  modelCalled: boolean;
  legacyInvestigationRecorded: boolean;
  modelReview: AnnualAnalysisModelReview | null;
  previewBusyId: string | null;
  query: string;
  priority: AnnualModelDiscovery["findings"][number]["priority"] | "all";
  expandedFindings: Set<string>;
  onQueryChange: (value: string) => void;
  onPriorityChange: (value: AnnualModelDiscovery["findings"][number]["priority"] | "all") => void;
  onToggleFinding: (findingId: string, isOpen: boolean) => void;
  onPreview: (evidenceId: string, findingId: string) => void;
}) {
  const findings = discovery?.findings ?? [];
  const evidenceById = new Map(evidenceIndex.map((item) => [item.evidence_id, item]));
  const legacyAiMode = activeMode === "model_investigation" || activeMode === "integrated_analysis";
  const hasLegacyAiRecord = modelCalled || legacyInvestigationRecorded;
  const isLegacyAiArchive = discovery === null && legacyAiMode;
  const hasDiscoveryFindings = findings.length > 0;
  const coverage = discovery?.coverage ?? null;
  const partialBatchCount = coverage === null ? 0 : discoveryPartialBatchCount(coverage);
  const batchOutcomeLabels = coverage === null ? [] : [
    coverage.successful_batches > 0 ? `${coverage.successful_batches} 批完整通过` : "",
    partialBatchCount > 0 ? `${partialBatchCount} 批部分有效` : "",
    coverage.failed_batches > 0 ? `${coverage.failed_batches} 批失败` : "",
    coverage.unattempted_batches > 0 ? `${coverage.unattempted_batches} 批未尝试` : "",
  ].filter(Boolean);
  const pendingReviewFailed = modelReview?.status === "failed" || modelReview?.status === "not_called";
  const priorityCounts = {
    high: findings.filter((finding) => finding.priority === "high").length,
    medium: findings.filter((finding) => finding.priority === "medium").length,
    low: findings.filter((finding) => finding.priority === "low").length,
  };
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const filteredFindings = findings.filter((finding) => {
    if (priority !== "all" && finding.priority !== priority) return false;
    if (normalizedQuery === "") return true;
    const searchableText = [
      finding.title,
      finding.observation,
      finding.why_it_matters,
      finding.follow_up,
      ...finding.citations.flatMap((citation) => [
        citation.quote,
        evidenceById.get(citation.evidence_id)?.raw_text ?? "",
      ]),
    ].join(" ").toLocaleLowerCase();
    return searchableText.includes(normalizedQuery);
  });
  return (
    <section id="annual-discovery" className="panel annual-section annual-discovery" aria-labelledby="annual-discovery-title" tabIndex={-1}>
      <div className="annual-section-heading">
        <div>
          <p className="eyebrow">独立全文文字扫描 · 仅可提取文字</p>
          <h3 id="annual-discovery-title">AI 待核查线索</h3>
        </div>
        {discovery === null ? <span className="review-chip">{hasLegacyAiRecord ? "旧版候选 AI 调查已归档" : isLegacyAiArchive ? "旧版 AI 模式归档" : "未运行"}</span> : <span className={"annual-discovery-status is-" + discovery.status}>{discoveryStatusLabel(discovery.status)}</span>}
      </div>
      <p className="annual-discovery-summary">{discoverySummary(discovery, activeMode, isLegacyAiArchive && hasLegacyAiRecord)}</p>
      {discovery === null ? (
        isLegacyAiArchive ? (
          <p className="annual-discovery-coverage-note" role="note">
            {hasLegacyAiRecord
              ? "此归档包含旧版候选触发后的模型调查记录，但没有全文发现层，不能视为已扫描整份年报。"
              : "此归档记录了旧版 AI 分析模式，但没有全文发现层或明确的旧候选模型调用记录；不能据此判断模型已运行或整份年报已扫描。"}
          </p>
        ) : (
          <p className="annual-discovery-coverage-note" role="note">本报告没有全文发现层。标准分析仍可单独核对已识别指标；基础年度比较与全文年报扫描是不同范围。</p>
        )
      ) : (
        <>
          {findings.length > 0 ? (
            <div className="annual-discovery-overview" aria-label="线索概况">
              <p className="annual-discovery-count">共 <strong>{findings.length}</strong> 条待核查线索</p>
              <div className="annual-discovery-priority-counts" aria-label="按核查顺序统计">
                <span className="priority-high">优先 {priorityCounts.high}</span>
                <span className="priority-medium">随后 {priorityCounts.medium}</span>
                <span className="priority-low">一般 {priorityCounts.low}</span>
              </div>
            </div>
          ) : null}
          <p className={`annual-discovery-coverage-alert${coverage?.complete ? " is-complete" : " is-incomplete"}`} role="note">
            {coverage?.source_status === "unavailable"
              ? `年报文本解析不可用，未规划扫描批次。模型尝试 ${discovery.model_call_attempt_count} 次、收到响应 ${discovery.model_call_count} 次；这些计数不表示文字已处理。`
              : coverage?.complete
                ? `扫描已处理全部可提取文字：${coverage.successful_batches}/${coverage.planned_batches} 批完整通过。`
                : coverage !== null && (coverage.successful_batches > 0 || partialBatchCount > 0)
                  ? `扫描未完整：${coverage.processed_batches}/${coverage.planned_batches} 批已结束；${batchOutcomeLabels.join("、")}；${partialBatchCount > 0 ? "保留的部分线索仍可查看；" : ""}文字覆盖未完整。`
                  : `扫描未完成：没有成功处理的全文批次。${findings.length} 条已保存线索仍可查看；模型响应计数不代表扫描覆盖。`}
          </p>
          <details className="annual-discovery-scan-details">
            <summary>扫描详情</summary>
            {coverage?.source_status === "unavailable" ? (
              <p className="annual-discovery-technical-note">年报文本解析不可用，未规划批次。模型尝试 {discovery.model_call_attempt_count} 次、收到响应 {discovery.model_call_count} 次；收到响应不等于成功处理了年报文字。</p>
            ) : coverage !== null ? (
              <>
                <dl className="annual-discovery-coverage-metrics">
                  <dt>可提取文字</dt><dd>{formatInteger(coverage.total_chars)} 字符，{formatInteger(coverage.total_blocks)} 个文字块</dd>
                  <dt>文字页 / PDF 总页数</dt><dd>{coverage.text_pages} / {coverage.total_pages} 页</dd>
                  <dt>字符处理进度</dt><dd>成功处理 {formatInteger(coverage.processed_chars)}，尝试 {formatInteger(coverage.attempted_chars)}，计划 {formatInteger(coverage.planned_chars)}，省略 {formatInteger(coverage.omitted_chars)} 字符</dd>
                  <dt>扫描范围</dt><dd>{coverage.text_only_scope}</dd>
                  <dt>OCR / 图片页</dt><dd>此流程不做 OCR；含图片页不等于整页没有可提取文字。</dd>
                  <dt>预算状态</dt><dd>{coverage.time_budget_exhausted ? "达到时间预算" : coverage.truncated ? "文字覆盖有截断" : coverage.complete ? "可提取文字覆盖完整" : "存在未完成批次或页面"}</dd>
                </dl>
                <div className="annual-discovery-page-groups" aria-label="覆盖页码明细">
                  <DiscoveryPageList label="未完整处理页码" pages={coverage.unread_pages} />
                  <DiscoveryPageList label="失败批次涉及页码" pages={coverage.failed_pages} />
                  <DiscoveryPageList label="无可提取文字页" pages={coverage.no_text_pages} />
                  <DiscoveryPageList label="无可提取文字的图片页" pages={coverage.unread_image_pages} />
                  <DiscoveryPageList label="含图片页（不代表整页未读）" pages={coverage.image_pages} />
                </div>
                {coverage.batch_results.length > 0 ? (
                  <div className="annual-discovery-batch-section">
                    <h4>每批处理记录</h4>
                    <ol className="annual-discovery-batches">
                      {coverage.batch_results.map((batch) => (
                        <li key={batch.batch_index}>
                          <div className="annual-discovery-batch-heading">
                            <strong>第 {batch.batch_index} 批 · {discoveryBatchStatusText(batch.status)}</strong>
                            <span>{formatInteger(batch.raw_chars)} 字符</span>
                          </div>
                          {batch.accepted_findings !== undefined || batch.rejected_findings !== undefined || batch.restored_quotes !== undefined ? (
                            <p className="annual-discovery-batch-counts">
                              保留线索 {formatInteger(batch.accepted_findings ?? 0)} · 剔除线索 {formatInteger(batch.rejected_findings ?? 0)} · 还原空白格式的引用 {formatInteger(batch.restored_quotes ?? 0)}
                            </p>
                          ) : null}
                          <DiscoveryPageList label="批次页码" pages={batch.pages} />
                          {batch.reason ? <DiscoveryReason reason={batch.reason} labelPrefix="批次说明" /> : null}
                        </li>
                      ))}
                    </ol>
                  </div>
                ) : null}
                {discovery.prompt_version === "annual_discovery_v2" ? (
                  <p className="annual-discovery-technical-note">
                    部分有效表示仅保留通过格式和原文引用检查的线索，仍可能有内容被剔除；不代表整批完整处理或模型观点已核验。空白还原只修复引用中的换行和空格格式。
                  </p>
                ) : null}
              </>
            ) : null}
            {discovery.reason ? <DiscoveryReason reason={discovery.reason} labelPrefix="扫描状态" /> : null}
            {discovery.limitations.length > 0 ? (
              <div className="annual-discovery-limitations">
                <h4>限制与未完成项</h4>
                <ul>{discovery.limitations.map((item, index) => <li key={index + "-" + item}>{item}</li>)}</ul>
              </div>
            ) : null}
          </details>
          {hasDiscoveryFindings && pendingReviewFailed ? (
            <p className="annual-discovery-review-gap" role="note">
              全文扫描已保留 {findings.length} 条待核查线索，但最终评审未完成；线索和原文出处仍可在本区查看。
            </p>
          ) : null}
          {findings.length === 0 ? (
            <p className="annual-discovery-empty">
              {coverage !== null && (coverage.successful_batches > 0 || partialBatchCount > 0)
                ? "本次没有提出带有效原文引用的具体待核查线索；这不代表整份年报没有风险。"
                : "没有成功扫描的全文批次，因此没有可展示的动态线索；这不代表整份年报没有风险。"}
            </p>
          ) : (
            <>
              <div className="annual-discovery-toolbar">
                <label>
                  <span>搜索线索</span>
                  <input
                    type="search"
                    value={query}
                    onChange={(event) => onQueryChange(event.target.value)}
                    placeholder="标题、观察、核查说明或引用原文"
                    aria-label="搜索线索标题与内容"
                  />
                </label>
                <label>
                  <span>核查顺序</span>
                  <select
                    value={priority}
                    onChange={(event) => onPriorityChange(event.target.value as typeof priority)}
                    aria-label="按核查顺序筛选线索"
                  >
                    <option value="all">全部优先级</option>
                    <option value="high">优先</option>
                    <option value="medium">随后</option>
                    <option value="low">一般</option>
                  </select>
                </label>
                <button
                  type="button"
                  className="text-button annual-discovery-clear"
                  onClick={() => { onQueryChange(""); onPriorityChange("all"); }}
                  disabled={query === "" && priority === "all"}
                >清空筛选</button>
                <p className="annual-discovery-filter-count" role="status" aria-live="polite">显示 {filteredFindings.length} / {findings.length} 条</p>
              </div>
              <p className="annual-discovery-priority-note">核查顺序是建议阅读先后，不是风险概率。</p>
              {filteredFindings.length === 0 ? (
                <p className="annual-discovery-empty" role="status">
                  {normalizedQuery !== "" || priority !== "all"
                    ? "没有符合当前搜索和筛选条件的线索。清空筛选可查看全部归档条目。"
                    : "没有带有效原文引用的具体待核查线索；这不代表整份年报没有风险。"}
                </p>
              ) : null}
              <div className="annual-discovery-findings">
                {filteredFindings.map((finding) => {
                  const pageCitations = Array.from(new Map(finding.citations.map((citation) => [citation.evidence_id, citation])).values());
                  const pageCounts = new Map<number, number>();
                  pageCitations.forEach((citation) => {
                    const evidence = evidenceById.get(citation.evidence_id);
                    if (evidence !== undefined) pageCounts.set(evidence.pdf_page, (pageCounts.get(evidence.pdf_page) ?? 0) + 1);
                  });
                  const pageOrdinals = new Map<number, number>();
                  return (
                  <article className="annual-discovery-finding" id={"annual-discovery-" + finding.finding_id} key={finding.finding_id} tabIndex={-1}>
                    <header className="annual-discovery-finding-heading">
                      <div>
                        <h4>{finding.title}</h4>
                      </div>
                      <div className="annual-discovery-badges">
                        <span className={"annual-discovery-priority priority-" + finding.priority}>核查顺序：{priorityText(finding.priority)}</span>
                        <span className="annual-discovery-unverified">待核查 · 模型观点</span>
                      </div>
                    </header>
                    <p className="annual-discovery-observation">{finding.observation}</p>
                    <div className="annual-discovery-page-actions" aria-label={`${finding.title} 的原文页码和定位`}>
                      <strong>原文页码：</strong>
                      {pageCitations.map((citation, index) => {
                        const evidence = evidenceById.get(citation.evidence_id);
                        const pageOrdinal = evidence === undefined ? 0 : (pageOrdinals.get(evidence.pdf_page) ?? 0) + 1;
                        if (evidence !== undefined) pageOrdinals.set(evidence.pdf_page, pageOrdinal);
                        const fragmentLabel = evidence !== undefined && (pageCounts.get(evidence.pdf_page) ?? 0) > 1 ? ` · 片段 ${pageOrdinal}` : "";
                        return (
                          <span className="annual-discovery-page-action" key={citation.evidence_id + "-" + index}>
                            {evidence === undefined ? (
                              <span className="annual-discovery-invalid-reference">出处未通过来源校验，未请求页图</span>
                            ) : (
                              <button type="button" className="secondary annual-discovery-page-link" onClick={() => onPreview(evidence.evidence_id, finding.finding_id)} disabled={previewBusyId === evidence.evidence_id}>
                                {previewBusyId === evidence.evidence_id ? "正在打开…" : `PDF 第 ${evidence.pdf_page} 页${fragmentLabel} · 查看原文`}
                              </button>
                            )}
                          </span>
                        );
                      })}
                    </div>
                    <details
                      className="annual-discovery-finding-details"
                      open={expandedFindings.has(finding.finding_id)}
                      onToggle={(event) => onToggleFinding(finding.finding_id, event.currentTarget.open)}
                    >
                      <summary>关注理由、后续核查与 {finding.citations.length} 处原文引用</summary>
                      <dl className="annual-discovery-opinions">
                        <dt>关注理由</dt><dd>{finding.why_it_matters}</dd>
                        <dt>后续核查</dt><dd>{finding.follow_up}</dd>
                      </dl>
                      <div className="annual-discovery-citations">
                        <h5>引用原文（仅用于定位，模型观点尚未独立核验）</h5>
                        {finding.citations.map((citation, index) => {
                          const evidence = evidenceById.get(citation.evidence_id);
                          return (
                            <section className="annual-discovery-citation" key={citation.evidence_id + "-detail-" + index}>
                              <blockquote className="annual-discovery-quote">{citation.quote}</blockquote>
                              {evidence === undefined ? (
                                <p className="annual-discovery-invalid-reference">原文出处未通过当前报告来源校验，未提供片段或页图。</p>
                              ) : (
                                <>
                                  <p className="annual-discovery-fragment-label">被引用文本片段（不是对完整段落的保证）</p>
                                  <pre className="annual-discovery-fragment">{evidence.raw_text}</pre>
                                  <small className="annual-discovery-source-meta">PDF 第 {evidence.pdf_page} 页 · 来源块 {evidence.block_index}</small>
                                </>
                              )}
                            </section>
                          );
                        })}
                      </div>
                    </details>
                  </article>
                  );
                })}
              </div>
            </>
          )}
        </>
      )}
    </section>
  );
}

function DiscoveryPageList({ label, pages }: { label: string; pages: number[] }) {
  if (pages.length === 0) return null;
  const compact = compactPageRanges(pages);
  return (
    <div className="annual-discovery-page-list">
      <strong>{label}：</strong><span>{compact}</span>
      <details>
        <summary>查看全部 {pages.length} 个页码</summary>
        <p>{[...pages].sort((left, right) => left - right).join("、")}</p>
      </details>
    </div>
  );
}

function compactPageRanges(pages: number[]): string {
  const sorted = [...new Set(pages)].sort((left, right) => left - right);
  if (sorted.length === 0) return "无";
  const ranges: string[] = [];
  let start = sorted[0];
  let end = start;
  for (const page of sorted.slice(1)) {
    if (page === end + 1) {
      end = page;
      continue;
    }
    ranges.push(start === end ? `${start}` : `${start}–${end}`);
    start = page;
    end = page;
  }
  ranges.push(start === end ? `${start}` : `${start}–${end}`);
  return ranges.join("、");
}

function discoveryBatchStatusText(status: string): string {
  if (status === "succeeded") return "完整通过";
  if (status === "partial") return "部分有效";
  return status === "failed" ? "失败" : "未尝试";
}

function discoveryPartialBatchCount(coverage: AnnualModelDiscovery["coverage"]): number {
  const value = (coverage as typeof coverage & { partial_batches?: unknown }).partial_batches;
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : 0;
}

function DiscoveryReason({ reason, labelPrefix }: { reason: string; labelPrefix: string }) {
  const labels: Record<string, string> = {
    invalid_model_json: "模型返回内容无法按 JSON 格式读取",
    model_response_too_large: "模型回应超过允许长度",
    invalid_model_schema: "模型回应未符合字段结构要求",
    findings_invalid: "线索字段格式不符合要求",
    limitations_invalid: "限制说明格式不符合要求",
    all_findings_rejected: "本批线索均未通过结构、字段或原文引用检查",
    invalid_model_response: "模型回应未通过结构与引用检查",
    limitation_invalid: "限制说明格式不符合要求",
    limitation_contains_unquoted_digit: "限制说明中的数字应放在原文引用中",
    limitation_contains_control_character: "限制说明含有无法读取的字符",
    definitive_conclusion_forbidden: "模型回应包含本流程不允许的定性结论",
    batch_missing: "批次处理记录不完整",
    model_call_failed: "本批次未能取得模型回应",
    model_call_timeout: "本批次等待模型回应超时",
    serialized_input_limit_breached: "本批次文字超过输入长度限制",
    some_findings_rejected: "部分线索未通过结构、字段或原文引用检查；其余有效线索已保留",
    workflow_time_budget_exhausted: "扫描达到本次处理时间预算",
  };
  const machineCode = /^[a-z][a-z0-9_]*$/.test(reason);
  const label = labels[reason] ?? (machineCode ? "扫描环节未完整完成，请查看批次记录" : reason);
  return (
    <div className="annual-discovery-technical-note">
      <p>{labelPrefix}：{label}</p>
      {machineCode ? (
        <details className="annual-technical-record">
          <summary>技术代码</summary>
          <code>{reason}</code>
        </details>
      ) : null}
    </div>
  );
}

function discoverySummary(discovery: AnnualModelDiscovery | null, mode: string | null, legacyAi: boolean): string {
  if (discovery === null) {
    if (legacyAi) return "此报告保存了旧版候选 AI 调查；没有按来源 PDF 全文扫描生成的动态发现条目。";
    if (mode === "deterministic" || mode === "m3_screening" || mode === "integrated_analysis") return "本次未运行 AI 全文发现。下面的基础年度比较仅覆盖固定财务指标。";
    return "本次归档没有全文发现层；不得据此推断年报已经扫描。";
  }
  return "模型观点尚未经过独立语义核验。";
}

function discoveryStatusLabel(status: AnnualModelDiscovery["status"]): string {
  return status === "completed" ? "扫描已结束" : status === "partial" ? "部分完成" : status === "failed" ? "扫描失败" : "未调用";
}

function priorityText(priority: "high" | "medium" | "low"): string {
  return priority === "high" ? "优先" : priority === "medium" ? "随后" : "一般";
}

function AnnualReportView({
  response,
  previewBusyId,
  onPreview,
  narrativeState,
  onNarrativePreview,
  onDiscoveryPreview,
  resultOrigin,
  selectedHistoryItem,
  activeMode,
}: {
  response: AnnualAnalysisResponse;
  previewBusyId: string | null;
  onPreview: (evidenceId: string) => void;
  narrativeState: NarrativeLoadState;
  onNarrativePreview: (evidenceId: string) => void;
  onDiscoveryPreview: (evidenceId: string, findingId: string) => void;
  resultOrigin: "new" | "history";
  selectedHistoryItem: AnnualAnalysisHistoryItem | null;
  activeMode: AnnualAnalysisJobMode | null;
}) {
  const [exporting, setExporting] = useState<"json" | "markdown" | "html" | null>(null);
  const [exportMessage, setExportMessage] = useState<string | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const [discoveryQuery, setDiscoveryQuery] = useState("");
  const [discoveryPriority, setDiscoveryPriority] = useState<AnnualModelDiscovery["findings"][number]["priority"] | "all">("all");
  const [expandedDiscoveryFindings, setExpandedDiscoveryFindings] = useState<Set<string>>(() => new Set());
  const [pendingDiscoveryTarget, setPendingDiscoveryTarget] = useState<string | null>(null);
  const { report, manifest, verification_evidences: evidenceIndex } = response;
  const evidenceById = new Map(evidenceIndex.map((evidence) => [evidence.evidence_id, evidence]));
  const verifiedMetricCount = report.confirmed.metrics.filter(isIndependentlyVerifiedMetric).length;
  const verifiedCalculationCount = report.confirmed.analyses.filter(isIndependentlyVerifiedCalculation).length;
  const coreMetrics = CORE_METRICS.map((spec) => buildCoreMetricRow(spec, report, evidenceById));
  const reportModelInvestigation = report.model_investigation;
  const discovery = report.model_discovery ?? null;
  const reportModelReview = report.model_review ?? null;
  const historicInvestigationRecord = record(reportModelInvestigation);
  const historicInvestigationRecorded = reportModelInvestigation !== undefined && reportModelInvestigation !== null;
  const historicModelCalled = report.model_called || historicInvestigationRecord?.model_called === true;
  const candidateCount = report.pending_review.candidate_signals.filter((item) => item.status === "candidate").length;
  const abstainedSignalCount = report.pending_review.candidate_signals.filter((item) => item.status === "abstained").length;
  const sourceMatches = report.source_sha256 === manifest.source_pdf_sha256;
  const registeredSyntheticCompanyName = response.source_provenance.status === "registered_synthetic"
    ? response.source_provenance.company_name
    : null;
  const companyName = registeredSyntheticCompanyName ?? trustedCompanyName(report.company_id, report.source_document_id, report.source_sha256);
  const mode = activeMode ?? selectedHistoryItem?.run_mode ?? selectedHistoryItem?.mode ??
    response.manifest.mode ?? (report.m3_screening !== null && report.m3_screening !== undefined ? "m3_screening" : null);
  const legacyAiLabel = historicModelCalled || historicInvestigationRecorded ? "旧版候选 AI 调查与评审" : "旧版 AI 模式归档（全文发现未记录）";
  const modeText = mode === "deterministic" ? "标准分析" : mode === "m3_screening" ? "四规则试行" :
    mode === "model_investigation" ? discovery === null ? legacyAiLabel : "AI 全文发现与评审" :
      mode === "integrated_analysis" ? discovery === null ? "四规则试行 + " + legacyAiLabel : "四规则试行 + AI 全文发现与评审" :
      mode === "unknown" ? "模式未记录" : report.model_called ? "模型调用记录" : "模式未记录";
  const headlineRates = coreMetrics
    .filter((item) => item.yoy !== null)
    .map((item) => `${item.label}同比 ${formatSignedPercent(item.yoy!.output_value)}`);
  const candidateSignals = report.pending_review.candidate_signals.filter((item) => item.status === "candidate");
  const candidateReasons = candidateSignals.map((item) => item.reason?.trim() ?? "").filter(Boolean);
  const investigationRecord = record(report.model_investigation);
  const investigationItems = Array.isArray(investigationRecord?.items)
    ? investigationRecord.items.map(record).filter((item): item is AnnualReportRecord => item !== null)
    : [];
  const hasUnverifiedExplanation = (reportModelReview?.reasons.some((item) => item.text.trim() !== "") ?? false) ||
    investigationItems.some((item) => [item.interpretation, item.explanation, item.text, item.reason].some((value) => stringValue(value)?.trim()));
  const primaryReason = candidateReasons.length > 0
    ? `报告记录原因（未独立证实）：${candidateReasons.join("；")}`
    : hasUnverifiedExplanation
      ? "报告包含模型解释，但解释尚未独立核实；变化原因仍待确认。"
      : "原因尚未说明。";

  useEffect(() => {
    setDiscoveryQuery("");
    setDiscoveryPriority("all");
    setExpandedDiscoveryFindings(new Set());
    setPendingDiscoveryTarget(null);
  }, [discovery?.run_id]);

  useEffect(() => {
    if (pendingDiscoveryTarget === null) return;
    const target = document.getElementById("annual-discovery-" + pendingDiscoveryTarget);
    if (target === null) return;
    target.scrollIntoView({ block: "start", behavior: "smooth" });
    target.focus({ preventScroll: true });
    setPendingDiscoveryTarget(null);
  }, [pendingDiscoveryTarget, discoveryQuery, discoveryPriority, expandedDiscoveryFindings]);

  const locateDiscoveryFinding = (findingId: string) => {
    if (discovery === null) return;
    if (!discovery.findings.some((item) => item.finding_id === findingId)) return;
    setDiscoveryQuery("");
    setDiscoveryPriority("all");
    setExpandedDiscoveryFindings((current) => new Set(current).add(findingId));
    setPendingDiscoveryTarget(findingId);
  };

  const toggleDiscoveryFinding = (findingId: string, isOpen: boolean) => {
    setExpandedDiscoveryFindings((current) => {
      const next = new Set(current);
      if (isOpen) next.add(findingId);
      else next.delete(findingId);
      return next;
    });
  };

  const downloadExport = async (format: "json" | "markdown" | "html") => {
    if (exporting !== null) return;
    setExporting(format);
    setExportMessage(null);
    setExportError(null);
    try {
      const result = await downloadAnnualAnalysisExport(report.run_id, format);
      const objectUrl = URL.createObjectURL(result.blob);
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = result.filename;
      link.hidden = true;
      document.body.append(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 30_000);
      setExportMessage(format === "html" ? "已下载 HTML；可离线查看或用浏览器打印成 PDF。" : "报告已下载。导出保留各层归档状态，不提升核验等级。");
    } catch (error) {
      setExportError(errorMessage(error));
    } finally {
      setExporting(null);
    }
  };

  return (
    <div className="annual-report" aria-label="年度分析报告">
      <header id="annual-report-heading" className="annual-report-heading" tabIndex={-1}>
        <div>
          <p className="eyebrow">{companyName} · {report.company_id} · {report.report_year} 年报</p>
          <h2>{companyName} {report.company_id} · {report.report_year} 年报</h2>
          <p className="annual-report-title-caption">{report.title?.trim() || "年度分析归档结果"}</p>
          <p className="annual-run-id">{resultOrigin === "new" ? "本次运行" : "历史归档"} · {modeText} · {report.model_called ? "已调用模型" : "模型未调用"}</p>
        </div>
        <span className={manifest.status === "completed_with_issues" ? "review-chip" : "annual-status-chip"}>
          {manifestStatusText(manifest.status)}
        </span>
      </header>

      <div className="annual-source-nature-wrap">
        <p className={response.source_provenance.status === "registered_synthetic" ? "annual-source-nature annual-source-nature-synthetic" : "annual-source-nature"} role="note">
          <strong>{response.source_provenance.label}</strong>
        </p>
        {response.source_provenance.use_conditions ? (
          <details className="annual-source-use-conditions">
            <summary>样例分发与使用条件</summary>
            <p>{response.source_provenance.use_conditions}</p>
          </details>
        ) : null}
      </div>

      {mode === "integrated_analysis" ? (
        <p className="annual-scope-summary" role="note">
          本报告分别呈现标准事实核验、M3 四规则审计和 AI 调查/最终评审。AI 只引用标准报告的证据白名单；模型评审不提升 M3 或事实核验状态。M3 分数为试行规则计分，不是风险概率。
        </p>
      ) : null}

      {selectedHistoryItem?.issues.length ? (
        <ul className="annual-report-history-issues" aria-label="历史归档问题">
          {selectedHistoryItem.issues.map((issue) => <li key={issue.code}>{issue.message}</li>)}
        </ul>
      ) : null}

      <nav className="annual-report-nav" aria-label="报告内容">
        <a href="#annual-model-review" onClick={expandReportDetails}>
          {reportModelReview?.status === "completed" && reportModelReview.assessment !== null
            ? "AI 评审意见"
            : reportModelReview !== null
              ? "评审状态与摘要"
              : "本次筛查摘要"}
        </a>
        <a href="#annual-discovery" onClick={expandReportDetails}>AI 待核查线索</a>
        <a href="#annual-financial-overview" onClick={expandReportDetails}>财务概览</a>
        <a href="#annual-candidate-overview" onClick={expandReportDetails}>基础年度比较</a>
        <a href="#annual-confirmed-metrics" onClick={expandReportDetails}>核实数据</a>
        <a href="#annual-analysis-details" onClick={expandReportDetails}>计算过程</a>
        <a href="#annual-verified-claims" onClick={expandReportDetails}>核验主张</a>
        {report.m3_screening == null ? null : <a href="#annual-m3-screening" onClick={expandReportDetails}>试行筛查</a>}
        <a href="#annual-scope" onClick={expandReportDetails}>分析范围</a>
      </nav>

      <AnnualDiscoverySection
        discovery={discovery}
        evidenceIndex={response.discovery_evidences?.items ?? []}
        activeMode={mode}
        modelCalled={report.model_called}
        legacyInvestigationRecorded={historicInvestigationRecorded}
        modelReview={reportModelReview}
        previewBusyId={previewBusyId}
        query={discoveryQuery}
        priority={discoveryPriority}
        expandedFindings={expandedDiscoveryFindings}
        onQueryChange={setDiscoveryQuery}
        onPriorityChange={setDiscoveryPriority}
        onToggleFinding={toggleDiscoveryFinding}
        onPreview={onDiscoveryPreview}
      />

      <section id="annual-financial-overview" className="annual-financial-overview" aria-labelledby="annual-indicators-title" tabIndex={-1}>
        <div className="annual-overview-heading">
          <div>
            <p className="eyebrow">已核验事实与归档计算</p>
            <h3 id="annual-indicators-title">年度核心指标</h3>
          </div>
          <span>{report.report_year} 年 / {report.report_year - 1} 年 · 期间与口径见各项记录</span>
        </div>
        <p className="annual-primary-finding">
          <strong>主要变化</strong>
          {headlineRates.length > 0 ? <span>{headlineRates.join("；")}。</span> : <span>当前归档没有满足展示条件的同比计算。</span>}
          <span className="annual-finding-candidate">{candidateSignals.length > 0 ? "候选待核查" : "变化原因尚未核实"}</span>
          <span className="annual-primary-reason">{primaryReason}</span>
          <span>数值核验不代表因果判断或舞弊结论。</span>
        </p>
        <div className="annual-core-table-wrap">
          <table className="annual-core-table">
            <thead>
              <tr>
                <th scope="col">指标</th>
                <th scope="col">{report.report_year} 本期</th>
                <th scope="col">{report.report_year - 1} 比较期</th>
                <th scope="col">年度差额</th>
                <th scope="col">同比</th>
                <th scope="col">核验与原文</th>
              </tr>
            </thead>
            <tbody>
              {coreMetrics.map((item) => (
                <tr key={item.metricId}>
                  <th scope="row">{item.label}</th>
                  <td>{item.currentMetric === undefined
                    ? <span className="annual-comparison-pending">金额待核实</span>
                    : <MetricAmount metric={item.currentMetric} evidence={item.currentEvidence} evidenceById={evidenceById} />}</td>
                  <td>{item.comparativeMetric === undefined
                    ? <span className="annual-comparison-pending">金额待核实</span>
                    : <MetricAmount metric={item.comparativeMetric} evidence={item.comparativeEvidence} evidenceById={evidenceById} />}</td>
                  <td>{item.difference === null
                    ? <span className="annual-comparison-pending" title={item.comparisonReason}>{item.comparisonReason}</span>
                    : <strong className="annual-core-computed-value">{formatSignedAmount(item.difference.output_value)}</strong>}</td>
                  <td>{item.yoy === null
                    ? <span className="annual-comparison-pending" title={item.comparisonReason}>{item.yoyReason}</span>
                    : <strong className="annual-core-computed-value">{formatSignedPercent(item.yoy.output_value)}</strong>}</td>
                  <td>
                    <span className={item.difference === null ? "review-chip" : "verification-chip"}>{item.difference === null ? "比较暂不能确认" : "独立核验通过"}</span>
                    <div className="annual-core-evidence-links">
                      {item.currentEvidence === null ? <span>本期原文待定位</span> : <button type="button" className="text-button" onClick={() => onPreview(item.currentEvidence!.evidence_id)} disabled={previewBusyId === item.currentEvidence.evidence_id}>{`本期 PDF 第 ${item.currentEvidence.pdf_page} 页`}</button>}
                      {item.comparativeEvidence === null ? null : <button type="button" className="text-button" onClick={() => onPreview(item.comparativeEvidence!.evidence_id)} disabled={previewBusyId === item.comparativeEvidence.evidence_id}>{`比较期 PDF 第 ${item.comparativeEvidence.pdf_page} 页`}</button>}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="annual-core-footnote">
          差额与同比只读取归档计算并要求两侧事实、独立重算和对应核验主张全部匹配。单侧金额按本侧已核验证据显示；缺少比较依据时保留金额并说明原因。
          {report.comparability?.limitations.length ? ` 可比性限制：${report.comparability.limitations.join("；")}` : ""}
        </p>
      </section>

      <section className="annual-export-tools" aria-label="下载报告">
        <div>
          <strong>下载报告</strong>
          <span>下载 HTML 后可离线查看，也可用浏览器打印成 PDF。</span>
        </div>
        <div className="annual-export-buttons">
          <button type="button" className="secondary" disabled={exporting !== null} onClick={() => void downloadExport("json")}>
            {exporting === "json" ? "正在下载 JSON…" : "下载 JSON"}
          </button>
          <button type="button" className="secondary" disabled={exporting !== null} onClick={() => void downloadExport("markdown")}>
            {exporting === "markdown" ? "正在下载 Markdown…" : "下载 Markdown"}
          </button>
          <button type="button" className="secondary" disabled={exporting !== null} onClick={() => void downloadExport("html")}>
            {exporting === "html" ? "正在下载 HTML…" : "下载可打印 HTML"}
          </button>
        </div>
        {exportMessage !== null ? <p className="annual-export-message" role="status">{exportMessage}</p> : null}
        {exportError !== null ? <p className="annual-export-error" role="alert">下载失败：{exportError}</p> : null}
      </section>

      <NarrativeEvidenceSection
        state={narrativeState}
        previewBusyId={previewBusyId}
        onPreview={onNarrativePreview}
      />

      <section id="annual-candidate-overview" className="panel annual-section annual-candidate-overview" tabIndex={-1}>
        <div className="annual-section-heading">
          <div>
            <p className="eyebrow">基础年度比较 · 固定候选</p>
            <h3>基础年度比较</h3>
          </div>
        </div>
        {candidateCount > 0 ? (
          <p className="annual-candidate-context">
            以下固定候选来自基础年度指标比较，不构成整份年报的问题目录。卡片只展示满足独立核验条件的年度差额；其他变化方向暂不判断。
          </p>
        ) : abstainedSignalCount > 0 ? (
          <p className="annual-candidate-context">
            下列基础年度比较因关键数据尚未确认而弃权；页面会说明缺少的前提，不能据此判断变化方向。
          </p>
        ) : (
          <p className="annual-candidate-context">
            当前没有满足展示条件的基础年度比较候选。此区域不是整份年报的问题目录；全文可提取文字扫描范围见上方“AI 待核查线索”。
          </p>
        )}
        {candidateCount > 0 ? (
          <div className="annual-record-grid">
            {report.pending_review.candidate_signals.filter((candidate) => candidate.status === "candidate").map((candidate) => (
              <CandidateCard
                key={candidate.signal_id}
                candidate={candidate}
                report={report}
                evidenceById={evidenceById}
                locations={locationsForFactIds(candidate.input_fact_ids, report, evidenceById)}
                previewBusyId={previewBusyId}
                onPreview={onPreview}
              />
            ))}
          </div>
        ) : null}
        {abstainedSignalCount > 0 ? (
          <div className="annual-abstained-list">
            {report.pending_review.candidate_signals.filter((candidate) => candidate.status === "abstained").map((candidate) => (
              <AbstainedSignalCard
                key={candidate.signal_id}
                candidate={candidate}
                report={report}
                evidenceById={evidenceById}
                previewBusyId={previewBusyId}
                onPreview={onPreview}
              />
            ))}
          </div>
        ) : null}
        <PendingItems
          pending={report.pending_review}
          report={report}
          evidenceById={evidenceById}
          previewBusyId={previewBusyId}
          onPreview={onPreview}
        />
        {candidateCount === 0 && abstainedSignalCount === 0 &&
        report.pending_review.facts.length === 0 &&
        report.pending_review.calculations.length === 0 &&
        report.pending_review.claims.length === 0 &&
        report.pending_review.uncited_evidences.length === 0 ? (
          <p className="annual-no-follow-up">本次报告没有列出需要优先核查的候选事项。</p>
        ) : null}
      </section>

      <ModelReviewSection
        review={reportModelReview}
        report={report}
        evidenceById={evidenceById}
        discoveryEvidenceIndex={response.discovery_evidences?.items ?? []}
        previewBusyId={previewBusyId}
        onPreview={onPreview}
        onDiscoveryPreview={onDiscoveryPreview}
        onLocateDiscoveryFinding={locateDiscoveryFinding}
      />

      {report.m3_screening == null ? null : <M3ScreeningBlock screening={report.m3_screening} />}

      <section id="annual-scope" className="panel annual-section" tabIndex={-1}>
        <div className="annual-section-heading">
          <div>
            <p className="eyebrow">分析范围</p>
            <h3>分析范围与报告缺口</h3>
          </div>
        </div>
        <p className="annual-scope-summary">
          基础财务核验层核对了 {report.confirmed.metrics.length} 项已识别财务数据及相关年度变化、原文出处；这不代表整份报告的全部分析范围。全文可提取文字扫描范围与待核查模型线索见上方“AI 待核查线索”，其状态与基础财务核验彼此独立。
        </p>
        <details className="annual-scope-original">
          <summary>查看本次分析范围说明</summary>
          <p>{report.scope.note}</p>
        </details>
        <p className="annual-comparability">
          年度数据可比性：{report.comparability === null
            ? "报告未提供结果"
            : comparabilityStatusText(report.comparability.status) + " · " + restatementStatusText(report.comparability.restatement_status)}
        </p>
        <GapList title="报告缺口" items={report.scope.gaps} emptyText="报告没有单列缺口。" />
        {report.comparability !== null ? (
          <GapList title="可比性限制" items={report.comparability.limitations} emptyText="报告没有记录可比性限制。" />
        ) : null}
        <GapList title="报告局限" items={report.limitations} emptyText="报告没有记录其他局限。" />
      </section>

      {!sourceMatches ? (
        <p className="alert" role="alert">报告记录的来源文件校验值与归档记录不一致。请勿将本次结果视为已确认。</p>
      ) : null}
      <details className="panel annual-technical-record annual-source-panel">
        <summary>技术记录：运行编号、来源文件与归档校验</summary>
        <p className="annual-source-note">报告与来源页图来自本次分析的本机归档；各页图分别用于已核验金额或仅作原文定位的模型引用，层级状态彼此独立，页图不提升模型观点的核验状态。</p>
        <dl className="annual-meta-grid">
          <dt>分析编号</dt><dd><code>{report.run_id}</code></dd>
          <dt>是否调用模型</dt><dd>{report.model_called ? "是" : "否"}</dd>
          <dt>公司 / 报告期</dt><dd>{report.company_id} · {report.report_year} 年</dd>
          <dt>来源文档编号</dt><dd><code>{manifest.document_id}</code></dd>
          <dt>原文 SHA256</dt><dd><code>{manifest.source_pdf_sha256}</code></dd>
          <dt>报告 SHA256</dt><dd><code>{manifest.report_sha256}</code></dd>
          <dt>归档状态</dt><dd>{manifestStatusText(manifest.status)}（{manifest.status}）</dd>
          <dt>原文与报告校验</dt><dd>{sourceMatches ? "一致" : "不一致"}</dd>
          <dt>期间可比性</dt>
          <dd>
            {report.comparability === null
              ? "报告未提供可比性结果"
              : comparabilityStatusText(report.comparability.status) +
                " · 追溯状态：" +
                restatementStatusText(report.comparability.restatement_status)}
          </dd>
        </dl>
      </details>

      <details id="annual-confirmed-metrics" className="panel annual-section annual-progressive-section" tabIndex={-1}>
        <summary className="annual-progressive-summary">
          <span>已核实的财务数据与原文证据</span>
          <small>{report.confirmed.metrics.length} 项数据 · {verifiedMetricCount} 项通过独立核验</small>
        </summary>
        <p className="help annual-progressive-help">展开后可查看金额、期间、单位、核验状态和对应年报页。</p>
        {report.confirmed.metrics.length === 0 ? (
          <p className="empty">报告没有可展示的已核对数据。</p>
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
      </details>

      <details id="annual-analysis-details" className="panel annual-section annual-progressive-section" tabIndex={-1}>
        <summary className="annual-progressive-summary">
          <span>计算过程与复核</span>
          <small>{report.confirmed.analyses.length} 项计算 · {verifiedCalculationCount} 项独立重算通过</small>
        </summary>
        <p className="help annual-progressive-help">展开后查看计算结果、复核状态和公式细节。</p>
        {report.confirmed.analyses.length === 0 ? (
          <p className="empty">报告没有已完成的计算结果。</p>
        ) : (
          <div className="annual-record-grid">
            {report.confirmed.analyses.map((calculation) => (
              <CalculationCard key={calculation.calculation_id} calculation={calculation} />
            ))}
          </div>
        )}
      </details>

      <details id="annual-verified-claims" className="panel annual-section annual-progressive-section" tabIndex={-1}>
        <summary className="annual-progressive-summary">
          <span>核验后的分析表述</span>
          <small>{report.verified_claims.length} 条分析表述 · 展开查看状态与依据</small>
        </summary>
        {report.verified_claims.length === 0 ? (
          <p className="empty">报告没有单独列出的分析说明。</p>
        ) : (
          <div className="annual-claim-list">
            {report.verified_claims.map((claim) => (
              <ClaimCard key={claim.claim_id} claim={claim} />
            ))}
          </div>
        )}
      </details>

      {reportModelInvestigation !== undefined && reportModelInvestigation !== null ? (
        <details className="annual-model-investigation-archive">
          <summary>补充：查看历史模型调查记录</summary>
          <ModelInvestigationBlock investigation={reportModelInvestigation} />
        </details>
      ) : null}
    </div>
  );
}

function ModelReviewSection({
  review,
  report,
  evidenceById,
  discoveryEvidenceIndex,
  previewBusyId,
  onPreview,
  onDiscoveryPreview,
  onLocateDiscoveryFinding,
}: {
  review: AnnualAnalysisModelReview | null;
  report: AnnualAnalysisResponse["report"];
  evidenceById: Map<string, VerificationEvidence>;
  discoveryEvidenceIndex: AnnualDiscoveryEvidenceIndexItem[];
  previewBusyId: string | null;
  onPreview: (evidenceId: string) => void;
  onDiscoveryPreview: (evidenceId: string, findingId: string) => void;
  onLocateDiscoveryFinding: (findingId: string) => void;
}) {
  const completed = review?.status === "completed" && review.assessment !== null && review.summary !== null;
  const historicInvestigation = record(report.model_investigation);
  const historicModelCalled = report.model_called || historicInvestigation?.model_called === true;
  const title = completed
    ? "AI 评审意见"
    : review !== null
      ? "AI 评审未完成"
      : "本次筛查摘要";
  return (
    <section id="annual-model-review" className="panel annual-model-review" aria-labelledby="annual-model-review-title" tabIndex={-1}>
      <div className="annual-section-heading annual-model-review-heading">
        <div>
          <p className="eyebrow">优先阅读</p>
          <h3 id="annual-model-review-title">{title}</h3>
        </div>
        {completed ? <span className="annual-review-assessment">{modelReviewAssessmentText(review.assessment)}</span> : null}
      </div>

      {completed ? (
        <>
          <p className="annual-model-review-summary">{review.summary}</p>
          {review.reasons.length > 0 ? (
            <div className="annual-model-review-block">
              <h4>评审依据</h4>
              <ol className="annual-review-reasons">
                {review.reasons.map((item, index) => (
                  <li key={`${index}-${item.text}`}>
                    <p>{item.text}</p>
                    <ModelReviewReferences
                      evidenceIds={item.evidence_ids}
                      report={report}
                      evidenceById={evidenceById}
                      discoveryEvidenceIndex={discoveryEvidenceIndex}
                      previewBusyId={previewBusyId}
                      onPreview={onPreview}
                      onDiscoveryPreview={onDiscoveryPreview}
                      onLocateDiscoveryFinding={onLocateDiscoveryFinding}
                    />
                  </li>
                ))}
              </ol>
            </div>
          ) : null}
          {review.follow_up_items.length > 0 ? (
            <div className="annual-model-review-block">
              <h4>建议后续核查</h4>
              <ol className="annual-review-follow-ups">
                {review.follow_up_items.map((item, index) => (
                  <li key={`${index}-${item.object}-${item.action}`}>
                    <strong>{modelReviewObjectLabel(item.object, report)}</strong>
                    <p>{item.action}</p>
                    <ModelReviewReferences
                      evidenceIds={item.evidence_ids}
                      report={report}
                      evidenceById={evidenceById}
                      discoveryEvidenceIndex={discoveryEvidenceIndex}
                      previewBusyId={previewBusyId}
                      onPreview={onPreview}
                      onDiscoveryPreview={onDiscoveryPreview}
                      onLocateDiscoveryFinding={onLocateDiscoveryFinding}
                    />
                    {isInternalReviewObject(item.object, report) ? (
                      <details className="annual-technical-record annual-review-object-reference">
                        <summary>查看原始关联编号</summary>
                        <code>{item.object}</code>
                      </details>
                    ) : null}
                  </li>
                ))}
              </ol>
            </div>
          ) : null}
          {review.limitations.length > 0 ? <GapList title="本次评审的范围限制" items={review.limitations} /> : null}
          <p className="annual-model-review-disclaimer">
            模型关于原因及核验状态的陈述属于未核实观点，事实与计算状态以上方独立核验记录为准；请结合补充证据复核模型意见。
          </p>
          <details className="annual-technical-record annual-model-review-technical">
            <summary>查看模型调用与归档记录</summary>
            <dl className="meta">
              <dt>模型是否实际调用</dt><dd>{review.model_called ? "是" : "否"}</dd>
              <dt>最终评审阶段归档请求数 / 尝试次数</dt><dd>{review.call_count} / {review.call_attempt_count}</dd>
              <dt>来源绑定</dt><dd><code>{review.source_identity.run_id} · {review.source_identity.source_document_id}</code></dd>
              <dt>来源 SHA256</dt><dd><code>{review.source_identity.source_sha256}</code></dd>
            </dl>
            {review.audit_artifacts.length > 0 ? (
              <ul className="annual-review-audit-list">
                {review.audit_artifacts.map((artifact, index) => (
                  <li key={recordIdentity(artifact) + "-" + index}><code>{stringValue(artifact.audit_dir) ?? stringValue(artifact.status) ?? "调用审计条目"}</code></li>
                ))}
              </ul>
            ) : <p className="help">报告未列出调用审计文件。</p>}
          </details>
        </>
      ) : (
        <>
          {review !== null ? (
            <p className="annual-model-review-failure" role="status">
              {report.model_discovery?.findings.length
                ? "全文扫描已保留 " + report.model_discovery.findings.length + " 条待核查线索；" + modelReviewReasonText(review.reason, review.status)
                : modelReviewReasonText(review.reason, review.status)}
            </p>
          ) : historicModelCalled ? (
            <p className="annual-model-review-history-note" role="status">
              {report.model_discovery?.findings.length
                ? "全文扫描已形成 " + report.model_discovery.findings.length + " 条待核查线索，但没有保存最终评审意见；线索仍可在上方查看。"
                : "这份历史归档包含旧版候选模型调查，未保存最终评审意见。"}
            </p>
          ) : report.model_investigation !== undefined && report.model_investigation !== null ? (
            <p className="annual-model-review-history-note" role="status">
              这份归档保留了历史逐条调查记录，但没有保存最终评审意见。
            </p>
          ) : (
            <p className="annual-model-review-failure" role="status">本次尚未生成 AI 评审意见；以下是本地确定性筛查结果摘要。</p>
          )}
          <DeterministicReviewSummary report={report} showHeading={review !== null} />
          {review !== null ? (
            <details className="annual-technical-record annual-model-review-technical">
              <summary>查看模型调用状态记录</summary>
              <dl className="meta">
                <dt>本次模型调用</dt><dd>{review.model_called ? "已尝试" : "未调用"}</dd>
                <dt>最终评审阶段归档请求数 / 尝试次数</dt><dd>{review.call_count} / {review.call_attempt_count}</dd>
                {review.reason !== null ? <><dt>内部原因代码</dt><dd><code>{review.reason}</code></dd></> : null}
                <dt>来源绑定</dt><dd><code>{review.source_identity.run_id} · {review.source_identity.source_document_id}</code></dd>
              </dl>
            </details>
          ) : null}
        </>
      )}
    </section>
  );
}

function DeterministicReviewSummary({ report, showHeading }: { report: AnnualAnalysisResponse["report"]; showHeading: boolean }) {
  const candidates = report.pending_review.candidate_signals.filter((item) => item.status === "candidate");
  const abstained = report.pending_review.candidate_signals.filter((item) => item.status === "abstained");
  const pendingFacts = report.pending_review.facts;
  const pendingCalculations = report.pending_review.calculations;
  const pendingClaims = report.pending_review.claims;
  const m3 = report.m3_screening;
  return (
    <div className="annual-deterministic-summary">
      {showHeading ? <h4>本地筛查摘要</h4> : null}
      {candidates.length > 0 ? (
        <p>本地确定性筛查记录了 {candidates.length} 项建议继续解释的数据变化：{candidates.map((item) => signalTitle(item.signal_id)).join("；")}。</p>
      ) : null}
      {abstained.length > 0 ? (
        <ul>
          {abstained.map((item) => (
            <li key={item.signal_id}>{signalTitle(item.signal_id)}：{abstentionReasonText(item, report)}</li>
          ))}
        </ul>
      ) : null}
      {m3 !== null && m3 !== undefined ? (
        <p>
          四项试行规则{m3.screening_status === "completed" && m3.status === "completed"
            ? m3.rules.every((rule) => rule.triggered === false) ? "均未触发" : "已形成结果，详细状态见下方筛查表"
            : "未能全部完成，详细状态见下方筛查表"}；该规则摘要不是 AI 评审意见。
        </p>
      ) : null}
      {pendingFacts.length > 0 ? (
        <p>另有 {pendingFacts.length} 项财务数据未确认{previewPendingNames(pendingFacts, report)}。</p>
      ) : null}
      {pendingCalculations.length > 0 ? <p>{pendingCalculations.length} 项年度计算因输入数据或可比性不足而暂未完成。</p> : null}
      {pendingClaims.length > 0 ? <p>{pendingClaims.length} 条分析主张仍需核对依据。</p> : null}
      {candidates.length === 0 && abstained.length === 0 && m3 === null && pendingFacts.length === 0 && pendingCalculations.length === 0 && pendingClaims.length === 0 ? (
        <p>本次确定性筛查没有记录候选事项或待确认数据。</p>
      ) : null}
    </div>
  );
}

function ModelReviewReferences({
  evidenceIds,
  report,
  evidenceById,
  discoveryEvidenceIndex,
  previewBusyId,
  onPreview,
  onDiscoveryPreview,
  onLocateDiscoveryFinding,
}: {
  evidenceIds: string[];
  report: AnnualAnalysisResponse["report"];
  evidenceById: Map<string, VerificationEvidence>;
  discoveryEvidenceIndex: AnnualDiscoveryEvidenceIndexItem[];
  previewBusyId: string | null;
  onPreview: (evidenceId: string) => void;
  onDiscoveryPreview: (evidenceId: string, findingId: string) => void;
  onLocateDiscoveryFinding: (findingId: string) => void;
}) {
  if (evidenceIds.length === 0) {
    return <p className="annual-review-reference-empty">本条意见没有关联可打开的年报出处。</p>;
  }
  const discoveryFindings = (report.model_discovery?.findings ?? []).filter((finding) => evidenceIds.includes(finding.finding_id));
  const discoveryIds = new Set(discoveryFindings.map((finding) => finding.finding_id));
  const otherEvidenceIds = evidenceIds.filter((id) => !discoveryIds.has(id));
  const result = locationsForReviewEvidenceIds(otherEvidenceIds, report, evidenceById);
  const discoveryEvidenceById = new Map(discoveryEvidenceIndex.map((item) => [item.evidence_id, item]));
  return (
    <details className="annual-review-references">
      <summary>查看本条关联出处</summary>
      {discoveryFindings.length > 0 ? (
        <div className="annual-review-discovery-references">
          <strong>关联的待核查模型观点</strong>
          <ul>
            {discoveryFindings.map((finding) => (
              <li key={finding.finding_id}>
                <button type="button" className="secondary annual-review-discovery-action" onClick={() => onLocateDiscoveryFinding(finding.finding_id)}>
                  {finding.title} · 定位待核查线索
                </button>
                <ul className="annual-review-discovery-pages">
                  {finding.citations.slice(0, 2).map((citation) => {
                    const evidence = discoveryEvidenceById.get(citation.evidence_id);
                    return evidence === undefined ? null : (
                      <li key={citation.evidence_id}>
                        <button
                          type="button"
                          className="secondary annual-review-discovery-action"
                          onClick={() => onDiscoveryPreview(evidence.evidence_id, finding.finding_id)}
                          disabled={previewBusyId === evidence.evidence_id}
                        >
                          {previewBusyId === evidence.evidence_id ? "正在打开…" : `打开 PDF 第 ${evidence.pdf_page} 页引用`}
                        </button>
                      </li>
                    );
                  })}
                </ul>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {otherEvidenceIds.length > 0 ? (
        <SourceLocationList
          heading="对应年报页"
          result={result}
          previewBusyId={previewBusyId}
          onPreview={onPreview}
        />
      ) : null}
    </details>
  );
}

function modelReviewAssessmentText(assessment: AnnualAnalysisModelReview["assessment"]): string {
  const labels: Record<Exclude<AnnualAnalysisModelReview["assessment"], null>, string> = {
    prioritize_review: "建议优先核查",
    no_priority_issue_identified_within_scope: "本次核对范围内未识别优先事项",
    insufficient_evidence: "证据不足，暂不能形成明确判断",
  };
  return assessment === null ? "评审判断未记录" : labels[assessment];
}

function modelReviewReasonText(reason: string | null, status: string): string {
  const messages: Record<string, string> = {
    model_configuration_unavailable: "后端尚未配置云端模型，最终评审阶段未能生成评审意见。",
    model_dependency_unavailable: "后端缺少 AI 分析所需依赖，最终评审阶段未能生成评审意见。",
    model_review_call_failed: "最终评审阶段的云端模型请求未完成。",
    model_review_numeric_claim_rejected: "最终评审阶段的模型意见包含未获准的数值表述，未生成可展示的评审意见。",
    model_review_response_invalid: "最终评审阶段的模型返回内容无法按评审格式读取。",
    model_review_no_priority_not_supported: "当前核查范围不支持“无需优先核查”的结论，最终评审阶段未生成该意见。",
    model_review_priority_without_reason: "最终评审阶段的模型没有提供足够的评审依据，未生成可展示的评审意见。",
    model_review_no_priority_without_reason: "最终评审阶段的模型没有提供足够的评审依据，未生成可展示的评审意见。",
  };
  if (reason !== null && messages[reason] !== undefined) {
    return messages[reason];
  }
  return status === "not_called"
    ? "最终评审阶段没有调用模型，因此未生成评审意见。"
    : "最终评审阶段未能完成。";
}

function modelReviewObjectLabel(object: string, report: AnnualAnalysisResponse["report"]): string {
  const metric = report.confirmed.metrics.find((item) => item.fact_id === object);
  if (metric !== undefined) {
    return metricLabelFromFactId(metric.fact_id) ?? metric.label_raw;
  }
  const pendingFact = report.pending_review.facts.find((item) => stringValue(item.fact_id) === object);
  if (pendingFact !== undefined) {
    return pendingItemTitle(pendingFact, report);
  }
  const calculation = report.confirmed.analyses.find((item) => item.calculation_id === object);
  if (calculation !== undefined) {
    return pendingItemTitle({
      calculation_id: calculation.calculation_id,
      input_fact_ids: calculation.input_fact_ids,
      formula_id: calculation.formula_id,
    }, report);
  }
  const pendingCalculation = report.pending_review.calculations.find((item) => stringValue(item.calculation_id) === object);
  if (pendingCalculation !== undefined) {
    return pendingItemTitle(pendingCalculation, report);
  }
  const pendingClaim = report.pending_review.claims.find((item) => stringValue(item.claim_id) === object);
  if (pendingClaim !== undefined) {
    return pendingItemTitle(pendingClaim, report);
  }
  const verifiedClaim = report.verified_claims.find((item) => item.claim_id === object);
  if (verifiedClaim !== undefined) {
    return verifiedClaim.text;
  }

  const pendingFactId = prefixedReviewObjectId(object, "pending_fact:");
  if (pendingFactId !== null) {
    const matched = report.pending_review.facts.find((item) => stringValue(item.fact_id) === pendingFactId);
    if (matched !== undefined) return pendingItemTitle(matched, report);
  }
  const pendingCalculationId = prefixedReviewObjectId(object, "pending_calculation:");
  if (pendingCalculationId !== null) {
    const matched = report.pending_review.calculations.find((item) => stringValue(item.calculation_id) === pendingCalculationId);
    if (matched !== undefined) return pendingItemTitle(matched, report);
  }
  const pendingClaimId = prefixedReviewObjectId(object, "pending_claim:");
  if (pendingClaimId !== null) {
    const matched = report.pending_review.claims.find((item) => stringValue(item.claim_id) === pendingClaimId);
    if (matched !== undefined) return pendingItemTitle(matched, report);
  }
  const investigationId = prefixedReviewObjectId(object, "investigation:");
  const signalId = investigationId ?? object;
  const candidate = report.pending_review.candidate_signals.find((item) => item.signal_id === signalId);
  if (candidate !== undefined) {
    return `${signalTitle(candidate.signal_id)}（${report.report_year - 1}—${report.report_year} 年）`;
  }
  const discoveryFinding = report.model_discovery?.findings.find((item) => item.finding_id === object);
  if (discoveryFinding !== undefined) return `${discoveryFinding.title}（待核查模型观点）`;
  return isInternalReviewObject(object, report) ? "相关财务项目" : object;
}

function prefixedReviewObjectId(value: string, prefix: string): string | null {
  return value.startsWith(prefix) && value.length > prefix.length ? value.slice(prefix.length) : null;
}

function isInternalReviewObject(object: string, report: AnnualAnalysisResponse["report"]): boolean {
  return object.includes(":") || object.startsWith("pending_") || object.startsWith("investigation:") ||
    report.pending_review.candidate_signals.some((item) => item.signal_id === object) ||
    report.confirmed.metrics.some((item) => item.fact_id === object) ||
    report.confirmed.analyses.some((item) => item.calculation_id === object) ||
    report.pending_review.facts.some((item) => stringValue(item.fact_id) === object) ||
    report.pending_review.calculations.some((item) => stringValue(item.calculation_id) === object) ||
    report.pending_review.claims.some((item) => stringValue(item.claim_id) === object) ||
    report.verified_claims.some((item) => item.claim_id === object);
}

function expandReportDetails(event: MouseEvent<HTMLAnchorElement>) {
  event.preventDefault();
  const target = document.getElementById(event.currentTarget.hash.replace(/^#/, ""));
  if (target === null) {
    return;
  }
  const details = target instanceof HTMLDetailsElement ? target : target.closest("details");
  if (details instanceof HTMLDetailsElement) {
    details.open = true;
  }
  window.requestAnimationFrame(() => {
    target.scrollIntoView({
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
      block: "start",
    });
    target.focus({ preventScroll: true });
  });
}

function verifiedMetricEvidences(
  metric: ConfirmedMetric,
  evidenceById: Map<string, VerificationEvidence>,
): VerificationEvidence[] {
  if (!isIndependentlyVerifiedMetric(metric)) {
    return [];
  }
  const verifiedEvidenceIds = new Set(
    metric.verifications
      .filter(
        (verification) =>
          verification.target_type === "financial_fact" &&
          verification.target_id === metric.fact_id &&
          verification.status === "verified",
      )
      .flatMap((verification) => verification.evidence_ids),
  );
  return metric.verification_evidences.flatMap((evidence) => {
    const indexedEvidence = evidenceById.get(evidence.evidence_id);
    if (
      !verifiedEvidenceIds.has(evidence.evidence_id) ||
      indexedEvidence === undefined ||
      indexedEvidence.document_id !== metric.source_document_id ||
      indexedEvidence.source_sha256 !== metric.source_sha256 ||
      indexedEvidence.value_region.page !== indexedEvidence.pdf_page ||
      indexedEvidence.value_normalized !== metric.normalized_value
    ) {
      return [];
    }
    return [indexedEvidence];
  });
}

function formatExactFinancialValue(value: string): string {
  const match = /^(-?)(\d+)(\.\d+)?$/.exec(value.trim());
  if (match === null) {
    return value;
  }
  const groupedInteger = match[2].replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${match[1]}${groupedInteger}${match[3] ?? ""}`;
}

function formatInHundredMillions(value: string): string | null {
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(value.trim());
  if (match === null) {
    return null;
  }
  const fraction = match[3] ?? "";
  const digits = BigInt(`${match[2]}${fraction}`);
  const divisor = 10n ** BigInt(fraction.length + 6);
  let hundredths = digits / divisor;
  if ((digits % divisor) * 2n >= divisor) {
    hundredths += 1n;
  }
  const fixed = hundredths.toString().padStart(3, "0");
  const integer = fixed.slice(0, -2).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const sign = match[1] === "-" && digits !== 0n ? "-" : "";
  return `${sign}${integer}.${fixed.slice(-2)}`;
}

function M3ScreeningBlock({ screening }: { screening: M3AnnualScreening }) {
  const complete = screening.screening_status === "completed" && screening.status === "completed";
  const scoreText =
    complete && screening.total_score !== null
      ? screening.total_score === 0
        ? `0 / ${screening.maximum_score} · 试行规则未触发`
        : `${screening.total_score} / ${screening.maximum_score} · 候选待核查`
      : "未形成完整汇总分";
  return (
    <section id="annual-m3-screening" className="panel annual-m3-screening" aria-label="四项试行筛查结果" tabIndex={-1}>
      <div className="annual-section-heading">
        <div>
          <p className="eyebrow">补充筛查结果</p>
          <h3>四项试行筛查</h3>
        </div>
        <span className={complete ? "review-chip" : "review-chip annual-m3-abstained"}>
          {m3StatusText(screening.status, screening.screening_status)}
        </span>
      </div>
      <p className="annual-candidate-warning">
        这组规则尚未验证对其他公司的适用性。结果不是事实核验、审计意见或舞弊结论；事实核验状态请展开“已核实的财务数据与原文证据”查看。
      </p>
      <div className="annual-m3-summary">
        <div><span>汇总分</span><strong>{scoreText}</strong></div>
      </div>
      <details className="annual-technical-record">
        <summary>技术记录：筛查版本与处理状态</summary>
        <dl className="annual-detail-grid">
          <dt>内部类型</dt><dd><code>{screening.kind}</code></dd>
          <dt>规则版本</dt><dd><code>{screening.rule_version}</code></dd>
          <dt>流程状态</dt><dd><code>{screening.status}</code></dd>
          <dt>筛查状态</dt><dd><code>{screening.screening_status}</code></dd>
        </dl>
      </details>
      {complete && screening.total_score === 0 ? (
        <p className="empty annual-m3-note">0 分只表示本次四项试行规则均未触发，不构成无风险判断，也不改变上方其他候选线索的状态。</p>
      ) : null}
      {screening.total_score === null ? (
        <p className="empty annual-m3-note">至少一条规则无法计算或 M3 流程弃权，因此没有总分；缺失结果不会按零分处理。</p>
      ) : null}
      {screening.rules.length === 0 ? (
        <p className="empty">报告没有可展示的规则结果。</p>
      ) : (
        <div className="annual-m3-rules">
          {screening.rules.map((rule) => (
            <article key={rule.rule_id} className="annual-m3-rule">
              <div className="annual-m3-rule-heading">
                <div>
                  <h4>{m3RuleTitle(rule.rule_id)}</h4>
                  <p className="annual-m3-rule-description">{m3RuleDescription(rule.rule_id)}</p>
                </div>
                <span className={rule.triggered === true ? "review-chip annual-m3-triggered" : "annual-m3-rule-chip"}>
                  {m3RuleStatusText(rule.status, rule.triggered)}
                </span>
              </div>
              {rule.calculated_value === null ? (
                <p className="annual-m3-rule-result">本次未能完成计算；展开详情查看具体情况。</p>
              ) : null}
              <details className="annual-technical-record annual-m3-technical-record">
                <summary>查看规则口径、计算阈值与详细数据</summary>
                <dl className="annual-m3-rule-meta">
                  <dt>计算口径</dt><dd>{rule.formula}</dd>
                  <dt>阈值</dt><dd>{rule.threshold ?? "无单独阈值"}</dd>
                  <dt>试行分值</dt><dd>{rule.points === null ? "弃权" : `${rule.points} / ${rule.points_if_triggered}`}</dd>
                  <dt>完整计算值</dt><dd>{rule.calculated_value === null ? "未形成" : <code>{rule.calculated_value}</code>}</dd>
                  <dt>规则编号</dt><dd><code>{rule.rule_id}</code></dd>
                  <dt>规则版本</dt><dd><code>{screening.rule_version}</code></dd>
                  <dt>内部状态</dt><dd><code>{rule.status}</code></dd>
                </dl>
                {rule.issues.length === 0 ? null : (
                  <ul className="annual-m3-issue">
                    {rule.issues.map((issue, index) => (
                      <li key={`${rule.rule_id}-${index}-${issue.code ?? "legacy"}`}>
                        <span>{issue.message}</span>
                        {issue.code === null ? null : <code>{issue.code}</code>}
                      </li>
                    ))}
                  </ul>
                )}
              </details>
            </article>
          ))}
        </div>
      )}
      {screening.limitations.length > 0 ? (
        <details className="annual-m3-limitations-details">
          <summary>查看试行范围和已知限制</summary>
          <ul className="annual-m3-limitations">
            {screening.limitations.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}
          </ul>
        </details>
      ) : null}
    </section>
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
  const verified = isIndependentlyVerifiedMetric(metric);
  const metricVerificationStatuses = metric.verifications
    .filter((item) => item.target_type === "financial_fact" && item.target_id === metric.fact_id)
    .map((item) => item.status);
  const verificationLabel = metricVerificationLabel(metricVerificationStatuses, verified);
  const verifiedIds = new Set(
    verified
      ? metric.verifications
          .filter(
        (item) =>
          item.target_type === "financial_fact" &&
          item.target_id === metric.fact_id &&
          item.status === "verified",
          )
          .flatMap((item) => item.evidence_ids)
      : [],
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
        </div>
        <span className={verified ? "verification-chip" : "review-chip"}>
          {verificationLabel}
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
        <dt>币种与单位</dt><dd>{currencyText(metric.currency)} · {unit}</dd>
        <dt>报表口径</dt>
        <dd>{statementTypeText(metric.statement_type)} · {scopeText(metric.scope)} · {periodTypeText(metric.period_type)}</dd>
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
      <details className="annual-technical-record">
        <summary>技术记录：事实编号与来源绑定</summary>
        <dl className="annual-detail-grid">
          <dt>事实编号</dt><dd><code>{metric.fact_id}</code></dd>
          <dt>内部指标名</dt><dd><code>{metric.metric_id ?? "报告未记录"}</code></dd>
          <dt>来源文件编号</dt><dd><code>{metric.source_document_id}</code></dd>
          <dt>来源 SHA256</dt><dd><code>{metric.source_sha256}</code></dd>
          <dt>单位换算倍率</dt><dd><code>{metric.unit_multiplier}</code></dd>
          <dt>原始币种代码</dt><dd><code>{metric.currency}</code></dd>
          <dt>内部期间角色</dt><dd><code>{metric.comparison_role}</code></dd>
          <dt>期间类型</dt><dd><code>{metric.period_type ?? "未记录"}</code></dd>
          <dt>核验证据编号</dt>
          <dd>{allowedEvidences.length === 0 ? "无" : allowedEvidences.map((item) => <code key={item.evidence_id}>{item.evidence_id}</code>)}</dd>
        </dl>
      </details>
    </article>
  );
}

function CalculationCard({ calculation }: { calculation: ConfirmedCalculation }) {
  const independentlyVerified = isIndependentlyVerifiedCalculation(calculation);
  const verification = calculation.independent_verification;
  const verificationLabel = independentlyVerified
    ? "独立重算核验通过"
    : verification === null
      ? "未记录独立重算核验"
      : verificationStatusText(verification.status);
  const value = calculation.output_value === null ? "未形成结果" : String(calculation.output_value);
  const recomputed = verification?.recomputed_value;
  return (
    <article className="annual-record-card">
      <div className="annual-record-heading">
        <h4>计算结果</h4>
        <span className={independentlyVerified ? "verification-chip" : "review-chip"}>
          {verificationLabel}
        </span>
      </div>
      <p className="annual-value">{value} {calculation.unit ?? ""}</p>
      <details className="annual-technical-record">
        <summary>查看计算过程和核验详情</summary>
        <dl className="annual-detail-grid">
          <dt>计算公式</dt><dd><code>{calculation.formula_expression}</code></dd>
          <dt>独立重算值</dt><dd>{recomputed === undefined || recomputed === null ? "未记录" : String(recomputed) + " " + (calculation.unit ?? "")}</dd>
          <dt>计算状态</dt><dd>{calculationStatusText(calculation.status)}（{calculation.status}）</dd>
          <dt>计算编号</dt><dd><code>{calculation.calculation_id}</code></dd>
          <dt>公式编号</dt><dd><code>{calculation.formula_id}</code></dd>
          <dt>输入事实编号</dt>
          <dd>{calculation.input_fact_ids.length === 0 ? "未记录" : calculation.input_fact_ids.map((id) => <code key={id}>{id}</code>)}</dd>
        </dl>
        {calculation.failure_reason !== null ? <p className="annual-reason">失败原因：{calculation.failure_reason}</p> : null}
        {calculation.independent_verification?.reason ? (
          <p className="annual-reason">独立核验说明：{calculation.independent_verification.reason}</p>
        ) : null}
      </details>
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

function CandidateCard({
  candidate,
  report,
  evidenceById,
  locations,
  previewBusyId,
  onPreview,
}: {
  candidate: CandidateSignal;
  report: AnnualAnalysisResponse["report"];
  evidenceById: Map<string, VerificationEvidence>;
  locations: SourceLocationResult;
  previewBusyId: string | null;
  onPreview: (evidenceId: string) => void;
}) {
  const changes = verifiedCandidateChanges(candidate, report, evidenceById);
  const why = candidate.reason?.trim() === ""
    ? "原因尚未说明。"
    : `报告记录原因（未独立证实）：${candidate.reason}`;
  const followUp = candidateFollowUpText(candidate.signal_id);
  return (
    <article className="annual-record-card annual-candidate-card">
      <div className="annual-record-heading">
        <div>
          <h4>{signalTitle(candidate.signal_id)}</h4>
          <p className="annual-candidate-period">比较期间 · {report.report_year - 1} → {report.report_year} 年</p>
        </div>
        <span className="review-chip">{changes.length > 0 ? "变化原因待解释" : "变化情况待确认"}</span>
      </div>
      <div className="annual-candidate-content">
        <div className="annual-candidate-data-block">
          <h5>已核对的变化</h5>
          {changes.length > 0 ? (
            <ul className="annual-candidate-change-list">
              {changes.map((change) => <li key={change.metricId}>{change.text}</li>)}
            </ul>
          ) : (
            <p className="annual-candidate-missing">本次报告没有提供满足独立核验条件的年度差额，暂不能确认变化方向。</p>
          )}
        </div>
        <div className="annual-candidate-data-block">
          <h5>解释与缺口</h5>
          <p>{why}</p>
        </div>
        <div className="annual-candidate-data-block annual-candidate-action-block">
          <h5>建议查看</h5>
          <p>{followUp}</p>
        </div>
      </div>
      <details className="annual-candidate-sources">
        <summary>查看已核对数据的年报出处</summary>
        <SourceLocationList
          heading="关联年报位置"
          result={locations}
          previewBusyId={previewBusyId}
          onPreview={onPreview}
        />
      </details>
      <details className="annual-technical-record">
        <summary>查看原始计算记录</summary>
        <dl className="annual-detail-grid">
          <dt>相关事实 ID</dt><dd>{idList(candidate.input_fact_ids)}</dd>
          <dt>相关计算 ID</dt><dd>{idList(candidate.calculation_ids)}</dd>
          <dt>筛查原始差额字段</dt><dd>{candidate.left_difference ?? "报告未记录"} / {candidate.right_difference ?? "报告未记录"}</dd>
          <dt>线索编号</dt><dd><code>{candidate.signal_id}</code></dd>
          <dt>内部状态</dt><dd><code>{candidate.status}</code></dd>
          <dt>状态说明</dt><dd>{candidate.placement_reasons.join("；") || "报告未单列"}</dd>
        </dl>
      </details>
    </article>
  );
}

function AbstainedSignalCard({
  candidate,
  report,
  evidenceById,
  previewBusyId,
  onPreview,
}: {
  candidate: CandidateSignal;
  report: AnnualAnalysisResponse["report"];
  evidenceById: Map<string, VerificationEvidence>;
  previewBusyId: string | null;
  onPreview: (evidenceId: string) => void;
}) {
  const missing = abstentionReasonText(candidate, report);
  return (
    <article className="annual-abstained-card">
      <div className="annual-record-heading">
        <div>
          <h4>{signalTitle(candidate.signal_id)}</h4>
          <p className="annual-candidate-period">涉及 {report.report_year - 1} 与 {report.report_year} 年</p>
        </div>
        <span className="review-chip">比较暂不能确认</span>
      </div>
      <p><strong>缺少的前提：</strong>{missing}</p>
      <p><strong>还需补充：</strong>{abstentionFollowUpText(missing)}</p>
      <details className="annual-candidate-sources">
        <summary>查看待核对数据的年报出处</summary>
        <SourceLocationList
          heading="待核对位置"
          result={locationsForFactIds(candidate.input_fact_ids, report, evidenceById)}
          previewBusyId={previewBusyId}
          onPreview={onPreview}
        />
      </details>
      <details className="annual-technical-record">
        <summary>查看内部编号和原始弃权原因</summary>
        <dl className="annual-detail-grid">
          <dt>相关事实 ID</dt><dd>{idList(candidate.input_fact_ids)}</dd>
          <dt>相关计算 ID</dt><dd>{idList(candidate.calculation_ids)}</dd>
          <dt>内部状态</dt><dd><code>{candidate.status}</code></dd>
          <dt>原始原因</dt><dd>{candidate.reason ?? "报告未记录"}</dd>
        </dl>
      </details>
    </article>
  );
}

function verifiedCandidateChanges(
  candidate: CandidateSignal,
  report: AnnualAnalysisResponse["report"],
  evidenceById: Map<string, VerificationEvidence>,
): Array<{ metricId: string; direction: "increase" | "decrease" | "unchanged"; text: string }> {
  const changes: Array<{ metricId: string; direction: "increase" | "decrease" | "unchanged"; text: string }> = [];
  const expectedMetricIds = candidate.signal_id === "profit_up_cash_down"
    ? new Set(["net_profit_parent", "operating_cash_flow"])
    : candidate.signal_id === "revenue_up_cash_down"
      ? new Set(["revenue", "operating_cash_flow"])
      : new Set<string>();
  const candidateInputIds = new Set(candidate.input_fact_ids);
  if (expectedMetricIds.size === 0) {
    return changes;
  }
  for (const spec of CORE_METRICS.filter((item) => expectedMetricIds.has(item.metricId))) {
    const row = buildCoreMetricRow(spec, report, evidenceById);
    if (
      row.difference === null || row.currentMetric === undefined || row.comparativeMetric === undefined ||
      !candidateInputIds.has(row.currentMetric.fact_id) || !candidateInputIds.has(row.comparativeMetric.fact_id) ||
      !candidate.calculation_ids.includes(row.difference.calculation_id)
    ) continue;
    const value = normalizeExactDecimal(row.difference.output_value);
    if (value === null) continue;
    const magnitude = value.replace(/^-/, "");
    const isZero = /^0(?:\.0+)?$/.test(magnitude);
    const isNegative = value.startsWith("-");
    changes.push({
      metricId: spec.metricId,
      direction: isZero ? "unchanged" : isNegative ? "decrease" : "increase",
      text: `${spec.label}：${isZero ? "无变化" : isNegative ? "减少" : "增加"}${formatSignedAmount(row.difference.output_value)}`,
    });
  }
  return changes;
}

function abstentionFollowUpText(reason: string): string {
  if (reason.includes("币种")) {
    return "先找到年报中明确披露币种的依据，再确认两年数据的单位和期间口径可比。";
  }
  if (reason.includes("单位")) {
    return "先确认两年金额单位一致，并核对期间和比较口径。";
  }
  return "补齐缺少的年度数据或披露口径依据，再判断差额和变化方向。";
}

function normalizeExactDecimal(value: string | number | null | undefined): string | null {
  if (typeof value !== "string") {
    return null;
  }
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(value.trim());
  if (match === null) {
    return null;
  }
  const integer = match[2].replace(/^0+(?=\d)/, "");
  const fraction = (match[3] ?? "").replace(/0+$/, "");
  const magnitude = fraction.length === 0 ? integer : `${integer}.${fraction}`;
  return match[1] === "-" && magnitude !== "0" ? `-${magnitude}` : magnitude;
}

function candidateFollowUpText(signalId: string): string {
  if (signalId === "profit_up_cash_down") {
    return "查看年报中“将净利润调节为经营活动现金流量”的附注、营运资金项目变化和非现金项目明细，核对差异来自哪些项目。";
  }
  if (signalId === "revenue_up_cash_down") {
    return "查看销售回款、应收账款及其变化、现金流入与流出明细，核对收入与现金收付的对应关系。";
  }
  return "结合相关指标的年报附注和现金流明细逐项核对变化原因。";
}

function abstentionReasonText(candidate: CandidateSignal, report: AnnualAnalysisResponse["report"]): string {
  const pendingFacts = candidate.input_fact_ids
    .map((factId) => report.pending_review.facts.find((item) => stringValue(item.fact_id) === factId))
    .filter((item): item is AnnualReportRecord => item !== undefined);
  const rawText = [candidate.reason ?? "", ...pendingFacts.flatMap((item) => pendingRawReasons(item, report))].join(" ").toLowerCase();
  if (rawText.includes("币种") || rawText.includes("currency")) {
    return "年报表头未明确币种，不能确认两年金额可以直接比较。";
  }
  if (rawText.includes("单位") || rawText.includes("unit")) {
    return "金额单位尚未确认，暂不能比较年度差额。";
  }
  if (rawText.includes("restatement") || rawText.includes("追溯调整") || rawText.includes("可比")) {
    return "比较期口径或追溯调整状态尚未确认。";
  }
  return nonEmpty(candidate.reason) ?? "所需年度数据尚未通过核验，暂不能比较变化方向。";
}

function SourceLocationList({
  heading,
  result,
  previewBusyId,
  onPreview,
}: {
  heading: string;
  result: SourceLocationResult;
  previewBusyId: string | null;
  onPreview: (evidenceId: string) => void;
}) {
  return (
    <section className="annual-source-locations" aria-label={heading}>
      <h5>{heading}</h5>
      {result.locations.length === 0 ? (
        <p className="annual-location-empty">原文位置待定位</p>
      ) : (
        <ul>
          {result.locations.map((location) => (
            <li key={location.key}>
              <strong className="annual-location-metric">
                {location.metricLabel}{location.periodYear === null ? "" : `（${location.periodYear} 年）`}
              </strong>
              <ul className="annual-location-pages">
                {location.pages.map((page) => {
                  const evidenceId = page.evidenceIds[0];
                  return (
                    <li key={`${location.key}-${page.pageNumber}`}>
                      <span>
                        第 {page.pageNumber} 页 · {location.source === "verified" ? "已独立核验出处" : "待核实定位"}
                      </span>
                      {location.source === "verified" && evidenceId !== undefined ? (
                        <button
                          type="button"
                          className="secondary annual-location-preview-button"
                          onClick={() => onPreview(evidenceId)}
                          disabled={previewBusyId !== null}
                        >
                          {previewBusyId === evidenceId ? "正在读取原文页图…" : "查看原文页图"}
                        </button>
                      ) : null}
                    </li>
                  );
                })}
              </ul>
            </li>
          ))}
        </ul>
      )}
      {result.unlocatedCount > 0 ? (
        <p className="annual-location-unlocated">
          另有 {result.unlocatedCount} 项原文位置待定位；未找到可靠页码。
        </p>
      ) : null}
    </section>
  );
}

function PendingItems({
  pending,
  report,
  evidenceById,
  previewBusyId,
  onPreview,
}: {
  pending: AnnualAnalysisResponse["report"]["pending_review"];
  report: AnnualAnalysisResponse["report"];
  evidenceById: Map<string, VerificationEvidence>;
  previewBusyId: string | null;
  onPreview: (evidenceId: string) => void;
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

  if (needsReason.length === 0 && nonAbstained.length === 0) {
    return null;
  }

  return (
    <div className="annual-pending-sections">
      {needsReason.length > 0 ? (
        <section className="annual-pending-group">
          <h4>尚未确认的数据</h4>
          <div className="annual-pending-items">
            {needsReason.map(({ item, key }) => (
              <PendingItemSummary
                key={key}
                item={item}
                report={report}
                locations={locationsForPendingItem(item, report, evidenceById)}
                previewBusyId={previewBusyId}
                onPreview={onPreview}
              />
            ))}
          </div>
        </section>
      ) : null}
      {nonAbstained.length > 0 ? (
        <section className="annual-pending-group">
          <h4>其他需要核对的项目</h4>
          <div className="annual-pending-items">
            {nonAbstained.map(({ item, key }) => (
              <PendingItemSummary
                key={key}
                item={item}
                report={report}
                locations={locationsForPendingItem(item, report, evidenceById)}
                previewBusyId={previewBusyId}
                onPreview={onPreview}
              />
            ))}
          </div>
        </section>
      ) : null}
    </div>
  );
}

function ModelInvestigationBlock({ investigation }: { investigation: AnnualReportRecord }) {
  const entries = Object.entries(investigation);
  const knownFields = new Set([
    "status", "model_called", "reason", "reason_detail", "items",
    "model_call_count", "model_call_attempt_count",
    "investigation_call_count", "investigation_call_attempt_count",
    "model_review_call_count", "model_review_call_attempt_count", "final_review", "discovery",
  ]);
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
          <p className="eyebrow">辅助解释</p>
          <h3>模型解释与弃权记录</h3>
        </div>
        <span className="review-chip">{investigationStatusText(status)}</span>
      </div>
      <p className="annual-candidate-warning">
        以下内容来自报告中的可选模型调查字段，解释和弃权均未独立核实，不作为已核验事实或确认结论。
      </p>
      <dl className="annual-model-overview">
        <dt>调查状态</dt>
        <dd>{status === null ? "未记录状态" : investigationStatusText(status)}</dd>
      </dl>
      {reason !== null || reasonDetail !== null ? (
        <div className="annual-model-reason" role="note">
          {reason !== null ? <p><strong>状态说明：</strong>{investigationReasonText(reason)}</p> : null}
          {reasonDetail !== null ? <p><strong>补充详情：</strong>{reasonDetail}</p> : null}
        </div>
      ) : null}
      {modelCalled !== null ? (
        <details className="annual-technical-record annual-model-technical">
          <summary>技术记录：调用状态与内部原因</summary>
          <dl className="meta">
            <dt>模型是否实际调用</dt><dd>{modelCalled ? "是" : "否"}</dd>
            <dt>完整模型流程调用总数</dt><dd>{modelFlowCountSummary(investigation)}</dd>
            {status !== null ? <><dt>内部状态代码</dt><dd><code>{status}</code></dd></> : null}
            {reason !== null ? <><dt>内部原因代码</dt><dd><code>{reason}</code></dd></> : null}
          </dl>
        </details>
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
        </div>
        <span className="review-chip">{investigationItemStatusText(status)}</span>
      </div>
      {status === "interpretation" ? (
        <p className="annual-model-explanation"><strong>未核实模型解释：</strong>{explanation ?? "报告未提供解释文字。"}</p>
      ) : status === "abstained" ? (
        <p className="annual-model-explanation"><strong>弃权原因：</strong>{reason === null ? "未提供具体原因。" : investigationReasonText(reason)}</p>
      ) : explanation !== null ? (
        <p className="annual-model-explanation"><strong>未核实说明：</strong>{explanation}</p>
      ) : null}
      {reason !== null && status !== "abstained" ? (
        <p className="annual-model-explanation"><strong>条目原因：</strong>{investigationReasonText(reason)}</p>
      ) : null}
      {alternatives.length > 0 ? <GapList title="替代解释" items={alternatives} /> : null}
      {limitations.length > 0 ? <GapList title="限制" items={limitations} /> : null}
      {narrativeEvidenceIds.length > 0 ? (
        <details className="annual-model-evidence-ids">
          <summary>查看模型引用的叙述证据编号（未核验）</summary>
          <ul>{narrativeEvidenceIds.map((id, index) => <li key={id + "-" + index}><code>{id}</code></li>)}</ul>
        </details>
      ) : null}
      {signalId !== null || reason !== null ? (
        <details className="annual-technical-record annual-model-technical">
          <summary>技术记录：条目编号与内部原因</summary>
          <dl className="meta">
            {signalId !== null ? <><dt>内部线索编号</dt><dd><code>{signalId}</code></dd></> : null}
            {reason !== null ? <><dt>内部原因代码</dt><dd><code>{reason}</code></dd></> : null}
          </dl>
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
  const statuses = metric.verifications
    .filter((item) => item.target_type === "financial_fact" && item.target_id === metric.fact_id)
    .map((item) => item.status);
  const hasVerificationConcern = statuses.some((status) =>
    ["abstained", "insufficient_evidence", "failed", "conflict", "conflicted", "unsupported"].includes(status),
  );
  return !hasVerificationConcern && statuses.includes("verified");
}

function metricVerificationLabel(statuses: string[], verified: boolean): string {
  if (verified) {
    return "独立核验通过";
  }
  const concerns = Array.from(new Set(
    statuses.filter((status) => ["abstained", "insufficient_evidence", "failed", "conflict", "conflicted", "unsupported", "unknown"].includes(status)),
  ));
  return concerns.length > 0
    ? concerns.map(verificationStatusText).join(" / ")
    : "报告未标记为独立核验通过";
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

function PendingItemSummary({
  item,
  report,
  locations,
  previewBusyId,
  onPreview,
}: {
  item: AnnualReportRecord;
  report: AnnualAnalysisResponse["report"];
  locations: SourceLocationResult;
  previewBusyId: string | null;
  onPreview: (evidenceId: string) => void;
}) {
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
  const reasons = pendingRawReasons(item, report);
  const statuses = Array.from(new Set(statusRecords.map(pendingStatusText)));
  return (
    <article className="annual-pending-item">
      <div className="annual-pending-item-heading">
        <h5>{pendingItemTitle(item, report)}</h5>
        {statuses.length > 0 ? <span className="annual-pending-status">{statuses.join(" / ")}</span> : null}
      </div>
      <p>{pendingItemFriendlyReason(item, report)}</p>
      <SourceLocationList
        heading="关联数据的年报位置"
        result={locations}
        previewBusyId={previewBusyId}
        onPreview={onPreview}
      />
      <details className="annual-technical-record annual-pending-technical-record">
        <summary>查看内部编号与原始核验说明</summary>
        <dl className="annual-detail-grid">
          <dt>内部编号</dt><dd><code>{id}</code></dd>
          <dt>原始状态</dt><dd>{statuses.join(" / ") || "未记录"}</dd>
        </dl>
        {reasons.length > 0 ? (
          <ul className="annual-pending-raw-reasons">
            {Array.from(new Set(reasons)).map((reason, index) => <li key={index}>{reason}</li>)}
          </ul>
        ) : <p className="help">报告未记录原始核验说明。</p>}
      </details>
    </article>
  );
}

function pendingRawReasons(item: AnnualReportRecord, report: AnnualAnalysisResponse["report"]): string[] {
  const reasons: string[] = [];
  appendReason(reasons, "原因", item.reason);
  appendReason(reasons, "失败原因", item.failure_reason);
  appendReason(reasons, "放置原因", item.placement_reasons);
  appendReason(reasons, "限制", item.limitations);
  appendReason(reasons, "核验说明", record(item.independent_verification)?.reason);
  appendReason(reasons, "核验说明", record(item.verification)?.reason);
  appendReason(reasons, "核验限制", record(item.verification)?.limitations);
  appendReason(reasons, "核验冲突", record(item.verification)?.conflicts);
  const verifications = Array.isArray(item.verifications)
    ? item.verifications.map(record).filter((value): value is AnnualReportRecord => value !== null)
    : [];
  for (const verification of verifications) {
    appendReason(reasons, "核验限制", verification.limitations);
    appendReason(reasons, "核验冲突", verification.conflicts);
    appendReason(reasons, "核验说明", verification.reason);
  }
  if (stringValue(item.calculation_id) !== null) {
    const inputIds = stringArray(item.input_fact_ids);
    for (const inputId of inputIds) {
      const fact = report.pending_review.facts.find((candidate) => stringValue(candidate.fact_id) === inputId);
      if (fact !== undefined) {
        appendReason(reasons, "输入数据核验限制", fact.limitations);
        appendReason(reasons, "输入数据核验限制", record(fact.verification)?.limitations);
        const factVerifications = Array.isArray(fact.verifications)
          ? fact.verifications.map(record).filter((value): value is AnnualReportRecord => value !== null)
          : [];
        for (const verification of factVerifications) {
          appendReason(reasons, "输入数据核验限制", verification.limitations);
        }
      }
    }
  }
  return Array.from(new Set(reasons));
}

function pendingItemTitle(item: AnnualReportRecord, report: AnnualAnalysisResponse["report"]): string {
  const factId = stringValue(item.fact_id);
  if (factId !== null) {
    const label = stringValue(item.label_raw) ?? stringValue(item.label) ?? metricLabelFromFactId(factId) ?? "年报财务数据";
    const year = pendingYear(item);
    return year === null ? label : `${label}（${year} 年）`;
  }
  const calculationId = stringValue(item.calculation_id);
  if (calculationId !== null) {
    const inputIds = stringArray(item.input_fact_ids);
    const inputFacts = inputIds.flatMap((id) => {
      const pending = report.pending_review.facts.find((fact) => stringValue(fact.fact_id) === id);
      if (pending !== undefined) {
        return [{
          label: stringValue(pending.label_raw) ?? stringValue(pending.label) ?? metricLabelFromFactId(id) ?? "财务数据",
          year: pendingYear(pending),
        }];
      }
      const confirmed = report.confirmed.metrics.find((fact) => fact.fact_id === id);
      return confirmed === undefined ? [] : [{
        label: metricLabelFromFactId(id) ?? confirmed.label_raw,
        year: confirmed.period_end?.slice(0, 4) ?? null,
      }];
    });
    const labels = Array.from(new Set(inputFacts.map((fact) => fact.label)));
    const metric = labels.length === 0 ? "相关财务数据" : labels.join("与");
    const formula = stringValue(item.formula_id) ?? stringValue(item.formula);
    const years = Array.from(new Set(inputFacts.map((fact) => fact.year).filter((value): value is string => value !== null))).sort();
    const period = years.length >= 2 ? `（${years[0]}—${years[years.length - 1]} 年）` : "";
    if (formula === "annual_yoy_rate" || calculationId.includes("annual_yoy_rate")) {
      return `${metric}同比变化率${period}`;
    }
    if (formula === "annual_difference" || calculationId.includes("annual_difference")) {
      return `${metric}年度差额${period}`;
    }
    return `${metric}计算${period}`;
  }
  const claimText = stringValue(item.text) ?? stringValue(item.expected_text) ?? stringValue(item.title);
  if (claimText !== null) {
    return claimText;
  }
  if (stringValue(item.evidence_id) !== null) {
    return "尚未用于分析结论的年报证据";
  }
  return "待核对的分析项目";
}

function pendingYear(item: AnnualReportRecord): string | null {
  const raw = stringValue(item.period_end) ?? stringValue(item.year) ?? stringValue(item.report_year);
  if (raw === null) {
    return null;
  }
  const match = /(?:^|\D)(20\d{2})(?:\D|$)/.exec(raw);
  return match?.[1] ?? null;
}

function metricLabelFromFactId(factId: string): string | null {
  const labels: Array<[string, string]> = [
    ["net_profit_parent_ex_nonrecurring", "扣非归母净利润"],
    ["net_profit_parent", "归属于母公司股东的净利润"],
    ["operating_cash_flow", "经营活动产生的现金流量净额"],
    ["non_recurring_total", "披露的非经常性损益合计"],
    ["accounts_receivable_net", "应收账款净额"],
    ["inventory_net", "存货净额"],
    ["cost_of_revenue", "营业成本"],
    ["net_income", "合并净利润"],
    ["revenue", "营业收入"],
  ];
  return labels.find(([key]) => factId.includes(`:${key}:`))?.[1] ?? null;
}

function pendingItemFriendlyReason(item: AnnualReportRecord, report: AnnualAnalysisResponse["report"]): string {
  const raw = pendingRawReasons(item, report).join(" ").toLowerCase();
  const itemType = stringValue(item.fact_id) !== null
    ? "fact"
    : stringValue(item.calculation_id) !== null
      ? "calculation"
      : stringValue(item.claim_id) !== null
        ? "claim"
        : "other";
  if (raw.includes("币种") || raw.includes("currency")) {
    return itemType === "calculation"
      ? "输入数据尚未确认币种，相关年度差额暂不能计算。"
      : "尚未确认币种，金额暂不能用于判断。";
  }
  if (raw.includes("单位") || raw.includes("unit")) {
    return itemType === "calculation"
      ? "输入数据的单位尚未确认，暂不能完成年度计算。"
      : "金额单位尚未确认，暂不能用于判断。";
  }
  if (raw.includes("restatement") || raw.includes("追溯调整")) {
    return itemType === "calculation"
      ? "比较期是否追溯调整尚未确认，暂不能比较年度变化。"
      : "比较期是否追溯调整尚待确认。";
  }
  if (raw.includes("conflict") || raw.includes("冲突")) {
    return "原文位置或披露口径存在冲突，需要回到年报核对。";
  }
  if (itemType === "calculation") {
    return "输入数据尚未核实，暂不能完成这项计算。";
  }
  if (itemType === "claim") {
    return "这段分析目前没有足够的已核验依据。";
  }
  if (itemType === "fact") {
    return "原文证据不足，暂未确认这项数据。";
  }
  return "报告没有提供足够信息来确认该项目。";
}

function previewPendingNames(items: AnnualReportRecord[], report: AnnualAnalysisResponse["report"]): string {
  const names = Array.from(new Set(items.slice(0, 3).map((item) => pendingItemTitle(item, report))));
  return names.length === 0 ? "" : `（如：${names.join("、")}${items.length > names.length ? "等" : ""}）`;
}

function locationsForReviewEvidenceIds(
  evidenceIds: string[],
  report: AnnualAnalysisResponse["report"],
  evidenceById: Map<string, VerificationEvidence>,
): SourceLocationResult {
  const factIds = new Set<string>();
  const investigation = record(report.model_investigation);
  const investigationItems = Array.isArray(investigation?.items)
    ? investigation.items.map(record).filter((item): item is AnnualReportRecord => item !== null)
    : [];

  for (const referenceId of evidenceIds) {
    if (report.confirmed.metrics.some((item) => item.fact_id === referenceId) ||
        report.pending_review.facts.some((item) => stringValue(item.fact_id) === referenceId)) {
      factIds.add(referenceId);
      continue;
    }

    const confirmedCalculation = report.confirmed.analyses.find((item) => item.calculation_id === referenceId);
    const pendingCalculation = report.pending_review.calculations.find((item) => stringValue(item.calculation_id) === referenceId);
    const calculation = confirmedCalculation ?? pendingCalculation;
    if (calculation !== undefined) {
      stringArray(calculation.input_fact_ids).forEach((factId) => factIds.add(factId));
      continue;
    }

    const confirmedClaim = report.verified_claims.find((item) => item.claim_id === referenceId);
    const pendingClaim = report.pending_review.claims.find((item) => stringValue(item.claim_id) === referenceId);
    const claimFacts = confirmedClaim?.supporting_fact_ids ?? stringArray(pendingClaim?.supporting_fact_ids);
    if (claimFacts.length > 0) {
      claimFacts.forEach((factId) => factIds.add(factId));
      continue;
    }

    const pendingFactId = prefixedReviewObjectId(referenceId, "pending_fact:");
    if (pendingFactId !== null && report.pending_review.facts.some((item) => stringValue(item.fact_id) === pendingFactId)) {
      factIds.add(pendingFactId);
      continue;
    }
    const pendingCalculationId = prefixedReviewObjectId(referenceId, "pending_calculation:");
    if (pendingCalculationId !== null) {
      const item = report.pending_review.calculations.find((entry) => stringValue(entry.calculation_id) === pendingCalculationId);
      stringArray(item?.input_fact_ids).forEach((factId) => factIds.add(factId));
      continue;
    }
    const pendingClaimId = prefixedReviewObjectId(referenceId, "pending_claim:");
    if (pendingClaimId !== null) {
      const item = report.pending_review.claims.find((entry) => stringValue(entry.claim_id) === pendingClaimId);
      stringArray(item?.supporting_fact_ids).forEach((factId) => factIds.add(factId));
      continue;
    }

    const investigationId = prefixedReviewObjectId(referenceId, "investigation:");
    const signalId = investigationId ?? referenceId;
    const candidate = report.pending_review.candidate_signals.find((item) => item.signal_id === signalId);
    if (candidate !== undefined) {
      candidate.input_fact_ids.forEach((factId) => factIds.add(factId));
      continue;
    }
    const investigationItem = investigationItems.find((item) => stringValue(item.signal_id) === signalId);
    stringArray(investigationItem?.input_fact_ids).forEach((factId) => factIds.add(factId));
  }
  return factIds.size === 0
    ? { locations: [], unlocatedCount: evidenceIds.length }
    : locationsForFactIds(Array.from(factIds), report, evidenceById);
}

function locationsForFactIds(
  factIds: string[],
  report: AnnualAnalysisResponse["report"],
  evidenceById: Map<string, VerificationEvidence>,
): SourceLocationResult {
  const locations: SourceLocation[] = [];
  let unlocatedCount = 0;
  for (const factId of Array.from(new Set(factIds))) {
    const metric = report.confirmed.metrics.find((item) => item.fact_id === factId);
    const confirmedLocations = metric === undefined ? [] : verifiedMetricLocations(metric, report, evidenceById);
    if (confirmedLocations.length > 0) {
      locations.push(...confirmedLocations);
      continue;
    }

    const pendingFact = report.pending_review.facts.find((item) => stringValue(item.fact_id) === factId);
    const pendingLocations = pendingFact === undefined ? [] : pendingFactLocations(pendingFact, report, evidenceById);
    if (pendingLocations.length > 0) {
      locations.push(...pendingLocations);
    } else {
      unlocatedCount += 1;
    }
  }
  return { locations: mergeSourceLocations(locations), unlocatedCount };
}

function locationsForPendingItem(
  item: AnnualReportRecord,
  report: AnnualAnalysisResponse["report"],
  evidenceById: Map<string, VerificationEvidence>,
): SourceLocationResult {
  const factIds = Array.from(new Set([
    stringValue(item.fact_id),
    ...stringArray(item.input_fact_ids),
    ...stringArray(item.supporting_fact_ids),
  ].filter((value): value is string => value !== null)));
  const relatedFacts = locationsForFactIds(factIds, report, evidenceById);
  const directVerified = verifiedRecordLocations(item, report, evidenceById);
  const directPending = directVerified.length > 0 ? [] : pendingExtractionLocations(item, report);
  const directLocations = directVerified.length > 0 ? directVerified : directPending;
  return {
    locations: mergeSourceLocations([...relatedFacts.locations, ...directLocations]),
    unlocatedCount: relatedFacts.unlocatedCount + (factIds.length === 0 && directLocations.length === 0 ? 1 : 0),
  };
}

function verifiedMetricLocations(
  metric: ConfirmedMetric,
  report: AnnualAnalysisResponse["report"],
  evidenceById: Map<string, VerificationEvidence>,
): SourceLocation[] {
  if (!isIndependentlyVerifiedMetric(metric)) {
    return [];
  }
  const verifiedEvidenceIds = new Set(
    metric.verifications
      .filter((item) => item.target_type === "financial_fact" && item.target_id === metric.fact_id && item.status === "verified")
      .flatMap((item) => item.evidence_ids),
  );
  const pages = metric.verification_evidences.flatMap((evidence) => {
    const indexed = evidenceById.get(evidence.evidence_id);
    if (
      !verifiedEvidenceIds.has(evidence.evidence_id) ||
      indexed === undefined ||
      indexed.document_id !== report.source_document_id ||
      indexed.source_sha256 !== report.source_sha256 ||
      indexed.pdf_page !== evidence.pdf_page ||
      indexed.value_region.page !== indexed.pdf_page
    ) {
      return [];
    }
    return [{ pageNumber: indexed.pdf_page, evidenceId: indexed.evidence_id }];
  });
  return sourceLocationsForRecord(metric, pages, "verified");
}

function pendingFactLocations(
  fact: AnnualReportRecord,
  report: AnnualAnalysisResponse["report"],
  evidenceById: Map<string, VerificationEvidence>,
): SourceLocation[] {
  const verified = verifiedRecordLocations(fact, report, evidenceById);
  if (verified.length > 0) {
    return verified;
  }
  return pendingExtractionLocations(fact, report);
}

function verifiedRecordLocations(
  item: AnnualReportRecord,
  report: AnnualAnalysisResponse["report"],
  evidenceById: Map<string, VerificationEvidence>,
): SourceLocation[] {
  const targetId = stringValue(item.fact_id) ?? stringValue(item.calculation_id) ?? stringValue(item.claim_id);
  if (targetId === null) {
    return [];
  }
  const verifications = [
    ...recordArray(item.verifications),
    record(item.independent_verification),
    record(item.verification),
  ].filter((value): value is AnnualReportRecord => value !== null);
  const matching = verifications.filter((verification) =>
    stringValue(verification.target_id) === targetId ||
    stringValue(verification.fact_id) === targetId ||
    stringValue(verification.calculation_id) === targetId ||
    stringValue(verification.claim_id) === targetId,
  );
  const statuses = matching.map((verification) => stringValue(verification.status)).filter((value): value is string => value !== null);
  const concerns = ["abstained", "insufficient_evidence", "failed", "conflict", "conflicted", "unsupported", "unknown"];
  if (!statuses.includes("verified") || statuses.some((status) => concerns.includes(status))) {
    return [];
  }
  const evidenceIds = new Set(matching.filter((verification) => verification.status === "verified").flatMap((verification) => stringArray(verification.evidence_ids)));
  if (evidenceIds.size === 0) {
    return [];
  }
  const pages = Array.from(evidenceIds).flatMap((evidenceId) => {
    const indexed = evidenceById.get(evidenceId);
    if (indexed === undefined) {
      return [];
    }
    const evidence = indexed as unknown as AnnualReportRecord;
    if (!isSameSourceEvidence(evidence, item, report)) {
      return [];
    }
    const pageNumber = physicalPdfPage(evidence, false);
    return pageNumber === null ? [] : [{ pageNumber, evidenceId }];
  });
  return sourceLocationsForRecord(item, pages, "verified");
}

function pendingExtractionLocations(
  item: AnnualReportRecord,
  report: AnnualAnalysisResponse["report"],
): SourceLocation[] {
  const evidenceIds = new Set([
    ...stringArray(item.evidence_ids),
    ...stringArray(item.supporting_evidence_ids),
    ...stringArray(item.input_evidence_ids),
    ...(stringValue(item.evidence_id) === null ? [] : [stringValue(item.evidence_id) as string]),
  ]);
  const evidenceRecords = recordArray(item.evidences);
  if (stringValue(item.evidence_id) !== null) {
    evidenceRecords.push(item);
  }
  const pages = evidenceRecords.flatMap((evidence) => {
    const evidenceId = stringValue(evidence.evidence_id);
    if (evidenceId === null || !evidenceIds.has(evidenceId) || !isSameSourceEvidence(evidence, item, report)) {
      return [];
    }
    const pageNumber = physicalPdfPage(evidence, true);
    return pageNumber === null ? [] : [{ pageNumber, evidenceId }];
  });
  return sourceLocationsForRecord(item, pages, "pending");
}

function sourceLocationsForRecord(
  item: AnnualReportRecord | ConfirmedMetric,
  pages: Array<{ pageNumber: number; evidenceId: string }>,
  source: SourceLocation["source"],
): SourceLocation[] {
  if (pages.length === 0) {
    return [];
  }
  const metricLabel = stringValue(item.label_raw) ?? "关联原文证据";
  const metricKey = stringValue(item.metric_id) ?? metricLabel;
  const periodYear = locationYear(item);
  const groupedPages = new Map<number, string[]>();
  for (const page of pages) {
    const evidenceIds = groupedPages.get(page.pageNumber) ?? [];
    if (!evidenceIds.includes(page.evidenceId)) {
      evidenceIds.push(page.evidenceId);
    }
    groupedPages.set(page.pageNumber, evidenceIds);
  }
  return [{
    key: `${source}:${metricKey}:${periodYear ?? "unknown"}`,
    metricLabel,
    metricKey,
    periodYear,
    source,
    pages: Array.from(groupedPages.entries())
      .sort(([left], [right]) => left - right)
      .map(([pageNumber, evidenceIds]) => ({ pageNumber, evidenceIds })),
  }];
}

function mergeSourceLocations(locations: SourceLocation[]): SourceLocation[] {
  const merged = new Map<string, SourceLocation>();
  for (const location of locations) {
    const existing = merged.get(location.key);
    if (existing === undefined) {
      merged.set(location.key, {
        ...location,
        pages: location.pages.map((page) => ({ ...page, evidenceIds: [...page.evidenceIds] })),
      });
      continue;
    }
    for (const page of location.pages) {
      const existingPage = existing.pages.find((item) => item.pageNumber === page.pageNumber);
      if (existingPage === undefined) {
        existing.pages.push({ ...page, evidenceIds: [...page.evidenceIds] });
      } else {
        existingPage.evidenceIds = Array.from(new Set([...existingPage.evidenceIds, ...page.evidenceIds]));
      }
    }
  }
  const metricOrder = new Map<string, number>();
  for (const location of merged.values()) {
    if (!metricOrder.has(location.metricKey)) {
      metricOrder.set(location.metricKey, metricOrder.size);
    }
  }
  return Array.from(merged.values()).sort((left, right) => {
    const orderDifference = (metricOrder.get(left.metricKey) ?? 0) - (metricOrder.get(right.metricKey) ?? 0);
    if (orderDifference !== 0) {
      return orderDifference;
    }
    if (left.periodYear !== null && right.periodYear !== null) {
      return Number(left.periodYear) - Number(right.periodYear);
    }
    return left.periodYear === null ? 1 : -1;
  }).map((location) => ({
    ...location,
    pages: location.pages.sort((left, right) => left.pageNumber - right.pageNumber),
  }));
}

function isSameSourceEvidence(
  evidence: AnnualReportRecord,
  parent: AnnualReportRecord,
  report: AnnualAnalysisResponse["report"],
): boolean {
  const evidenceDocumentId = stringValue(evidence.document_id) ?? stringValue(evidence.source_document_id);
  const parentDocumentId = stringValue(parent.document_id) ?? stringValue(parent.source_document_id);
  const evidenceSha256 = stringValue(evidence.source_sha256);
  const parentSha256 = stringValue(parent.source_sha256);
  const sourceSha256 = report.source_sha256.toLowerCase();
  return (
    (parentDocumentId === null || parentDocumentId === report.source_document_id) &&
    (parentSha256 === null || parentSha256.toLowerCase() === sourceSha256) &&
    (evidenceDocumentId === null || evidenceDocumentId === report.source_document_id) &&
    (evidenceSha256 === null || evidenceSha256.toLowerCase() === sourceSha256) &&
    (evidenceDocumentId ?? parentDocumentId) === report.source_document_id &&
    (evidenceSha256 ?? parentSha256)?.toLowerCase() === sourceSha256
  );
}

function physicalPdfPage(evidence: AnnualReportRecord, requireRelevantRegion: boolean): number | null {
  const valuePage = positivePageNumber(record(evidence.value_region)?.page);
  const pdfPage = positivePageNumber(evidence.pdf_page);
  if (requireRelevantRegion) {
    // For unverified extraction, only the value or row region can establish
    // where the financial figure appears. A title/header may legitimately sit
    // on another page, so it must not override the value location.
    const valueOrRowPage = valuePage ?? positivePageNumber(record(evidence.row_region)?.page);
    if (valueOrRowPage === null || (pdfPage !== null && pdfPage !== valueOrRowPage)) {
      return null;
    }
    return valueOrRowPage;
  }
  const pageNumber = pdfPage ?? valuePage ?? null;
  if (pageNumber === null || (valuePage !== null && valuePage !== pageNumber)) {
    return null;
  }
  return pageNumber;
}

function positivePageNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value > 0 ? value : null;
}

function locationYear(item: AnnualReportRecord | ConfirmedMetric): string | null {
  const periodEnd = stringValue(item.period_end);
  const periodYear = periodEnd?.match(/^(\d{4})-\d{2}-\d{2}$/)?.[1];
  if (periodYear !== undefined) {
    return periodYear;
  }
  const reportYear = typeof item.report_year === "number" && Number.isInteger(item.report_year)
    ? item.report_year
    : null;
  if (reportYear !== null && item.comparison_role === "current") {
    return String(reportYear);
  }
  if (reportYear !== null && item.comparison_role === "comparative") {
    const periodStart = stringValue(item.period_start);
    const startYear = periodStart?.match(/^(\d{4})-\d{2}-\d{2}$/)?.[1];
    return startYear ?? null;
  }
  return reportYear === null ? null : String(reportYear);
}

function recordArray(value: unknown): AnnualReportRecord[] {
  return Array.isArray(value)
    ? value.map(record).filter((item): item is AnnualReportRecord => item !== null)
    : [];
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
    verified: "已核验",
    insufficient_evidence: "证据不足",
    failed: "核验未通过",
    conflict: "存在冲突",
    conflicted: "存在冲突",
    unsupported: "缺少支持依据",
    pending_review: "待核查",
    pending: "待核查",
    unverified: "未核验",
    unknown: "状态未知",
  };
  return labels[status] ?? "状态待确认";
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

function modelFlowCountSummary(investigation: AnnualReportRecord): string {
  const finalReview = record(investigation.final_review);
  const investigationCalls = nonNegativeInteger(investigation.investigation_call_count);
  const reviewCalls = nonNegativeInteger(investigation.model_review_call_count) ?? nonNegativeInteger(finalReview?.call_count);
  const investigationAttempts = nonNegativeInteger(investigation.investigation_call_attempt_count);
  const reviewAttempts = nonNegativeInteger(investigation.model_review_call_attempt_count) ?? nonNegativeInteger(finalReview?.call_attempt_count);
  if (investigationCalls === null || reviewCalls === null) {
    return "调查或最终评审的分阶段调用数未完整记录，无法确认流程总数。";
  }
  const totalCalls = investigationCalls + reviewCalls;
  const attemptText = investigationAttempts === null || reviewAttempts === null
    ? ""
    : `；尝试 ${investigationAttempts + reviewAttempts} 次（调查 ${investigationAttempts} + 最终评审 ${reviewAttempts}）`;
  const archivedTotal = nonNegativeInteger(investigation.model_call_count);
  const mismatchText = archivedTotal !== null && archivedTotal !== totalCalls
    ? `；归档总数 ${archivedTotal} 与分阶段合计不一致`
    : "";
  return `总计 ${totalCalls} 次（调查 ${investigationCalls} + 最终评审 ${reviewCalls}；评审已计入总数）${attemptText}${mismatchText}`;
}

function nonNegativeInteger(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
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
  if (role === "current") return "本期";
  if (role === "comparative") return "比较期";
  return role === "unknown" ? "期间角色待确认" : "期间角色待确认";
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
  return type === "unknown" ? "报表类别待确认" : type ?? "报表类型未记录";
}

function scopeText(scope: string): string {
  return scope === "consolidated"
    ? "合并口径"
    : scope === "parent"
      ? "母公司口径"
      : scope === "unknown"
        ? "报表口径待确认"
        : scope;
}

function periodTypeText(periodType: string | undefined): string {
  if (periodType === "instant") return "期末余额";
  if (periodType === "duration") return "期间金额";
  return periodType === "unknown" ? "期间类型待确认" : periodType ?? "期间类型未记录";
}

function currencyText(currency: string): string {
  if (currency === "CNY" || currency === "RMB") return "人民币";
  if (currency === "USD") return "美元";
  if (currency === "HKD") return "港元";
  if (currency === "unknown") return "币种待确认";
  return currency;
}

function calculationStatusText(status: string): string {
  return status === "succeeded" ? "计算完成" : status === "failed" ? "计算失败" : status;
}

function verificationStatusText(status: string): string {
  const labels: Record<string, string> = {
    verified: "已核验",
    failed: "核验失败",
    abstained: "弃权",
    insufficient_evidence: "证据不足",
    conflict: "存在冲突",
    conflicted: "存在冲突",
    unsupported: "缺少支持依据",
    pending_review: "待核查",
    unknown: "状态未知",
  };
  return labels[status] ?? "待核验";
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

function signalTitle(signalId: string): string {
  const labels: Record<string, string> = {
    profit_up_cash_down: "归母净利润与经营现金流的年度比较",
    revenue_up_cash_down: "营业收入与经营现金流的年度比较",
  };
  return labels[signalId] ?? "待核查的年度财务项目";
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

function isActiveJob(job: AnnualAnalysisJob): boolean {
  return job.status === "queued" || job.status === "running";
}

function modeUsesModel(mode: AnnualAnalysisJobMode): boolean {
  return mode === "model_investigation" || mode === "integrated_analysis";
}

function missingRequestedReportLayers(
  mode: AnnualAnalysisJobMode,
  report: AnnualAnalysisResponse["report"],
  requireDiscovery = false,
): string[] {
  const missing: string[] = [];
  if ((mode === "m3_screening" || mode === "integrated_analysis") && report.m3_screening == null) {
    missing.push("M3 四规则审计");
  }
  if (modeUsesModel(mode)) {
    if (report.model_investigation == null) {
      missing.push("AI 调查记录");
    }
    if (requireDiscovery && report.model_discovery == null) {
      missing.push("AI 全文发现层");
    }
    if (report.model_review == null) {
      missing.push("AI 最终评审");
    }
  }
  return missing;
}

function isSuccessfulJob(job: AnnualAnalysisJob): boolean {
  return job.status === "completed" || job.status === "completed_with_issues";
}

function isFailedJob(job: AnnualAnalysisJob): boolean {
  return job.status === "failed" || job.status === "interrupted";
}

function canReanalyzeJob(job: AnnualAnalysisJob): boolean {
  return isFailedJob(job) || job.status === "completed_with_issues";
}

function jobStatusText(status: AnnualAnalysisJob["status"]): string {
  switch (status) {
    case "queued":
      return "排队中";
    case "running":
      return "进行中";
    case "completed":
      return "已完成";
    case "completed_with_issues":
      return "已完成，报告含缺口或弃权";
    case "failed":
      return "分析失败";
    case "interrupted":
      return "任务中断";
  }
}

function jobStageText(stage: string, status: AnnualAnalysisJob["status"]): string {
  const labels: Record<string, string> = {
    queued: "等待可用的分析位置",
    validating_source: "正在检查年报与公司和年份是否一致",
    annual_analysis_cli: "正在核对年报数据、计算和出处",
    validating_report_archive: "正在检查报告完整性和来源记录",
    completed: "报告已准备好，可查看本次分析结果",
    completed_with_issues: "报告已准备好，其中保留了数据缺口或规则弃权",
    failed: "任务未完成，可按同一来源重新提交",
    interrupted: "服务重启或退出导致任务中断，可按同一来源重新提交",
  };
  if (labels[stage] !== undefined) {
    return labels[stage];
  }
  if (status === "running") {
    return "正在处理年报";
  }
  return "等待查看处理状态";
}

function discoveryProgressStatusText(
  status: NonNullable<AnnualAnalysisJob["discovery_progress"]>["status"],
  event: NonNullable<AnnualAnalysisJob["discovery_progress"]>["event"],
  jobStatus: AnnualAnalysisJob["status"],
): string {
  if (status === "completed") {
    return jobStatus === "queued" || jobStatus === "running"
      ? "全文扫描批次已结束，等待最终评审与报告归档。"
      : "全文扫描阶段已结束；整个任务状态以最终报告与任务记录为准。";
  }
  if (status === "partial" || status === "budget_exhausted" || event === "budget_exhausted") return "扫描因缺口或时间预算提前结束；未尝试批次和失败页面会在最终报告中列出。";
  if (status === "failed") return "全文扫描失败；任务可能仍在进行其他归档步骤。";
  if (status === "not_called") return "全文扫描尚未发出模型请求。";
  if (event === "planned") return "已规划全文扫描批次，准备开始处理。";
  if (event === "batch_started") return "正在处理当前批次；页面状态不是整个任务的完成标志。";
  return "扫描批次已更新；仍需等待最终评审和报告归档。";
}

function formatInteger(value: number): string {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(value);
}

function m3StatusText(status: string, screeningStatus: string): string {
  if (status === "failed") {
    return "试行筛查流程失败";
  }
  if (screeningStatus === "failed") {
    return "四规则筛查失败";
  }
  if (status === "abstained") {
    return "试行筛查流程弃权";
  }
  if (screeningStatus === "abstained") {
    return "四规则筛查弃权";
  }
  return status === "completed" && screeningStatus === "completed" ? "试行筛查已完成" : "筛查状态待确认";
}

function m3RuleStatusText(status: string, triggered: boolean | null): string {
  if (status === "abstained") {
    return "弃权";
  }
  if (status === "failed") {
    return "计算失败";
  }
  if (["conflict", "conflicted"].includes(status)) {
    return "存在冲突";
  }
  if (status === "insufficient_evidence") {
    return "证据不足";
  }
  if (status === "calculable" && triggered === true) {
    return "触发 · 需核查";
  }
  if (status === "calculable" && triggered === false) {
    return "未触发";
  }
  if (status === "calculable") {
    return "可计算，触发状态未知";
  }
  return "未形成结论";
}

function m3RuleTitle(ruleId: string): string {
  const labels: Record<string, string> = {
    consolidated_profit_up_cash_down: "净利润与经营现金流",
    deducted_parent_profit_share: "非经常性损益对利润的影响",
    receivables_growth_gap: "应收账款与营业收入增长",
    inventory_growth_gap: "存货与营业成本增长",
  };
  return labels[ruleId] ?? "其他筛查规则";
}

function m3RuleDescription(ruleId: string): string {
  const descriptions: Record<string, string> = {
    consolidated_profit_up_cash_down: "查看净利润为正时，经营活动产生的现金流是否也为正。",
    deducted_parent_profit_share: "比较扣除非经常性损益前后的归母净利润差异。",
    receivables_growth_gap: "比较应收账款余额与营业收入的年度增速。",
    inventory_growth_gap: "比较存货余额与营业成本的年度增速。",
  };
  return descriptions[ruleId] ?? "比较年报中的相关财务数据；展开详情可查看计算口径。";
}

function formatTimestamp(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN");
}

function readUrlSelection(): UrlSelection {
  const params = new URL(window.location.href).searchParams;
  const runId = params.get("run_id");
  if (runId !== null) return { kind: "run", id: runId };
  const jobId = params.get("job_id");
  return jobId === null ? null : { kind: "job", id: jobId };
}

function writeUrlSelection(selection: UrlSelection, push: boolean) {
  const url = new URL(window.location.href);
  url.searchParams.delete("run_id");
  url.searchParams.delete("job_id");
  if (selection !== null) url.searchParams.set(selection.kind === "run" ? "run_id" : "job_id", selection.id);
  const next = `${url.pathname}${url.search}${url.hash}`;
  if (push) window.history.pushState({ fintraceAnnualSelection: true }, "", next);
  else window.history.replaceState({ fintraceAnnualSelection: true }, "", next);
}

function historyTimeText(value: string | null): string {
  return value === null ? "未记录创建时间" : formatTimestamp(value);
}

function historyStatusText(value: string | null): string {
  if (value === null || value.trim() === "") return "未记录状态";
  const labels: Record<string, string> = {
    queued: "等待执行",
    pending: "等待执行",
    running: "正在执行",
    completed: "已完成",
    completed_with_issues: "完成但有问题",
    failed: "执行失败",
    interrupted: "已中断",
    archived: "已归档",
    unavailable: "暂不可读取",
  };
  return labels[value] ?? value;
}

function historyModeText(mode: AnnualAnalysisHistoryItem["mode"]): string {
  const labels: Record<AnnualAnalysisHistoryItem["mode"], string> = {
    deterministic: "标准分析",
    m3_screening: "标准分析 + 四规则试行",
    model_investigation: "AI 调查与评审（范围以归档为准）",
    integrated_analysis: "四规则试行 + AI 调查与评审（范围以归档为准）",
    unknown: "未记录模式",
  };
  return labels[mode];
}

function narrativeSectionLabel(value: string): string {
  const labels: Record<string, string> = {
    mda: "管理层讨论与分析",
    financial_notes: "财务报表附注",
    financial_statements: "财务报表",
    business_overview: "经营情况讨论",
    risk_factors: "风险因素",
  };
  return labels[value] ?? "年报原文片段";
}

function narrativeContextNote(item: NarrativeEvidence): string {
  const context = item.reading_context;
  if (context === undefined) return "当前后端只提供引用文字；可以打开原文页图查看周边内容。";
  if (context.limited && context.expanded) return "已补入同页部分前后文；其余内容可打开页图查看，原始引用见下方详情。";
  if (context.limited) return "没有补入前后文；可以打开原文页图查看周边内容。";
  if (context.expanded) return "已补入同页前后文，方便连续阅读。最初引用的文字可在下方详情查看。";
  return "当前只显示报告引用的文字；可以打开原文页图查看周边内容。";
}

function narrativeContextStatus(item: NarrativeEvidence): string {
  const context = item.reading_context;
  if (context === undefined) return "后端未提供扩展上下文，页面回退显示原始引用";
  if (context.expanded && context.limited) return `已扩展但受限：${context.limit_reason ?? "相邻内容无法安全确认"}`;
  if (context.expanded) return "同页相邻正文已扩展";
  if (context.limited) return `未扩展：${context.limit_reason ?? "相邻内容无法安全确认"}`;
  return `仅原始引用：${context.boundary_reason ?? "未发现安全相邻正文"}`;
}

function narrativeWhyShown(item: NarrativeEvidence): string {
  const selectionReason = item.selection_reason;
  const text = item.raw_text;
  const termGroups: Array<[string, string[]]> = [
    ["利润或营业收入词项", ["营业收入", "销售收入", "收入确认", "收入合同", "合同收入", "确认为收入", "合同负债", "合同負債", "净利润", "净利率", "归属于上市公司股东的净利润"]],
    ["经营现金流词项", ["经营活动产生的现金流量净额", "经营活动现金流", "现金流量表", "现金流量", "经营现金流"]],
    ["披露说明词项", ["原因说明", "主要是", "主要由于", "由于", "因收到", "因本期", "变动原因", "变动说明", "所致", "增加", "减少", "上升", "下降", "后确认", "所确认"]],
  ];
  const matchedTerms = termGroups
    .filter(([category]) => selectionReason.includes(category))
    .flatMap(([, terms]) => terms.filter((term) => text.includes(term)))
    .filter((term, index, terms) => terms.indexOf(term) === index)
    .slice(0, 3);
  if (matchedTerms.length === 0) {
    return "这段被报告中的线索引用，系统将它列为相关披露供你核对；指标变化原因仍待查证。";
  }
  return `原文提到${matchedTerms.map((term) => `“${term}”`).join("、")}，系统据此将它列为相关披露供你核对；指标变化原因仍待查证。`;
}

function readStoredJobId(): string | null {
  try {
    const stored = window.localStorage.getItem(ACTIVE_JOB_STORAGE_KEY);
    if (stored === null) {
      return null;
    }
    if (!JOB_ID_PATTERN.test(stored)) {
      window.localStorage.removeItem(ACTIVE_JOB_STORAGE_KEY);
      return null;
    }
    return stored;
  } catch {
    return null;
  }
}

function writeStoredJobId(jobId: string) {
  try {
    window.localStorage.setItem(ACTIVE_JOB_STORAGE_KEY, jobId);
  } catch {
    // Task status remains available until this page is closed when storage is disabled.
  }
}

function removeStoredJobId() {
  try {
    window.localStorage.removeItem(ACTIVE_JOB_STORAGE_KEY);
  } catch {
    // Storage is optional; clearing the in-memory message still lets the user continue.
  }
}
