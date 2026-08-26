/*
 * The one place the console talks to a server.
 *
 * Two backends behind one function: the seeded fixture in `mock/`, and the real
 * FastAPI service. `VITE_USE_MOCKS` picks which. The mock returns the same
 * shapes the service does, so nothing above this file knows or cares — which is
 * the point, because a console built against convenient fake shapes is a
 * console that has to be rewritten the day it meets the API.
 */

import { clearSession, getToken, setToken } from "./auth.js";

const BASE = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

/** Mocks are the default, so `npm run dev` works with no backend running. */
export const USING_MOCKS = import.meta.env.VITE_USE_MOCKS !== "false";

/**
 * The fixture is loaded lazily and only once.
 *
 * A static import would pull the whole simulation — every user, payment and
 * material — into the production bundle even with `VITE_USE_MOCKS=false`,
 * because a module that is imported is a module that gets shipped. Behind a
 * dynamic import it becomes a separate chunk that a live build never fetches.
 */
let mockModule = null;
async function loadMocks() {
  if (!mockModule) mockModule = import("./mock/server.js");
  return mockModule;
}

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

function toQueryString(query) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    // Empty strings are how a cleared <select> reports itself; sending them
    // would filter on "" and return nothing.
    if (value === undefined || value === null || value === "") continue;
    params.set(key, String(value));
  }
  const encoded = params.toString();
  return encoded ? `?${encoded}` : "";
}

/**
 * @param {string} path  Under `/api/v1/admin` — pass `/users`, not the whole URL.
 */
export async function request(path, { method = "GET", query = {}, body = null } = {}) {
  const token = getToken();

  if (USING_MOCKS) {
    const { mockRequest } = await loadMocks();
    try {
      return await mockRequest({ method, path, query, body, token });
    } catch (error) {
      throw new ApiError(error.message, error.status ?? 500);
    }
  }

  const response = await fetch(`${BASE}/api/v1/admin${path}${toQueryString(query)}`, {
    method,
    headers: {
      ...(body ? { "Content-Type": "application/json" } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });

  if (response.status === 401) {
    // The service refuses a stale or revoked token on every request, not only
    // at refresh. Holding onto it just means the next screen fails the same
    // way, so it goes now and the router sends them to sign in.
    clearSession();
    throw new ApiError("Your session has ended. Please sign in again.", 401);
  }

  if (!response.ok) {
    // Errors leave the API as `{ "message": ... }` — see app/core/errors.py.
    let message = `Request failed (${response.status}).`;
    try {
      const payload = await response.json();
      if (payload?.message) message = payload.message;
    } catch {
      // A non-JSON body means a proxy or a crash, not the app. The status is
      // all there is to report.
    }
    throw new ApiError(message, response.status);
  }

  if (response.status === 204) return null;
  return response.json();
}

export const api = {
  get: (path, query) => request(path, { query }),
  post: (path, body, query) => request(path, { method: "POST", body, query }),
  patch: (path, body) => request(path, { method: "PATCH", body }),
  del: (path, query) => request(path, { method: "DELETE", query }),
};

export async function signIn(email, password) {
  const tokens = await request("/auth/login", {
    method: "POST",
    body: { email, password },
  });
  setToken(tokens.access_token, tokens.refresh_token);
  return tokens.admin;
}

export async function signOut() {
  try {
    await request("/auth/logout", {
      method: "POST",
      body: { refresh_token: localStorage.getItem("als.admin.refresh") ?? "" },
    });
  } catch {
    // Signing out locally must succeed even when the call does not — otherwise
    // an expired session traps someone on a screen they cannot leave.
  }
  clearSession();
}
