import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { createApiClient } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { server } from "@/test/server";

import { SendingSettings } from "./sending-settings";

type Slot = components["schemas"]["ProfileSlot"];
type Policy = components["schemas"]["PolicyOut"];

const BASE = "http://app.test/bff";
const SETTINGS = `${BASE}/api/v1/settings`;

const emptySlots = (): Slot[] => [
  { mode: "nexora", profile: null, missing: ["the sender's name", "the sender's postal address"] },
  { mode: "job_hunter", profile: null, missing: ["the sender's name"] },
];

const readySlot = (): Slot => ({
  mode: "nexora",
  profile: {
    display_name: "Alex Founder",
    postal_address: "1 Example Street",
    reply_to: null,
    signature_text: null,
    website: null,
    updated_at: "2026-11-25T10:00:00Z",
  },
  missing: [],
});

const policy = (overrides: Partial<Policy> = {}): Policy => ({
  country: "DE",
  cold_b2b_marketing: "requires_consent",
  cold_to_individuals: "requires_consent",
  requires_unsubscribe: true,
  requires_postal_address: true,
  requires_sender_identification: true,
  notes: null,
  reviewed_on: null,
  version: 1,
  ...overrides,
});

const profiles = (slots: Slot[]) =>
  http.get(`${SETTINGS}/sender-profiles`, () => HttpResponse.json({ profiles: slots }));
const policies = (rows: Policy[]) =>
  http.get(`${SETTINGS}/policies`, () =>
    HttpResponse.json({ policies: rows, unreviewed: rows.filter((r) => !r.reviewed_on).length }),
  );

function renderPage() {
  const queries = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={queries}>
      <SendingSettings api={createApiClient({ baseUrl: BASE })} />
    </QueryClientProvider>,
  );
}

describe("<SendingSettings /> — sender identity", () => {
  it("says what is still missing for each mode", async () => {
    server.use(profiles(emptySlots()), policies([]));
    renderPage();
    const nexora = await screen.findByLabelText("NEXORA Sales");
    expect(within(nexora).getByText("Not ready")).toBeInTheDocument();
    expect(within(nexora).getByText(/the sender's name, the sender's postal address/)).toBeInTheDocument();
    expect(within(screen.getByLabelText("Job Hunter")).getByText(/the sender's name\./)).toBeInTheDocument();
  });

  it("shows a complete profile as ready, with its stored values in the form", async () => {
    server.use(profiles([readySlot(), emptySlots()[1]]), policies([]));
    renderPage();
    const nexora = await screen.findByLabelText("NEXORA Sales");
    expect(within(nexora).getByText("Ready to send")).toBeInTheDocument();
    expect(within(nexora).getByLabelText("Your name as it appears on the e-mail")).toHaveValue(
      "Alex Founder",
    );
    expect(within(nexora).getByLabelText("Postal address")).toHaveValue("1 Example Street");
  });

  it("saves what was typed, with empty optional fields sent as null", async () => {
    let sent: unknown;
    server.use(
      profiles(emptySlots()),
      policies([]),
      http.put(`${SETTINGS}/sender-profiles/nexora`, async ({ request }) => {
        sent = await request.json();
        return HttpResponse.json(readySlot());
      }),
    );
    renderPage();
    const nexora = await screen.findByLabelText("NEXORA Sales");
    const user = userEvent.setup();
    await user.type(within(nexora).getByLabelText("Your name as it appears on the e-mail"), "Alex Founder");
    await user.type(within(nexora).getByLabelText("Postal address"), "1 Example Street");
    await user.click(within(nexora).getByRole("button", { name: /save the nexora sales sender identity/i }));

    await waitFor(() => expect(sent).toBeDefined());
    expect(sent).toEqual({
      display_name: "Alex Founder",
      postal_address: "1 Example Street",
      reply_to: null,
      website: null,
      signature_text: null,
    });
    expect(await within(nexora).findByText("Saved")).toBeInTheDocument();
  });

  it("keeps its confirmation when the list refreshes after a save, and shows what the server normalised", async () => {
    let stored: Slot = emptySlots()[0];
    server.use(
      http.get(`${SETTINGS}/sender-profiles`, () =>
        HttpResponse.json({ profiles: [stored, emptySlots()[1]] }),
      ),
      policies([]),
      http.put(`${SETTINGS}/sender-profiles/nexora`, () => {
        // What the real server does: stores a canonical website and stamps a new update time.
        stored = {
          mode: "nexora",
          missing: [],
          profile: {
            ...readySlot().profile!,
            website: "https://example.com",
            updated_at: "2026-11-25T11:00:00Z",
          },
        };
        return HttpResponse.json(stored);
      }),
    );
    renderPage();
    const nexora = await screen.findByLabelText("NEXORA Sales");
    const user = userEvent.setup();
    await user.type(within(nexora).getByLabelText("Your name as it appears on the e-mail"), "Alex Founder");
    await user.type(within(nexora).getByLabelText("Postal address"), "1 Example Street");
    await user.type(within(nexora).getByLabelText("Website (optional)"), "https://www.Example.com/about");
    await user.click(within(nexora).getByRole("button", { name: /save the nexora sales/i }));

    expect(await within(nexora).findByText("Ready to send")).toBeInTheDocument(); // the refreshed list has arrived
    expect(within(nexora).getByText("Saved")).toBeInTheDocument(); // …and the confirmation survived it
    expect(within(nexora).getByLabelText("Website (optional)")).toHaveValue("https://example.com");
  });

  it("puts the server's complaint next to the field it is about", async () => {
    server.use(
      profiles(emptySlots()),
      policies([]),
      http.put(`${SETTINGS}/sender-profiles/nexora`, () =>
        HttpResponse.json(
          {
            error: {
              code: "validation_failed",
              message: "reply_to: this is not a valid e-mail address",
              details: { field: "reply_to" },
            },
          },
          { status: 422 },
        ),
      ),
    );
    renderPage();
    const nexora = await screen.findByLabelText("NEXORA Sales");
    const user = userEvent.setup();
    await user.type(within(nexora).getByLabelText("Your name as it appears on the e-mail"), "Alex Founder");
    await user.type(within(nexora).getByLabelText("Reply-to address (optional)"), "nope");
    await user.click(within(nexora).getByRole("button", { name: /save the nexora sales/i }));

    const alert = await within(nexora).findByRole("alert");
    expect(alert).toHaveTextContent("not a valid e-mail address");
    expect(within(nexora).getByLabelText("Reply-to address (optional)")).toHaveAttribute(
      "aria-invalid",
      "true",
    );
    expect(within(nexora).queryByText("Saved")).not.toBeInTheDocument();
  });

  it("renders hostile stored text as inert text", async () => {
    const hostile = '<img src=x onerror="window.__pwned=true">';
    const slot = readySlot();
    slot.profile = { ...slot.profile!, display_name: hostile };
    server.use(profiles([slot, emptySlots()[1]]), policies([]));
    const { container } = renderPage();
    const nexora = await screen.findByLabelText("NEXORA Sales");
    expect(within(nexora).getByLabelText("Your name as it appears on the e-mail")).toHaveValue(hostile);
    expect(container.querySelector("img")).toBeNull();
  });

  it("tells the owner when the identity could not be loaded", async () => {
    server.use(
      http.get(`${SETTINGS}/sender-profiles`, () => HttpResponse.json({}, { status: 500 })),
      policies([]),
    );
    renderPage();
    expect(await screen.findByText(/could not load the sender identity/i)).toBeInTheDocument();
  });
});

describe("<SendingSettings /> — country rules", () => {
  it("counts the countries that have not been reviewed", async () => {
    server.use(
      profiles(emptySlots()),
      policies([policy(), policy({ country: "FR" }), policy({ country: "US", reviewed_on: "2026-11-25" })]),
    );
    renderPage();
    expect(await screen.findByText(/2 of 3 countries still use the built-in default/i)).toBeInTheDocument();
  });

  it("names the fallback row plainly", async () => {
    server.use(profiles(emptySlots()), policies([policy({ country: "ZZ" })]));
    renderPage();
    expect(await screen.findByRole("rowheader", { name: "Everywhere else" })).toBeInTheDocument();
  });

  it("filters by country code and by unreviewed", async () => {
    server.use(
      profiles(emptySlots()),
      policies([policy(), policy({ country: "FR" }), policy({ country: "US", reviewed_on: "2026-11-25" })]),
    );
    renderPage();
    await screen.findByRole("rowheader", { name: "DE" });
    const user = userEvent.setup();

    await user.type(screen.getByLabelText("Country"), "fr");
    expect(screen.queryByRole("rowheader", { name: "DE" })).not.toBeInTheDocument();
    expect(screen.getByRole("rowheader", { name: "FR" })).toBeInTheDocument();

    await user.clear(screen.getByLabelText("Country"));
    await user.click(screen.getByLabelText(/only countries i have not reviewed/i));
    expect(screen.queryByRole("rowheader", { name: "US" })).not.toBeInTheDocument();
    expect(screen.getByRole("rowheader", { name: "DE" })).toBeInTheDocument();
  });

  it("saves a tightening straight away and marks the country reviewed", async () => {
    let sent: unknown;
    server.use(
      profiles(emptySlots()),
      policies([policy({ country: "US", cold_b2b_marketing: "allowed", cold_to_individuals: "allowed" })]),
      http.patch(`${SETTINGS}/policies/US`, async ({ request }) => {
        sent = await request.json();
        return HttpResponse.json(
          policy({
            country: "US",
            cold_to_individuals: "requires_consent",
            reviewed_on: "2026-11-25",
            version: 2,
          }),
        );
      }),
    );
    renderPage();
    await screen.findByRole("rowheader", { name: "US" });
    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText("Individuals for US"), "requires_consent");
    await user.click(screen.getByRole("button", { name: "Save and mark US reviewed" }));

    await waitFor(() => expect(sent).toBeDefined());
    expect(sent).toMatchObject({
      cold_to_individuals: "requires_consent",
      cold_b2b_marketing: "allowed",
      mark_reviewed: true,
    });
    expect(sent).toMatchObject({ acknowledge_loosening: false }); // a tightening needs no acknowledgement
    expect(await screen.findByText("Saved the policy for US.")).toBeInTheDocument();
  });

  it("asks before loosening, saves nothing if declined, and sends the acknowledgement if confirmed", async () => {
    const requests: Record<string, unknown>[] = [];
    server.use(
      profiles(emptySlots()),
      policies([policy()]),
      http.patch(`${SETTINGS}/policies/DE`, async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        requests.push(body);
        if (!body.acknowledge_loosening) {
          return HttpResponse.json(
            {
              error: {
                code: "validation_failed",
                message: "This change makes sending easier.",
                details: { loosens: ["cold_b2b_marketing"] },
              },
            },
            { status: 422 },
          );
        }
        return HttpResponse.json(
          policy({ cold_b2b_marketing: "allowed", reviewed_on: "2026-11-25", version: 2 }),
        );
      }),
    );
    renderPage();
    await screen.findByRole("rowheader", { name: "DE" });
    const user = userEvent.setup();

    await user.selectOptions(screen.getByLabelText("Companies for DE"), "allowed");
    await user.click(screen.getByRole("button", { name: "Save and mark DE reviewed" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText(/relaxing cold e-mail to companies for DE/i)).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: /keep it as it was/i }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(requests).toHaveLength(1);

    await user.click(screen.getByRole("button", { name: "Save and mark DE reviewed" }));
    await user.click(
      within(await screen.findByRole("alertdialog")).getByRole("button", { name: /i understand, relax it/i }),
    );
    await waitFor(() => expect(requests).toHaveLength(3));
    expect(requests[2]).toMatchObject({ cold_b2b_marketing: "allowed", acknowledge_loosening: true });
    expect(await screen.findByText("Saved the policy for DE.")).toBeInTheDocument();
  });

  it("shows the server's message when a save fails for another reason", async () => {
    server.use(
      profiles(emptySlots()),
      policies([policy()]),
      http.patch(`${SETTINGS}/policies/DE`, () =>
        HttpResponse.json(
          { error: { code: "not_found", message: "There is no policy for that country." } },
          { status: 404 },
        ),
      ),
    );
    renderPage();
    await screen.findByRole("rowheader", { name: "DE" });
    await userEvent.setup().click(screen.getByRole("button", { name: "Save and mark DE reviewed" }));
    expect(await screen.findByText("There is no policy for that country.")).toBeInTheDocument();
  });

  it("never offers to switch off naming the sender", async () => {
    server.use(profiles(emptySlots()), policies([policy()]));
    renderPage();
    await screen.findByRole("rowheader", { name: "DE" });
    expect(screen.queryByLabelText(/sender identification|name the sender/i)).not.toBeInTheDocument();
  });

  it("tells the owner when the policies could not be loaded", async () => {
    server.use(
      profiles(emptySlots()),
      http.get(`${SETTINGS}/policies`, () => HttpResponse.json({}, { status: 500 })),
    );
    renderPage();
    expect(await screen.findByText(/could not load the country policies/i)).toBeInTheDocument();
  });
});
