// Self-service 021 linking: the browser encrypts the UCC and password with the 021 gateway's RSA public key
// (RSA-OAEP, SHA-256), so the Countersign API only ever relays ciphertext. Nothing here logs or stores the password.

import type { Link021Key } from "./types";

/** A 021 client code (UCC): letters and digits, 4 to 16 characters, upper case. */
export const UCC_RE = /^[A-Z0-9]{4,16}$/;
export const LINK021_PASSWORD_MAX = 64;

/** Trimmed and upper-cased, as the gateway expects it. */
export function normalizeUcc(v: string): string {
  return v.trim().toUpperCase();
}

/** Why a UCC cannot be sent, or null when it looks valid. */
export function uccProblem(ucc: string): string | null {
  if (!ucc) return "Enter your 021 client code (UCC).";
  if (!/^[A-Z0-9]+$/.test(ucc)) return "The UCC has only letters and digits.";
  if (ucc.length < 4 || ucc.length > 16) return "The UCC is 4 to 16 characters long.";
  return null;
}

/** A problem found in the browser before anything is sent. */
export class Link021InputError extends Error {
  readonly code: string;
  constructor(code: string, message: string) {
    super(message);
    this.name = "Link021InputError";
    this.code = code;
  }
}

function base64ToBytes(b64: string): Uint8Array<ArrayBuffer> {
  const bin = atob(b64.trim());
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function bytesToBase64(bytes: Uint8Array): string {
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}

function subtle(): SubtleCrypto {
  const s = typeof globalThis.crypto !== "undefined" ? globalThis.crypto.subtle : undefined;
  if (!s) {
    throw new Link021InputError(
      "WEBCRYPTO_UNAVAILABLE",
      "This browser cannot encrypt your 021 password on this page. Open Countersign over https or on localhost and try again.",
    );
  }
  return s;
}

/**
 * Encrypts {ucc, password, nonce, user_id} for the 021 gateway and returns standard base64 ciphertext. Refuses in the
 * browser when the plaintext does not fit one RSA-OAEP block (a password that is too long).
 */
export async function encryptLink021(key: Link021Key, ucc: string, password: string): Promise<string> {
  const plain = new TextEncoder().encode(JSON.stringify({ ucc, password, nonce: key.nonce, user_id: key.user_id }));
  try {
    const max = key.max_plaintext_bytes > 0 ? key.max_plaintext_bytes : 190;
    if (plain.length > max) {
      throw new Link021InputError(
        "PASSWORD_TOO_LONG",
        "That password is too long to encrypt for the 021 gateway. Use a shorter 021 password and try again.",
      );
    }
    const s = subtle();
    let pub: CryptoKey;
    try {
      pub = await s.importKey("spki", base64ToBytes(key.public_key), { name: "RSA-OAEP", hash: "SHA-256" }, false, ["encrypt"]);
    } catch {
      throw new Link021InputError("LINK_KEY_INVALID", "The 021 gateway sent a key this browser cannot use. Try again in a moment.");
    }
    const ct = await s.encrypt({ name: "RSA-OAEP" }, pub, plain);
    return bytesToBase64(new Uint8Array(ct));
  } finally {
    // Overwrite the plaintext bytes once they are no longer needed.
    plain.fill(0);
  }
}

/** Exposed for the mock backend, which decrypts with its own key pair. */
export const link021Codec = { base64ToBytes, bytesToBase64 };
