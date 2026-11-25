// Mirrors the backend's response models (backend/app/schemas/documents.py and api/search.py).

export type DocStatus = "queued" | "processing" | "completed" | "failed" | "needs_review";
export const DOC_STATUSES: DocStatus[] = ["queued", "processing", "completed", "failed", "needs_review"];
export const isActive = (s: string): boolean => s === "queued" || s === "processing";

export type FieldName =
  | "invoice_number" | "invoice_date" | "supplier_name" | "customer_name"
  | "subtotal" | "tax" | "total" | "currency";
export const FIELD_NAMES: FieldName[] = [
  "invoice_number", "invoice_date", "supplier_name", "customer_name", "subtotal", "tax", "total", "currency",
];
export type ItemField = "description" | "quantity" | "unit_price" | "line_total";
export const ITEM_FIELDS: ItemField[] = ["description", "quantity", "unit_price", "line_total"];

export interface Info {
  llm_provider: string;
  llm_model: string;
  max_file_size_mb: number;
  max_files_per_upload: number;
  extensions: string[];
}

export interface DocumentListItem {
  id: string;
  filename: string;
  status: DocStatus;
  stage: string | null;
  progress: number;
  flagged: boolean;
  file_type: string;
  page_count: number | null;
  created_at: string;
  errors: number;
  warnings: number;
}

export interface DocumentList {
  items: DocumentListItem[];
  total: number;
  page: number;
  page_size: number;
  pages: number;
}

export interface StatusResponse {
  id: string;
  status: DocStatus;
  stage: string | null;
  progress: number;
  error_message: string | null;
  attempts: number;
  updated_at: string;
}

export interface FieldValue {
  value: string | null;
  confidence?: number | null;
  source_snippet?: string | null;
  manual?: boolean;
}

export interface LineItem {
  position: number;
  description: string | null;
  quantity: string | null;
  unit_price: string | null;
  line_total: string | null;
}

export interface Invoice {
  id: string;
  invoice_index: number;
  pages: (number | null)[];
  version: number;
  source: string | null;
  is_manually_corrected: boolean;
  fields: Record<FieldName, FieldValue>;
  line_items: LineItem[];
}

export interface Finding {
  rule_code: string;
  severity: "error" | "warning";
  field: string | null;
  message: string;
  resolved: boolean;
  result_id: string | null;
}

export interface HistoryItem {
  stage: string;
  status: string;
  started_at: string | null;
  duration_ms: number | null;
  details: Record<string, unknown> | null;
}

export interface DocumentSummary {
  id: string;
  filename: string;
  status: DocStatus;
  stage: string | null;
  progress: number;
  flagged: boolean;
  file_type: string;
  file_size: number;
  page_count: number | null;
  error_message: string | null;
  created_at: string;
}

export interface ResultResponse {
  document: DocumentSummary;
  results: Invoice[];
  findings: Finding[];
  history: HistoryItem[];
}

export interface CorrectionEntry {
  id: string;
  invoice_index: number;
  version: number;
  field: string;
  old_value: string | null;
  new_value: string | null;
  corrected_by: string | null;
  corrected_at: string;
}

export interface CorrectionRequest {
  invoice_index?: number;
  fields?: Partial<Record<FieldName, string | null>>;
  line_items?: ({ position: number } & Partial<Record<ItemField, string | null>>)[];
  corrected_by?: string | null;
}

export type SearchMode = "keyword" | "semantic" | "hybrid";

export interface SearchHit {
  document_id: string;
  filename: string;
  status: DocStatus;
  file_type: string;
  score: number;
  matched_by: string[];
  keyword_rank: number | null;
  semantic_rank: number | null;
  snippet: { text: string; highlights: number[][]; page: number | null } | null;
  invoice_count: number;
  invoice: {
    invoice_index: number;
    invoice_number: string | null;
    invoice_date: string | null;
    supplier_name: string | null;
    customer_name: string | null;
    subtotal: string | null;
    tax: string | null;
    total: string | null;
    currency: string | null;
  } | null;
}

export interface SearchResponse {
  query: string;
  mode: SearchMode;
  items: SearchHit[];
  total: number;
  page: number;
  page_size: number;
  pages: number;
  truncated: boolean;
}

export interface UploadFileResult {
  filename: string;
  status: "queued" | "rejected";
  id: string | null;
  error: { code: string; message: string; http_status: number; existing_document_id?: string } | null;
}
