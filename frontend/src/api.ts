export const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL ?? (import.meta.env.PROD ? "/api" : "http://localhost:8000");
const AUTH_BASE_URL = import.meta.env.VITE_AUTH_BASE_URL ?? API_BASE_URL;

type AuthStatus = {
  required: boolean;
  authenticated: boolean;
  csrf_token: string | null;
};

let csrfToken: string | null = null;
let authStatusRequest: Promise<AuthStatus> | null = null;
let sessionBridgeRequest: Promise<void> | null = null;
let redirectingToLogin = false;

function isUnsafeMethod(method?: string) {
  return !["GET", "HEAD", "OPTIONS"].includes((method ?? "GET").toUpperCase());
}

function loginUrl() {
  const returnTo = encodeURIComponent(window.location.href);
  return `${AUTH_BASE_URL}/auth/login?return_to=${returnTo}`;
}

function beginLogin() {
  if (redirectingToLogin) return;
  redirectingToLogin = true;
  window.location.assign(loginUrl());
}

async function consumeSessionBridge() {
  const parameters = new URLSearchParams(window.location.hash.slice(1));
  const code = parameters.get("maestro_bridge");
  if (!code) return;
  if (!sessionBridgeRequest) {
    sessionBridgeRequest = fetch(`${API_BASE_URL}/auth/bridge`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    }).then(async (response) => {
      if (!response.ok) throw new Error("Unable to complete the Maestro owner session.");
      const result = await response.json() as { csrf_token?: string | null };
      csrfToken = result.csrf_token ?? null;
      const url = new URL(window.location.href);
      url.hash = "";
      window.history.replaceState({}, "", url);
    });
  }
  await sessionBridgeRequest;
}

async function loadAuthStatus(): Promise<AuthStatus> {
  await consumeSessionBridge();
  if (!authStatusRequest) {
    authStatusRequest = fetch(`${API_BASE_URL}/auth/status`, { credentials: "include" })
      .then(async (response) => {
        if (!response.ok) {
          throw new Error("Unable to verify the Maestro owner session.");
        }
        return response.json() as Promise<AuthStatus>;
      })
      .finally(() => {
        authStatusRequest = null;
      });
  }
  const status = await authStatusRequest;
  csrfToken = status.csrf_token;
  if (status.required && !status.authenticated) {
    beginLogin();
    throw new Error("Owner authentication required.");
  }
  return status;
}

export async function apiJson<T>(path: string, options?: RequestInit): Promise<T> {
  await consumeSessionBridge();
  const headers = new Headers(options?.headers);
  if (isUnsafeMethod(options?.method)) {
    if (!csrfToken) {
      await loadAuthStatus();
    }
    if (csrfToken) {
      headers.set("X-CSRF-Token", csrfToken);
    }
  }
  const response = await fetch(`${API_BASE_URL}${path}`, {
    credentials: "include",
    ...options,
    headers,
  });
  if (response.status === 401) {
    csrfToken = null;
    beginLogin();
    throw new Error("Owner authentication required.");
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(body.detail ?? response.statusText);
  }
  return response.json() as Promise<T>;
}
