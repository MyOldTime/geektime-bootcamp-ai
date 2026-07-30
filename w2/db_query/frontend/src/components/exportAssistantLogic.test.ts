import { describe, expect, it } from "vitest";
import { parseExportIntent } from "./exportAssistantLogic";

describe("parseExportIntent", () => {
  it("understands a current-result CSV request", () => {
    expect(parseExportIntent("把结果导出成 CSV")).toEqual({
      action: "export",
      format: "csv",
      executeFirst: false,
    });
  });

  it("understands an execute-and-export JSON command", () => {
    expect(parseExportIntent("执行查询并导出 JSON")).toEqual({
      action: "export",
      format: "json",
      executeFirst: true,
    });
  });

  it("maps Excel wording to CSV", () => {
    expect(parseExportIntent("Download this as an Excel table")).toEqual({
      action: "export",
      format: "csv",
      executeFirst: false,
    });
  });

  it("tolerates CVS typo next to Chinese text", () => {
    expect(parseExportIntent("把数据导出为cvs文件")).toEqual({
      action: "export",
      format: "csv",
      executeFirst: false,
    });
  });

  it("recognizes help and unrelated messages", () => {
    expect(parseExportIntent("如何使用？").action).toBe("help");
    expect(parseExportIntent("你好").action).toBe("unknown");
  });
});
