"""Import every model so ``Base.metadata`` is complete (Alembic autogenerate + tests rely on this)."""

from app.db.models.accounts import (
    ConnectedEmailAccount,
    IntegrationSetting,
    OAuthToken,
    ResumeAsset,
    SenderProfile,
    WatchState,
)
from app.db.models.campaigns import Campaign, CampaignSettings, EmailTemplate
from app.db.models.discovery import DiscoveryJob, DiscoveryJobResult
from app.db.models.leads import Company, Contact, EmailVerification, Fact, Lead, LeadSource
from app.db.models.messaging import (
    EmailMessage,
    EmailThread,
    FollowUpSchedule,
    IncomingReply,
    ReplyClassification,
)
from app.db.models.ops import (
    ActivityEvent,
    AuditLog,
    DeadLetter,
    NotificationLog,
    NotificationPreference,
)
from app.db.models.providers import ProviderState, ProviderUsageEvent
from app.db.models.safety import ConsentRecord, JurisdictionPolicy, SuppressionEntry
from app.db.models.tenancy import User, Workspace

__all__ = [
    "ActivityEvent",
    "AuditLog",
    "Campaign",
    "CampaignSettings",
    "Company",
    "ConnectedEmailAccount",
    "ConsentRecord",
    "Contact",
    "DeadLetter",
    "DiscoveryJob",
    "DiscoveryJobResult",
    "EmailMessage",
    "EmailTemplate",
    "EmailThread",
    "EmailVerification",
    "Fact",
    "FollowUpSchedule",
    "IncomingReply",
    "IntegrationSetting",
    "JurisdictionPolicy",
    "Lead",
    "LeadSource",
    "NotificationLog",
    "NotificationPreference",
    "OAuthToken",
    "ProviderState",
    "ProviderUsageEvent",
    "ReplyClassification",
    "ResumeAsset",
    "SenderProfile",
    "SuppressionEntry",
    "User",
    "WatchState",
    "Workspace",
]
