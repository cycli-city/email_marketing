export interface Notice {
  tone: "success" | "error";
  text: string;
}

const ERRORS: Record<string, string> = {
  denied: "You chose not to allow access, so nothing was connected.",
  google: "Google reported a problem. Please try connecting again.",
  invalid: "That link was incomplete. Please start the connection again.",
  state: "That connection attempt expired, or it was not started from this session. Please start again.",
  exchange: "Google did not accept the approval. Please try connecting again.",
  scopes: "Gmail needs permission to both send and read mail. Connect again and tick every box.",
  no_refresh:
    "Google did not provide a long-lived connection. Remove this app at myaccount.google.com/permissions, then connect again.",
  already_connected: "That mailbox is already connected for the other mode. Disconnect it there first.",
  not_configured: "Gmail is not configured on the server yet.",
  unavailable: "Google or the backend could not be reached. Please try again shortly.",
  failed: "Something went wrong while connecting. Please try again.",
};

const CONNECTED = "Connected. The mailbox is ready — nothing is sent until you approve a campaign.";

/**
 * Turns the `?connected=` / `?error=` the OAuth callback redirects with into a message. Both values come from the URL
 * (attacker-influenced), so they only ever *select* from this fixed list — they are never displayed.
 */
export function noticeFrom(params: { connected?: string; error?: string }): Notice | null {
  if (params.error) {
    return { tone: "error", text: ERRORS[params.error] ?? ERRORS.failed };
  }
  if (params.connected) return { tone: "success", text: CONNECTED };
  return null;
}
