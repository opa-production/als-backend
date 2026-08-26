/*
 * Session storage.
 *
 * `localStorage`, not a cookie, because the console and the API are on
 * different origins and the token goes out in an `Authorization` header
 * anyway. Everything here is deliberately dumb: reading and writing three
 * keys, with no expiry logic of its own.
 *
 * That last part matters. The only authority on whether a token is still good
 * is the server, which checks it on every request — a client-side expiry guess
 * would either sign people out early or, worse, keep showing them a console
 * whose every call is about to 401.
 */

const TOKEN_KEY = "als.admin.token";
const REFRESH_KEY = "als.admin.refresh";
const ADMIN_KEY = "als.admin.who";

/** Private windows and locked-down browsers throw on access, not on write. */
function safeRead(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function safeWrite(key, value) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    // A session that lasts only as long as the tab is still a usable session.
  }
}

export function getToken() {
  return safeRead(TOKEN_KEY);
}

export function setToken(accessToken, refreshToken) {
  safeWrite(TOKEN_KEY, accessToken);
  if (refreshToken) safeWrite(REFRESH_KEY, refreshToken);
}

export function getStoredAdmin() {
  const raw = safeRead(ADMIN_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

export function setStoredAdmin(admin) {
  safeWrite(ADMIN_KEY, admin ? JSON.stringify(admin) : null);
}

export function clearSession() {
  safeWrite(TOKEN_KEY, null);
  safeWrite(REFRESH_KEY, null);
  safeWrite(ADMIN_KEY, null);
}

/**
 * Ranked, matching `ROLE_RANK` in `app/api/deps.py`.
 *
 * This gates what the console *offers*, never what it permits — the server
 * checks the same ranks on every request. Hiding a button the API would refuse
 * is a courtesy; it is not the enforcement, and treating it as such is how
 * admin tools end up with a permission model that exists only in the browser.
 */
const RANK = { support: 1, admin: 2, owner: 3 };

export function hasRole(admin, minimum) {
  return (RANK[admin?.role] ?? 0) >= RANK[minimum];
}
