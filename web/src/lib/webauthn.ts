// Thin wrappers over @simplewebauthn/browser with human-readable errors.
// In mock mode the browser prompt is simulated so the flow is demoable without a backend
// (a real passkey cannot sign a challenge the mock server invented).

import {
  browserSupportsWebAuthn,
  startAuthentication,
  startRegistration,
  type AuthenticationResponseJSON,
  type PublicKeyCredentialCreationOptionsJSON,
  type PublicKeyCredentialRequestOptionsJSON,
  type RegistrationResponseJSON,
} from "@simplewebauthn/browser";
import { IS_MOCK } from "./env";

export class PasskeyError extends Error {
  readonly code: string;
  constructor(code: string, message: string) {
    super(message);
    this.name = "PasskeyError";
    this.code = code;
  }
}

export function passkeysSupported(): boolean {
  if (IS_MOCK) return true;
  try {
    return browserSupportsWebAuthn();
  } catch {
    return false;
  }
}

function mapError(e: unknown, action: "sign" | "register" | "login"): PasskeyError {
  const err = e as { name?: string; code?: string; message?: string; cause?: { name?: string } };
  // SimpleWebAuthn wraps browser errors; surface the browser's own error name when it does.
  const passthrough = !err?.code || err.code === "ERROR_PASSTHROUGH_SEE_CAUSE_PROPERTY";
  const code = passthrough ? (err?.cause?.name ?? err?.name ?? "PASSKEY_ERROR") : err.code!;
  const verb = action === "sign" ? "Nothing was signed" : action === "login" ? "You are not signed in" : "No passkey was created";
  switch (err?.code) {
    case "ERROR_CEREMONY_ABORTED":
      return new PasskeyError(code, `The passkey request was cancelled. ${verb}.`);
    case "ERROR_INVALID_DOMAIN":
    case "ERROR_INVALID_RP_ID":
      return new PasskeyError(
        code,
        "This page's address does not match the passkey's relying party. Open the app at http://localhost:3000.",
      );
    case "ERROR_AUTHENTICATOR_PREVIOUSLY_REGISTERED":
      return new PasskeyError(code, "This device already has a Countersign passkey registered.");
    case "ERROR_AUTHENTICATOR_NO_SUPPORTED_PUBKEYCREDPARAMS_ALG":
      return new PasskeyError(code, "This authenticator does not support the required signature algorithm.");
    default:
      break;
  }
  switch (err?.name) {
    case "NotAllowedError":
      return new PasskeyError(
        code,
        action === "sign"
          ? `The passkey prompt was dismissed, timed out, or this device has no matching passkey. ${verb}. Try again, register this device's passkey, or use your app PIN.`
          : action === "login"
            ? "The passkey prompt was dismissed or timed out, or this device has no Countersign passkey yet. Sign in with your email and password instead."
            : `The passkey prompt was dismissed or timed out. ${verb}. Try again.`,
      );
    case "InvalidStateError":
      return new PasskeyError(
        code,
        action === "register"
          ? "This device already has a Countersign passkey registered."
          : action === "login"
            ? "This device has no Countersign passkey. Sign in with your email and password, then register one."
            : "This device has no passkey for this account. Register it first, or use your app PIN.",
      );
    case "SecurityError":
      return new PasskeyError(code, "Passkeys need a secure origin. Use https or http://localhost.");
    case "AbortError":
      return new PasskeyError(code, `The passkey request was interrupted. ${verb}.`);
    case "NotSupportedError":
      return new PasskeyError(
        code,
        action === "login"
          ? "This browser or device cannot use passkeys. Sign in with your email and password."
          : "This browser or device cannot use passkeys. Use your app PIN or authenticator code.",
      );
    default:
      return new PasskeyError(code, err?.message ? `Passkey error: ${err.message}` : `The passkey step failed. ${verb}.`);
  }
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function b64url(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomB64(n: number): string {
  const a = new Uint8Array(n);
  crypto.getRandomValues(a);
  return b64url(a);
}

/** Ask the platform authenticator to sign the approval challenge (the payload hash). */
export async function signWithPasskey(
  optionsJSON: PublicKeyCredentialRequestOptionsJSON,
): Promise<AuthenticationResponseJSON> {
  if (IS_MOCK) {
    await sleep(1300);
    const id = optionsJSON.allowCredentials?.[0]?.id ?? randomB64(16);
    return {
      id,
      rawId: id,
      type: "public-key",
      clientExtensionResults: {},
      authenticatorAttachment: "platform",
      response: {
        clientDataJSON: b64url(
          new TextEncoder().encode(
            JSON.stringify({ type: "webauthn.get", challenge: optionsJSON.challenge, origin: location.origin }),
          ),
        ),
        authenticatorData: randomB64(37),
        signature: randomB64(64),
        userHandle: randomB64(16),
      },
    };
  }
  if (!passkeysSupported()) {
    throw new PasskeyError(
      "UNSUPPORTED",
      "This browser does not support passkeys. Use your app PIN or authenticator code instead.",
    );
  }
  try {
    return await startAuthentication({ optionsJSON });
  } catch (e) {
    throw mapError(e, "sign");
  }
}

/**
 * Sign in with a discoverable passkey (no username typed). This only proves who you are;
 * it is never a trade approval, which always needs a fresh countersignature.
 */
export async function loginWithPasskey(
  optionsJSON: PublicKeyCredentialRequestOptionsJSON,
): Promise<AuthenticationResponseJSON> {
  if (IS_MOCK) return signWithPasskey(optionsJSON);
  if (!passkeysSupported()) {
    throw new PasskeyError("UNSUPPORTED", "This browser does not support passkeys. Sign in with your email and password.");
  }
  try {
    return await startAuthentication({ optionsJSON });
  } catch (e) {
    throw mapError(e, "login");
  }
}

/** Create a passkey on this device for countersigning. */
export async function createPasskey(
  optionsJSON: PublicKeyCredentialCreationOptionsJSON,
): Promise<RegistrationResponseJSON> {
  if (IS_MOCK) {
    await sleep(1100);
    const id = randomB64(16);
    return {
      id,
      rawId: id,
      type: "public-key",
      clientExtensionResults: {},
      authenticatorAttachment: "platform",
      response: {
        clientDataJSON: randomB64(64),
        attestationObject: randomB64(96),
        transports: ["internal", "hybrid"],
      },
    };
  }
  if (!passkeysSupported()) {
    throw new PasskeyError("UNSUPPORTED", "This browser does not support passkeys. Set an app PIN instead.");
  }
  try {
    return await startRegistration({ optionsJSON });
  } catch (e) {
    throw mapError(e, "register");
  }
}

/** Suggest a device label from the user agent (no fingerprinting, just a friendly name). */
export function suggestDeviceLabel(): string {
  if (typeof navigator === "undefined") return "This device";
  const ua = navigator.userAgent;
  const os = /Windows/.test(ua)
    ? "Windows"
    : /iPhone|iPad/.test(ua)
      ? "iPhone"
      : /Mac OS X/.test(ua)
        ? "Mac"
        : /Android/.test(ua)
          ? "Android"
          : /Linux/.test(ua)
            ? "Linux"
            : "Device";
  const browser = /Edg\//.test(ua)
    ? "Edge"
    : /Chrome\//.test(ua)
      ? "Chrome"
      : /Firefox\//.test(ua)
        ? "Firefox"
        : /Safari\//.test(ua)
          ? "Safari"
          : "Browser";
  return `${os} ${browser}`;
}
