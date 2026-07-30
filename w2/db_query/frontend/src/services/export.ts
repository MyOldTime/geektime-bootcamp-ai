/** Browser download helpers for query result export endpoints. */

import axios, { AxiosResponse } from "axios";
import { apiClient } from "./api";
import {
  ExportFormat,
  ExportReceipt,
  QueryResult,
} from "../types/query";

const extensionFor = (format: ExportFormat) => format;

const fallbackFileName = (
  databaseName: string,
  format: ExportFormat
): string => {
  const timestamp = new Date()
    .toISOString()
    .replace(/[:.]/g, "-")
    .slice(0, -5);
  return `${databaseName}_query_${timestamp}.${extensionFor(format)}`;
};

const fileNameFromHeaders = (
  response: AxiosResponse<Blob>,
  fallback: string
): string => {
  const disposition = response.headers["content-disposition"] as
    | string
    | undefined;
  if (!disposition) return fallback;

  const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf8Match?.[1]) {
    try {
      return decodeURIComponent(utf8Match[1]);
    } catch {
      return fallback;
    }
  }

  const plainMatch = disposition.match(/filename="?([^";]+)"?/i);
  return plainMatch?.[1] || fallback;
};

const startDownload = (
  response: AxiosResponse<Blob>,
  databaseName: string,
  format: ExportFormat
): string => {
  const fallback = fallbackFileName(databaseName, format);
  const fileName = fileNameFromHeaders(response, fallback);
  const objectUrl = URL.createObjectURL(response.data);
  const link = document.createElement("a");

  link.href = objectUrl;
  link.download = fileName;
  link.style.display = "none";
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);

  return fileName;
};

const readHeaderNumber = (
  response: AxiosResponse<Blob>,
  name: string,
  fallback?: number
): number | undefined => {
  const value = Number(response.headers[name]);
  return Number.isFinite(value) ? value : fallback;
};

const exportError = async (error: unknown): Promise<Error> => {
  if (!axios.isAxiosError(error)) {
    return error instanceof Error ? error : new Error("Export failed");
  }

  let detail: unknown = error.response?.data;
  if (detail instanceof Blob) {
    const text = await detail.text();
    try {
      detail = JSON.parse(text);
    } catch {
      detail = text;
    }
  }

  if (detail && typeof detail === "object" && "detail" in detail) {
    const message = (detail as { detail?: unknown }).detail;
    if (typeof message === "string") return new Error(message);
  }
  if (typeof detail === "string" && detail.trim()) {
    return new Error(detail);
  }
  return new Error(error.message || "Export failed");
};

const postExport = async (
  path: string,
  payload: Record<string, unknown>,
  databaseName: string,
  format: ExportFormat,
  fallbackRowCount?: number
): Promise<ExportReceipt> => {
  try {
    const response = await apiClient.post<Blob>(path, payload, {
      responseType: "blob",
    });
    const fileName = startDownload(response, databaseName, format);
    const rowCount =
      readHeaderNumber(response, "x-row-count", fallbackRowCount) ?? 0;
    const executionTimeMs = readHeaderNumber(
      response,
      "x-execution-time-ms"
    );

    return { format, fileName, rowCount, executionTimeMs };
  } catch (error) {
    throw await exportError(error);
  }
};

export const exportQueryResult = async (
  databaseName: string,
  result: QueryResult,
  format: ExportFormat
): Promise<ExportReceipt> =>
  postExport(
    `/api/v1/dbs/${encodeURIComponent(databaseName)}/export`,
    {
      format,
      columns: result.columns,
      rows: result.rows,
    },
    databaseName,
    format,
    result.rowCount
  );

export const executeAndExportQuery = async (
  databaseName: string,
  sql: string,
  format: ExportFormat
): Promise<ExportReceipt> =>
  postExport(
    `/api/v1/dbs/${encodeURIComponent(databaseName)}/query/export`,
    { sql, format },
    databaseName,
    format
  );
