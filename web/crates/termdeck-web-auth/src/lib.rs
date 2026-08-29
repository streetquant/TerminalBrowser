#![forbid(unsafe_code)]

//! Security foundation for TermDeck's one-owner, many-device web host.
//!
//! This crate deliberately contains no HTTP routes, tmux integration, terminal
//! bytes, process actions, or writable access to the existing TermDeck registry.
//! It owns a separate permission-checked SQLite database whose fixed filename is
//! [`AUTH_DATABASE_FILENAME`]. Raw invitations, session cookies, and CSRF
//! capabilities are generated from the operating system CSPRNG, redacted from
//! `Debug`, zeroized on drop, and persisted only as domain-separated SHA-256
//! digests.

mod password;
mod rate_limit;
mod secret;
mod store;

pub use password::{
    MAX_OWNER_PASSWORD_BYTES, MIN_OWNER_PASSWORD_BYTES, OWNER_PASSWORD_FILENAME,
    OwnerPasswordStore, PasswordError, VerifiedOwnerPassword,
};
pub use rate_limit::{BoundedRateLimiter, RateDecision, RateLimitConfig, RateLimitError};
pub use secret::{
    SecretError, SecretKind, SecretToken, digest_validated_secret, rate_subject_digest,
    validate_secret,
};
pub use store::{
    AUTH_DATABASE_FILENAME, AuditEventId, AuditRecord, AuthError, AuthLimits, AuthSessionId,
    AuthStore, AuthenticatedDeviceId, AuthenticatedSession, DeviceRecord, InvitationId,
    MAX_AUTH_DATABASE_BYTES, PairingInvitation, SessionContext,
};
