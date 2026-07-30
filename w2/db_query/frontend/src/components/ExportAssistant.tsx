import React, { useEffect, useRef, useState } from "react";
import {
  Button,
  Drawer,
  Input,
  Space,
  Tag,
  Typography,
} from "antd";
import {
  CloseOutlined,
  FileTextOutlined,
  RobotOutlined,
  SendOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import {
  ExportAssistantChatMessage,
  ExportFormat,
  ExportReceipt,
  QueryResult,
} from "../types/query";
import { chatWithExportAssistant } from "../services/exportAssistant";
import { parseExportIntent } from "./exportAssistantLogic";
import "./ExportAssistant.css";

const { Text, Title } = Typography;
const { TextArea } = Input;

interface AssistantMessage {
  id: number;
  role: "assistant" | "user";
  text: string;
}

interface ExportAssistantProps {
  databaseName: string | null;
  sql: string;
  result: QueryResult | null;
  busy?: boolean;
  onExportCurrent: (format: ExportFormat) => Promise<ExportReceipt>;
  onExecuteAndExport: (
    format: ExportFormat,
    sqlOverride?: string
  ) => Promise<ExportReceipt>;
}

const initialMessage: AssistantMessage = {
  id: 1,
  role: "assistant",
  text: (
    "你好，我是 AI 导出助手。直接告诉我表名、筛选条件和格式即可，"
    + "例如“把 users 表导出成 CSV”。"
  ),
};

export const ExportAssistant: React.FC<ExportAssistantProps> = ({
  databaseName,
  sql,
  result,
  busy = false,
  onExportCurrent,
  onExecuteAndExport,
}) => {
  const [open, setOpen] = useState(false);
  const [nudgeVisible, setNudgeVisible] = useState(false);
  const [input, setInput] = useState("");
  const [acting, setActing] = useState(false);
  const [messages, setMessages] = useState<AssistantMessage[]>([
    initialMessage,
  ]);
  const nextMessageId = useRef(2);
  const lastResultKey = useRef<string | null>(null);
  const messageEnd = useRef<HTMLDivElement | null>(null);

  const appendMessage = (
    role: AssistantMessage["role"],
    text: string
  ): void => {
    setMessages((current) => [
      ...current,
      { id: nextMessageId.current++, role, text },
    ]);
  };

  useEffect(() => {
    if (!result || !databaseName) return;

    const key = [
      databaseName,
      result.sql,
      result.rowCount,
      result.executionTimeMs,
    ].join(":");
    if (lastResultKey.current === key) return;

    lastResultKey.current = key;
    appendMessage(
      "assistant",
      `查询完成，共 ${result.rowCount.toLocaleString()} 行。需要导出为 CSV 还是 JSON？`
    );
    setNudgeVisible(true);
  }, [databaseName, result]);

  useEffect(() => {
    messageEnd.current?.scrollIntoView({ block: "nearest" });
  }, [messages, acting]);

  const describeReceipt = (
    receipt: ExportReceipt,
    executed: boolean
  ): string => {
    const action = executed ? "查询并导出完成" : "导出完成";
    return `${action}：${receipt.rowCount.toLocaleString()} 行已保存到 ${receipt.fileName}。`;
  };

  const performExport = async (
    format: ExportFormat,
    executeFirst: boolean,
    sqlOverride?: string
  ): Promise<void> => {
    if (!databaseName) {
      appendMessage("assistant", "请先在左侧选择一个数据库，我再帮你导出。");
      return;
    }
    if (busy || acting) {
      appendMessage("assistant", "当前任务还在处理中，请稍等一下。");
      return;
    }

    const plannedSql = sqlOverride?.trim() || sql.trim();
    const shouldExecute = Boolean(sqlOverride) || executeFirst || !result;
    if (shouldExecute && !plannedSql) {
      appendMessage("assistant", "SQL 编辑器还是空的，请先输入要执行的查询。");
      return;
    }

    setActing(true);
    setNudgeVisible(false);
    try {
      const receipt = shouldExecute
        ? await onExecuteAndExport(format, sqlOverride)
        : await onExportCurrent(format);
      appendMessage("assistant", describeReceipt(receipt, shouldExecute));
    } catch (error) {
      appendMessage(
        "assistant",
        `导出没有完成：${
          error instanceof Error ? error.message : "请稍后重试"
        }`
      );
    } finally {
      setActing(false);
    }
  };

  const submitCommand = async (): Promise<void> => {
    const command = input.trim();
    if (!command) return;

    const userTurn: AssistantMessage = {
      id: nextMessageId.current++,
      role: "user",
      text: command,
    };
    const conversation = [...messages, userTurn];
    setMessages(conversation);
    setInput("");
    if (!databaseName) {
      appendMessage("assistant", "请先在左侧选择一个数据库，我再帮你分析导出需求。");
      return;
    }
    if (busy || acting) {
      appendMessage(
        "assistant",
        "当前任务还在处理中，我已经看到你的消息，请稍后再发送。"
      );
      return;
    }

    setActing(true);
    setNudgeVisible(false);
    try {
      const chatMessages: ExportAssistantChatMessage[] = conversation.map(
        (message) => ({
          role: message.role,
          content: message.text,
        })
      );
      const reply = await chatWithExportAssistant(databaseName, {
        messages: chatMessages,
        currentSql: sql.trim() || undefined,
        hasCurrentResult: Boolean(result),
      });
      appendMessage("assistant", reply.message);

      if (reply.action === "chat") return;
      if (!reply.format) {
        appendMessage("assistant", "请确认要使用 CSV 还是 JSON 格式。");
        return;
      }

      let receipt: ExportReceipt;
      if (reply.action === "export_current") {
        if (!result) {
          appendMessage(
            "assistant",
            "当前没有查询结果，请告诉我要导出的表名或数据条件。"
          );
          return;
        }
        receipt = await onExportCurrent(reply.format);
        appendMessage("assistant", describeReceipt(receipt, false));
        return;
      }

      if (!reply.sql?.trim()) {
        appendMessage(
          "assistant",
          "我还缺少可执行的查询，请再说明表名或筛选条件。"
        );
        return;
      }
      receipt = await onExecuteAndExport(reply.format, reply.sql);
      appendMessage("assistant", describeReceipt(receipt, true));
    } catch (error) {
      const intent = parseExportIntent(command);
      const guidance =
        intent.action === "help"
          ? "你可以告诉我表名、筛选条件以及 CSV/JSON 格式。"
          : error instanceof Error
            ? error.message
            : "请稍后重试";
      appendMessage("assistant", `这次对话没有完成：${guidance}`);
    } finally {
      setActing(false);
    }
  };

  const openAssistant = (): void => {
    setOpen(true);
    setNudgeVisible(false);
  };

  return (
    <>
      {!open && nudgeVisible && (
        <div className="export-assistant__nudge" role="status">
          <button
            type="button"
            className="export-assistant__nudge-main"
            onClick={openAssistant}
          >
            <RobotOutlined />
            <span>
              查询完成
              <strong>需要导出为 CSV 或 JSON 吗？</strong>
            </span>
          </button>
          <button
            type="button"
            className="export-assistant__nudge-close"
            aria-label="关闭导出提示"
            onClick={() => setNudgeVisible(false)}
          >
            <CloseOutlined />
          </button>
        </div>
      )}

      {!open && (
        <button
          type="button"
          className="export-assistant__launcher"
          aria-label="打开 AI 导出助手"
          onClick={openAssistant}
        >
          <RobotOutlined />
          <span>AI 导出</span>
          {result && <i aria-hidden="true" />}
        </button>
      )}

      <Drawer
        title={
          <Space size={10}>
            <span className="export-assistant__avatar">
              <RobotOutlined />
            </span>
            <span>
              <Title level={5} style={{ margin: 0 }}>
                AI 导出助手
              </Title>
              <Text type="secondary" style={{ fontSize: 12 }}>
                自然语言驱动 · CSV / JSON
              </Text>
            </span>
          </Space>
        }
        open={open}
        onClose={() => setOpen(false)}
        width={420}
        styles={{
          header: { borderBottom: "2px solid #000000" },
          body: { padding: 0, display: "flex", flexDirection: "column" },
        }}
      >
        <div className="export-assistant__context">
          <div>
            <Text type="secondary">当前数据库</Text>
            <Text strong>{databaseName || "未选择"}</Text>
          </div>
          <Tag color={result ? "success" : "processing"}>
            {result ? `${result.rowCount} 行可导出` : "可按表名直接导出"}
          </Tag>
        </div>

        <div className="export-assistant__messages">
          {messages.map((item) => (
            <div
              key={item.id}
              className={`export-assistant__message export-assistant__message--${item.role}`}
            >
              {item.text}
            </div>
          ))}
          {acting && (
            <div className="export-assistant__message export-assistant__message--assistant">
              正在理解需求并准备下一步…
            </div>
          )}
          <div ref={messageEnd} />
        </div>

        <div className="export-assistant__actions">
          {result && (
            <div className="export-assistant__action-group">
              <Text strong>导出当前结果</Text>
              <Space.Compact block>
                <Button
                  icon={<FileTextOutlined />}
                  disabled={busy || acting}
                  onClick={() => void performExport("csv", false)}
                >
                  CSV
                </Button>
                <Button
                  icon={<FileTextOutlined />}
                  disabled={busy || acting}
                  onClick={() => void performExport("json", false)}
                >
                  JSON
                </Button>
              </Space.Compact>
            </div>
          )}

          <div className="export-assistant__action-group">
            <Text strong>执行编辑器 SQL 并导出</Text>
            <Space.Compact block>
              <Button
                icon={<ThunderboltOutlined />}
                disabled={!databaseName || !sql.trim() || busy || acting}
                onClick={() => void performExport("csv", true)}
              >
                运行 + CSV
              </Button>
              <Button
                icon={<ThunderboltOutlined />}
                disabled={!databaseName || !sql.trim() || busy || acting}
                onClick={() => void performExport("json", true)}
              >
                运行 + JSON
              </Button>
            </Space.Compact>
          </div>
        </div>

        <div className="export-assistant__composer">
          <TextArea
            value={input}
            autoSize={{ minRows: 1, maxRows: 3 }}
            placeholder="例如：把 wpy_content_audit 导出为 CSV"
            disabled={acting}
            onChange={(event) => setInput(event.target.value)}
            onPressEnter={(event) => {
              if (!event.shiftKey) {
                event.preventDefault();
                void submitCommand();
              }
            }}
          />
          <Button
            type="primary"
            aria-label="发送导出指令"
            icon={<SendOutlined />}
            disabled={!input.trim() || acting}
            onClick={() => void submitCommand()}
          />
        </div>
      </Drawer>
    </>
  );
};
