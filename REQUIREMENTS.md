# REQUIREMENTS — Original Specification (verbatim from the owner)

> Source: the owner's first brief, saved verbatim on 2026-11-19 so it never has to be re-pasted.
> This is the **source of truth** for scope. Read together with `REQUIREMENTS_AUTONOMY.md` (mandatory addendum),
> `CONTEXT.md` (owner/accounts/decisions) and `CLAUDE.md` (how to work).
> The owner later said: *"you don't have to exactly do what I say, just do everything best, whatever's better do that, no strict rule to follow."*
> → Deviations that make the product better are allowed, but must be recorded in `docs/DECISIONS.md`.

---

# Build a Complete AI-Powered Autonomous Email Outreach Platform

Act as a senior software architect, full-stack engineer, AI engineer, DevOps engineer and product designer.

I want you to build a complete, production-oriented AI email outreach and lead-generation platform from scratch. This is not a prototype, landing page, UI-only project or simple email automation script. Build a functional end-to-end application with real integrations, a working backend, database, authentication, background workers, AI processing and email delivery.

The application should be reliable, secure, modular, scalable and easy to operate.

Do not just give me instructions or code snippets. Actually implement the application, create the files, install dependencies, configure the architecture, run migrations, execute tests and fix errors.

If something requires API credentials, implement the complete integration and provide clear setup instructions. Never pretend an integration is working when credentials are missing.

# 1. PRODUCT OVERVIEW

Product name: AI Outreach Intelligence

The application is an AI-powered autonomous lead-generation, cold-email outreach and reply-management platform.

Its primary purpose is to:

1. Automatically discover potential business customers and HR/recruitment contacts.
2. Extract publicly available professional email addresses.
3. Research and qualify companies using AI.
4. Verify email addresses wherever possible.
5. Generate personalized emails using AI.
6. Send emails through the Gmail API.
7. Monitor incoming replies automatically.
8. Use AI to understand and classify incoming replies.
9. Notify me about interested prospects and important conversations.
10. Automatically manage appropriate follow-ups.
11. Keep a complete internal record of leads, conversations and campaign performance without requiring me to manually monitor every email.

The application must support two separate operating modes.

## Mode A: NEXORA SALES

This mode is for finding customers for NEXORA AI.

NEXORA is an AI automation platform for businesses. It provides WhatsApp, Instagram and Messenger automation, AI-powered customer communication, appointment booking, lead management and business automation.

Target customers:
- Dental clinics
- Gyms and fitness studios
- Hotels
- Restaurants
- Real estate agencies
- Beauty salons
- Medical practices
- Other small and medium businesses

Target decision-makers:
- Business owners
- Founders
- CEOs
- Practice managers
- Operations managers
- Marketing managers

Target locations:
- USA
- Canada
- UK
- Germany
- India
- Other countries configurable by the user.

## Mode B: JOB HUNTER

This mode is for finding job opportunities and relevant recruitment contacts.

Target contacts:
- HR managers
- Technical recruiters
- Talent acquisition specialists
- Recruitment managers
- Hiring managers
- Founders of technology startups

Target companies:
- Technology startups
- Software development companies
- AI companies
- SaaS companies
- International companies offering remote positions

Target roles:
- Python Developer
- Backend Developer
- AI Engineer
- Automation Engineer
- Full Stack Developer
- Software Engineer
- Other configurable roles.

Maintain completely separate campaign data, email templates, lead categories and sender identities for the two modes.

# 2. TECHNOLOGY STACK

Use the following stack.

Frontend:
- Next.js (latest stable version)
- TypeScript
- Tailwind CSS
- shadcn/ui
- Recharts
- Lucide icons

Backend:
- Python
- FastAPI
- Pydantic
- SQLAlchemy 2.0
- Alembic

Database:
- Supabase PostgreSQL
- Supabase Auth where appropriate
- PostgreSQL full-text search
- pgvector only if genuinely useful.

Background processing:
- Redis
- ARQ workers
- Scheduled background tasks

AI:
- Groq API
- Configurable LLM model
- Structured JSON outputs
- Prompt templates stored separately from business logic.

Email:
- Google Gmail API
- OAuth 2.0
- Gmail threads and message history
- Gmail API watch / Google Cloud Pub/Sub for incoming-message notifications where configured.
- Polling through Gmail History API as a fallback.

Lead discovery:
- Hunter.io API (optional)
- Search provider adapters
- Firecrawl API (optional)
- Public company websites
- Public business directories whose terms permit the intended use.

Email verification:
- Hunter verification API where available
- Syntax validation
- Domain and MX record checks
- Optional verification provider adapters.

Deployment:
- Frontend: Vercel
- Backend: Render or another suitable hosting provider
- Redis: Upstash
- Database: Supabase

Keep all integrations modular so providers can be replaced without rewriting the application.

# 3. LEAD DISCOVERY ENGINE

This is one of the most important components.

I do not want to manually search for hundreds of companies or copy email addresses.

I want an AI-powered lead discovery engine.

Create a lead discovery interface where I can enter:

- Industry
- Country
- City or region (optional)
- Target company type
- Target decision-maker
- Number of desired prospects
- Keywords
- Company size (optional)
- Additional requirements.

Example:

"Find 500 dental clinics in the United States. Identify publicly listed business contact emails and owners or practice managers. Prioritize clinics that might benefit from WhatsApp appointment automation."

The discovery engine should:

1. Find relevant companies through configured search APIs and permitted directories.
2. Collect company name, website, location, industry and public contact information.
3. Visit permitted public company pages.
4. Research About, Contact, Team and relevant business pages.
5. Extract publicly published business email addresses.
6. Identify publicly disclosed relevant decision-makers.
7. Use Hunter API where configured to discover additional professional emails.
8. Verify email addresses where possible.
9. Remove duplicates.
10. Store source URLs and discovery timestamps.
11. Score lead quality.
12. Save qualified leads to the database.

IMPORTANT:
- Never fabricate email addresses.
- Never treat guessed email patterns as verified contacts.
- Never claim an email is verified simply because its syntax is valid.
- Distinguish discovered, guessed, unverified, risky and verified addresses.
- Respect robots.txt, provider API limits, website terms and applicable privacy regulations.
- Do not scrape LinkedIn or bypass login restrictions.
- Do not bulk scrape Google Maps or violate third-party data licensing.
- Do not bypass CAPTCHAs, access controls or rate limits.
- Implement retry logic, exponential backoff and rate limiting.

Create modular discovery providers:

LeadDiscoveryProvider (interface)
- Search companies
- Search relevant contacts
- Retrieve public company information
- Return normalized lead records.

Implement providers independently so additional sources can be added later.

# 4. AI LEAD ENRICHMENT AND SCORING

Use Groq to analyze the collected company information.

The AI should determine:
- What the company does.
- Whether it matches the target industry.
- Whether it is likely to benefit from NEXORA.
- Which contact is most relevant.
- Whether the email is a general business address or a named professional contact.
- Why this company is a potential customer.
- What outreach angle would be most relevant.

Create a configurable lead score from 0 to 100.

Example:
- Industry relevance: 25 points
- Business fit: 25 points
- Decision-maker relevance: 20 points
- Contact quality: 15 points
- Publicly evidenced opportunity: 15 points

Store the score, explanation and evidence.

For Job Hunter, use different scoring logic:
- Role relevance
- Company relevance
- Recruiter relevance
- Location and remote-work compatibility
- Available job openings where verified.

Never let AI invent facts about a company.

# 5. EMAIL GENERATION ENGINE

Build an AI email generation engine.

The system must generate genuinely personalized emails rather than sending identical messages to everyone.

For NEXORA:
- Research the company's services.
- Identify a relevant automation use case.
- Generate a short, natural introduction.
- Explain how NEXORA could help.
- Use a simple, professional call to action.

For Job Hunter:
- Research the company and relevant role.
- Write a professional introduction.
- Highlight relevant skills and projects.
- Generate a suitable application or recruiter outreach email.

Email requirements:
- Natural human tone.
- No exaggerated claims.
- No fabricated personal experience.
- Short and professional.
- Avoid generic AI-sounding language.
- Different subject lines where appropriate.
- Personalized opening based on verified information.

Create editable templates.

Before sending, show a preview and allow manual approval.

Support three sending modes:
1. Manual approval for every email.
2. Approve an entire campaign before sending.
3. Automatically send eligible emails after campaign-level authorization.

Default to approval mode.

# 6. GMAIL API INTEGRATION

This is the primary email infrastructure.

Integrate directly with the official Gmail API.

Documentation:
https://developers.google.com/workspace/gmail/api

Implement:

OAuth 2.0:
- Connect Gmail account.
- Authenticate securely.
- Store access and refresh tokens securely.
- Handle token expiration.
- Support reconnecting accounts.
- Provide disconnect functionality.
- Use the minimum required OAuth scopes.

Sending:
- Send emails through Gmail API.
- Support HTML and plain-text email bodies.
- Include proper reply-to and message headers.
- Store Gmail message IDs and thread IDs.
- Support attachments for job applications, including CVs.
- Preserve conversation threading for replies.

Receiving:
- Read incoming replies.
- Match replies to original campaigns.
- Identify sender and thread.
- Retrieve message content safely.
- Handle Gmail labels and inbox changes.
- Avoid processing the same reply twice.

Monitoring:
- Implement Gmail API watch with Pub/Sub where available.
- Implement Gmail History API polling as a fallback.
- Automatically recover from expired watches.
- Handle webhook retries and duplicate events.

IMPORTANT:
Do not assume Gmail API quotas are equivalent to permission to send unlimited cold emails.

Build configurable daily and hourly sending limits based on the connected account and provider rules.

For personal Gmail accounts, clearly warn about sending restrictions. Do not force 300 emails per day through an account that cannot safely or legitimately support that volume.

# 7. AUTONOMOUS REPLY MONITORING ENGINE

This is the most important feature after sending.

I do not want to manually read hundreds of email responses.

Build an AI-powered reply classification engine.

Whenever a reply arrives:

1. Detect the incoming message.
2. Identify the original campaign and recipient.
3. Extract and sanitize the message content.
4. Send the content to Groq for classification.
5. Store the classification and confidence score.
6. Take the appropriate action.
7. Notify me when attention is required.

Create these reply categories:

INTERESTED:
The recipient expresses interest in the product or opportunity.

HIGH_INTENT:
The recipient wants pricing, a demo, a meeting or more information.

JOB_OPPORTUNITY:
A recruiter expresses interest, asks for a CV or proposes an interview.

NOT_INTERESTED:
The recipient declines.

NOT_NOW:
The recipient asks to be contacted later.

REFERRAL:
The recipient refers me to another person.

OUT_OF_OFFICE:
Automatic vacation or absence response.

UNSUBSCRIBE:
The recipient asks not to receive further emails.

BOUNCE:
Delivery failure.

OTHER:
Unclassified responses.

The classification must include:
- Category
- Confidence
- Short explanation
- Relevant extracted information
- Suggested next action
- Whether a human response is required.

Use structured JSON outputs validated through Pydantic.

IMPORTANT:
Never automatically classify a message as interested based only on polite language.

Never automatically send replies to interested prospects without my approval.

Automatically stop all follow-ups after:
- Any meaningful human response.
- An unsubscribe request.
- A hard bounce.
- A negative response.

Automatic out-of-office responses should not be treated as positive interest.

# 8. NOTIFICATION SYSTEM

My goal is to receive only important notifications.

I want incoming replies to remain visible in my regular Gmail inbox.

Additionally, provide optional notifications through Telegram or another push notification provider.

Send notifications for:

- Interested prospects.
- High-intent replies.
- Meeting requests.
- Recruiters interested in my profile.
- Requests for CVs.
- Important referrals.
- Unsubscribe requests if action is needed.
- Campaign errors or account authentication problems.

Example notification:

"NEXORA LEAD INTERESTED

Sarah Johnson
ABC Dental Clinic
Toronto, Canada

Reply: Interested in your WhatsApp automation solution. Please share pricing.

AI recommendation: Send pricing details.

Campaign: Dental Clinics USA"

Another example:

"JOB OPPORTUNITY

Recruiter from XYZ Technologies

Reply: Your profile looks interesting. Please share your updated CV.

AI recommendation: Respond with CV attached."

Do not send unnecessary notifications for ordinary automated replies.

# 9. AUTOMATIC FOLLOW-UP ENGINE

Implement a follow-up scheduler.

If an email has not received a meaningful reply:

- Follow-up 1: configurable, default 4 business days.
- Follow-up 2: configurable, default 7 business days after the first follow-up.
- Maximum follow-ups: configurable, default 2.

Before sending any follow-up:
- Check whether a reply has arrived.
- Check unsubscribe status.
- Check bounce status.
- Check campaign status.
- Check applicable sending limits.
- Check whether the contact has been suppressed.

Use natural, contextual follow-up messages.

Never continue following up after a negative response or unsubscribe.

Follow-ups must be configurable per campaign.

# 10. MAIN DASHBOARD

Build a modern, professional SaaS-style dashboard.

Use a clean interface with a sidebar and responsive design.

Navigation:

- Overview
- Lead Discovery
- All Leads
- Campaigns
- Email Templates
- Inbox Intelligence
- Interested Prospects
- Job Hunter
- NEXORA Sales
- Analytics
- Settings
- Integrations

Dashboard metrics:
- Total companies discovered.
- Contacts discovered.
- Emails verified.
- Emails sent.
- Delivered emails.
- Replies received.
- Interested prospects.
- Meetings requested.
- Job opportunities.
- Follow-ups scheduled.

Display real database metrics only. Never hardcode sample metrics into production.

Create charts for:
- Daily sending volume.
- Reply rate.
- Positive reply rate.
- Campaign performance.
- Lead discovery trends.

# 11. LEAD MANAGEMENT

Create a complete lead database.

Fields:

- ID
- Name
- Company
- Website
- Email
- Email verification status
- Contact position
- Industry
- Country
- City
- Source URL
- Discovery date
- Lead score
- Lead qualification explanation
- Campaign ID
- Last contacted date
- Last reply date
- Reply category
- Follow-up status
- Unsubscribe status
- Notes.

Support:
- Search
- Filtering
- Sorting
- CSV export
- Duplicate detection
- Manual lead addition
- Bulk import
- Lead deletion
- Suppression lists.

# 12. CAMPAIGN MANAGEMENT

Users should be able to create separate campaigns.

Example campaigns:

NEXORA:
- US Dental Clinics
- Canadian Gyms
- UK Hotels
- Indian Dental Clinics

JOB HUNTER:
- Germany Python Developer Roles
- European AI Startups
- Remote Backend Developer Opportunities

Each campaign should contain:
- Name
- Objective
- Target audience
- Discovery criteria
- Email template
- Sending account
- Daily sending limit
- Sending schedule
- Time zone
- Follow-up settings
- Approval settings
- Campaign status.

Campaign states:
- Draft
- Awaiting approval
- Running
- Paused
- Completed
- Failed.

Provide pause, resume and stop controls.

# 13. EMAIL SAFETY AND COMPLIANCE

This application must not be designed as a spam tool.

Implement:
- Unsubscribe links or equivalent clear opt-out mechanisms.
- Central suppression list.
- Contact source tracking.
- Consent/lawful-basis and compliance metadata where required.
- Sender identification.
- Appropriate postal address configuration where applicable.
- Bounce handling.
- Rate limiting.
- Sending schedules.
- Domain authentication guidance (SPF, DKIM and DMARC).
- Duplicate prevention.
- Campaign kill switch.
- Provider-specific daily limits.

Respect GDPR, applicable US CAN-SPAM requirements, Canada's CASL, India's applicable data protection and commercial communication requirements, and other relevant local laws.

Do not assume that a publicly listed email address grants permission for unrestricted marketing.

For cold outreach, implement compliance checks and configurable eligibility rules before a contact can enter a campaign.

# 14. DATABASE DESIGN

Design proper relational schemas and migrations.

Suggested tables:

- users
- connected_email_accounts
- oauth_tokens
- workspaces
- campaigns
- campaign_settings
- leads
- contacts
- lead_sources
- email_verifications
- email_templates
- email_messages
- email_threads
- incoming_replies
- reply_classifications
- follow_up_schedules
- notification_preferences
- notification_logs
- suppression_list
- discovery_jobs
- discovery_job_results
- audit_logs
- integration_settings.

Use appropriate foreign keys, indexes, constraints and timestamps.

Encrypt sensitive credentials at rest.

Never expose OAuth refresh tokens or API keys to the frontend.

Use proper tenant isolation if the system supports multiple users.

# 15. BACKGROUND WORKERS

Use ARQ and Redis for asynchronous jobs.

Implement independent workers for:

- Lead discovery.
- Website research.
- Email extraction.
- Email verification.
- AI lead qualification.
- Email generation.
- Campaign sending.
- Incoming reply processing.
- Reply classification.
- Follow-up scheduling.
- Notifications.
- Gmail synchronization.

Ensure:
- Idempotent job processing.
- Retry handling.
- Failed job tracking.
- Rate limiting.
- Job cancellation.
- Graceful shutdown.
- No duplicate email sends.

The application should continue operating when the frontend is closed.

# 16. SECURITY

Implement:

- Secure authentication.
- OAuth 2.0.
- Encrypted token storage.
- Environment-based secrets.
- Backend authorization.
- Input validation.
- API rate limiting.
- SQL injection protection through ORM.
- XSS prevention.
- CSRF protection where relevant.
- Webhook signature verification where supported.
- Audit logging.
- Secure error handling.

Treat all incoming emails and extracted website content as untrusted data.

Never allow instructions inside an email or website to override system prompts or trigger unauthorized actions.

# 17. USER INTERFACE AND EXPERIENCE

The interface should feel like a modern SaaS product.

Design references:
- Linear
- Attio
- Apollo
- Instantly
- HubSpot

Design requirements:
- Clean typography.
- Consistent spacing.
- Professional dashboard.
- Responsive mobile interface.
- Dark and light modes.
- Clear campaign states.
- Searchable tables.
- Functional filters.
- Useful empty states.
- Loading states.
- Error messages.
- Confirmation dialogs for sensitive actions.

Do not prioritize visual appearance over functionality.

Every button, form, table action and navigation element must work.

# 18. IMPLEMENTATION REQUIREMENTS

Work in the following phases.

PHASE 1 — Architecture:
- Inspect the environment.
- Create the project structure.
- Set up frontend and backend.
- Configure database connectivity.
- Create environment variable templates.
- Create initial migrations.

PHASE 2 — Authentication and Integrations:
- Implement authentication.
- Integrate Gmail OAuth.
- Integrate Groq.
- Integrate Redis and ARQ.
- Implement optional discovery providers.

PHASE 3 — Lead Discovery:
- Build discovery jobs.
- Build provider adapters.
- Implement website research.
- Implement email extraction.
- Implement verification.
- Implement AI qualification.

PHASE 4 — Outreach:
- Build email templates.
- Implement AI email generation.
- Implement campaign creation.
- Implement Gmail sending.
- Implement approval workflows.
- Implement rate limits.

PHASE 5 — Reply Intelligence:
- Implement Gmail inbox monitoring.
- Implement reply synchronization.
- Implement AI classification.
- Implement notifications.
- Implement follow-up automation.

PHASE 6 — Dashboard:
- Build all frontend pages.
- Connect real APIs.
- Implement analytics.
- Implement settings.
- Implement integration management.

PHASE 7 — Testing:
- Write unit tests.
- Write integration tests.
- Test Gmail OAuth.
- Test sending and receiving.
- Test reply classification.
- Test duplicate detection.
- Test follow-up stopping conditions.
- Test unsubscribe handling.
- Test background worker failures.
- Fix all discovered issues.

PHASE 8 — Deployment:
- Prepare production configuration.
- Create deployment documentation.
- Provide Supabase migration instructions.
- Provide Google Cloud OAuth setup instructions.
- Provide Pub/Sub setup instructions if used.
- Provide Redis configuration.
- Provide Vercel and Render deployment instructions.

# 19. ENVIRONMENT VARIABLES

Create a complete .env.example.

Include variables for:

```
DATABASE_URL=
SUPABASE_URL=
SUPABASE_ANON_KEY=
SUPABASE_SERVICE_ROLE_KEY=
REDIS_URL=
GROQ_API_KEY=
GROQ_MODEL=
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_REDIRECT_URI=
GOOGLE_PUBSUB_TOPIC=
HUNTER_API_KEY=
FIRECRAWL_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
ENCRYPTION_KEY=
JWT_SECRET=
FRONTEND_URL=
BACKEND_URL=
```

Make optional integrations genuinely optional.

Never commit real credentials.

# 20. IMPORTANT DEVELOPMENT RULES

1. Do not build a fake demo.
2. Do not use hardcoded sample data as real metrics.
3. Do not create buttons that do nothing.
4. Do not leave core functionality as TODO comments.
5. Do not fabricate API responses.
6. Do not silently swallow errors.
7. Do not put API keys in frontend code.
8. Do not assume OAuth is configured until tested.
9. Do not send real emails during automated tests.
10. Create mock providers for development and testing.
11. Document any functionality that requires paid API credits.
12. Use clean architecture and modular services.
13. Keep code maintainable and properly typed.
14. Make the application deployable.
15. Do not stop after creating the frontend.

If an API key is unavailable, implement the integration interface, configuration validation and mock implementation. Continue developing the rest of the application.

If an external integration requires approval or account verification, clearly document that dependency.

# 21. FINAL DELIVERABLE

At completion, provide:

1. Complete working source code.
2. Frontend and backend.
3. Database schema and migrations.
4. Background workers.
5. Gmail API integration.
6. AI reply classification.
7. Lead discovery integrations.
8. Campaign management.
9. Follow-up automation.
10. Notification system.
11. Tests and test results.
12. Complete README.
13. Environment setup guide.
14. Deployment guide.
15. List of remaining external credentials or approvals.

Also provide a feature-completion report distinguishing:
- Fully implemented and tested.
- Implemented but awaiting credentials.
- Dependent on external approval.
- Not yet implemented.

# FINAL PRODUCT VISION

I want to be able to open the application, choose either NEXORA SALES or JOB HUNTER, enter my target audience and start a campaign.

The application should then:

- Discover relevant prospects.
- Find publicly available contact emails.
- Research and qualify the prospects.
- Generate personalized emails.
- Send authorized campaigns.
- Monitor incoming replies in the background.
- Understand whether recipients are interested.
- Stop inappropriate follow-ups.
- Notify me about important opportunities.
- Keep replies accessible in my regular Gmail inbox.

I should not have to manually monitor hundreds of email conversations.

The system should act as my AI-powered outreach assistant.

Build the complete application, not merely a proposal or tutorial.

Start by inspecting the development environment and presenting a concise architecture and implementation plan. Then proceed with implementation phase by phase. Do not stop after the planning stage. Ask me for credentials only when they are actually needed, and never ask me to share secret keys in chat or commit them to source control.
