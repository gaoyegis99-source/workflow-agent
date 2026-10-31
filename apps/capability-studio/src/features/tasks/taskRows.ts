import type { Task } from "../../shared/types";

export type TaskRow = Task & {
  id: string;
  goal: string;
  revision: number;
  mode: string;
  status: string;
};

/** Project task records into the table's display-only shape. */
export function projectTaskRows(
  tasks: Task[],
  state: string,
  search: string,
): TaskRow[] {
  const query = search.toLowerCase();
  return tasks
    .filter(
      (task) =>
        (state === "ALL" ||
          task.lifecycle === state ||
          task.control === state) &&
        (task.spec.goal.toLowerCase().includes(query) ||
          task.task_id.includes(search)),
    )
    .map((task) => ({
      ...task,
      id: task.task_id,
      goal: task.spec.goal,
      revision: task.spec_revision,
      mode: task.execution_mode,
      status: `${task.lifecycle} / ${task.control}`,
    }));
}
