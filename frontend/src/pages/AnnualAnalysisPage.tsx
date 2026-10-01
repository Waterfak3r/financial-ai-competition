import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent, MouseEvent, ReactNode } from "react";
import { loadAnnualAnalysisCapabilities } from "../api/annualAnalysisCapabilities";
import { AnnualReportApiError, loadAnnualAnalysis, loadVerificationEvidencePreview } from "../api/annualReports";
import { createAnnualAnalysisJob, loadAnnualAnalysisJob } from "../api/annualAnalysisJobs";
import { textPdfUploadIssues, uploadTextPdf } from "../api/prechecks";
import type {
  AnnualAnalysisResponse,
  AnnualAnalysisModelReview,
  AnnualReportRecord,
  CandidateSignal,
  ConfirmedCalculation,
  ConfirmedMetric,
  M3AnnualScreening,
  VerificationEvidence,
  VerifiedClaim,
} from "../types/annualReport";
import type { AnnualAnalysisJob, AnnualAnalysisJobMode, AnnualAnalysisJobRequest } from "../types/annualAnalysisJob";
import type { TextPdfUploadReceipt } from "../types/precheck";

const SAMPLE_RUN_ID = "annual-analysis-ffcd6078-a1df-416b-88b8-6774ebe66d42";
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
  { metricId: "non_recurring_total", label: "披露的非经常性损益合计", tone: "violet" },
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
  evidenceId: string;
  pageNumber: number;
  objectUrl: string;
}

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
  const [companyId, setCompanyId] = useState("");
  const [reportYear, setReportYear] = useState("");
  const [pdfFile, setPdfFile] = useState<File | null>(null);
  const [mode, setMode] = useState<AnnualAnalysisJobMode>("deterministic");
  const [modelCapability, setModelCapability] = useState<"ready" | "not_configured" | "dependency_unavailable" | null>(null);
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
  const [previewBusyId, setPreviewBusyId] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const requestLock = useRef(false);
  const jobSubmitLock = useRef(false);
  const jobRefreshLock = useRef(false);
  const jobReadSequence = useRef(0);
  const reportRequest = useRef(0);
  const taskGeneration = useRef(0);
  const previewRequest = useRef(0);

  const refreshModelCapability = useCallback(async () => {
    setModelCapabilityLoading(true);
    setModelCapabilityError(null);
    try {
      const capabilities = await loadAnnualAnalysisCapabilities();
      setModelCapability(capabilities.model_investigation.status);
    } catch {
      setModelCapability(null);
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
      if (completedJob.request.mode === "m3_screening" && loaded.report.m3_screening == null) {
        setJobError("任务已完成，但报告没有包含所选的四项试行筛查结果；请按原来源重新提交。候选线索不是已核验事实。");
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
    if (preview === null) {
      return;
    }
    const heading = document.getElementById("annual-evidence-preview-heading");
    if (heading === null) {
      return;
    }
    heading.focus({ preventScroll: true });
    heading.scrollIntoView({
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
      block: "start",
    });
  }, [preview]);

  useEffect(() => {
    if (previewError === null) {
      return;
    }
    const alert = document.getElementById("annual-evidence-preview-error");
    if (alert === null) {
      return;
    }
    alert.focus({ preventScroll: true });
    alert.scrollIntoView({
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
      block: "center",
    });
  }, [previewError]);
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
    const savedJobId = readStoredJobId();
    if (savedJobId === null) {
      return;
    }
    const generation = taskGeneration.current + 1;
    taskGeneration.current = generation;
    let active = true;
    setRestoreBusy(true);
    loadAnnualAnalysisJob(savedJobId)
      .then((restored) => {
        if (!active || taskGeneration.current !== generation) {
          return;
        }
        setJob(restored);
        setRestoreError(null);
        writeStoredJobId(restored.job_id);
        if (restored.run_id !== null) {
          setRunId(restored.run_id);
        }
        if (isSuccessfulJob(restored)) {
          void loadReportForJob(restored, generation);
        }
      })
      .catch((error: unknown) => {
        if (!active || taskGeneration.current !== generation) {
          return;
        }
        setRestoreError(errorMessage(error));
      })
      .finally(() => {
        if (active && taskGeneration.current === generation) {
          setRestoreBusy(false);
        }
      });
    return () => {
      active = false;
      if (taskGeneration.current === generation) {
        taskGeneration.current += 1;
      }
    };
  }, [loadReportForJob]);

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
  ) {
    if (jobSubmitLock.current || jobRefreshLock.current) {
      return;
    }
    if (mode === "model_investigation" && modelCapability !== "ready") {
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
    setPreview(null);
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
      writeStoredJobId(created.job_id);
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

  async function onRetryJob() {
    if (job === null || !isFailedJob(job)) {
      return;
    }
    await submitJob(() => job.request, "retrying");
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

  async function loadReportByRunId(value: string) {
    if (requestLock.current) {
      return;
    }
    const checkedRunId = value.trim();
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
    const requestId = reportRequest.current + 1;
    reportRequest.current = requestId;
    requestLock.current = true;
    setLoading(true);
    try {
      const loaded = await loadAnnualAnalysis(checkedRunId);
      if (reportRequest.current === requestId) {
        setResponse(loaded);
      }
    } catch (error) {
      if (reportRequest.current === requestId) {
        setLoadError(errorMessage(error));
        setResponse(null);
      }
    } finally {
      if (reportRequest.current === requestId) {
        requestLock.current = false;
        setLoading(false);
      }
    }
  }

  async function onLoad(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await loadReportByRunId(runId);
  }

  async function onViewArchivedSample() {
    await loadReportByRunId(SAMPLE_RUN_ID);
  }

  function focusUploadForm() {
    const upload = document.getElementById("annual-job-pdf-file");
    upload?.scrollIntoView({
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
      block: "center",
    });
    upload?.focus({ preventScroll: true });
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
      {response === null ? (
        <section className="annual-analysis-intro" aria-labelledby="annual-analysis-title">
          <div className="annual-analysis-copy">
            <p className="eyebrow">FINTRACE / 年度财报</p>
            <h1 id="annual-analysis-title">让年报分析更清晰</h1>
            <p className="lede">
              从财务数据、计算到原文出处，逐项呈现核验结果与需要继续查看的线索。
            </p>
            <div className="annual-intro-actions">
              <button type="button" className="primary" onClick={focusUploadForm}>
                选择年报开始分析
              </button>
              <button type="button" className="secondary" disabled={loading} onClick={onViewArchivedSample}>
                {loading ? "正在读取归档…" : "查看海天 2024 已归档结果"}
              </button>
            </div>
            <ul className="annual-intro-points" aria-label="分析内容">
              <li>财务数据与计算分别核对</li>
              <li>保留报告页码与证据来源</li>
              <li>候选线索始终保持待核查状态</li>
            </ul>
          </div>
          <AnnualAnalysisIllustration />
        </section>
      ) : null}

      <details className="annual-start-another" open={response === null}>
        <summary>{response === null ? "上传年报并启动分析" : "分析另一份年报"}</summary>
        <section id="annual-job-create" className="panel annual-job-create" aria-labelledby="annual-job-create-title">
          <div className="annual-section-heading">
            <div>
              <p className="eyebrow">开始分析</p>
              <h2 id="annual-job-create-title">选择年报并开始分析</h2>
            </div>
            <span className="sub">标准分析默认开启</span>
          </div>
          <p className="annual-job-intro">
            填写公司代码与年份后选择可读取文字的 PDF。原始文件保存在本机，不会覆盖已有资料。
          </p>
          <form className="annual-job-form" onSubmit={onUploadAndStart} noValidate>
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

          <fieldset className="annual-job-mode">
            <legend>分析方式</legend>
            <div className="annual-job-mode-options">
              <label className={`annual-job-mode-option${mode === "deterministic" ? " selected" : ""}`}>
                <input
                  type="radio"
                  name="annual-analysis-mode"
                  value="deterministic"
                  checked={mode === "deterministic"}
                  onChange={() => setMode("deterministic")}
                />
                <span><strong>标准分析</strong><small>核对年报中的数据、计算和原文引用。</small></span>
              </label>
              <label className={`annual-job-mode-option annual-job-mode-experimental${mode === "m3_screening" ? " selected" : ""}`}>
                <input
                  type="radio"
                  name="annual-analysis-mode"
                  value="m3_screening"
                  checked={mode === "m3_screening"}
                  onChange={() => setMode("m3_screening")}
                />
                <span><strong>标准分析 + 四项试行筛查</strong><small>另用尚未校准的规则提示可继续核对的数据组合；不判断是否存在异常。</small></span>
              </label>
              <label className={`annual-job-mode-option annual-job-mode-ai${mode === "model_investigation" ? " selected" : ""}${modelCapability !== "ready" ? " is-unavailable" : ""}`}>
                <input
                  type="radio"
                  name="annual-analysis-mode"
                  value="model_investigation"
                  checked={mode === "model_investigation"}
                  disabled={modelCapability !== "ready"}
                  onChange={() => setMode("model_investigation")}
                />
                <span><strong>AI 分析与评审</strong><small>使用云端模型分析本次年报的相关片段，给出判断、依据和建议核查事项。</small></span>
              </label>
            </div>
            <p className={`annual-job-capability${modelCapability === "ready" ? " is-ready" : ""}`} role="status">
              {modelCapabilityLoading
                ? "正在检查 AI 服务状态…"
                : modelCapability === "ready"
                  ? "模型连接信息已配置。选择该方式后，相关年报片段会发送给已配置的云端模型。"
                  : modelCapability === "not_configured"
                    ? "当前不可用：后端尚未配置云端模型。标准分析和试行筛查仍可使用。"
                    : modelCapability === "dependency_unavailable"
                      ? "当前不可用：后端缺少 AI 分析所需依赖。标准分析和试行筛查仍可使用。"
                      : modelCapabilityError ?? "正在确认 AI 服务状态；确认前不能启动 AI 分析。"}
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
              <button type="submit" className="primary" disabled={jobAction !== null}>
                {jobAction === "uploading" ? "正在上传并启动…" : jobAction === "starting" ? "正在启动…" : "上传并开始分析"}
              </button>
              <button type="button" className="secondary" disabled={jobAction !== null} onClick={onRunHaitianSample}>
                {jobAction === "starting" ? "正在启动…" : "启动海天 2024 样例分析"}
              </button>
              <span className="help">此按钮会新建分析任务，并读取本机已保存的 PDF。</span>
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
          {runIdError !== null ? (
            <p id="annual-analysis-run-id-error" className="field-error">
              {runIdError}
            </p>
          ) : null}
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

      {response === null && !loading && loadError === null ? (
        <section className="panel annual-empty">
          <h2>报告会显示在这里</h2>
          <p className="empty">
            任务完成后自动加载对应年报结果与核验证据。也可以展开高级选项读取此前保存的分析。
          </p>
        </section>
      ) : null}

      {response !== null ? (
        <>
          {job !== null && job.run_id === response.report.run_id && job.request.mode === "m3_screening" && response.report.m3_screening == null ? (
            <p className="alert" role="alert">本次任务选择了四项试行筛查，但报告没有对应结果。当前报告仍可查看；请核对任务归档后再重试。</p>
          ) : null}
          <AnnualReportView
            response={response}
            preview={preview}
            previewBusyId={previewBusyId}
            previewError={previewError}
            onPreview={onPreview}
          />
        </>
      ) : null}
    </>
  );
}

function AnnualAnalysisIllustration() {
  return (
    <div className="annual-analysis-illustration" aria-hidden="true">
      <svg viewBox="0 0 520 370" role="presentation" focusable="false">
        <defs>
          <linearGradient id="report-paper" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stopColor="#ffffff" />
            <stop offset="1" stopColor="#e9f3ff" />
          </linearGradient>
          <linearGradient id="report-blue" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="#58a5ff" />
            <stop offset="1" stopColor="#1677ff" />
          </linearGradient>
          <filter id="report-shadow" x="-30%" y="-30%" width="160%" height="170%">
            <feDropShadow dx="0" dy="16" stdDeviation="14" floodColor="#4e8fe0" floodOpacity="0.16" />
          </filter>
        </defs>
        <path d="M20 302c74-58 131-77 204-57 76 21 111-18 169-60 43-31 80-39 113-23v208H20z" fill="#d9eaff" opacity=".52" />
        <path d="M18 320c88-47 146-43 218-18 64 22 132 20 194-12 29-15 53-24 78-23" fill="none" stroke="#b7d7ff" strokeWidth="2" opacity=".7" />
        <g filter="url(#report-shadow)" transform="rotate(-5 232 166)">
          <rect x="94" y="33" width="260" height="290" rx="19" fill="url(#report-paper)" stroke="#c8def9" />
          <rect x="122" y="62" width="68" height="9" rx="4.5" fill="#9cc8ff" />
          <rect x="122" y="83" width="128" height="6" rx="3" fill="#d9e8fb" />
          <rect x="122" y="112" width="204" height="91" rx="12" fill="#f2f7ff" stroke="#dfeafa" />
          <path d="M143 183v-20h17v20m12 0v-38h17v38m12 0v-53h17v53m12 0v-31h17v31" fill="url(#report-blue)" opacity=".86" />
          <path d="M139 183h164" stroke="#c5d8ef" strokeWidth="2" />
          <rect x="122" y="221" width="124" height="7" rx="3.5" fill="#d4e4f7" />
          <rect x="122" y="239" width="190" height="7" rx="3.5" fill="#e0ebf8" />
          <rect x="122" y="257" width="156" height="7" rx="3.5" fill="#e0ebf8" />
          <rect x="122" y="282" width="80" height="20" rx="7" fill="#e7f2ff" />
          <circle cx="298" cy="291" r="12" fill="#dff6ec" />
          <path d="m292 291 4 4 8-9" fill="none" stroke="#24a971" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
        </g>
        <g filter="url(#report-shadow)" transform="rotate(8 371 202)">
          <rect x="319" y="85" width="142" height="190" rx="16" fill="#ffffff" stroke="#d0e3fb" />
          <rect x="339" y="105" width="70" height="7" rx="3.5" fill="#b8d7ff" />
          <rect x="339" y="121" width="101" height="6" rx="3" fill="#e0ebf8" />
          <rect x="339" y="151" width="102" height="84" rx="10" fill="#f2f7ff" />
          <path d="M355 220v-24h13v24m9 0v-39h13v39m9 0v-29h13v29" fill="#70afff" />
          <path d="M351 220h79" stroke="#c5d8ef" strokeWidth="2" />
          <rect x="339" y="246" width="63" height="8" rx="4" fill="#e2edfa" />
        </g>
        <g transform="rotate(-14 362 257)">
          <circle cx="363" cy="246" r="48" fill="#ffffff" fillOpacity=".5" stroke="#1677ff" strokeWidth="12" />
          <circle cx="363" cy="246" r="37" fill="#cce4ff" fillOpacity=".38" />
          <path d="m397 281 50 47" stroke="#1677ff" strokeWidth="17" strokeLinecap="round" />
          <path d="m400 284 39 37" stroke="#5ba2ff" strokeWidth="5" strokeLinecap="round" />
        </g>
        <circle cx="94" cy="92" r="5" fill="#7bb6ff" />
        <circle cx="443" cy="55" r="4" fill="#b8d7ff" />
        <circle cx="465" cy="302" r="6" fill="#9dcbff" />
      </svg>
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
          : job.request.mode === "model_investigation"
            ? "本次由本机完成数据核对，并使用云端模型分析年报相关片段、给出评审意见。"
            : "本次使用标准分析，在本机核对年报数据、计算和引用。"}
        {` 公司代码 ${job.request.company_id}，报告年份 ${job.request.report_year}。`}
        {terminal ? " 处理状态已稳定。" : " 页面会自动更新处理状态。"}
      </p>
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
        {isFailedJob(job) ? (
          <button type="button" className="primary" disabled={action !== null} onClick={onRetry}>
            {action === "retrying" ? "正在重新提交…" : "重新提交同一来源"}
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
  const verifiedClaimCount = report.verified_claims.filter(
    (claim) => claim.verification_status === "verified" && claim.independent_verification?.status === "verified",
  ).length;
  const coreMetrics = CORE_METRICS.map((spec) => {
    const metric = report.confirmed.metrics.find(
      (item) =>
        item.metric_id === spec.metricId &&
        item.comparison_role === "current" &&
        item.period_end === `${report.report_year}-12-31`,
    );
    const comparativeMetric = report.confirmed.metrics.find(
      (item) =>
        item.metric_id === spec.metricId &&
        item.comparison_role === "comparative" &&
        item.period_end === `${report.report_year - 1}-12-31`,
    );
    return {
      ...spec,
      metric,
      verified: metric !== undefined && isIndependentlyVerifiedMetric(metric),
      comparativeMetric,
      comparativeVerified: comparativeMetric !== undefined && isIndependentlyVerifiedMetric(comparativeMetric),
    };
  });
  const evidenceById = new Map(evidenceIndex.map((evidence) => [evidence.evidence_id, evidence]));
  const reportModelInvestigation = report.model_investigation;
  const reportModelReview = report.model_review ?? null;
  const signalCount = report.pending_review.candidate_signals.length;
  const candidateCount = report.pending_review.candidate_signals.filter((item) => item.status === "candidate").length;
  const abstainedSignalCount = report.pending_review.candidate_signals.filter((item) => item.status === "abstained").length;
  const otherSignalCount = signalCount - candidateCount - abstainedSignalCount;
  const sourceMatches = report.source_sha256 === manifest.source_pdf_sha256;

  return (
    <div className="annual-report" aria-label="年度分析报告">
      <header id="annual-report-heading" className="annual-report-heading" tabIndex={-1}>
        <div>
          <p className="eyebrow">{report.company_id} · {report.report_year} 年报</p>
          <h2>{report.company_id} · {report.report_year} 年报分析</h2>
          <p className="annual-report-title-caption">{report.title?.trim() || "年度分析归档结果"}</p>
          <p className="annual-run-id">归档编号 <code>{report.run_id}</code></p>
        </div>
        <span className={manifest.status === "completed_with_issues" ? "review-chip" : "annual-status-chip"}>
          {manifestStatusText(manifest.status)}
        </span>
      </header>

      <nav className="annual-report-nav" aria-label="报告内容">
        <a href="#annual-model-review" onClick={expandReportDetails}>
          {reportModelReview?.status === "completed" && reportModelReview.assessment !== null
            ? "AI 评审意见"
            : reportModelReview !== null
              ? "评审状态与摘要"
              : "本次筛查摘要"}
        </a>
        <a href="#annual-financial-overview" onClick={expandReportDetails}>财务概览</a>
        <a href="#annual-candidate-overview" onClick={expandReportDetails}>建议核查事项</a>
        <a href="#annual-confirmed-metrics" onClick={expandReportDetails}>核实数据</a>
        <a href="#annual-analysis-details" onClick={expandReportDetails}>计算过程</a>
        <a href="#annual-verified-claims" onClick={expandReportDetails}>核验主张</a>
        {report.m3_screening == null ? null : <a href="#annual-m3-screening" onClick={expandReportDetails}>试行筛查</a>}
        <a href="#annual-scope" onClick={expandReportDetails}>分析范围</a>
      </nav>

      <ModelReviewSection
        review={reportModelReview}
        report={report}
        evidenceById={evidenceById}
        previewBusyId={previewBusyId}
        onPreview={onPreview}
      />

      <section id="annual-financial-overview" className="annual-financial-overview" aria-labelledby="annual-indicators-title" tabIndex={-1}>
        <div className="annual-overview-heading">
          <div>
            <p className="eyebrow">本期核心数据</p>
            <h3 id="annual-indicators-title">财务指标概览</h3>
          </div>
          <span>{report.report_year} 年度 · 归档报告数据</span>
        </div>
        <div className="annual-indicator-grid">
          {coreMetrics.map((item) => (
            <article key={item.metricId} className={`annual-indicator-card annual-indicator-${item.tone}`}>
              <div className="annual-indicator-topline">
                <span className="annual-indicator-symbol" aria-hidden="true"><IndicatorGlyph /></span>
                <span className={item.verified ? "verification-chip" : "review-chip"}>
                  {item.verified ? "独立核验通过" : "待核验"}
                </span>
              </div>
              <h4>{item.label}</h4>
              <strong className="annual-indicator-value">
                {item.verified && item.metric !== undefined
                  ? formatCoreMetricDisplay(item.metric, evidenceById).value
                  : "待核验"}
              </strong>
              <p>
                {item.verified && item.metric !== undefined
                  ? `${item.metric.currency || "币种未记录"} · ${formatCoreMetricDisplay(item.metric, evidenceById).unit}`
                  : "独立核验通过前不展示金额"}
              </p>
            </article>
          ))}
        </div>
        <p className="annual-indicator-footnote">
          本期指标来自已核验年报数据，金额仅作展示舍入；完整数值与核验证据见下方“核实数据”。
        </p>
        <section className="annual-year-comparison" aria-labelledby="annual-year-comparison-title">
          <div className="annual-year-comparison-heading">
            <div>
              <p className="eyebrow">年报披露值</p>
              <h4 id="annual-year-comparison-title">两年数据对照</h4>
            </div>
            <span>按报告列示期间展示，不推算增幅</span>
          </div>
          <div className="annual-year-comparison-table-wrap">
            <table className="annual-year-comparison-table">
              <thead>
                <tr>
                  <th scope="col">指标</th>
                  <th scope="col">{report.report_year - 1} 年</th>
                  <th scope="col">{report.report_year} 年</th>
                </tr>
              </thead>
              <tbody>
                {coreMetrics.map((item) => (
                  <tr key={item.metricId}>
                    <th scope="row">{item.label}</th>
                    <td>
                      {item.comparativeVerified && item.comparativeMetric !== undefined ? (
                        <>
                          <strong>{formatCoreMetricDisplay(item.comparativeMetric, evidenceById).value}</strong>
                          <small>{item.comparativeMetric.currency || "币种未记录"} · {formatCoreMetricDisplay(item.comparativeMetric, evidenceById).unit}</small>
                        </>
                      ) : <span className="annual-comparison-pending">待核验</span>}
                    </td>
                    <td>
                      {item.verified && item.metric !== undefined ? (
                        <>
                          <strong>{formatCoreMetricDisplay(item.metric, evidenceById).value}</strong>
                          <small>{item.metric.currency || "币种未记录"} · {formatCoreMetricDisplay(item.metric, evidenceById).unit}</small>
                        </>
                      ) : <span className="annual-comparison-pending">待核验</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
        <div className="annual-summary-grid" aria-label="分析与核验摘要">
          <SummaryTile label="独立核验通过的数据" value={verifiedMetricCount} note={`共记录 ${report.confirmed.metrics.length} 项`} />
          <SummaryTile
            label="建议核查事项"
            value={candidateCount}
            note={abstainedSignalCount > 0 ? `${abstainedSignalCount} 项比较暂不能确认` : "逐项查看数据变化与待查材料"}
          />
          <SummaryTile label="独立重算通过" value={verifiedCalculationCount} note={`共记录 ${report.confirmed.analyses.length} 项计算`} />
          <SummaryTile label="独立核验通过的主张" value={verifiedClaimCount} note={`共记录 ${report.verified_claims.length} 条`} />
        </div>
        <p className="annual-report-summary">
          <strong>分析范围：</strong>本次选取 {report.confirmed.metrics.length} 项财务数据，并核对相关年度变化和原文出处，不代表覆盖整份年报。{" "}
          <strong>建议继续核查：</strong>{candidateCount} 项数据变化
          {abstainedSignalCount > 0 ? `，${abstainedSignalCount} 项年度比较因证据不足而暂不能确认` : ""}
          {otherSignalCount > 0 ? `，另有 ${otherSignalCount} 项状态待核对` : ""}。
        </p>
      </section>

      <section id="annual-candidate-overview" className="panel annual-section annual-candidate-overview" tabIndex={-1}>
        <div className="annual-section-heading">
          <div>
            <p className="eyebrow">核查清单</p>
            <h3>建议核查的具体事项</h3>
          </div>
        </div>
        {candidateCount > 0 ? (
          <p className="annual-candidate-context">
            以下事项来自本地规则筛查。卡片只展示满足独立核验条件的年度差额；其余变化方向暂不作判断，请按建议项目继续查证。
          </p>
        ) : abstainedSignalCount > 0 ? (
          <p className="annual-candidate-context">
            下列年度比较因关键数据尚未确认而弃权；页面会说明缺少的前提，不能据此判断变化方向。
          </p>
        ) : null}
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

      {report.m3_screening == null ? null : <M3ScreeningBlock screening={report.m3_screening} />}

      <section id="annual-scope" className="panel annual-section" tabIndex={-1}>
        <div className="annual-section-heading">
          <div>
            <p className="eyebrow">分析范围</p>
            <h3>分析范围与报告缺口</h3>
          </div>
        </div>
        <p className="annual-scope-summary">
          本次只核对年报中选取的 {report.confirmed.metrics.length} 项财务数据及相关年度变化、原文出处，不代表核查整份年报。
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
        <p className="annual-source-note">报告和证据页图来自本次分析的本机归档；页图只对应已返回的独立核验证据。</p>
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

      {preview !== null ? (
        <section id="annual-evidence-preview" className="panel annual-evidence-preview" aria-live="polite">
          <div className="annual-section-heading">
            <div>
              <p className="eyebrow">核验证据</p>
              <h3 id="annual-evidence-preview-heading" tabIndex={-1}>独立核验证据 · 第 {preview.pageNumber} 页</h3>
            </div>
          </div>
          <p className="help">
            此页图由服务端根据独立核验证据生成，黄色框标出该证据的数值区域；它是核验材料，不表示异常或舞弊结论。
          </p>
          <img
            src={preview.objectUrl}
            alt={"独立核验证据 PDF 页图，第 " + preview.pageNumber + " 页"}
            className="annual-evidence-image"
          />
          <details className="annual-technical-record">
            <summary>技术记录：核验证据编号</summary>
            <code>{preview.evidenceId}</code>
          </details>
        </section>
      ) : null}
      {previewError !== null ? (
        <p id="annual-evidence-preview-error" className="alert" role="alert" tabIndex={-1}>
          {previewError}
        </p>
      ) : null}

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
  previewBusyId,
  onPreview,
}: {
  review: AnnualAnalysisModelReview | null;
  report: AnnualAnalysisResponse["report"];
  evidenceById: Map<string, VerificationEvidence>;
  previewBusyId: string | null;
  onPreview: (evidenceId: string) => void;
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
                      previewBusyId={previewBusyId}
                      onPreview={onPreview}
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
                      previewBusyId={previewBusyId}
                      onPreview={onPreview}
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
            这是模型根据本次年报材料给出的分析判断，尚未独立核实；请结合补充证据复核。
          </p>
          <details className="annual-technical-record annual-model-review-technical">
            <summary>查看模型调用与归档记录</summary>
            <dl className="meta">
              <dt>模型是否实际调用</dt><dd>{review.model_called ? "是" : "否"}</dd>
              <dt>已归档请求数 / 尝试次数</dt><dd>{review.call_count} / {review.call_attempt_count}</dd>
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
            <p className="annual-model-review-failure" role="status">{modelReviewReasonText(review.reason, review.status)}</p>
          ) : historicModelCalled ? (
            <p className="annual-model-review-history-note" role="status">
              这份历史归档包含逐条模型调查，未保存最终评审意见。
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
                <dt>已归档请求数 / 尝试次数</dt><dd>{review.call_count} / {review.call_attempt_count}</dd>
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
  previewBusyId,
  onPreview,
}: {
  evidenceIds: string[];
  report: AnnualAnalysisResponse["report"];
  evidenceById: Map<string, VerificationEvidence>;
  previewBusyId: string | null;
  onPreview: (evidenceId: string) => void;
}) {
  if (evidenceIds.length === 0) {
    return <p className="annual-review-reference-empty">本条意见没有关联可打开的年报出处。</p>;
  }
  const result = locationsForReviewEvidenceIds(evidenceIds, report, evidenceById);
  return (
    <details className="annual-review-references">
      <summary>查看本条关联出处</summary>
      <SourceLocationList
        heading="对应年报页"
        result={result}
        previewBusyId={previewBusyId}
        onPreview={onPreview}
      />
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
    model_configuration_unavailable: "后端尚未配置云端模型，本次未能生成 AI 评审意见。下方保留本地确定性筛查摘要。",
    model_dependency_unavailable: "后端缺少 AI 分析所需依赖，本次未能生成 AI 评审意见。下方保留本地确定性筛查摘要。",
    model_review_call_failed: "云端模型请求未完成，本次未能生成 AI 评审意见。下方保留本地确定性筛查摘要。",
    model_review_numeric_claim_rejected: "模型意见包含未获准的数值表述，本次未生成可展示的 AI 评审意见。下方保留本地确定性筛查摘要。",
    model_review_response_invalid: "模型返回内容无法按评审格式读取，本次未生成可展示的 AI 评审意见。下方保留本地确定性筛查摘要。",
    model_review_no_priority_not_supported: "当前核查范围不支持“无需优先核查”的结论，因此未生成该评审意见。下方保留本地确定性筛查摘要。",
    model_review_priority_without_reason: "模型没有提供足够的评审依据，本次未生成可展示的 AI 评审意见。下方保留本地确定性筛查摘要。",
    model_review_no_priority_without_reason: "模型没有提供足够的评审依据，本次未生成可展示的 AI 评审意见。下方保留本地确定性筛查摘要。",
  };
  if (reason !== null && messages[reason] !== undefined) {
    return messages[reason];
  }
  return status === "not_called"
    ? "本次没有调用云端模型，因此未生成 AI 评审意见。下方保留本地确定性筛查摘要。"
    : "本次 AI 评审未能完成。下方保留本地确定性筛查摘要。";
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

function formatCoreMetricDisplay(
  metric: ConfirmedMetric,
  evidenceById: Map<string, VerificationEvidence>,
): { value: string; unit: string } {
  const verifiedEvidences = verifiedMetricEvidences(metric, evidenceById);
  const units = [...new Set(
    verifiedEvidences
      .map((evidence) => evidence.unit)
      .filter((unit): unit is string => typeof unit === "string" && unit.trim().length > 0),
  )];
  if (units.length > 1) {
    return { value: formatExactFinancialValue(metric.normalized_value), unit: "单位待确认" };
  }
  const verifiedUnit = units[0];
  if (verifiedUnit === "元") {
    const scaledValue = formatInHundredMillions(metric.normalized_value);
    return scaledValue === null
      ? { value: formatExactFinancialValue(metric.normalized_value), unit: verifiedUnit }
      : { value: scaledValue, unit: "亿元" };
  }
  if (verifiedUnit !== undefined) {
    const matchingEvidence = verifiedEvidences.find(
      (evidence) =>
        evidence.unit === verifiedUnit &&
        typeof evidence.value_raw === "string" &&
        evidence.value_raw.trim().length > 0,
    );
    if (matchingEvidence?.value_raw !== null && matchingEvidence?.value_raw !== undefined) {
      return { value: matchingEvidence.value_raw, unit: verifiedUnit };
    }
    return { value: formatExactFinancialValue(metric.normalized_value), unit: "单位待确认" };
  }
  return {
    value: formatExactFinancialValue(metric.normalized_value),
    unit: "单位未记录",
  };
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

function SummaryTile({ label, value, note }: { label: string; value: number; note: string }) {
  return (
    <article className="annual-summary-tile">
      <p>{label}</p>
      <strong>{value}</strong>
      <span>{note}</span>
    </article>
  );
}

function IndicatorGlyph() {
  return (
    <svg viewBox="0 0 24 24" focusable="false">
      <path d="M4 19.5h16" />
      <path d="M6.5 17V11h3v6m2.5 0V7h3v10m2.5 0V4h3v13" />
    </svg>
  );
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
                {rule.issues.length === 0 ? null : <ul className="annual-m3-issue">{rule.issues.map((issue, index) => <li key={`${index}-${issue}`}>{issue}</li>)}</ul>}
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
  const why = candidateWhyText(candidate.signal_id, changes);
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
          <h5>为什么要看</h5>
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
  for (const calculationId of candidate.calculation_ids) {
    const calculation = report.confirmed.analyses.find((item) => item.calculation_id === calculationId);
    if (
      calculation === undefined ||
      calculation.formula_id !== "annual_difference" ||
      calculation.status !== "succeeded" ||
      !isIndependentlyVerifiedCalculation(calculation) ||
      typeof calculation.output_value !== "string" ||
      (calculation.unit !== null && calculation.unit !== "元") ||
      calculation.input_fact_ids.length !== 2 ||
      new Set(calculation.input_fact_ids).size !== 2 ||
      calculation.input_fact_ids.some((factId) => !candidateInputIds.has(factId)) ||
      (calculation.independent_verification?.calculation_id !== undefined &&
        calculation.independent_verification.calculation_id !== calculation.calculation_id) ||
      typeof calculation.independent_verification?.recomputed_value !== "string" ||
      normalizeExactDecimal(calculation.output_value) === null ||
      normalizeExactDecimal(calculation.output_value) !== normalizeExactDecimal(calculation.independent_verification.recomputed_value)
    ) {
      continue;
    }
    const inputMetrics = calculation.input_fact_ids.map((factId) =>
      report.confirmed.metrics.find((metric) => metric.fact_id === factId),
    );
    if (inputMetrics.some((metric) => metric === undefined)) {
      continue;
    }
    const [first, second] = inputMetrics as [ConfirmedMetric, ConfirmedMetric];
    const current = first.comparison_role === "current" ? first : second.comparison_role === "current" ? second : undefined;
    const comparative = first.comparison_role === "comparative" ? first : second.comparison_role === "comparative" ? second : undefined;
    if (
      current === undefined ||
      comparative === undefined ||
      current.metric_id === undefined ||
      !expectedMetricIds.has(current.metric_id) ||
      current.metric_id !== comparative.metric_id ||
      current.period_end !== `${report.report_year}-12-31` ||
      comparative.period_end !== `${report.report_year - 1}-12-31` ||
      current.source_document_id !== report.source_document_id ||
      comparative.source_document_id !== report.source_document_id ||
      current.source_sha256.toLowerCase() !== report.source_sha256.toLowerCase() ||
      comparative.source_sha256.toLowerCase() !== report.source_sha256.toLowerCase() ||
      current.currency !== comparative.currency ||
      !isRenminbiCurrency(current.currency) ||
      current.scope !== comparative.scope ||
      !isIndependentlyVerifiedMetric(current) ||
      !isIndependentlyVerifiedMetric(comparative)
    ) {
      continue;
    }
    // Legacy M2 reports may omit calculation.unit; inherit the display unit only
    // when both independently verified inputs establish the same unit.
    const currentUnits = Array.from(new Set(verifiedMetricEvidences(current, evidenceById).map((item) => item.unit).filter(Boolean)));
    const comparativeUnits = Array.from(new Set(verifiedMetricEvidences(comparative, evidenceById).map((item) => item.unit).filter(Boolean)));
    if (currentUnits.length !== 1 || currentUnits[0] !== "元" || comparativeUnits.length !== 1 || comparativeUnits[0] !== "元") {
      continue;
    }
    const value = calculation.output_value.trim();
    const parsed = /^(-?)(\d+)(?:\.(\d+))?$/.exec(value);
    if (parsed === null) {
      continue;
    }
    const exactDigits = BigInt(`${parsed[2]}${parsed[3] ?? ""}`);
    const amount = formatInHundredMillions(value.replace(/^-/, ""));
    if (amount === null) {
      continue;
    }
    const direction = exactDigits === 0n ? "变动" : parsed[1] === "-" ? "减少" : "增加";
    const directionKey = exactDigits === 0n ? "unchanged" : parsed[1] === "-" ? "decrease" : "increase";
    changes.push({
      metricId: current.metric_id,
      direction: directionKey,
      text: `${candidateMetricLabel(current)}：${direction}约${amount}亿元`,
    });
  }
  return changes;
}

function candidateMetricLabel(metric: ConfirmedMetric): string {
  const labels: Record<string, string> = {
    net_profit_parent: "归母净利润",
    revenue: "营业收入",
    operating_cash_flow: "经营现金流净额",
    non_recurring_total: "披露的非经常性损益合计",
  };
  return metric.metric_id === undefined ? metric.label_raw : labels[metric.metric_id] ?? metric.label_raw;
}

function isRenminbiCurrency(currency: string | null): boolean {
  return currency === "CNY" || currency === "RMB" || currency === "人民币";
}

function candidateWhyText(
  signalId: string,
  changes: Array<{ metricId: string; direction: "increase" | "decrease" | "unchanged"; text: string }>,
): string {
  if (changes.length === 0) {
    return "本次报告没有可展示的独立核验年度差额，暂不能确认这组指标是否按候选规则所示方向变化，也不能据此解释原因。";
  }
  const directions = new Map(changes.map((item) => [item.metricId, item.direction]));
  if (signalId === "profit_up_cash_down") {
    if (directions.get("net_profit_parent") === "increase" && directions.get("operating_cash_flow") === "decrease") {
      return "归母净利润增加而经营现金流减少，两项年度变化方向相反；这些数据不能说明差异原因，需要结合年报附注继续核对。";
    }
  }
  if (signalId === "revenue_up_cash_down") {
    if (directions.get("revenue") === "increase" && directions.get("operating_cash_flow") === "decrease") {
      return "营业收入增加而经营现金流减少；这组变化本身不能说明销售回款情况，需要结合收付款明细核对。";
    }
  }
  return "上方列出了可展示的已核验年度差额，但当前数值方向不足以支持该候选项的完整描述；请先核对具体计算输入和变化原因。";
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

function isSuccessfulJob(job: AnnualAnalysisJob): boolean {
  return job.status === "completed" || job.status === "completed_with_issues";
}

function isFailedJob(job: AnnualAnalysisJob): boolean {
  return job.status === "failed" || job.status === "interrupted";
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
