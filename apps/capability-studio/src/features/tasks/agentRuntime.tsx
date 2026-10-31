import { useEffect, useRef, useState } from "react";

import { authenticatedHeaders } from "../../shared/api";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  Field,
  JsonView,
  Skeleton,
} from "../../shared/ui";
import type { Task } from "../../shared/types";

function wakeRevision(value: Record<string, unknown> | undefined) {
  const revision = Number(value?.wake_revision ?? -1);
  return Number.isFinite(revision) ? revision : -1;
}

/** Prefer the newest durable projection when SSE and polling arrive out of order. */
export function latestAgentView(
  stream: Record<string, unknown> | undefined,
  query: Record<string, unknown> | undefined,
) {
  if (!stream) return query;
  if (!query) return stream;
  // The stream response can be assembled just before the transaction that
  // commits the final projection while still carrying that transaction's wake
  // revision.  On an equal revision, the direct query is the authoritative
  // durable read and must be allowed to replace that incomplete snapshot.
  return wakeRevision(query) >= wakeRevision(stream) ? query : stream;
}

/** Poll the durable Agent event stream without leaking a previous task cursor. */
export function useAgentStream(
  token: string,
  projectId: string,
  task: Task | undefined,
) {
  const [snapshot, setSnapshot] = useState<Record<string, unknown> | undefined>();
  const cursor = useRef(-1);
  useEffect(() => {
    setSnapshot(undefined);
    cursor.current = -1;
    if (!task || task.execution_mode !== "AGENT") return;
    const controller = new AbortController();
    let active = true;
    const wait = () => new Promise((resolve) => window.setTimeout(resolve, 1000));
    void (async () => {
      while (active) {
        try {
          const response = await fetch(
            `/api/v1/projects/${projectId}/tasks/${task.task_id}/agent/events?after=${cursor.current}`,
            {
              headers: authenticatedHeaders(token, { Accept: "text/event-stream" }),
              signal: controller.signal,
            },
          );
          if (!response.ok) throw new Error(`SSE ${response.status}`);
          const wire = await response.text();
          const id = wire.split("\n").find((line) => line.startsWith("id: "));
          const data = wire
            .split("\n")
            .find((line) => line.startsWith("data: "));
          if (id) cursor.current = Math.max(cursor.current, Number(id.slice(4)));
          if (data) {
            const value = JSON.parse(data.slice(6)) as Record<string, unknown>;
            if (!value.heartbeat) {
              cursor.current = Math.max(
                cursor.current,
                Number(value.wake_revision ?? cursor.current),
              );
              setSnapshot(value);
            }
          }
        } catch (error) {
          if (
            !active ||
            (error instanceof DOMException && error.name === "AbortError")
          )
            return;
        }
        await wait();
      }
    })();
    return () => {
      active = false;
      controller.abort();
    };
  }, [projectId, task?.task_id, task?.execution_mode, token]);
  return snapshot;
}

export function AgentPanel({
  value,
  answer,
  setAnswer,
  onAnswer,
}: {
  value?: Record<string, unknown>;
  answer: string;
  setAnswer: (value: string) => void;
  onAnswer: (id: string) => void;
}) {
  if (!value)
    return (
      <Card>
        <Skeleton lines={6} />
      </Card>
    );
  const questions =
    (value.questions as Array<Record<string, unknown>> | undefined) ?? [];
  const open = questions.filter((item) => item.status === "OPEN");
  return (
    <div className="detail-grid">
      <Card title="Agent 状态">
        <dl className="definition-list">
          <div>
            <dt>状态</dt>
            <dd>
              <Badge>{String(value.status)}</Badge>
            </dd>
          </div>
          <div>
            <dt>计划修订</dt>
            <dd>{String(value.plan_revision ?? "—")}</dd>
          </div>
          <div>
            <dt>Runtime epoch</dt>
            <dd>{String(value.epoch ?? "—")}</dd>
          </div>
          <div>
            <dt>事件游标</dt>
            <dd>{String(value.wake_revision ?? "—")}</dd>
          </div>
        </dl>
        <JsonView
          value={{
            model_usage: value.model_usage,
            wait_dependencies: value.wait_dependencies,
            runtime_recoveries: value.runtime_recoveries,
          }}
        />
      </Card>
      <Card title="待回答问题">
        {open.length ? (
          open.map((question) => (
            <div className="question-card" key={String(question.question_id)}>
              <strong>
                {String(
                  (question.question as Record<string, unknown> | undefined)
                    ?.question ?? question.gap_id,
                )}
              </strong>
              <Field label="结构化回答（JSON）">
                <textarea
                  rows={5}
                  value={answer}
                  onChange={(event) => setAnswer(event.target.value)}
                />
              </Field>
              <Button onClick={() => onAnswer(String(question.question_id))}>
                提交结构化回答
              </Button>
            </div>
          ))
        ) : (
          <EmptyState title="没有待回答问题" />
        )}
      </Card>
      <Card title="近期决策">
        <JsonView value={value.recent_decisions ?? []} />
      </Card>
      <Card title="交付尝试">
        <JsonView value={value.delivery_attempts ?? []} />
      </Card>
    </div>
  );
}
