export type AnnualAnalysisJobMode = "deterministic" | "m3_screening";

export type AnnualAnalysisJobStatus =
  | "queued"
  | "running"
  | "completed"
  | "completed_with_issues"
  | "failed"
  | "interrupted";

export interface AnnualAnalysisJobRequest {
  company_id: string;
  report_year: number;
  document_id: string;
  source_pdf_path: string;
  sha256: string;
  mode: AnnualAnalysisJobMode;
}

export interface AnnualAnalysisJobErrorDetail {
  code: string;
  message: string;
}

export interface AnnualAnalysisJob {
  job_id: string;
  status: AnnualAnalysisJobStatus;
  stage: string;
  created_at: string;
  updated_at: string;
  request: AnnualAnalysisJobRequest;
  run_id: string | null;
  result_url: string | null;
  error: AnnualAnalysisJobErrorDetail | null;
}
