export type AnnualHistoryMode =
  | "deterministic"
  | "m3_screening"
  | "model_investigation"
  | "integrated_analysis"
  | "unknown";

export interface AnnualAnalysisHistoryIssue {
  code: string;
  message: string;
}

export interface AnnualAnalysisHistoryItem {
  item_id: string;
  company_code: string;
  year: number;
  report_type: "annual";
  source_document_id: string;
  source_id: string;
  source_name: string | null;
  status: string;
  job_status: string | null;
  run_status: string | null;
  mode: AnnualHistoryMode;
  run_mode: AnnualHistoryMode | null;
  replay: boolean;
  created_at: string | null;
  job_id: string | null;
  run_id: string | null;
  result_available: boolean;
  issues: AnnualAnalysisHistoryIssue[];
}

export interface AnnualAnalysisHistoryResponse {
  items: AnnualAnalysisHistoryItem[];
  total: number;
  limit: number;
  offset: number;
}
