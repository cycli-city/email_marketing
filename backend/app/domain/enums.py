"""Domain enumerations — single source of truth for every state/category in the system.

Pure module: no I/O, no framework imports. Values are lowercase snake_case strings and are
persisted as VARCHAR + CHECK constraints (see ``app.db.types``), so adding a value needs a migration.
"""

from __future__ import annotations

from enum import StrEnum


class CampaignMode(StrEnum):
    """The two operating modes. Data, templates, senders and categories never mix across modes."""

    NEXORA = "nexora"
    JOB_HUNTER = "job_hunter"


class CampaignStatus(StrEnum):
    DRAFT = "draft"
    AWAITING_APPROVAL = "awaiting_approval"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class ApprovalMode(StrEnum):
    """REQUIREMENTS §5 — the three sending modes. Default is MANUAL_EACH."""

    MANUAL_EACH = "manual_each"
    APPROVE_CAMPAIGN = "approve_campaign"
    AUTO_AFTER_AUTHORIZATION = "auto_after_authorization"


class LeadStage(StrEnum):
    """Pipeline stage of a lead (a company inside a campaign). See docs/ARCHITECTURE.md §3.1."""

    DISCOVERED = "discovered"
    RESEARCHED = "researched"
    CONTACTS_EXTRACTED = "contacts_extracted"
    VERIFIED = "verified"
    QUALIFIED = "qualified"
    DRAFTED = "drafted"
    APPROVED = "approved"
    QUEUED = "queued"
    SENT = "sent"
    REPLIED = "replied"
    BOUNCED = "bounced"
    UNSUBSCRIBED = "unsubscribed"
    EXHAUSTED = "exhausted"  # all follow-ups sent, no reply
    DISQUALIFIED = "disqualified"
    SUPPRESSED = "suppressed"
    DUPLICATE = "duplicate"
    NEEDS_REVIEW = "needs_review"


class EmailOrigin(StrEnum):
    """Where an email address came from. ``GUESSED`` addresses are never treated as verified."""

    DISCOVERED = "discovered"  # published on a public page we fetched
    PROVIDER = "provider"  # returned by a discovery provider (Hunter, Snov, …)
    GUESSED = "guessed"  # produced from a name/domain pattern
    MANUAL = "manual"
    IMPORTED = "imported"


class VerificationStatus(StrEnum):
    """Result of verification. Syntax validity alone must never produce ``VERIFIED``."""

    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    RISKY = "risky"  # catch-all, role/disposable heuristics, provider says "risky"
    INVALID = "invalid"


class EmailKind(StrEnum):
    NAMED = "named"  # firstname.lastname@
    ROLE_BASED = "role_based"  # info@, contact@, hr@ …
    UNKNOWN = "unknown"


class SubscriberType(StrEnum):
    """UK PECR / EU distinction: corporate vs individual subscribers."""

    CORPORATE = "corporate"
    INDIVIDUAL = "individual"
    UNKNOWN = "unknown"


class LawfulBasis(StrEnum):
    CONSENT = "consent"
    LEGITIMATE_INTEREST = "legitimate_interest"
    IMPLIED_PUBLICATION = "implied_publication"  # CASL conspicuous publication
    NONE = "none"


class MessageKind(StrEnum):
    INITIAL = "initial"
    FOLLOW_UP = "follow_up"
    REPLY = "reply"  # owner-approved reply to a prospect


class MessageStatus(StrEnum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    SEND_UNCERTAIN = "send_uncertain"  # provider outcome unknown — reconcile, never blind-retry
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"  # ineligible at send time (suppressed, replied, limits…)


class ReplyKind(StrEnum):
    HUMAN = "human"
    AUTO_REPLY = "auto_reply"
    BOUNCE = "bounce"
    UNKNOWN = "unknown"


class ReplyCategory(StrEnum):
    """REQUIREMENTS §7 — classification categories."""

    INTERESTED = "interested"
    HIGH_INTENT = "high_intent"
    JOB_OPPORTUNITY = "job_opportunity"
    NOT_INTERESTED = "not_interested"
    NOT_NOW = "not_now"
    REFERRAL = "referral"
    OUT_OF_OFFICE = "out_of_office"
    UNSUBSCRIBE = "unsubscribe"
    BOUNCE = "bounce"
    OTHER = "other"


class ReplyProcessingStatus(StrEnum):
    PENDING = "pending"
    CLASSIFIED = "classified"
    NEEDS_REVIEW = "needs_review"
    ERROR = "error"


class FollowUpStatus(StrEnum):
    """Lead-level follow-up state."""

    NONE = "none"
    SCHEDULED = "scheduled"
    STOPPED = "stopped"
    COMPLETED = "completed"


class FollowUpScheduleStatus(StrEnum):
    SCHEDULED = "scheduled"
    SENT = "sent"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


class SuppressionReason(StrEnum):
    UNSUBSCRIBE = "unsubscribe"
    HARD_BOUNCE = "hard_bounce"
    COMPLAINT = "complaint"
    NEGATIVE_REPLY = "negative_reply"
    MANUAL = "manual"
    LEGAL = "legal"
    IMPORT = "import"


class ConsentPolicy(StrEnum):
    ALLOWED = "allowed"
    REQUIRES_CONSENT = "requires_consent"
    BLOCKED = "blocked"


class DiscoveryJobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_FOR_PROVIDER = "waiting_for_provider"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DiscoveryOutcome(StrEnum):
    NEW = "new"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"
    ERROR = "error"


class Capability(StrEnum):
    """What a provider can do. Business logic asks for a capability, never for a vendor."""

    LLM = "llm"
    SEARCH = "search"
    COMPANY_DISCOVERY = "company_discovery"
    CONTACT_DISCOVERY = "contact_discovery"
    EMAIL_VERIFICATION = "email_verification"
    WEBSITE_RESEARCH = "website_research"
    EMAIL_SEND = "email_send"
    INBOX_READ = "inbox_read"
    NOTIFY = "notify"


class ProviderStatus(StrEnum):
    READY = "ready"
    COOLDOWN = "cooldown"
    RATE_LIMITED = "rate_limited"
    EXHAUSTED = "exhausted"
    NEEDS_ATTENTION = "needs_attention"
    DISABLED = "disabled"
    UNCONFIGURED = "unconfigured"


class CreditsAccuracy(StrEnum):
    """How trustworthy ``remaining_credits`` is. Never invent credits (REQUIREMENTS_AUTONOMY §D)."""

    PROVIDER_REPORTED = "provider_reported"
    INTERNAL_COUNTER = "internal_counter"
    UNKNOWN = "unknown"


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class UsageOutcome(StrEnum):
    SUCCESS = "success"
    RETRYABLE_ERROR = "retryable_error"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
    AUTH_ERROR = "auth_error"
    PERMISSION_ERROR = "permission_error"
    PERMANENT_ERROR = "permanent_error"
    CIRCUIT_OPEN = "circuit_open"


class AccountProvider(StrEnum):
    GMAIL = "gmail"
    BREVO = "brevo"
    RESEND = "resend"


class AccountType(StrEnum):
    PERSONAL_GMAIL = "personal_gmail"
    GOOGLE_WORKSPACE = "google_workspace"
    TRANSACTIONAL = "transactional"


class AccountStatus(StrEnum):
    ACTIVE = "active"
    NEEDS_REAUTH = "needs_reauth"
    DISCONNECTED = "disconnected"
    ERROR = "error"


class NotificationChannel(StrEnum):
    TELEGRAM = "telegram"
    EMAIL = "email"
    WEBHOOK = "webhook"


class NotificationStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SUPPRESSED = "suppressed"


class ActivityLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class DeadLetterStatus(StrEnum):
    OPEN = "open"
    RETRIED = "retried"
    DISCARDED = "discarded"


class TemplateKind(StrEnum):
    INITIAL = "initial"
    FOLLOW_UP_1 = "follow_up_1"
    FOLLOW_UP_2 = "follow_up_2"
    REPLY = "reply"


class ActorType(StrEnum):
    USER = "user"
    SYSTEM = "system"
    WORKER = "worker"


class FactKind(StrEnum):
    SERVICE = "service"
    LOCATION = "location"
    TEAM = "team"
    OPENING = "opening"
    CONTACT = "contact"
    OTHER = "other"
