export type AnnualAnalysisJobMode =
  | "deterministic"
  | "m3_screening"
  | "model_investigation"
  | "integrated_analysis";

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

export interface AnnualAnalysisDiscoveryProgress {
  event: "planned" | "batch_started" | "batch_finished" | "budget_exhausted";
  status: "not_called" | "running" | "completed" | "partial" | "failed" | "budget_exhausted";
  batch_index: number;
  planned_batches: number;
  processed_batches: number;
  successful_batches: number;
  partial_batches: number;
  failed_batches: number;
  total_pages: number;
  total_chars: number;
  updated_at: string;
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
  discovery_progress?: AnnualAnalysisDiscoveryProgress;
}
