export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

type AuthStatus = {
  required: boolean;
  authenticated: boolean;
  csrf_token: string | null;
};

let csrfToken: string | null = null;
let authStatusRequest: Promise<AuthStatus> | null = null;

function isUnsafeMethod(method?: string) {
  return !["GET", "HEAD", "OPTIONS"].includes((method ?? "GET").toUpperCase());
}

function loginUrl() {
  const returnTo = encodeURIComponent(window.location.href);
  return `${API_BASE_URL}/auth/login?return_to=${returnTo}`;
}

async function loadAuthStatus(): Promise<AuthStatus> {
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
    window.location.assign(loginUrl());
    throw new Error("Owner authentication required.");
  }
  return status;
}

export async function apiJson<T>(path: string, options?: RequestInit): Promise<T> {
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
    window.location.assign(loginUrl());
    throw new Error("Owner authentication required.");
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(body.detail ?? response.statusText);
  }
  return response.json() as Promise<T>;
}

export function websocketUrl(path: string) {
  const url = new URL(path, API_BASE_URL);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}
