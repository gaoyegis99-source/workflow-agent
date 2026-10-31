import { describe, expect, it } from "vitest";

import { latestAgentView } from "./agentRuntime";

describe("latestAgentView", () => {
  it("does not let a stale stream snapshot hide a newer polled projection", () => {
    const stream = { wake_revision: 5, recent_decisions: ["WAIT_REQUEST"] };
    const query = { wake_revision: 6, recent_decisions: ["EXPLANATION"] };

    expect(latestAgentView(stream, query)).toBe(query);
  });

  it("keeps the stream snapshot only when it is newer", () => {
    const stream = { wake_revision: 7, status: "READY" };
    const query = { wake_revision: 6, status: "WAITING" };

    expect(latestAgentView(stream, query)).toBe(stream);
    expect(latestAgentView(stream, undefined)).toBe(stream);
    expect(latestAgentView(undefined, query)).toBe(query);
  });

  it("prefers the durable query when an equal-revision stream snapshot is incomplete", () => {
    const stream = { wake_revision: 6, recent_decisions: ["WAIT_REQUEST"] };
    const query = {
      wake_revision: 6,
      recent_decisions: ["WAIT_REQUEST", "EXPLANATION"],
    };

    expect(latestAgentView(stream, query)).toBe(query);
  });
});
