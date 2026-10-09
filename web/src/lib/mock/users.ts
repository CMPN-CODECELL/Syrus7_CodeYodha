// Mock accounts (NEXT_PUBLIC_MOCK=1). Kept in localStorage so a sign-up survives reloads and
// tabs, like a real account would. Passwords are stored in plain text because this is an
// offline fixture for the UI only; the real backend hashes them.

import type { Device } from "../types";

export interface MockUser {
  id: string;
  email: string;
  display_name: string;
  password: string;
  account_id: string;
  created_at: string;
  pin: string | null;
  has_totp: boolean;
  devices: Device[];
  pin_fails: number;
  pin_locked_until: number | null;
}

const USERS_KEY = "cs_mock_users_v1";
// Which mock user this browser's simulated authenticator holds a passkey for.
const PASSKEY_OWNER_KEY = "cs_mock_passkey_owner";

export function loadUsers(): MockUser[] {
  try {
    const raw = typeof window === "undefined" ? null : localStorage.getItem(USERS_KEY);
    return raw ? (JSON.parse(raw) as MockUser[]) : [];
  } catch {
    return [];
  }
}

function saveUsers(list: MockUser[]) {
  try {
    localStorage.setItem(USERS_KEY, JSON.stringify(list));
  } catch {
    // storage unavailable: accounts last only for this page load
  }
}

export function findUserByEmail(email: string): MockUser | undefined {
  const e = email.trim().toLowerCase();
  return loadUsers().find((u) => u.email.toLowerCase() === e);
}

export function findUser(id: string | null | undefined): MockUser | undefined {
  return id ? loadUsers().find((u) => u.id === id) : undefined;
}

export function upsertUser(u: MockUser) {
  const list = loadUsers().filter((x) => x.id !== u.id);
  saveUsers([...list, u]);
}

export function passkeyOwner(): string | null {
  try {
    return localStorage.getItem(PASSKEY_OWNER_KEY);
  } catch {
    return null;
  }
}

export function setPasskeyOwner(userId: string) {
  try {
    localStorage.setItem(PASSKEY_OWNER_KEY, userId);
  } catch {
    // ignore
  }
}

const B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

/** Paper account id like the backend's: "PT" plus 6 base32 characters. */
export function newAccountId(): string {
  const bytes = new Uint8Array(6);
  crypto.getRandomValues(bytes);
  return `PT${[...bytes].map((b) => B32[b % 32]).join("")}`;
}

export function newUserId(): string {
  const bytes = new Uint8Array(6);
  crypto.getRandomValues(bytes);
  return `usr_${[...bytes].map((b) => b.toString(16).padStart(2, "0")).join("")}`;
}
