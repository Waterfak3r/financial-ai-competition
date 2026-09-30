import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { AnnualReportApiError, loadAnnualAnalysis, loadVerificationEvidencePreview } from "../api/annualReports";
import { createAnnualAnalysisJob, loadAnnualAnalysisJob } from "../api/annualAnalysisJobs";
import { textPdfUploadIssues, uploadTextPdf } from "../api/prechecks";
import type {
  AnnualAnalysisResponse,
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

export function AnnualAnalysisPage() {
  const [runId, setRunId] = useState(SAMPLE_RUN_ID);
  const [companyId, setCompanyId] = useState("");
  const [reportYear, setReportYear] = useState("");
  const [pdfFile, setPdfFile] = useState<File | null>(null);
  const [mode, setMode] = useState<AnnualAnalysisJobMode>("deterministic");
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
          <p className="eyebrow">FINTRACE / 年度财报</p>
          <h1>分析年报</h1>
          <p className="lede">
            上传年报后开始本地分析。完成后会显示已核对的数据、需要进一步核查的线索，以及可查看的原文页。
          </p>
        </div>
      </section>

      <details className="annual-start-another" open={response === null}>
      <summary>{response === null ? "开始分析年报" : "分析另一份年报"}</summary>
      <section className="panel annual-job-create" aria-labelledby="annual-job-create-title">
        <div className="annual-section-heading">
          <div>
            <p className="eyebrow">开始分析</p>
            <h2 id="annual-job-create-title">选择年报并开始分析</h2>
          </div>
          <span className="sub">默认使用标准分析</span>
        </div>
        <p className="annual-job-intro">
          选择公司代码、年报年份和 PDF 文件，然后开始分析。页面只接受可读取文字的 PDF；原始文件保存在本机，不会覆盖已有文件。
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
            <p className="annual-job-default-mode">标准分析默认开启：核对年报中的数据、计算和引用。</p>
            <details className="annual-job-advanced-mode">
              <summary>高级选项：增加试行筛查规则</summary>
              <label className="annual-job-mode-option annual-job-mode-experimental">
                <input
                  type="checkbox"
                  name="annual-analysis-extra-screening"
                  checked={mode === "m3_screening"}
                  onChange={(event) => setMode(event.target.checked ? "m3_screening" : "deterministic")}
                />
                <span><strong>运行四项试行筛查</strong><small>用尚未校准的规则提示可进一步核对的数据组合。筛查结果不是已核实事实，也不判断是否存在问题。</small></span>
              </label>
              <p className="help">标准分析始终会运行；此选项只会额外增加试行筛查。</p>
            </details>
          </fieldset>

          <div className="annual-job-actions">
            <button type="submit" className="primary" disabled={jobAction !== null}>
              {jobAction === "uploading" ? "正在上传并启动…" : jobAction === "starting" ? "正在启动…" : "上传并开始分析"}
            </button>
            <button type="button" className="secondary" disabled={jobAction !== null} onClick={onRunHaitianSample}>
              {jobAction === "starting" ? "正在启动…" : "一键运行海天 2024 样例"}
            </button>
            <span className="help">样例直接使用本机已保存的 PDF；文件缺失时会显示可读错误。</span>
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
          ? "本次另加四项试行筛查；结果仅用于提示后续核查。"
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
  const evidenceById = new Map(evidenceIndex.map((evidence) => [evidence.evidence_id, evidence]));
  const reportModelInvestigation = report.model_investigation;
  const signalCount = report.pending_review.candidate_signals.length;
  const candidateCount = report.pending_review.candidate_signals.filter((item) => item.status === "candidate").length;
  const abstainedSignalCount = report.pending_review.candidate_signals.filter((item) => item.status === "abstained").length;
  const otherSignalCount = signalCount - candidateCount - abstainedSignalCount;
  const sourceMatches = report.source_sha256 === manifest.source_pdf_sha256;

  return (
    <div className="annual-report" aria-label="年度分析报告">
      <section id="annual-report-heading" className="annual-report-heading" tabIndex={-1}>
        <div>
          <p className="eyebrow">{report.company_id} · {report.report_year} 年报</p>
          <h2>本次分析结果</h2>
        </div>
        <span className={manifest.status === "completed_with_issues" ? "review-chip" : "annual-status-chip"}>
          {manifestStatusText(manifest.status)}
        </span>
      </section>

      <p className="annual-report-summary">
        <strong>分析范围：</strong>本次从年报中选取 {report.confirmed.metrics.length} 项财务数据，并核对相关年度变化和原文出处；不代表覆盖整份年报。{" "}
        <strong>已核对数据：</strong>{verifiedMetricCount} / {report.confirmed.metrics.length} 项财务数据通过独立核验；{" "}
        <strong>需进一步查看：</strong>{candidateCount} 条候选线索
        {abstainedSignalCount > 0 ? `，${abstainedSignalCount} 条规则弃权` : ""}
        {otherSignalCount > 0 ? `，另有 ${otherSignalCount} 条状态待核对的线索` : ""}。
        {candidateCount === 0 ? "没有记录候选线索不代表没有风险。" : "候选线索只用于后续核查，不是已确认异常或舞弊。"}
      </p>

      <section className="annual-summary-grid" aria-label="分析结果摘要">
        <SummaryTile label="独立核验通过的财务数据" value={verifiedMetricCount} note={`报告共记录 ${report.confirmed.metrics.length} 项`} />
        <SummaryTile label="候选线索" value={candidateCount} note="需结合原文进一步核查" />
        <SummaryTile label="筛查规则弃权" value={abstainedSignalCount} note="保留为待核对状态" />
        <SummaryTile
          label="独立重算通过"
          value={verifiedCalculationCount}
          note={`报告共记录 ${report.confirmed.analyses.length} 项计算`}
        />
      </section>

      <section className="panel annual-section annual-candidate-overview">
        <div className="annual-section-heading">
          <div>
            <p className="eyebrow">需要进一步核查</p>
            <h3>候选线索、弃权与待核对事项</h3>
          </div>
          <span className="review-chip">线索不是确认异常</span>
        </div>
        <p className="annual-candidate-warning">
          这些线索只指出值得继续查看的数据组合，不是异常认定、审计意见或舞弊结论。规则弃权和核验冲突也会保留在这里。
        </p>
        {report.pending_review.candidate_signals.length === 0 ? (
          <p className="empty">报告没有记录候选线索；这不代表没有风险。</p>
        ) : (
          <div className="annual-record-grid">
            {report.pending_review.candidate_signals.map((candidate) => (
              <CandidateCard
                key={candidate.signal_id}
                candidate={candidate}
                locations={locationsForFactIds(candidate.input_fact_ids, report, evidenceById)}
                previewBusyId={previewBusyId}
                onPreview={onPreview}
              />
            ))}
          </div>
        )}
        <PendingItems
          pending={report.pending_review}
          report={report}
          evidenceById={evidenceById}
          previewBusyId={previewBusyId}
          onPreview={onPreview}
        />
      </section>

      {report.m3_screening == null ? null : <M3ScreeningBlock screening={report.m3_screening} />}

      <section className="panel annual-section">
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

      <details className="panel annual-section annual-progressive-section">
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

      <details className="panel annual-section annual-progressive-section">
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

      <details className="panel annual-section annual-progressive-section">
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

function M3ScreeningBlock({ screening }: { screening: M3AnnualScreening }) {
  const complete = screening.screening_status === "completed" && screening.status === "completed";
  const scoreText =
    complete && screening.total_score !== null
      ? screening.total_score === 0
        ? `0 / ${screening.maximum_score} · 试行规则未触发`
        : `${screening.total_score} / ${screening.maximum_score} · 候选待核查`
      : "未形成完整汇总分";
  return (
    <section className="panel annual-m3-screening" aria-label="四项试行筛查结果">
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
  locations,
  previewBusyId,
  onPreview,
}: {
  candidate: CandidateSignal;
  locations: SourceLocationResult;
  previewBusyId: string | null;
  onPreview: (evidenceId: string) => void;
}) {
  const status = candidateStatusText(candidate.status);
  return (
    <article className="annual-record-card annual-candidate-card">
      <div className="annual-record-heading">
        <div>
          <h4>{signalTitle(candidate.signal_id)}</h4>
        </div>
        <span className="review-chip">{status}</span>
      </div>
      <p className="annual-candidate-description">
        {nonEmpty(candidate.reason) ?? "报告没有提供单独的原因说明；请结合下方已核对数据和原文页继续查看。"}
      </p>
      <SourceLocationList
        heading="相关年报位置"
        result={locations}
        previewBusyId={previewBusyId}
        onPreview={onPreview}
      />
      <p className="annual-candidate-reminder">这是一条需要进一步核查的候选线索，不是已确认异常或舞弊。</p>
      {candidate.placement_reasons.length > 0 ? <GapList title="列入待核查的说明" items={candidate.placement_reasons} /> : null}
      <details className="annual-technical-record">
        <summary>技术记录：线索编号、计算及差额</summary>
      <dl className="annual-detail-grid">
        <dt>相关事实 ID</dt><dd>{idList(candidate.input_fact_ids)}</dd>
        <dt>相关计算 ID</dt><dd>{idList(candidate.calculation_ids)}</dd>
        <dt>左侧差额</dt><dd>{candidate.left_difference ?? "报告未记录"}</dd>
        <dt>右侧差额</dt><dd>{candidate.right_difference ?? "报告未记录"}</dd>
        <dt>线索编号</dt><dd><code>{candidate.signal_id}</code></dd>
        <dt>内部状态</dt><dd><code>{candidate.status}</code></dd>
        <dt>舞弊结论</dt><dd>{candidate.fraud_conclusion ?? "报告未给出舞弊结论。"}</dd>
      </dl>
      </details>
    </article>
  );
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
                  <PendingItemSummary
                    item={item}
                    locations={locationsForPendingItem(item, report, evidenceById)}
                    previewBusyId={previewBusyId}
                    onPreview={onPreview}
                  />
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
                  <PendingItemSummary
                    item={item}
                    locations={locationsForPendingItem(item, report, evidenceById)}
                    previewBusyId={previewBusyId}
                    onPreview={onPreview}
                  />
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
  locations,
  previewBusyId,
  onPreview,
}: {
  item: AnnualReportRecord;
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
    <div className="annual-pending-summary">
      {statuses.length > 0 ? <strong>{statuses.join(" / ")}</strong> : null}
      {uniqueReasons.length > 0 ? (
        <ul>
          {uniqueReasons.map((reason, index) => <li key={index}>{reason}</li>)}
        </ul>
      ) : (
        <span className="sub">报告未记录具体原因。</span>
      )}
      <SourceLocationList
        heading="关联数据的年报位置"
        result={locations}
        previewBusyId={previewBusyId}
        onPreview={onPreview}
      />
      <details className="annual-technical-record annual-pending-technical-record">
        <summary>查看内部编号</summary>
        <code>{id}</code>
      </details>
    </div>
  );
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
  return "状态待确认";
}

function signalTitle(signalId: string): string {
  const labels: Record<string, string> = {
    profit_up_cash_down: "利润增加，经营现金流减少",
    revenue_up_cash_down: "营业收入增加，经营现金流减少",
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
