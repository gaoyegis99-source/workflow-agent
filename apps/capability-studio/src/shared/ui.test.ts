import { describe, expect, it } from "vitest";
import { roleAllows, toneForState } from "./ui";

describe("semantic presentation rules", () => {
  it("maps lifecycle states to one shared visual vocabulary", () => {
    expect(toneForState("SUCCEEDED")).toBe("success");
    expect(toneForState("RUNNING")).toBe("info");
    expect(toneForState("STALE")).toBe("warning");
    expect(toneForState("REVOKED")).toBe("danger");
    expect(toneForState("SEALED")).toBe("neutral");
  });

  it("keeps viewer actions visible but unauthorized", () => {
    expect(roleAllows("VIEWER", "EDITOR")).toBe(false);
    expect(roleAllows("EDITOR", "EDITOR")).toBe(true);
    expect(roleAllows("EDITOR", "OWNER")).toBe(false);
    expect(roleAllows("OWNER", "OWNER")).toBe(true);
  });
});
