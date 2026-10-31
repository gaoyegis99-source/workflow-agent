import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  TRUSTED_PROXY_SESSION,
  authenticatedHeaders,
  mutate,
  newKey,
  releaseExecutionBody,
  request,
} from "./api";

afterEach(() => vi.unstubAllGlobals());

describe("newKey", () => {
  it("creates an API-safe request key", () => {
    expect(newKey()).toMatch(/^[a-f0-9]{32}$/);
  });
});

describe("trusted-proxy session", () => {
  it("does not turn the in-memory proxy marker into a Bearer credential", () => {
    expect(authenticatedHeaders(TRUSTED_PROXY_SESSION).has("Authorization")).toBe(false);
    expect(authenticatedHeaders("user-token").get("Authorization")).toBe("Bearer user-token");
  });
});

describe("request errors", () => {
  it("translates authorization failures without hiding the server code", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      JSON.stringify({ code: "SCOPE_DENIED" }),
      { status: 403, headers: { "Content-Type": "application/json" } },
    )));
    await expect(request("token", "/projects")).rejects.toMatchObject({
      name: "ApiError", status: 403, code: "SCOPE_DENIED",
    } satisfies Partial<ApiError>);
  });

  it("preserves an idempotency key after an ambiguous network failure", async () => {
    const keys: string[] = [];
    const fetchMock = vi.fn(async (_url: string, options?: RequestInit) => {
      keys.push(new Headers(options?.headers).get("Idempotency-Key") ?? "");
      if (keys.length === 1) throw new TypeError("offline");
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    await expect(mutate("token", "/projects", { name: "A" })).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    await expect(mutate("token", "/projects", { name: "A" })).resolves.toEqual({ ok: true });
    expect(keys[0]).toBeTruthy();
    expect(keys[1]).toBe(keys[0]);
  });
});

describe("releaseExecutionBody", () => {
  it("contains only task-controlled inputs and never a package or target override", () => {
    const body = releaseExecutionBody(
      { task_id: "task-1", spec_revision: 3, lifecycle: "OPEN", control: "ACTIVE" },
      { observations: { asset: "asset-1" } },
      4,
    );
    expect(body).toEqual({
      task_id: "task-1",
      expected_spec_revision: 3,
      inputs: { observations: { asset: "asset-1" } },
      max_node_executions: 4,
    });
    expect(body).not.toHaveProperty("target");
    expect(body).not.toHaveProperty("package_ref");
  });
});
