import { afterEach, describe, expect, it, vi } from "vitest";
import { requestAllPages } from "./pagination";

afterEach(() => vi.unstubAllGlobals());

describe("requestAllPages", () => {
  it("follows opaque cursors and exposes the 101st item", async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        calls.push(url);
        const second = url.includes("after=opaque%2Fcursor%3D");
        return new Response(
          JSON.stringify(
            second
              ? { items: [{ id: 101 }], next_cursor: null }
              : {
                  items: Array.from({ length: 100 }, (_, index) => ({
                    id: index + 1,
                  })),
                  next_cursor: "opaque/cursor=",
                },
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }),
    );

    const items = await requestAllPages<{ id: number }>(
      "token",
      "/projects/p/assets",
    );

    expect(items).toHaveLength(101);
    expect(items.at(-1)?.id).toBe(101);
    expect(calls).toEqual([
      "/api/v1/projects/p/assets?limit=100",
      "/api/v1/projects/p/assets?limit=100&after=opaque%2Fcursor%3D",
    ]);
  });

  it("fails closed when a service repeats a cursor", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ items: [], next_cursor: "same" }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
      ),
    );
    await expect(
      requestAllPages("token", "/projects/p/assets"),
    ).rejects.toThrow("重复游标");
  });
});
