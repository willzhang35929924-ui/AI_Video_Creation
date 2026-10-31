import { describe, expect, it, vi } from "vitest";
import { ApiError, request } from "./api";

describe("API error handling", () => {
  it("extracts the structured backend error without leaking an object string", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 409,
        headers: new Headers({ "content-type": "application/json" }),
        json: async () => ({
          error: {
            code: "script_not_approved",
            message: "脚本审核通过后才能审核分镜",
            details: { project_id: "project-1" },
          },
        }),
      }),
    );

    await expect(request("/api/test")).rejects.toMatchObject({
      name: "ApiError",
      status: 409,
      code: "script_not_approved",
      message: "脚本审核通过后才能审核分镜",
      details: { project_id: "project-1" },
    } satisfies Partial<ApiError>);
  });

  it("reports connection failures as a network error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("connection refused")));

    await expect(request("/api/health")).rejects.toMatchObject({
      status: 0,
      code: "NETWORK_ERROR",
    });
  });

  it("surfaces the server request ID when an unexpected error occurs", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 500,
        headers: new Headers({ "content-type": "application/json" }),
        json: async () => ({ error: { code: "internal_error", message: "服务器内部错误", request_id: "req-123" } }),
      }),
    );

    await expect(request("/api/test")).rejects.toMatchObject({
      status: 500,
      code: "internal_error",
      requestId: "req-123",
      message: "服务器内部错误（请求编号：req-123）",
    } satisfies Partial<ApiError>);
  });
});
