import { z } from "zod";

// App-agnostic typed fetch wrapper with a zod-validated response boundary.
//
// The base URL, the token accessor and the 401 handling are injected through
// `createApiClient`, so this module knows no backend. Each API module builds its own
// typed endpoint object on top of the returned `request`.

// 204 No Content endpoints carry no body — z.undefined() validates the
// undefined we synthesize on that branch instead of attempting to parse JSON.
export const VoidSchema = z.undefined() as unknown as z.ZodType<void>;

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

// A bound `request<T>` — what an app's endpoint functions call.
export type RequestFn = <T>(path: string, schema: z.ZodType<T>, init?: RequestInit) => Promise<T>;

export interface ApiClientConfig {
  // Base URL every path is appended to, e.g. "http://localhost:8000". Required;
  // the caller validates its own env var presence before constructing.
  baseUrl: string;
  // Returns the bearer token to attach, or null to omit the Authorization
  // header. Injected so the token store (and its key) stays app-local.
  getToken?: () => string | null;
  // Invoked once when a response is 401, before ApiError(401) is thrown. The
  // app decides what "logged out" means (clear token, redirect, best-effort
  // cookie clear). No-op by default — studio-client v1 has no auth.
  onUnauthorized?: () => void;
}

export interface ApiClient {
  request: RequestFn;
}

export function createApiClient(config: ApiClientConfig): ApiClient {
  const { baseUrl, getToken, onUnauthorized } = config;

  async function request<T>(
    path: string,
    schema: z.ZodType<T>,
    init: RequestInit = {},
  ): Promise<T> {
    const token = getToken?.() ?? null;
    const headers = new Headers(init.headers);
    headers.set("Content-Type", "application/json");
    // Bearer header attached only when a token is present. New logins (where the
    // app uses cookie auth) leave it off and rely on credentials: "include".
    if (token !== null) {
      headers.set("Authorization", `Bearer ${token}`);
    }

    const response = await fetch(`${baseUrl}${path}`, {
      ...init,
      headers,
      credentials: "include",
    });

    if (response.status === 401) {
      onUnauthorized?.();
      throw new ApiError(401, "Unauthorized");
    }

    if (!response.ok) {
      const detail = await response.text();
      throw new ApiError(response.status, detail || response.statusText);
    }

    if (response.status === 204) {
      return schema.parse(undefined);
    }

    return schema.parse(await response.json());
  }

  return { request };
}
