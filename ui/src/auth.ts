
const domain = import.meta.env.VITE_COGNITO_DOMAIN;
const clientId = import.meta.env.VITE_COGNITO_CLIENT_ID;

const redirectUri =
  import.meta.env.VITE_REDIRECT_URI ||
  window.location.origin + "/";

export const cognitoEnabled = Boolean(
  domain && clientId
);

const ACCESS_KEY = "ledgerguard_access_token";
const EXPIRY_KEY = "ledgerguard_access_expiry";

function base64url(bytes: Uint8Array): string {
  let binary = "";

  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }

  return btoa(binary)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

function randomValue(): string {
  return base64url(
    crypto.getRandomValues(new Uint8Array(32))
  );
}

async function challengeFor(
  verifier: string
): Promise<string> {
  const bytes = new TextEncoder().encode(verifier);

  const digest = await crypto.subtle.digest(
    "SHA-256",
    bytes
  );

  return base64url(new Uint8Array(digest));
}

export async function login(): Promise<void> {
  if (!cognitoEnabled) return;

  const verifier = randomValue();
  const state = randomValue();

  sessionStorage.setItem(
    "ledgerguard_pkce_verifier",
    verifier
  );

  sessionStorage.setItem(
    "ledgerguard_oauth_state",
    state
  );

  const challenge = await challengeFor(verifier);

  const url = new URL(
    `https://${domain}/oauth2/authorize`
  );

  url.searchParams.set("client_id", clientId);
  url.searchParams.set("response_type", "code");
  url.searchParams.set("scope", "openid email profile");
  url.searchParams.set("redirect_uri", redirectUri);
  url.searchParams.set("code_challenge", challenge);
  url.searchParams.set(
    "code_challenge_method",
    "S256"
  );
  url.searchParams.set("state", state);

  window.location.assign(url.toString());
}

export async function initializeAuth(): Promise<string> {
  if (!cognitoEnabled) return "";

  const url = new URL(window.location.href);

  const code = url.searchParams.get("code");
  const state = url.searchParams.get("state");

  if (code) {
    const expectedState = sessionStorage.getItem(
      "ledgerguard_oauth_state"
    );

    const verifier = sessionStorage.getItem(
      "ledgerguard_pkce_verifier"
    );

    if (!state || !expectedState || state !== expectedState) {
      throw new Error(
        "OAuth state verification failed"
      );
    }

    if (!verifier) {
      throw new Error(
        "PKCE verifier missing"
      );
    }

    const body = new URLSearchParams({
      grant_type: "authorization_code",
      client_id: clientId,
      code,
      redirect_uri: redirectUri,
      code_verifier: verifier
    });

    const response = await fetch(
      `https://${domain}/oauth2/token`,
      {
        method: "POST",
        headers: {
          "Content-Type":
            "application/x-www-form-urlencoded"
        },
        body
      }
    );

    if (!response.ok) {
      throw new Error(
        `Cognito token exchange failed: ${response.status}`
      );
    }

    const tokens = await response.json();

    if (!tokens.access_token) {
      throw new Error(
        "Cognito did not return an access token"
      );
    }

    sessionStorage.setItem(
      ACCESS_KEY,
      tokens.access_token
    );

    const expiresAt =
      Date.now() + Number(tokens.expires_in || 3600) * 1000;

    sessionStorage.setItem(
      EXPIRY_KEY,
      String(expiresAt)
    );

    sessionStorage.removeItem(
      "ledgerguard_pkce_verifier"
    );

    sessionStorage.removeItem(
      "ledgerguard_oauth_state"
    );

    url.searchParams.delete("code");
    url.searchParams.delete("state");

    window.history.replaceState(
      {},
      "",
      url.pathname + url.search + url.hash
    );
  }

  const token = sessionStorage.getItem(ACCESS_KEY);
  const expiry = Number(
    sessionStorage.getItem(EXPIRY_KEY) || 0
  );

  if (!token || Date.now() >= expiry - 30000) {
    sessionStorage.removeItem(ACCESS_KEY);
    sessionStorage.removeItem(EXPIRY_KEY);
    return "";
  }

  return token;
}

export function logout(): void {
  sessionStorage.removeItem(ACCESS_KEY);
  sessionStorage.removeItem(EXPIRY_KEY);

  if (!cognitoEnabled) {
    window.location.reload();
    return;
  }

  const url = new URL(
    `https://${domain}/logout`
  );

  url.searchParams.set("client_id", clientId);
  url.searchParams.set("logout_uri", redirectUri);

  window.location.assign(url.toString());
}
