# REQUIREMENTS ADDENDUM — Full Autonomy, Multi-Provider Orchestration and Intelligent Failover (verbatim)

> Source: the owner's second brief, saved verbatim on 2026-11-19. It **extends** `REQUIREMENTS.md` and is **mandatory**.
> Autonomy is a *core architectural requirement*, not an optional future feature.

---

# ADDITIONAL REQUIREMENTS: FULL AUTONOMY, MULTI-PROVIDER ORCHESTRATION AND INTELLIGENT FAILOVER

This section extends the original project requirements and is mandatory.

I want the application to operate as a fully autonomous background system. I should not need to manually initiate individual discovery jobs, process leads, generate emails, send messages, check replies or schedule follow-ups.

I should only need to configure the system, authorize campaigns and occasionally monitor or control operations.

## A. FULLY AUTONOMOUS OPERATION

Implement a persistent autonomous orchestration engine.

After I activate a campaign, the system should automatically:

1. Discover new prospects.
2. Research their companies.
3. Extract publicly available contact information.
4. Verify contact information where possible.
5. Qualify prospects using AI.
6. Remove duplicates.
7. Generate personalized emails.
8. Check compliance and eligibility.
9. Queue eligible emails.
10. Send emails according to configured limits and schedules.
11. Monitor incoming replies.
12. Classify replies using AI.
13. Update lead status.
14. Schedule appropriate follow-ups.
15. Stop follow-ups when necessary.
16. Notify me about important opportunities.
17. Continue finding new prospects until the campaign's target, budget or configured stopping condition is reached.

The frontend must not need to remain open.

All operations must run through persistent background workers and scheduled jobs.

If the server restarts, the system must recover unfinished jobs without duplicating sent emails.

## B. MULTI-PROVIDER ORCHESTRATION ENGINE

Build a centralized provider registry and orchestration service.

Do not hardcode a single API provider into business logic.

Create provider adapters with common interfaces.

Support multiple providers for:

1. Company discovery.
2. Contact discovery.
3. Email extraction.
4. Email verification.
5. Website research.
6. Search.
7. AI/LLM inference.
8. Email sending.
9. Incoming email processing.
10. Notifications.

Potential integrations to evaluate and support where suitable:

LEAD DISCOVERY:
- Hunter.io
- Snov.io
- People Data Labs
- Apollo, where an authorized plan/API is available
- Search provider APIs
- Brave Search API
- SerpAPI
- Tavily
- Exa
- Firecrawl
- Public business directories with permitted access
- Direct company websites.

EMAIL VERIFICATION:
- Hunter
- Abstract API
- ZeroBounce
- NeverBounce
- Other legitimate verification APIs.

AI:
- Groq
- Google Gemini API
- Cerebras
- OpenRouter
- Other providers with documented free or affordable API access.
- Local models through Ollama when practical.

EMAIL DELIVERY:
- Gmail API
- Brevo
- Resend
- Other legitimate email providers with inbound and outbound API support.

NOTIFICATIONS:
- Telegram Bot API
- Email notifications
- Other configurable notification providers.

These are candidate integrations, not assumptions that every provider offers a permanent free API.

Evaluate each provider's current API availability, free-tier eligibility, restrictions, commercial-use permission, rate limits and terms before implementing it.

## C. AUTOMATIC API FAILOVER

This is a critical feature.

Create a centralized Provider Manager.

It must maintain for each provider:

- Provider name.
- Service category.
- API credentials status.
- Connection status.
- Current plan.
- Remaining known credits.
- Daily usage.
- Monthly usage.
- Rate limits.
- Last successful request.
- Last failure.
- Failure count.
- Cooldown period.
- Reset date when documented.
- Estimated cost.
- Priority.
- Enabled/disabled status.

Implement intelligent provider selection.

Example:

1. The primary provider is used first.
2. If its quota is exhausted, switch to the next eligible provider.
3. If a provider returns a temporary error, retry it with exponential backoff.
4. If it returns an authentication error, mark it as requiring attention.
5. If a provider is temporarily unavailable, place it in cooldown.
6. When its documented quota resets, make it eligible again.
7. Continue the task using another eligible provider.
8. If all providers are exhausted, preserve the queue and pause only the affected task.
9. Automatically resume when a provider becomes available.

Never lose collected data when switching providers.

Normalize all provider responses into common internal schemas.

Prevent duplicate records when the same company or email appears through multiple providers.

Implement provider health monitoring.

## D. AUTOMATIC CREDIT AND FREE-TIER MANAGEMENT

The objective is to minimize operating expenses and maximize legitimate free-tier utilization.

Implement:

- Free-tier detection where documented.
- Usage counters.
- Credit tracking.
- Daily and monthly quota tracking.
- Reset-date tracking.
- Automatic provider selection.
- Cost-aware routing.
- Free-provider preference.
- Optional paid-provider fallback only when explicitly enabled.
- Budget limits.
- Usage alerts.

I want a settings page where I can see every connected provider, its usage and remaining credits.

Example:

Hunter: 12/50 credits used.
Search provider: 300 requests used.
Groq: Usage available through configured account.
Resend: Daily limit status.
Brevo: Daily limit status.

The figures above are illustrative; always use actual provider data or clearly mark estimates.

Never invent remaining credits when the provider does not expose usage information.

For providers without usage endpoints, maintain internal usage counters and mark their accuracy accordingly.

Do not automatically create accounts, rotate identities, rotate API keys or switch Gmail accounts to evade free-tier restrictions, sending quotas, verification requirements or provider terms.

Provider failover must respect the limits and terms of each account.

## E. SMART RETRY SYSTEM

Implement a robust retry engine.

Retry temporary network errors and eligible HTTP 5xx errors.

For HTTP 429:
- Respect Retry-After.
- Use exponential backoff.
- Add random jitter.
- Avoid repeated requests while rate limited.

For HTTP 401:
- Stop using the provider until authentication is restored.

For HTTP 403:
- Determine whether the cause is quota exhaustion, missing permissions, account restrictions or policy rejection.
- Switch providers only if the task is permitted through another provider.

For invalid responses:
- Validate the response.
- Log the error.
- Do not repeatedly retry permanent failures.

Use configurable maximum attempts and retry windows.

Implement dead-letter queues for permanently failed tasks.

All retries must be idempotent.

Never send the same email twice because a request timed out after the provider accepted it.

## F. AUTONOMOUS CAMPAIGN CONTROLLER

Build an autonomous campaign controller.

The campaign should continue running without user intervention after initial authorization.

It should:
- Maintain a target number of qualified prospects.
- Continuously replenish eligible leads.
- Avoid previously contacted leads.
- Respect sending schedules.
- Respect provider and account limits.
- Automatically pause if authentication fails.
- Automatically pause if email bounce rates become concerning.
- Automatically pause if provider policies or compliance checks prevent further sending.
- Stop when the campaign target is reached.
- Resume when a temporary issue is resolved.

Provide controls:
- Start.
- Pause.
- Resume.
- Stop.
- Emergency kill switch.

The user must always retain control.

## G. AUTONOMOUS REPLY MANAGEMENT

The system must monitor incoming replies 24/7.

It should classify messages, identify high-intent responses, update the CRM, stop inappropriate follow-ups and notify me.

Do not require manual inbox monitoring.

Keep all real email conversations accessible through Gmail.

Only notify me when human attention is genuinely required.

## H. AUTONOMOUS OPERATION DASHBOARD

Create a dedicated System Operations page.

Display:

- Current autonomous status.
- Active campaigns.
- Running discovery jobs.
- Current provider.
- Provider failovers.
- API usage.
- Available credits.
- Failed tasks.
- Pending retries.
- Emails queued.
- Emails sent.
- Replies processed.
- Interested prospects.
- Background worker health.
- Last successful synchronization.

Include a chronological activity log.

Example:

09:00 - Started US Dental campaign.
09:02 - Discovered 80 companies.
09:05 - Hunter credits exhausted.
09:05 - Switched to public website discovery.
09:10 - Extracted 25 publicly listed contacts.
09:15 - AI qualified 18 leads.
09:20 - Generated personalized emails.
09:25 - Sending eligible campaign messages.
09:30 - Received a reply.
09:30 - AI classified it as HIGH_INTENT.
09:30 - Notification sent.

These are example events, not hardcoded dashboard entries.

## I. AUTONOMY AND SAFETY REQUIREMENTS

I want maximum autonomy, but not uncontrolled behavior.

Implement:
- One-time campaign authorization.
- Configurable daily sending limits.
- Per-provider limits.
- Compliance checks.
- Unsubscribe enforcement.
- Bounce monitoring.
- Duplicate prevention.
- Human review for ambiguous or high-risk situations.
- Emergency stop.
- Complete audit history.

Do not evade Gmail daily limits by rotating accounts.

Do not evade API limits by creating additional accounts.

Do not use provider failover to bypass anti-spam restrictions.

Do not send to contacts who have opted out or are otherwise ineligible.

The system should continue discovery and processing when email delivery is paused, but must not send messages until delivery is eligible.

## J. FINAL REQUIREMENT

I want a genuinely autonomous application, not a dashboard that simply displays automation statistics.

The backend must perform actual work continuously.

I should be able to deploy it, configure my integrations, authorize a campaign once and leave it running.

My involvement should generally be limited to:
- Reviewing interested prospects.
- Responding to important conversations.
- Changing targeting.
- Pausing or stopping campaigns.
- Handling exceptional integration or compliance issues.

Implement this as a core architectural requirement, not as an optional future feature.
