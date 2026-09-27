/** 后端以精确十进制字符串返回金额和同比率，不用 JavaScript number。 */
export type DecimalString = string;

/** 当前创建接口写入的运行状态。读取旧记录时仍保留原始字符串。 */
export type AnnualPrecheckRunStatus =
  | "completed"
  | "completed_with_issues"
  | "verification_failed";

/** 原文金额定位与数值换算复核的逐条状态。 */
export type SourceAmountCheckStatus = "passed" | "failed" | "abstained";

export type FactColumnRole = "current" | "comparative";

export interface AnnualPrecheckRequest {
  parsed_path: string;
  source_pdf_path: string;
  company_id: string;
  report_year: number;
}

/** POST /v1/text-pdf-uploads 的成功响应。路径相对 data/raw 与 data/processed。 */
export interface TextPdfUploadReceipt {
  document_id: string;
  source_pdf_path: string;
  parsed_path: string;
  sha256: string;
  page_count: number;
}

export interface PrecheckInputs {
  parsed_path: string;
  source_pdf_path: string;
  parsed_file_sha256: string;
  source_pdf_sha256: string;
  parsed_source_sha256: string;
  hashes_match: boolean;
  company_id: string;
  report_year: number;
  document_id: string;
}

export interface PrecheckCodeSnapshot {
  git_available: boolean;
  git_head: string | null;
  git_dirty: boolean | null;
  source_file_sha256: Record<string, string>;
  missing_source_files: string[];
}

export interface FactHit {
  page_number: number;
  block_index: number;
  text: string;
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

export interface FinancialFact {
  document_id: string;
  source_sha256: string;
  company_id: string;
  indicator_name: string;
  table_name: string;
  raw_value: string;
  normalized_value: DecimalString;
  report_year: number;
  period_label: string;
  period_type: string;
  column_role: FactColumnRole;
  restatement_status: string;
  unit_multiplier: DecimalString;
  currency: string;
  statement_scope: string;
  extraction_method: string;
  hits: FactHit[];
  limitations: string[];
}

export interface PrecheckIssue {
  code: string | null;
  message: string | null;
  indicator_name: string | null;
  table_name?: string | null;
}

export interface FactExtraction {
  facts: FinancialFact[];
  issues: PrecheckIssue[];
}

export interface AnnualChange {
  indicator_name: string;
  report_year: number;
  prior_year: number;
  document_id: string;
  source_sha256: string;
  company_id: string;
  currency: string;
  unit_multiplier: DecimalString;
  statement_scope: string;
  period_type: string;
  current_value: DecimalString;
  prior_value: DecimalString;
  difference: DecimalString;
  rate: DecimalString;
  formula: string;
}

export interface AnnualChangeCalculation {
  changes: AnnualChange[];
  issues: PrecheckIssue[];
}

export interface PrecheckIssueGroups {
  extraction: PrecheckIssue[];
  calculation: PrecheckIssue[];
}

export interface AmountMatch {
  matched_text: string;
  context: string;
}

export interface SourceAmountEvidence {
  page_number: number;
  block_index: number;
  x0: number;
  y0: number;
  x1: number;
  y1: number;
  extracted_text: string;
  amount_in_this_clip: boolean;
  problem: string | null;
}

export interface SourceAmountResult {
  indicator_name: string;
  report_year: number;
  column_role: FactColumnRole;
  status: string;
  amount_located: boolean;
  calculation_ok: boolean | null;
  reason: string | null;
  amount_match: AmountMatch | null;
  evidence: SourceAmountEvidence[];
}

export interface SourceAmountVerification {
  kind: string;
  scope_note: string;
  status: string;
  passed_count: number;
  failed_count: number;
  abstained_count: number;
  results: SourceAmountResult[];
}

/** 旧版年度确定性筛查的状态；它只表示计算或候选线索。 */
export type AnnualScreeningStatus = "candidate" | "not_triggered" | "calculated" | "abstained";

export interface ScreeningFactHit {
  page_number: number;
  block_index: number;
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

/** Screening 将使用到的旧事实摘要，不代表该事实已经独立核验。 */
export interface ScreeningFactInput {
  role: "fact";
  indicator_name: string;
  report_year: number;
  column_role: FactColumnRole;
  normalized_value: DecimalString;
  document_id: string;
  source_sha256: string;
  company_id: string;
  currency: string;
  statement_scope: string;
  period_type: string;
  hits: ScreeningFactHit[];
}

export interface ScreeningDifferenceValue {
  role: "calculation";
  left_difference: DecimalString;
  right_difference: DecimalString;
}

export interface ScreeningRatioValue {
  role: "calculation";
  year: number;
  report_year: number;
  numerator: DecimalString;
  denominator: DecimalString;
  ratio: DecimalString;
}

export type ScreeningValue = ScreeningDifferenceValue | ScreeningRatioValue;

export interface AnnualScreeningItem {
  signal_id: string;
  title: string;
  status: AnnualScreeningStatus;
  statement_kind: "inference" | "calculation";
  formula: string;
  inputs: ScreeningFactInput[];
  value: ScreeningValue | null;
  reason: string | null;
  note: string | null;
  limitation: string;
}

export interface AnnualScreening {
  kind: "deterministic_annual_candidate_screen";
  role: "candidate_input_for_later_agent_or_report";
  limitation: string;
  items: AnnualScreeningItem[];
}

export interface AnnualPrecheckRecord {
  run_id: string;
  status: string;
  created_at: string;
  inputs: PrecheckInputs;
  model_called: boolean;
  independently_verified: boolean;
  code?: PrecheckCodeSnapshot;
  note?: string;
  formula?: string;
  verification?: SourceAmountVerification;
  screening?: AnnualScreening;
  facts: FactExtraction;
  calculation: AnnualChangeCalculation;
  issues: PrecheckIssueGroups;
}
