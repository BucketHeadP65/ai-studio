import { HttpResponse, http } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";
import { z } from "zod";

import { server } from "@/test-support/test-server";

import { ApiError, createApiClient, VoidSchema } from "./client";

const API_URL = "http://core.test";

const BodySchema = z.object({ value: z.string() });

describe("createApiClient — request<T>", () => {
  afterEach(() => localStorage.clear());

  it("parses and returns the JSON body validated against the schema", async () => {
    const { request } = createApiClient({ baseUrl: API_URL });
    server.use(http.get(`${API_URL}/thing`, () => HttpResponse.json({ value: "ok" })));

    const result = await request("/thing", BodySchema);

    expect(result).toEqual({ value: "ok" });
  });

  it("attaches the injected token as a Bearer header", async () => {
    const { request } = createApiClient({ baseUrl: API_URL, getToken: () => "the-token" });
    let receivedAuth: string | null = null;
    server.use(
      http.get(`${API_URL}/thing`, ({ request }) => {
        receivedAuth = request.headers.get("authorization");
        return HttpResponse.json({ value: "ok" });
      }),
    );

    await request("/thing", BodySchema);

    expect(receivedAuth).toBe("Bearer the-token");
  });

  it("omits the auth header when no token getter is configured", async () => {
    const { request } = createApiClient({ baseUrl: API_URL });
    let receivedAuth: string | null = "not-set";
    server.use(
      http.get(`${API_URL}/thing`, ({ request }) => {
        receivedAuth = request.headers.get("authorization");
        return HttpResponse.json({ value: "ok" });
      }),
    );

    await request("/thing", BodySchema);

    expect(receivedAuth).toBeNull();
  });

  it("omits the auth header when the token getter returns null", async () => {
    const { request } = createApiClient({ baseUrl: API_URL, getToken: () => null });
    let receivedAuth: string | null = "not-set";
    server.use(
      http.get(`${API_URL}/thing`, ({ request }) => {
        receivedAuth = request.headers.get("authorization");
        return HttpResponse.json({ value: "ok" });
      }),
    );

    await request("/thing", BodySchema);

    expect(receivedAuth).toBeNull();
  });

  it("sends credentials: include so the auth cookie travels", async () => {
    const { request } = createApiClient({ baseUrl: API_URL });
    let credentials: RequestCredentials | undefined;
    server.use(
      http.get(`${API_URL}/thing`, ({ request }) => {
        credentials = request.credentials;
        return HttpResponse.json({ value: "ok" });
      }),
    );

    await request("/thing", BodySchema);

    expect(credentials).toBe("include");
  });

  it("invokes onUnauthorized and throws ApiError(401) on a 401", async () => {
    const onUnauthorized = vi.fn();
    const { request } = createApiClient({ baseUrl: API_URL, onUnauthorized });
    server.use(http.get(`${API_URL}/thing`, () => new HttpResponse(null, { status: 401 })));

    await expect(request("/thing", BodySchema)).rejects.toMatchObject({
      status: 401,
      name: "ApiError",
    });
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });

  it("does not require onUnauthorized to be configured to throw on 401", async () => {
    const { request } = createApiClient({ baseUrl: API_URL });
    server.use(http.get(`${API_URL}/thing`, () => new HttpResponse(null, { status: 401 })));

    await expect(request("/thing", BodySchema)).rejects.toBeInstanceOf(ApiError);
  });

  it("throws ApiError with the response body as the message on a non-2xx", async () => {
    const { request } = createApiClient({ baseUrl: API_URL });
    server.use(http.get(`${API_URL}/thing`, () => HttpResponse.text("offline", { status: 503 })));

    const error = await request("/thing", BodySchema).catch((e) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 503, message: "offline" });
  });

  it("falls back to the status text when the error body is empty", async () => {
    const { request } = createApiClient({ baseUrl: API_URL });
    server.use(http.get(`${API_URL}/thing`, () => new HttpResponse("", { status: 500 })));

    const error = await request("/thing", BodySchema).catch((e) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(500);
    expect((error as ApiError).message.length).toBeGreaterThan(0);
  });

  it("resolves to undefined on a 204 No Content", async () => {
    const { request } = createApiClient({ baseUrl: API_URL });
    server.use(http.delete(`${API_URL}/thing`, () => new HttpResponse(null, { status: 204 })));

    const result = await request("/thing", VoidSchema, { method: "DELETE" });

    expect(result).toBeUndefined();
  });

  it("surfaces a backend shape mismatch as a ZodError at the boundary", async () => {
    const { request } = createApiClient({ baseUrl: API_URL });
    server.use(http.get(`${API_URL}/thing`, () => HttpResponse.json({ value: 123 })));

    await expect(request("/thing", BodySchema)).rejects.toBeInstanceOf(z.ZodError);
  });

  it("threads the request body and method through to the backend", async () => {
    const { request } = createApiClient({ baseUrl: API_URL });
    let receivedBody: unknown = null;
    let receivedMethod: string | null = null;
    server.use(
      http.post(`${API_URL}/thing`, async ({ request }) => {
        receivedMethod = request.method;
        receivedBody = await request.json();
        return HttpResponse.json({ value: "ok" });
      }),
    );

    await request("/thing", BodySchema, {
      method: "POST",
      body: JSON.stringify({ a: 1 }),
    });

    expect(receivedMethod).toBe("POST");
    expect(receivedBody).toEqual({ a: 1 });
  });
});

describe("ApiError", () => {
  it("carries the http status alongside the message", () => {
    const err = new ApiError(418, "i'm a teapot");
    expect(err.status).toBe(418);
    expect(err.message).toBe("i'm a teapot");
    expect(err.name).toBe("ApiError");
    expect(err).toBeInstanceOf(Error);
  });
});
