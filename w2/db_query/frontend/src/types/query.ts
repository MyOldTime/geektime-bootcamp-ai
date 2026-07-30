/** Query execution types. */

export interface QueryColumn {
  name: string;
  dataType: string;
}

export interface QueryResult {
  columns: QueryColumn[];
  rows: Record<string, any>[];
  rowCount: number;
  executionTimeMs: number;
  sql: string;
}

export interface QueryInput {
  sql: string;
}

export type ExportFormat = "csv" | "json";

export interface ExportReceipt {
  format: ExportFormat;
  fileName: string;
  rowCount: number;
  executionTimeMs?: number;
}

export type ExportAssistantAction =
  | "chat"
  | "export_current"
  | "execute_export";

export interface ExportAssistantChatMessage {
  role: "user" | "assistant";
  content: string;
}

export interface ExportAssistantReply {
  message: string;
  action: ExportAssistantAction;
  format?: ExportFormat | null;
  sql?: string | null;
}

export interface QueryHistoryEntry {
  id: number;
  databaseName: string;
  sqlText: string;
  executedAt: string;
  executionTimeMs?: number | null;
  rowCount?: number | null;
  success: boolean;
  errorMessage?: string | null;
  querySource: "manual" | "natural_language";
}
