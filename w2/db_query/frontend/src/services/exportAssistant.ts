/** Multi-turn conversation client for the schema-aware export assistant. */

import axios from "axios";
import { apiClient } from "./api";
import {
  ExportAssistantChatMessage,
  ExportAssistantReply,
} from "../types/query";

interface ExportAssistantContext {
  messages: ExportAssistantChatMessage[];
  currentSql?: string;
  hasCurrentResult: boolean;
}

const assistantError = (error: unknown): Error => {
  if (axios.isAxiosError(error)) {
    const detail = error.response?.data?.detail;
    if (typeof detail === "string") return new Error(detail);
    return new Error(error.message || "Export assistant is unavailable");
  }
  return error instanceof Error
    ? error
    : new Error("Export assistant is unavailable");
};

export const chatWithExportAssistant = async (
  databaseName: string,
  context: ExportAssistantContext
): Promise<ExportAssistantReply> => {
  try {
    const response = await apiClient.post<ExportAssistantReply>(
      `/api/v1/dbs/${encodeURIComponent(databaseName)}/export/assistant`,
      {
        messages: context.messages.slice(-20),
        currentSql: context.currentSql || null,
        hasCurrentResult: context.hasCurrentResult,
      }
    );
    return response.data;
  } catch (error) {
    throw assistantError(error);
  }
};
