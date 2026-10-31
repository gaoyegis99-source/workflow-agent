import type { WorkflowRelease } from "../../../shared/api";

/** Group immutable releases for the Studio's read-only release library. */
export function groupWorkflowReleases(
  releases: WorkflowRelease[],
): Array<[string, WorkflowRelease[]]> {
  return Object.entries(
    releases.reduce<Record<string, WorkflowRelease[]>>((groups, item) => {
      (groups[item.stable_key] ??= []).push(item);
      return groups;
    }, {}),
  ).sort(([left], [right]) => left.localeCompare(right));
}
