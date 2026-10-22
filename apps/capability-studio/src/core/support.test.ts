import { describe, expect, it } from "vitest";
import { resolveRole, targetLabel } from "./support";
import type { WorkspaceSummary } from "../shared/types";

describe("platform module boundaries", () => {
  it("uses the server summary as the authority for the current role", () => {
    const summary = {
      role: "OWNER",
    } as WorkspaceSummary;

    expect(
      resolveRole(
        summary,
        [{ project_id: "project-1", name: "项目", role: "VIEWER" }],
        "project-1",
      ),
    ).toBe("OWNER");
  });

  it("falls back to the memory session while the summary is loading", () => {
    expect(
      resolveRole(
        undefined,
        [{ project_id: "project-1", name: "项目", role: "EDITOR" }],
        "project-1",
      ),
    ).toBe("EDITOR");
    expect(resolveRole(undefined, [], "missing")).toBe("VIEWER");
  });

  it("normalizes operator and workflow targets for feature pages", () => {
    expect(targetLabel({ target: { operator_id: "qgis-buffer" } })).toBe(
      "qgis-buffer",
    );
    expect(
      targetLabel({ target: { package_ref: { package_id: "workflow-1" } } }),
    ).toBe("workflow-1");
  });
});
