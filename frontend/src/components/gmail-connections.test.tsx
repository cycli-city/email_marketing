import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { noticeFrom, type Notice } from "@/lib/gmail/notices";
import { server } from "@/test/server";

import { GmailConnections } from "./gmail-connections";

type Account = components["schemas"]["AccountOut"];

const BASE = "http://app.test/bff";
const URL_ = `${BASE}/api/v1/integrations/gmail`;

const account = (overrides: Partial<Account> = {}): Account => ({
  id: "11111111-1111-4111-8111-111111111111",
  email: "owner.mailbox@example.com",
  display_name: "Owner",
  mode: "job_hunter",
  status: "active",
  account_type: "personal_gmail",
  scopes: ["https://www.googleapis.com/auth/gmail.send"],
  connected_at: "2026-11-19T10:00:00Z",
  last_sync_at: null,
  last_error: null,
  ...overrides,
});

function renderScreen(props: { notice?: Notice | null; navigate?: (url: string) => void } = {}) {
  const queries = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={queries}>
      <GmailConnections api={createApiClient({ baseUrl: BASE })} {...props} />
    </QueryClientProvider>,
  );
}

const listing = (accounts: Account[]) => http.get(`${URL_}/accounts`, () => HttpResponse.json({ accounts }));

describe("<GmailConnections />", () => {
  it("shows both modes with an invitation to connect when nothing is connected", async () => {
    server.use(listing([]));
    renderScreen();
    expect(await screen.findByText("NEXORA Sales")).toBeInTheDocument();
    expect(screen.getByText("Job Hunter")).toBeInTheDocument();
    expect(screen.getAllByText("No mailbox connected yet.")).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: /connect a gmail mailbox for/i })).toHaveLength(2);
  });

  it("lists each mailbox under its own mode with its real status", async () => {
    server.use(
      listing([
        account(),
        account({
          id: "22222222-2222-4222-8222-222222222222",
          email: "founder@example.org",
          mode: "nexora",
          account_type: "google_workspace",
        }),
      ]),
    );
    renderScreen();
    const nexora = await screen.findByLabelText("NEXORA Sales");
    const jobs = screen.getByLabelText("Job Hunter");
    expect(within(nexora).getByText("founder@example.org")).toBeInTheDocument();
    expect(within(nexora).getByText(/Google Workspace/)).toBeInTheDocument();
    expect(within(jobs).getByText("owner.mailbox@example.com")).toBeInTheDocument();
    expect(within(jobs).getByText("Active")).toBeInTheDocument();
    expect(within(jobs).queryByText("founder@example.org")).not.toBeInTheDocument();
  });

  it("renders a hostile address as inert text", async () => {
    const hostile = '<img src=x onerror="window.__pwned=true"> evil@example.com';
    server.use(listing([account({ email: hostile })]));
    const { container } = renderScreen();
    expect(await screen.findByText(hostile)).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
  });

  it("starts the connection for the chosen mode and sends the browser to Google", async () => {
    const navigate = vi.fn();
    let sent: unknown;
    server.use(
      listing([]),
      http.post(`${URL_}/connect`, async ({ request }) => {
        sent = await request.json();
        return HttpResponse.json({
          authorization_url: "https://accounts.google.com/o/oauth2/v2/auth?state=abc",
        });
      }),
    );
    renderScreen({ navigate });
    await userEvent.click(
      await screen.findByRole("button", { name: /connect a gmail mailbox for nexora sales/i }),
    );
    await waitFor(() =>
      expect(navigate).toHaveBeenCalledWith("https://accounts.google.com/o/oauth2/v2/auth?state=abc"),
    );
    expect(sent).toEqual({ mode: "nexora" });
  });

  it("explains it when the connection cannot be started, and does not navigate", async () => {
    const navigate = vi.fn();
    server.use(
      listing([]),
      http.post(`${URL_}/connect`, () =>
        HttpResponse.json(
          { error: { code: "integration_not_configured", message: "Gmail is not configured" } },
          { status: 503 },
        ),
      ),
    );
    renderScreen({ navigate });
    await userEvent.click(
      await screen.findByRole("button", { name: /connect a gmail mailbox for job hunter/i }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("Gmail is not configured");
    expect(navigate).not.toHaveBeenCalled();
  });

  it("tests a connection and reports the outcome inline", async () => {
    server.use(
      listing([account()]),
      http.post(`${URL_}/accounts/${account().id}/test`, () =>
        HttpResponse.json({ ok: true, email: "owner.mailbox@example.com", messages_total: 1234 }),
      ),
    );
    renderScreen();
    await userEvent.click(
      await screen.findByRole("button", { name: /test the connection to owner.mailbox@example.com/i }),
    );
    expect(await screen.findByText("Working — reached owner.mailbox@example.com")).toBeInTheDocument();
  });

  it("shows why a connection test failed", async () => {
    server.use(
      listing([account()]),
      http.post(`${URL_}/accounts/${account().id}/test`, () =>
        HttpResponse.json(
          { error: { code: "google_unavailable", message: "Google is temporarily unavailable." } },
          { status: 503 },
        ),
      ),
    );
    renderScreen();
    await userEvent.click(await screen.findByRole("button", { name: /test the connection/i }));
    expect(await screen.findByText("Google is temporarily unavailable.")).toBeInTheDocument();
  });

  it("offers Reconnect (not Test) for a mailbox Google no longer accepts, and shows the reason", async () => {
    const navigate = vi.fn();
    server.use(
      listing([
        account({
          status: "needs_reauth",
          last_error: "Google no longer accepts this connection — reconnect the mailbox.",
        }),
      ]),
      http.post(`${URL_}/connect`, () =>
        HttpResponse.json({ authorization_url: "https://accounts.google.com/x" }),
      ),
    );
    renderScreen({ navigate });
    expect(await screen.findByText("Needs reconnecting")).toBeInTheDocument();
    expect(screen.getByText(/no longer accepts this connection/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /test the connection/i })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /reconnect owner.mailbox@example.com/i }));
    await waitFor(() => expect(navigate).toHaveBeenCalled());
  });

  it("asks for confirmation before disconnecting, and Keep it changes nothing", async () => {
    let deleted = false;
    server.use(
      listing([account()]),
      http.delete(`${URL_}/accounts/${account().id}`, () => {
        deleted = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderScreen();
    await userEvent.click(
      await screen.findByRole("button", { name: /disconnect owner.mailbox@example.com/i }),
    );
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText(/revokes Google's access/)).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Keep it" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(deleted).toBe(false);
  });

  it("disconnects after confirmation and refreshes the list", async () => {
    let accounts = [account()];
    server.use(
      http.get(`${URL_}/accounts`, () => HttpResponse.json({ accounts })),
      http.delete(`${URL_}/accounts/${account().id}`, () => {
        accounts = [];
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderScreen();
    await userEvent.click(
      await screen.findByRole("button", { name: /disconnect owner.mailbox@example.com/i }),
    );
    await userEvent.click(
      within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Disconnect" }),
    );
    await waitFor(() => expect(screen.queryByText("owner.mailbox@example.com")).not.toBeInTheDocument());
    expect(screen.getAllByText("No mailbox connected yet.")).toHaveLength(2);
  });

  it("explains what is missing when the server has no Google credentials", async () => {
    server.use(
      http.get(`${URL_}/accounts`, () =>
        HttpResponse.json({ error: { code: "integration_not_configured", message: "x" } }, { status: 503 }),
      ),
    );
    renderScreen();
    expect(await screen.findByText("Gmail is not configured on the server")).toBeInTheDocument();
    expect(screen.getByText(/no Google sign-in credentials or encryption key yet/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /connect a gmail mailbox/i })).not.toBeInTheDocument();
  });

  it("reports a failure to load without pretending there are no mailboxes", async () => {
    server.use(http.get(`${URL_}/accounts`, () => HttpResponse.json({}, { status: 500 })));
    renderScreen();
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not load the connected mailboxes/i);
    expect(screen.queryByText("No mailbox connected yet.")).not.toBeInTheDocument();
  });

  it("shows the result of the connection attempt that just finished", async () => {
    server.use(listing([]));
    renderScreen({ notice: noticeFrom({ connected: "nexora" }) });
    expect(await screen.findByRole("status")).toHaveTextContent(
      /nothing is sent until you approve a campaign/i,
    );
  });
});

describe("noticeFrom", () => {
  it("only ever selects from the fixed list and never displays the URL's own text", () => {
    expect(noticeFrom({ error: "<script>alert(1)</script>" })?.text).toBe(
      "Something went wrong while connecting. Please try again.",
    );
    expect(noticeFrom({ connected: "<img src=x>" })?.tone).toBe("success");
    expect(noticeFrom({ error: "scopes" })?.text).toMatch(/tick every box/);
    expect(noticeFrom({})).toBeNull();
  });
});
