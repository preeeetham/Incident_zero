export class ApiError extends Error {}

export async function get<T>(path: string): Promise<T> {
  const r = await fetch(path, { headers: { Accept: "application/json" } });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new ApiError((body as { error?: string }).error ?? `${r.status}`);
  return body as T;
}

export async function post<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const out = await r.json().catch(() => ({}));
  if (!r.ok) throw new ApiError((out as { error?: string }).error ?? `${r.status}`);
  return out as T;
}
