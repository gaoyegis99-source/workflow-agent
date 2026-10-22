import { useInfiniteQuery } from "@tanstack/react-query";
import { request } from "./api";
import type { Page } from "./types";

function pagePath(path: string, cursor: string | null) {
  const separator = path.includes("?") ? "&" : "?";
  return `${path}${separator}limit=100${cursor ? `&after=${encodeURIComponent(cursor)}` : ""}`;
}

export function useCursorPages<T>(
  token: string,
  path: string,
  queryKey: readonly unknown[],
  options: { enabled?: boolean; refetchInterval?: number } = {},
) {
  const query = useInfiniteQuery({
    queryKey,
    queryFn: ({ pageParam }) =>
      request<Page<T>>(token, pagePath(path, pageParam)),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: options.enabled,
    refetchInterval: options.refetchInterval,
  });
  return {
    ...query,
    items: query.data?.pages.flatMap((page) => page.items) ?? [],
  };
}

export async function requestAllPages<T>(
  token: string,
  path: string,
): Promise<T[]> {
  const items: T[] = [];
  const seen = new Set<string>();
  let cursor: string | null = null;
  for (let pageNumber = 0; pageNumber < 1_000; pageNumber += 1) {
    const page: Page<T> = await request<Page<T>>(token, pagePath(path, cursor));
    items.push(...page.items);
    cursor = page.next_cursor ?? null;
    if (!cursor) return items;
    if (seen.has(cursor)) throw new Error("列表服务返回了重复游标");
    seen.add(cursor);
  }
  throw new Error("列表页数超过客户端安全上限");
}
