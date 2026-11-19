import { setupServer } from "msw/node";

/** MSW intercepts real `fetch` calls, so tests exercise the genuine typed API client. */
export const server = setupServer();
