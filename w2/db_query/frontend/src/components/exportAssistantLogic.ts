import { ExportFormat } from "../types/query";

export interface ExportIntent {
  action: "export" | "help" | "unknown";
  format?: ExportFormat;
  executeFirst: boolean;
}

const includesAny = (value: string, patterns: RegExp[]): boolean =>
  patterns.some((pattern) => pattern.test(value));

export const parseExportIntent = (input: string): ExportIntent => {
  const value = input.trim().toLowerCase();
  if (!value) {
    return { action: "unknown", executeFirst: false };
  }

  if (
    includesAny(value, [
      /帮助/,
      /怎么/,
      /如何/,
      /\bhelp\b/,
      /what can you do/,
    ])
  ) {
    return { action: "help", executeFirst: false };
  }

  const format: ExportFormat | undefined =
    /(?<![a-z0-9])json(?![a-z0-9])/.test(value)
    ? "json"
    : includesAny(value, [
          /(?<![a-z0-9])(?:csv|cvs)(?![a-z0-9])/,
          /excel/,
          /表格/,
          /逗号分隔/,
        ])
      ? "csv"
      : undefined;
  const executeFirst = includesAny(value, [
    /执行/,
    /运行/,
    /重新查询/,
    /一键/,
    /\brun\b/,
    /\bexecute\b/,
    /\bquery\b/,
  ]);
  const mentionsExport = includesAny(value, [
    /导出/,
    /下载/,
    /保存/,
    /\bexport\b/,
    /\bdownload\b/,
    /\bsave\b/,
  ]);

  if (format || mentionsExport || executeFirst) {
    return { action: "export", format, executeFirst };
  }
  return { action: "unknown", executeFirst: false };
};
