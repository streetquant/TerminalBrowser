use crate::secret::{
    SecretError, SecretKind, SecretToken, digest_validated_secret, random_public_id,
};
use rusqlite::{
    Connection, OpenFlags, OptionalExtension, Transaction, TransactionBehavior, params,
};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::error::Error;
use std::fmt;
use std::fs::{self, OpenOptions};
use std::path::{Component, Path, PathBuf};
use std::sync::Mutex;
use std::time::Duration;
use termdeck_web_core::{HostId, OwnerId};

#[cfg(unix)]
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};

pub const AUTH_DATABASE_FILENAME: &str = "termdeck-web-auth-v1.sqlite3";
pub const MAX_AUTH_DATABASE_BYTES: u64 = 128 * 1024 * 1024;
const AUTH_SCHEMA_VERSION: i64 = 1;
const AUTH_APPLICATION_ID: i64 = 0x5444_5741;
const MAX_LABEL_BYTES: usize = 128;
const MAX_AUDIT_KIND_BYTES: usize = 48;
const MAX_AUDIT_DETAIL_BYTES: usize = 512;
const DIGEST_BYTES: usize = 32;
const HARD_MAX_DEVICES: usize = 4_096;
const HARD_MAX_INVITATIONS: usize = 16_384;
const HARD_MAX_SESSIONS: usize = 32_768;
const HARD_MAX_AUDIT_EVENTS: usize = 100_000;
const HARD_MAX_INVITATION_TTL_MS: u64 = 7 * 24 * 60 * 60 * 1_000;
const HARD_MAX_SESSION_TTL_MS: u64 = 180 * 24 * 60 * 60 * 1_000;
const MAX_AUDIT_QUERY_RECORDS: usize = 10_000;
const MAX_OPAQUE_ID_BYTES: usize = 256;
const MAX_AUTH_RECORD_ID_BYTES: usize = 64;

const SCHEMA: &str = r#"
CREATE TABLE auth_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL,
    host_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
) STRICT;
CREATE TABLE devices (
    device_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    revoked_at_ms INTEGER
) STRICT;
CREATE TABLE invitations (
    invitation_id TEXT PRIMARY KEY,
    secret_hash BLOB NOT NULL UNIQUE CHECK (length(secret_hash) = 32),
    device_label TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    expires_at_ms INTEGER NOT NULL,
    consumed_at_ms INTEGER,
    consumed_device_id TEXT REFERENCES devices(device_id)
) STRICT;
CREATE TABLE auth_sessions (
    session_id TEXT PRIMARY KEY,
    secret_hash BLOB NOT NULL UNIQUE CHECK (length(secret_hash) = 32),
    csrf_hash BLOB NOT NULL CHECK (length(csrf_hash) = 32),
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    created_at_ms INTEGER NOT NULL,
    expires_at_ms INTEGER NOT NULL,
    revoked_at_ms INTEGER,
    replaced_by_session_id TEXT REFERENCES auth_sessions(session_id)
) STRICT;
CREATE TABLE audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    occurred_at_ms INTEGER NOT NULL,
    kind TEXT NOT NULL,
    actor_device_id TEXT,
    subject_id TEXT,
    detail TEXT NOT NULL,
    previous_hash BLOB NOT NULL CHECK (length(previous_hash) = 32),
    event_hash BLOB NOT NULL UNIQUE CHECK (length(event_hash) = 32)
) STRICT;
CREATE TRIGGER audit_events_no_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit events are immutable');
END;
CREATE TRIGGER audit_events_no_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit events are immutable');
END;
"#;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct AuthLimits {
    pub max_devices: usize,
    pub max_invitations: usize,
    pub max_sessions: usize,
    pub max_audit_events: usize,
    pub max_invitation_ttl_ms: u64,
    pub max_session_ttl_ms: u64,
}

impl Default for AuthLimits {
    fn default() -> Self {
        Self {
            max_devices: 1_024,
            max_invitations: 4_096,
            max_sessions: 8_192,
            max_audit_events: HARD_MAX_AUDIT_EVENTS,
            max_invitation_ttl_ms: 24 * 60 * 60 * 1_000,
            max_session_ttl_ms: 90 * 24 * 60 * 60 * 1_000,
        }
    }
}

impl AuthLimits {
    fn validate(self) -> Result<Self, AuthError> {
        if self.max_devices == 0
            || self.max_invitations == 0
            || self.max_sessions == 0
            || self.max_audit_events == 0
            || self.max_invitation_ttl_ms == 0
            || self.max_session_ttl_ms == 0
            || self.max_devices > HARD_MAX_DEVICES
            || self.max_invitations > HARD_MAX_INVITATIONS
            || self.max_sessions > HARD_MAX_SESSIONS
            || self.max_audit_events > HARD_MAX_AUDIT_EVENTS
            || self.max_invitation_ttl_ms > HARD_MAX_INVITATION_TTL_MS
            || self.max_session_ttl_ms > HARD_MAX_SESSION_TTL_MS
        {
            return Err(AuthError::InvalidLimits);
        }
        Ok(self)
    }
}

macro_rules! auth_id {
    ($name:ident, $prefix:literal) => {
        #[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
        pub struct $name(String);

        impl $name {
            fn generate() -> Result<Self, AuthError> {
                Ok(Self(random_public_id($prefix)?))
            }

            fn from_database(value: String) -> Result<Self, AuthError> {
                let expected = $prefix.len() + 32;
                if value.len() != expected
                    || !value.starts_with($prefix)
                    || !value.as_bytes()[$prefix.len()..]
                        .iter()
                        .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
                {
                    return Err(AuthError::CorruptDatabase("invalid stored identifier"));
                }
                Ok(Self(value))
            }

            pub fn as_str(&self) -> &str {
                self.0.as_str()
            }
        }
    };
}

auth_id!(InvitationId, "inv_");
auth_id!(AuthenticatedDeviceId, "dev_");
auth_id!(AuthSessionId, "ses_");
auth_id!(AuditEventId, "aud_");

#[derive(Debug)]
pub struct PairingInvitation {
    invitation_id: InvitationId,
    secret: SecretToken,
    expires_at_ms: u64,
}

impl PairingInvitation {
    pub fn invitation_id(&self) -> &InvitationId {
        &self.invitation_id
    }

    pub fn secret(&self) -> &SecretToken {
        &self.secret
    }

    pub fn expires_at_ms(&self) -> u64 {
        self.expires_at_ms
    }
}

#[derive(Debug)]
pub struct AuthenticatedSession {
    session_id: AuthSessionId,
    device_id: AuthenticatedDeviceId,
    session_secret: SecretToken,
    csrf_secret: SecretToken,
    expires_at_ms: u64,
}

impl AuthenticatedSession {
    pub fn session_id(&self) -> &AuthSessionId {
        &self.session_id
    }

    pub fn device_id(&self) -> &AuthenticatedDeviceId {
        &self.device_id
    }

    pub fn session_secret(&self) -> &SecretToken {
        &self.session_secret
    }

    pub fn csrf_secret(&self) -> &SecretToken {
        &self.csrf_secret
    }

    pub fn expires_at_ms(&self) -> u64 {
        self.expires_at_ms
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SessionContext {
    session_id: AuthSessionId,
    device_id: AuthenticatedDeviceId,
    created_at_ms: u64,
    expires_at_ms: u64,
}

impl SessionContext {
    pub fn session_id(&self) -> &AuthSessionId {
        &self.session_id
    }

    pub fn device_id(&self) -> &AuthenticatedDeviceId {
        &self.device_id
    }

    pub fn created_at_ms(&self) -> u64 {
        self.created_at_ms
    }

    pub fn expires_at_ms(&self) -> u64 {
        self.expires_at_ms
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct AuditRecord {
    pub sequence: u64,
    pub event_id: AuditEventId,
    pub occurred_at_ms: u64,
    pub kind: String,
    pub actor_device_id: Option<AuthenticatedDeviceId>,
    pub subject_id: Option<String>,
    pub detail: String,
    pub previous_hash: [u8; 32],
    pub event_hash: [u8; 32],
}

pub struct AuthStore {
    database_path: PathBuf,
    host_id: HostId,
    owner_id: OwnerId,
    limits: AuthLimits,
    connection: Mutex<Connection>,
}

impl AuthStore {
    pub fn open_or_create(
        secure_directory: &Path,
        host_id: HostId,
        owner_id: OwnerId,
        created_at_ms: u64,
        limits: AuthLimits,
    ) -> Result<Self, AuthError> {
        let limits = limits.validate()?;
        validate_timestamp(created_at_ms)?;
        validate_secure_directory(secure_directory)?;
        let database_path = secure_directory.join(AUTH_DATABASE_FILENAME);
        let (new_database, expected_identity) = prepare_database_file(&database_path)?;
        let flags = OpenFlags::SQLITE_OPEN_READ_WRITE
            | OpenFlags::SQLITE_OPEN_NO_MUTEX
            | OpenFlags::SQLITE_OPEN_NOFOLLOW;
        let connection = Connection::open_with_flags(&database_path, flags)?;
        configure_connection(&connection)?;
        validate_database_file(&database_path)?;
        let opened_identity = database_identity(&fs::symlink_metadata(&database_path)?)?;
        if opened_identity != expected_identity {
            return Err(AuthError::InsecurePath(
                "auth database identity changed while it was being opened",
            ));
        }
        let initialize = new_database || database_is_uninitialized(&connection)?;
        if initialize {
            initialize_schema(&connection, &host_id, &owner_id, created_at_ms)?;
            sync_directory(secure_directory)?;
        }
        validate_schema(&connection, &host_id, &owner_id, limits)?;
        Ok(Self {
            database_path,
            host_id,
            owner_id,
            limits,
            connection: Mutex::new(connection),
        })
    }

    pub fn database_path(&self) -> &Path {
        self.database_path.as_path()
    }

    pub fn host_id(&self) -> &HostId {
        &self.host_id
    }

    pub fn owner_id(&self) -> &OwnerId {
        &self.owner_id
    }

    pub fn create_invitation(
        &self,
        device_label: &str,
        now_ms: u64,
        ttl_ms: u64,
    ) -> Result<PairingInvitation, AuthError> {
        validate_label(device_label)?;
        let expires_at_ms = checked_expiry(now_ms, ttl_ms, self.limits.max_invitation_ttl_ms)?;
        let invitation_id = InvitationId::generate()?;
        let secret = SecretToken::generate(SecretKind::Invitation)?;
        let digest = secret.digest();
        let mut connection = self.lock_connection()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        prune_expired_auth_state(&transaction, now_ms)?;
        enforce_capacity(
            &transaction,
            "invitations",
            self.limits.max_invitations,
            "invitations",
        )?;
        transaction.execute(
            "INSERT INTO invitations (
                invitation_id, secret_hash, device_label, created_at_ms, expires_at_ms
             ) VALUES (?1, ?2, ?3, ?4, ?5)",
            params![
                invitation_id.as_str(),
                digest.as_slice(),
                device_label,
                db_timestamp(now_ms)?,
                db_timestamp(expires_at_ms)?,
            ],
        )?;
        append_audit(
            &transaction,
            self.limits,
            now_ms,
            "invitation-created",
            None,
            Some(invitation_id.as_str()),
            "created",
        )?;
        transaction.commit()?;
        Ok(PairingInvitation {
            invitation_id,
            secret,
            expires_at_ms,
        })
    }

    pub fn redeem_invitation(
        &self,
        invitation_secret: &str,
        now_ms: u64,
        session_ttl_ms: u64,
    ) -> Result<AuthenticatedSession, AuthError> {
        let invitation_digest = credential_digest(SecretKind::Invitation, invitation_secret)?;
        let expires_at_ms = checked_expiry(now_ms, session_ttl_ms, self.limits.max_session_ttl_ms)?;
        let device_id = AuthenticatedDeviceId::generate()?;
        let session_id = AuthSessionId::generate()?;
        let session_secret = SecretToken::generate(SecretKind::Session)?;
        let csrf_secret = SecretToken::generate(SecretKind::Csrf)?;
        let session_digest = session_secret.digest();
        let csrf_digest = csrf_secret.digest();
        let mut connection = self.lock_connection()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let invitation = transaction
            .query_row(
                "SELECT invitation_id, device_label, expires_at_ms, consumed_at_ms
                 FROM invitations WHERE secret_hash = ?1",
                params![invitation_digest.as_slice()],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, i64>(2)?,
                        row.get::<_, Option<i64>>(3)?,
                    ))
                },
            )
            .optional()?;
        let Some((stored_invitation_id, device_label, stored_expiry, consumed_at)) = invitation
        else {
            return Err(AuthError::Unauthenticated);
        };
        let invitation_id = InvitationId::from_database(stored_invitation_id)?;
        validate_label(&device_label)?;
        if consumed_at.is_some() || stored_expiry <= db_timestamp(now_ms)? {
            return Err(AuthError::InvitationUnavailable);
        }
        prune_expired_auth_state(&transaction, now_ms)?;
        enforce_capacity(&transaction, "devices", self.limits.max_devices, "devices")?;
        enforce_capacity(
            &transaction,
            "auth_sessions",
            self.limits.max_sessions,
            "sessions",
        )?;
        transaction.execute(
            "INSERT INTO devices (device_id, label, created_at_ms)
             VALUES (?1, ?2, ?3)",
            params![device_id.as_str(), device_label, db_timestamp(now_ms)?],
        )?;
        transaction.execute(
            "INSERT INTO auth_sessions (
                session_id, secret_hash, csrf_hash, device_id, created_at_ms, expires_at_ms
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![
                session_id.as_str(),
                session_digest.as_slice(),
                csrf_digest.as_slice(),
                device_id.as_str(),
                db_timestamp(now_ms)?,
                db_timestamp(expires_at_ms)?,
            ],
        )?;
        let changed = transaction.execute(
            "UPDATE invitations
             SET consumed_at_ms = ?1, consumed_device_id = ?2
             WHERE invitation_id = ?3 AND consumed_at_ms IS NULL AND expires_at_ms > ?1",
            params![
                db_timestamp(now_ms)?,
                device_id.as_str(),
                invitation_id.as_str(),
            ],
        )?;
        if changed != 1 {
            return Err(AuthError::InvitationUnavailable);
        }
        append_audit(
            &transaction,
            self.limits,
            now_ms,
            "pairing-redeemed",
            Some(device_id.as_str()),
            Some(invitation_id.as_str()),
            "device-and-session-created",
        )?;
        transaction.commit()?;
        Ok(AuthenticatedSession {
            session_id,
            device_id,
            session_secret,
            csrf_secret,
            expires_at_ms,
        })
    }

    pub fn authenticate(
        &self,
        session_secret: &str,
        now_ms: u64,
    ) -> Result<SessionContext, AuthError> {
        let digest = credential_digest(SecretKind::Session, session_secret)?;
        let connection = self.lock_connection()?;
        let row = connection
            .query_row(
                "SELECT s.session_id, s.device_id, s.created_at_ms, s.expires_at_ms
                 FROM auth_sessions AS s
                 JOIN devices AS d ON d.device_id = s.device_id
                 WHERE s.secret_hash = ?1
                   AND s.revoked_at_ms IS NULL
                   AND d.revoked_at_ms IS NULL
                   AND s.expires_at_ms > ?2",
                params![digest.as_slice(), db_timestamp(now_ms)?],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, i64>(2)?,
                        row.get::<_, i64>(3)?,
                    ))
                },
            )
            .optional()?;
        let Some((session_id, device_id, created_at_ms, expires_at_ms)) = row else {
            return Err(AuthError::Unauthenticated);
        };
        Ok(SessionContext {
            session_id: AuthSessionId::from_database(session_id)?,
            device_id: AuthenticatedDeviceId::from_database(device_id)?,
            created_at_ms: host_timestamp(created_at_ms)?,
            expires_at_ms: host_timestamp(expires_at_ms)?,
        })
    }

    pub fn verify_csrf(
        &self,
        session_id: &AuthSessionId,
        csrf_secret: &str,
        now_ms: u64,
    ) -> Result<(), AuthError> {
        let digest = credential_digest(SecretKind::Csrf, csrf_secret)?;
        let connection = self.lock_connection()?;
        let valid = connection
            .query_row(
                "SELECT 1
                 FROM auth_sessions AS s
                 JOIN devices AS d ON d.device_id = s.device_id
                 WHERE s.session_id = ?1
                   AND s.csrf_hash = ?2
                   AND s.revoked_at_ms IS NULL
                   AND d.revoked_at_ms IS NULL
                   AND s.expires_at_ms > ?3",
                params![
                    session_id.as_str(),
                    digest.as_slice(),
                    db_timestamp(now_ms)?,
                ],
                |_row| Ok(()),
            )
            .optional()?;
        valid.ok_or(AuthError::CsrfMismatch)
    }

    pub fn rotate_session(
        &self,
        old_session_secret: &str,
        now_ms: u64,
        ttl_ms: u64,
    ) -> Result<AuthenticatedSession, AuthError> {
        let old_digest = credential_digest(SecretKind::Session, old_session_secret)?;
        let expires_at_ms = checked_expiry(now_ms, ttl_ms, self.limits.max_session_ttl_ms)?;
        let new_session_id = AuthSessionId::generate()?;
        let new_session_secret = SecretToken::generate(SecretKind::Session)?;
        let new_csrf_secret = SecretToken::generate(SecretKind::Csrf)?;
        let new_session_digest = new_session_secret.digest();
        let new_csrf_digest = new_csrf_secret.digest();
        let mut connection = self.lock_connection()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let old = transaction
            .query_row(
                "SELECT s.session_id, s.device_id
                 FROM auth_sessions AS s
                 JOIN devices AS d ON d.device_id = s.device_id
                 WHERE s.secret_hash = ?1
                   AND s.revoked_at_ms IS NULL
                   AND d.revoked_at_ms IS NULL
                   AND s.expires_at_ms > ?2",
                params![old_digest.as_slice(), db_timestamp(now_ms)?],
                |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
            )
            .optional()?;
        let Some((old_session_id, device_id)) = old else {
            return Err(AuthError::Unauthenticated);
        };
        let old_session_id = AuthSessionId::from_database(old_session_id)?;
        let device_id = AuthenticatedDeviceId::from_database(device_id)?;
        prune_expired_auth_state(&transaction, now_ms)?;
        enforce_capacity_or_equal(
            &transaction,
            "auth_sessions",
            self.limits.max_sessions,
            "sessions",
        )?;
        transaction.execute(
            "INSERT INTO auth_sessions (
                session_id, secret_hash, csrf_hash, device_id, created_at_ms, expires_at_ms
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![
                new_session_id.as_str(),
                new_session_digest.as_slice(),
                new_csrf_digest.as_slice(),
                device_id.as_str(),
                db_timestamp(now_ms)?,
                db_timestamp(expires_at_ms)?,
            ],
        )?;
        let changed = transaction.execute(
            "DELETE FROM auth_sessions
             WHERE session_id = ?1 AND revoked_at_ms IS NULL",
            params![old_session_id.as_str()],
        )?;
        if changed != 1 {
            return Err(AuthError::SessionUnavailable);
        }
        append_audit(
            &transaction,
            self.limits,
            now_ms,
            "session-rotated",
            Some(device_id.as_str()),
            Some(old_session_id.as_str()),
            "old-session-revoked",
        )?;
        transaction.commit()?;
        Ok(AuthenticatedSession {
            session_id: new_session_id,
            device_id,
            session_secret: new_session_secret,
            csrf_secret: new_csrf_secret,
            expires_at_ms,
        })
    }

    pub fn revoke_session(
        &self,
        session_id: &AuthSessionId,
        now_ms: u64,
    ) -> Result<bool, AuthError> {
        validate_timestamp(now_ms)?;
        let mut connection = self.lock_connection()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let device_id = transaction
            .query_row(
                "SELECT device_id FROM auth_sessions WHERE session_id = ?1",
                params![session_id.as_str()],
                |row| row.get::<_, String>(0),
            )
            .optional()?;
        let actor_device_id = device_id
            .map(AuthenticatedDeviceId::from_database)
            .transpose()?;
        let changed = transaction.execute(
            "UPDATE auth_sessions SET revoked_at_ms = ?1
             WHERE session_id = ?2 AND revoked_at_ms IS NULL",
            params![db_timestamp(now_ms)?, session_id.as_str()],
        )?;
        if changed == 1 {
            append_audit(
                &transaction,
                self.limits,
                now_ms,
                "session-revoked",
                actor_device_id.as_ref().map(AuthenticatedDeviceId::as_str),
                Some(session_id.as_str()),
                "revoked",
            )?;
        }
        transaction.commit()?;
        Ok(changed == 1)
    }

    pub fn revoke_device(
        &self,
        device_id: &AuthenticatedDeviceId,
        now_ms: u64,
    ) -> Result<bool, AuthError> {
        validate_timestamp(now_ms)?;
        let mut connection = self.lock_connection()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let changed = transaction.execute(
            "UPDATE devices SET revoked_at_ms = ?1
             WHERE device_id = ?2 AND revoked_at_ms IS NULL",
            params![db_timestamp(now_ms)?, device_id.as_str()],
        )?;
        if changed == 1 {
            transaction.execute(
                "UPDATE auth_sessions SET revoked_at_ms = ?1
                 WHERE device_id = ?2 AND revoked_at_ms IS NULL",
                params![db_timestamp(now_ms)?, device_id.as_str()],
            )?;
            append_audit(
                &transaction,
                self.limits,
                now_ms,
                "device-revoked",
                Some(device_id.as_str()),
                Some(device_id.as_str()),
                "device-and-sessions-revoked",
            )?;
        }
        transaction.commit()?;
        Ok(changed == 1)
    }

    pub fn audit_records(&self, maximum: usize) -> Result<Vec<AuditRecord>, AuthError> {
        if maximum == 0
            || maximum > self.limits.max_audit_events
            || maximum > MAX_AUDIT_QUERY_RECORDS
        {
            return Err(AuthError::InvalidAuditLimit);
        }
        let connection = self.lock_connection()?;
        let mut statement = connection.prepare(
            "SELECT sequence, event_id, occurred_at_ms, kind, actor_device_id,
                    subject_id, detail, previous_hash, event_hash
             FROM audit_events ORDER BY sequence ASC LIMIT ?1",
        )?;
        let rows = statement.query_map(params![db_count(maximum)?], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, i64>(2)?,
                row.get::<_, String>(3)?,
                row.get::<_, Option<String>>(4)?,
                row.get::<_, Option<String>>(5)?,
                row.get::<_, String>(6)?,
                row.get::<_, Vec<u8>>(7)?,
                row.get::<_, Vec<u8>>(8)?,
            ))
        })?;
        let mut records = Vec::new();
        for row in rows {
            let (
                sequence,
                event_id,
                occurred_at_ms,
                kind,
                actor_device_id,
                subject_id,
                detail,
                previous_hash,
                event_hash,
            ) = row?;
            records.push(AuditRecord {
                sequence: host_timestamp(sequence)?,
                event_id: AuditEventId::from_database(event_id)?,
                occurred_at_ms: host_timestamp(occurred_at_ms)?,
                kind,
                actor_device_id: actor_device_id
                    .map(AuthenticatedDeviceId::from_database)
                    .transpose()?,
                subject_id,
                detail,
                previous_hash: digest_array(previous_hash)?,
                event_hash: digest_array(event_hash)?,
            });
        }
        Ok(records)
    }

    pub fn verify_audit_chain(&self) -> Result<usize, AuthError> {
        let connection = self.lock_connection()?;
        verify_audit_chain_connection(&connection, self.limits.max_audit_events)
    }

    fn lock_connection(&self) -> Result<std::sync::MutexGuard<'_, Connection>, AuthError> {
        self.connection.lock().map_err(|_| AuthError::LockPoisoned)
    }
}

fn validate_secure_directory(directory: &Path) -> Result<(), AuthError> {
    if !directory.is_absolute()
        || directory
            .components()
            .any(|component| matches!(component, Component::CurDir | Component::ParentDir))
    {
        return Err(AuthError::InsecurePath(
            "auth directory must be absolute and normalized",
        ));
    }
    let metadata = fs::symlink_metadata(directory)?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Err(AuthError::InsecurePath(
            "auth directory must be a real directory",
        ));
    }
    #[cfg(unix)]
    {
        if metadata.uid() != effective_uid()? {
            return Err(AuthError::InsecurePath(
                "auth directory must be owned by the current user",
            ));
        }
        if metadata.permissions().mode() & 0o777 != 0o700 {
            return Err(AuthError::InsecurePath(
                "auth directory permissions must be exactly 0700",
            ));
        }
    }
    let canonical = fs::canonicalize(directory)?;
    if canonical != directory {
        return Err(AuthError::InsecurePath(
            "auth directory cannot contain symlink components",
        ));
    }
    Ok(())
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct DatabaseIdentity {
    device: u64,
    inode: u64,
}

fn sync_directory(directory: &Path) -> Result<(), AuthError> {
    let handle = fs::File::open(directory)?;
    handle.sync_all()?;
    Ok(())
}

fn prepare_database_file(path: &Path) -> Result<(bool, DatabaseIdentity), AuthError> {
    match fs::symlink_metadata(path) {
        Ok(metadata) => {
            validate_database_file(path)?;
            Ok((false, database_identity(&metadata)?))
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            let mut options = OpenOptions::new();
            options.read(true).write(true).create_new(true);
            #[cfg(unix)]
            options.mode(0o600);
            let file = options.open(path)?;
            file.sync_all()?;
            let descriptor_identity = database_identity(&file.metadata()?)?;
            validate_database_file(path)?;
            let path_identity = database_identity(&fs::symlink_metadata(path)?)?;
            if descriptor_identity != path_identity {
                return Err(AuthError::InsecurePath(
                    "auth database identity changed during creation",
                ));
            }
            Ok((true, descriptor_identity))
        }
        Err(error) => Err(AuthError::Io(error)),
    }
}

fn database_identity(metadata: &fs::Metadata) -> Result<DatabaseIdentity, AuthError> {
    #[cfg(unix)]
    {
        Ok(DatabaseIdentity {
            device: metadata.dev(),
            inode: metadata.ino(),
        })
    }
    #[cfg(not(unix))]
    {
        let _metadata = metadata;
        Err(AuthError::InsecurePath(
            "the authentication store requires Unix file identity semantics",
        ))
    }
}

fn validate_database_file(path: &Path) -> Result<(), AuthError> {
    if path.file_name().and_then(|value| value.to_str()) != Some(AUTH_DATABASE_FILENAME) {
        return Err(AuthError::InsecurePath("unexpected auth database filename"));
    }
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err(AuthError::InsecurePath(
            "auth database must be a non-symlink regular file",
        ));
    }
    if metadata.len() > MAX_AUTH_DATABASE_BYTES {
        return Err(AuthError::InsecurePath(
            "auth database exceeds the configured size bound",
        ));
    }
    #[cfg(unix)]
    {
        if metadata.uid() != effective_uid()? {
            return Err(AuthError::InsecurePath(
                "auth database must be owned by the current user",
            ));
        }
        if metadata.nlink() != 1 {
            return Err(AuthError::InsecurePath(
                "auth database must not have additional hard links",
            ));
        }
        if metadata.permissions().mode() & 0o777 != 0o600 {
            return Err(AuthError::InsecurePath(
                "auth database permissions must be exactly 0600",
            ));
        }
    }
    Ok(())
}

#[cfg(unix)]
fn effective_uid() -> Result<u32, AuthError> {
    let status = fs::read_to_string("/proc/self/status")?;
    let line = status
        .lines()
        .find(|line| line.starts_with("Uid:"))
        .ok_or(AuthError::InsecurePath("effective uid is unavailable"))?;
    let value = line
        .split_ascii_whitespace()
        .nth(2)
        .ok_or(AuthError::InsecurePath("effective uid is malformed"))?;
    value
        .parse::<u32>()
        .map_err(|_| AuthError::InsecurePath("effective uid is malformed"))
}

fn configure_connection(connection: &Connection) -> Result<(), AuthError> {
    connection.busy_timeout(Duration::from_millis(2_000))?;
    connection.execute_batch(
        "PRAGMA foreign_keys = ON;
         PRAGMA trusted_schema = OFF;
         PRAGMA secure_delete = ON;
         PRAGMA temp_store = MEMORY;
         PRAGMA journal_mode = DELETE;
         PRAGMA synchronous = FULL;",
    )?;
    let foreign_keys: i64 = connection.query_row("PRAGMA foreign_keys", [], |row| row.get(0))?;
    let trusted_schema: i64 =
        connection.query_row("PRAGMA trusted_schema", [], |row| row.get(0))?;
    let secure_delete: i64 = connection.query_row("PRAGMA secure_delete", [], |row| row.get(0))?;
    let temp_store: i64 = connection.query_row("PRAGMA temp_store", [], |row| row.get(0))?;
    let synchronous: i64 = connection.query_row("PRAGMA synchronous", [], |row| row.get(0))?;
    let journal_mode: String = connection.query_row("PRAGMA journal_mode", [], |row| row.get(0))?;
    if foreign_keys != 1
        || trusted_schema != 0
        || secure_delete != 1
        || temp_store != 2
        || synchronous != 2
        || !journal_mode.eq_ignore_ascii_case("delete")
    {
        return Err(AuthError::CorruptDatabase(
            "required SQLite safety pragmas are unavailable",
        ));
    }
    configure_database_size_bound(connection)
}

fn configure_database_size_bound(connection: &Connection) -> Result<(), AuthError> {
    let page_size: i64 = connection.query_row("PRAGMA page_size", [], |row| row.get(0))?;
    let page_size = u64::try_from(page_size)
        .map_err(|_| AuthError::CorruptDatabase("invalid SQLite page size"))?;
    if !(512..=65_536).contains(&page_size) || !page_size.is_power_of_two() {
        return Err(AuthError::CorruptDatabase("invalid SQLite page size"));
    }
    let maximum_pages = MAX_AUTH_DATABASE_BYTES / page_size;
    let maximum_pages = i64::try_from(maximum_pages)
        .map_err(|_| AuthError::CorruptDatabase("invalid SQLite page bound"))?;
    connection.pragma_update(None, "max_page_count", maximum_pages)?;
    let configured_maximum: i64 =
        connection.query_row("PRAGMA max_page_count", [], |row| row.get(0))?;
    let page_count: i64 = connection.query_row("PRAGMA page_count", [], |row| row.get(0))?;
    let configured_maximum = u64::try_from(configured_maximum)
        .map_err(|_| AuthError::CorruptDatabase("invalid SQLite page bound"))?;
    let page_count = u64::try_from(page_count)
        .map_err(|_| AuthError::CorruptDatabase("invalid SQLite page count"))?;
    let current_bytes = page_count
        .checked_mul(page_size)
        .ok_or(AuthError::CorruptDatabase("SQLite size overflow"))?;
    if configured_maximum > u64::try_from(maximum_pages).unwrap_or(0)
        || current_bytes > MAX_AUTH_DATABASE_BYTES
    {
        return Err(AuthError::CorruptDatabase(
            "auth database exceeds the SQLite page bound",
        ));
    }
    Ok(())
}

fn database_is_uninitialized(connection: &Connection) -> Result<bool, AuthError> {
    let application_id: i64 =
        connection.query_row("PRAGMA application_id", [], |row| row.get(0))?;
    let user_version: i64 = connection.query_row("PRAGMA user_version", [], |row| row.get(0))?;
    let user_table_count: i64 = connection.query_row(
        "SELECT COUNT(*) FROM sqlite_schema
         WHERE type = 'table' AND name NOT LIKE 'sqlite_%'",
        [],
        |row| row.get(0),
    )?;
    Ok(application_id == 0 && user_version == 0 && user_table_count == 0)
}

fn initialize_schema(
    connection: &Connection,
    host_id: &HostId,
    owner_id: &OwnerId,
    created_at_ms: u64,
) -> Result<(), AuthError> {
    connection.execute_batch("BEGIN IMMEDIATE;")?;
    let result = (|| {
        connection.execute_batch(SCHEMA)?;
        connection.execute(
            "INSERT INTO auth_metadata (
                singleton, schema_version, host_id, owner_id, created_at_ms
             ) VALUES (1, ?1, ?2, ?3, ?4)",
            params![
                AUTH_SCHEMA_VERSION,
                host_id.as_str(),
                owner_id.as_str(),
                db_timestamp(created_at_ms)?,
            ],
        )?;
        connection.pragma_update(None, "application_id", AUTH_APPLICATION_ID)?;
        connection.pragma_update(None, "user_version", AUTH_SCHEMA_VERSION)?;
        Ok::<(), AuthError>(())
    })();
    match result {
        Ok(()) => {
            connection.execute_batch("COMMIT;")?;
            Ok(())
        }
        Err(error) => {
            let _rollback = connection.execute_batch("ROLLBACK;");
            Err(error)
        }
    }
}

fn validate_schema(
    connection: &Connection,
    host_id: &HostId,
    owner_id: &OwnerId,
    limits: AuthLimits,
) -> Result<(), AuthError> {
    let integrity: String = connection.query_row("PRAGMA quick_check", [], |row| row.get(0))?;
    if integrity != "ok" {
        return Err(AuthError::CorruptDatabase("SQLite quick_check failed"));
    }
    let application_id: i64 =
        connection.query_row("PRAGMA application_id", [], |row| row.get(0))?;
    let user_version: i64 = connection.query_row("PRAGMA user_version", [], |row| row.get(0))?;
    if application_id != AUTH_APPLICATION_ID || user_version != AUTH_SCHEMA_VERSION {
        return Err(AuthError::CorruptDatabase(
            "unsupported auth schema identity",
        ));
    }
    let invalid_metadata: i64 = connection.query_row(
        "SELECT
             (SELECT COUNT(*) FROM auth_metadata) != 1
             OR EXISTS (
                 SELECT 1 FROM auth_metadata
                 WHERE singleton != 1
                    OR schema_version != ?1
                    OR length(CAST(host_id AS BLOB)) NOT BETWEEN 1 AND ?2
                    OR length(CAST(owner_id AS BLOB)) NOT BETWEEN 1 AND ?2
                    OR created_at_ms < 0
             )",
        params![AUTH_SCHEMA_VERSION, db_count(MAX_OPAQUE_ID_BYTES)?],
        |row| row.get(0),
    )?;
    if invalid_metadata != 0 {
        return Err(AuthError::CorruptDatabase("invalid auth metadata row"));
    }
    let metadata = connection
        .query_row(
            "SELECT schema_version, host_id, owner_id
             FROM auth_metadata WHERE singleton = 1",
            [],
            |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                ))
            },
        )
        .optional()?;
    let Some((schema_version, stored_host, stored_owner)) = metadata else {
        return Err(AuthError::CorruptDatabase("missing auth metadata"));
    };
    if schema_version != AUTH_SCHEMA_VERSION {
        return Err(AuthError::CorruptDatabase(
            "unsupported auth schema version",
        ));
    }
    if stored_host != host_id.as_str() {
        return Err(AuthError::WrongHost);
    }
    if stored_owner != owner_id.as_str() {
        return Err(AuthError::WrongOwner);
    }
    let expected_tables = BTreeSet::from([
        "audit_events".to_owned(),
        "auth_metadata".to_owned(),
        "auth_sessions".to_owned(),
        "devices".to_owned(),
        "invitations".to_owned(),
    ]);
    let actual_tables = schema_objects(connection, "table")?;
    if actual_tables != expected_tables {
        return Err(AuthError::CorruptDatabase("unexpected auth tables"));
    }
    if !schema_objects(connection, "view")?.is_empty() {
        return Err(AuthError::CorruptDatabase("unexpected auth views"));
    }
    validate_strict_tables(connection, &expected_tables)?;
    validate_table_definitions(connection)?;
    if !schema_objects(connection, "index")?.is_empty() {
        return Err(AuthError::CorruptDatabase("unexpected auth indexes"));
    }
    let expected_triggers = BTreeSet::from([
        "audit_events_no_delete".to_owned(),
        "audit_events_no_update".to_owned(),
    ]);
    if schema_objects(connection, "trigger")? != expected_triggers {
        return Err(AuthError::CorruptDatabase(
            "missing audit immutability triggers",
        ));
    }
    validate_trigger_definitions(connection)?;
    validate_foreign_keys(connection)?;
    validate_table_columns(
        connection,
        "auth_metadata",
        &[
            ("singleton", "INTEGER", 1),
            ("schema_version", "INTEGER", 0),
            ("host_id", "TEXT", 0),
            ("owner_id", "TEXT", 0),
            ("created_at_ms", "INTEGER", 0),
        ],
    )?;
    validate_table_columns(
        connection,
        "devices",
        &[
            ("device_id", "TEXT", 1),
            ("label", "TEXT", 0),
            ("created_at_ms", "INTEGER", 0),
            ("revoked_at_ms", "INTEGER", 0),
        ],
    )?;
    validate_table_columns(
        connection,
        "invitations",
        &[
            ("invitation_id", "TEXT", 1),
            ("secret_hash", "BLOB", 0),
            ("device_label", "TEXT", 0),
            ("created_at_ms", "INTEGER", 0),
            ("expires_at_ms", "INTEGER", 0),
            ("consumed_at_ms", "INTEGER", 0),
            ("consumed_device_id", "TEXT", 0),
        ],
    )?;
    validate_table_columns(
        connection,
        "auth_sessions",
        &[
            ("session_id", "TEXT", 1),
            ("secret_hash", "BLOB", 0),
            ("csrf_hash", "BLOB", 0),
            ("device_id", "TEXT", 0),
            ("created_at_ms", "INTEGER", 0),
            ("expires_at_ms", "INTEGER", 0),
            ("revoked_at_ms", "INTEGER", 0),
            ("replaced_by_session_id", "TEXT", 0),
        ],
    )?;
    validate_table_columns(
        connection,
        "audit_events",
        &[
            ("sequence", "INTEGER", 1),
            ("event_id", "TEXT", 0),
            ("occurred_at_ms", "INTEGER", 0),
            ("kind", "TEXT", 0),
            ("actor_device_id", "TEXT", 0),
            ("subject_id", "TEXT", 0),
            ("detail", "TEXT", 0),
            ("previous_hash", "BLOB", 0),
            ("event_hash", "BLOB", 0),
        ],
    )?;
    for (table, maximum, label) in [
        ("devices", limits.max_devices, "devices"),
        ("invitations", limits.max_invitations, "invitations"),
        ("auth_sessions", limits.max_sessions, "sessions"),
        ("audit_events", limits.max_audit_events, "audit events"),
    ] {
        enforce_capacity_or_equal(connection, table, maximum, label)?;
    }
    validate_stored_row_bounds(connection)?;
    validate_stored_identifiers(connection)?;
    let _verified_events = verify_audit_chain_connection(connection, limits.max_audit_events)?;
    Ok(())
}

fn invalid_row_exists(connection: &Connection, query: &str) -> Result<bool, AuthError> {
    let value: i64 = connection.query_row(query, [], |row| row.get(0))?;
    match value {
        0 => Ok(false),
        1 => Ok(true),
        _ => Err(AuthError::CorruptDatabase("invalid SQLite boolean result")),
    }
}

fn validate_stored_row_bounds(connection: &Connection) -> Result<(), AuthError> {
    let device_query = format!(
        "SELECT EXISTS(
             SELECT 1 FROM devices
             WHERE length(CAST(device_id AS BLOB)) NOT BETWEEN 1 AND {MAX_AUTH_RECORD_ID_BYTES}
                OR length(CAST(label AS BLOB)) NOT BETWEEN 1 AND {MAX_LABEL_BYTES}
                OR created_at_ms < 0
                OR (revoked_at_ms IS NOT NULL AND revoked_at_ms < created_at_ms)
         )"
    );
    let invitation_query = format!(
        "SELECT EXISTS(
             SELECT 1 FROM invitations
             WHERE length(CAST(invitation_id AS BLOB)) NOT BETWEEN 1 AND {MAX_AUTH_RECORD_ID_BYTES}
                OR length(CAST(secret_hash AS BLOB)) != {DIGEST_BYTES}
                OR length(CAST(device_label AS BLOB)) NOT BETWEEN 1 AND {MAX_LABEL_BYTES}
                OR created_at_ms < 0
                OR expires_at_ms <= created_at_ms
                OR ((consumed_at_ms IS NULL) != (consumed_device_id IS NULL))
                OR (
                    consumed_at_ms IS NOT NULL
                    AND (consumed_at_ms < created_at_ms OR consumed_at_ms >= expires_at_ms)
                )
                OR (
                    consumed_device_id IS NOT NULL
                    AND length(CAST(consumed_device_id AS BLOB)) NOT BETWEEN 1 AND {MAX_AUTH_RECORD_ID_BYTES}
                )
         )"
    );
    let session_query = format!(
        "SELECT EXISTS(
             SELECT 1 FROM auth_sessions
             WHERE length(CAST(session_id AS BLOB)) NOT BETWEEN 1 AND {MAX_AUTH_RECORD_ID_BYTES}
                OR length(CAST(secret_hash AS BLOB)) != {DIGEST_BYTES}
                OR length(CAST(csrf_hash AS BLOB)) != {DIGEST_BYTES}
                OR length(CAST(device_id AS BLOB)) NOT BETWEEN 1 AND {MAX_AUTH_RECORD_ID_BYTES}
                OR created_at_ms < 0
                OR expires_at_ms <= created_at_ms
                OR (revoked_at_ms IS NOT NULL AND revoked_at_ms < created_at_ms)
                OR (
                    replaced_by_session_id IS NOT NULL
                    AND (
                        length(CAST(replaced_by_session_id AS BLOB)) NOT BETWEEN 1 AND {MAX_AUTH_RECORD_ID_BYTES}
                        OR replaced_by_session_id = session_id
                    )
                )
         )"
    );
    let audit_query = format!(
        "SELECT EXISTS(
             SELECT 1 FROM audit_events
             WHERE sequence <= 0
                OR length(CAST(event_id AS BLOB)) NOT BETWEEN 1 AND {MAX_AUTH_RECORD_ID_BYTES}
                OR occurred_at_ms < 0
                OR length(CAST(kind AS BLOB)) NOT BETWEEN 1 AND {MAX_AUDIT_KIND_BYTES}
                OR (
                    actor_device_id IS NOT NULL
                    AND length(CAST(actor_device_id AS BLOB)) NOT BETWEEN 1 AND {MAX_AUTH_RECORD_ID_BYTES}
                )
                OR (
                    subject_id IS NOT NULL
                    AND length(CAST(subject_id AS BLOB)) NOT BETWEEN 1 AND {MAX_OPAQUE_ID_BYTES}
                )
                OR length(CAST(detail AS BLOB)) NOT BETWEEN 1 AND {MAX_AUDIT_DETAIL_BYTES}
                OR length(CAST(previous_hash AS BLOB)) != {DIGEST_BYTES}
                OR length(CAST(event_hash AS BLOB)) != {DIGEST_BYTES}
         )"
    );
    for (query, message) in [
        (device_query.as_str(), "invalid stored device row"),
        (invitation_query.as_str(), "invalid stored invitation row"),
        (session_query.as_str(), "invalid stored session row"),
        (audit_query.as_str(), "invalid stored audit row"),
    ] {
        if invalid_row_exists(connection, query)? {
            return Err(AuthError::CorruptDatabase(message));
        }
    }
    Ok(())
}

fn validate_stored_identifiers(connection: &Connection) -> Result<(), AuthError> {
    let mut device_statement =
        connection.prepare("SELECT device_id, label FROM devices ORDER BY device_id")?;
    let device_rows = device_statement.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;
    for row in device_rows {
        let (device_id, label) = row?;
        AuthenticatedDeviceId::from_database(device_id)
            .map_err(|_| AuthError::CorruptDatabase("invalid stored device id"))?;
        validate_label(&label)
            .map_err(|_| AuthError::CorruptDatabase("invalid stored device label"))?;
    }

    let mut invitation_statement = connection.prepare(
        "SELECT invitation_id, device_label, consumed_device_id
         FROM invitations ORDER BY invitation_id",
    )?;
    let invitation_rows = invitation_statement.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, Option<String>>(2)?,
        ))
    })?;
    for row in invitation_rows {
        let (invitation_id, label, consumed_device_id) = row?;
        InvitationId::from_database(invitation_id)
            .map_err(|_| AuthError::CorruptDatabase("invalid stored invitation id"))?;
        validate_label(&label)
            .map_err(|_| AuthError::CorruptDatabase("invalid stored invitation label"))?;
        if let Some(device_id) = consumed_device_id {
            AuthenticatedDeviceId::from_database(device_id)
                .map_err(|_| AuthError::CorruptDatabase("invalid stored invitation device id"))?;
        }
    }

    let mut session_statement = connection.prepare(
        "SELECT session_id, device_id, replaced_by_session_id
         FROM auth_sessions ORDER BY session_id",
    )?;
    let session_rows = session_statement.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, Option<String>>(2)?,
        ))
    })?;
    for row in session_rows {
        let (session_id, device_id, replacement_id) = row?;
        AuthSessionId::from_database(session_id)
            .map_err(|_| AuthError::CorruptDatabase("invalid stored session id"))?;
        AuthenticatedDeviceId::from_database(device_id)
            .map_err(|_| AuthError::CorruptDatabase("invalid stored session device id"))?;
        if let Some(replacement_id) = replacement_id {
            AuthSessionId::from_database(replacement_id)
                .map_err(|_| AuthError::CorruptDatabase("invalid stored replacement session id"))?;
        }
    }
    Ok(())
}

fn validate_audit_subject(value: &str) -> Result<(), AuthError> {
    if value.is_empty() || value.len() > MAX_OPAQUE_ID_BYTES || value.chars().any(char::is_control)
    {
        return Err(AuthError::CorruptDatabase("invalid stored audit subject"));
    }
    Ok(())
}

fn verify_audit_chain_connection(
    connection: &Connection,
    maximum_events: usize,
) -> Result<usize, AuthError> {
    let mut statement = connection.prepare(
        "SELECT sequence, event_id, occurred_at_ms, kind, actor_device_id,
                subject_id, detail, previous_hash, event_hash
         FROM audit_events ORDER BY sequence ASC",
    )?;
    let rows = statement.query_map([], |row| {
        Ok((
            row.get::<_, i64>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, i64>(2)?,
            row.get::<_, String>(3)?,
            row.get::<_, Option<String>>(4)?,
            row.get::<_, Option<String>>(5)?,
            row.get::<_, String>(6)?,
            row.get::<_, Vec<u8>>(7)?,
            row.get::<_, Vec<u8>>(8)?,
        ))
    })?;
    let mut previous = [0_u8; DIGEST_BYTES];
    let mut count = 0_usize;
    for row in rows {
        let (
            sequence,
            event_id,
            occurred_at_ms,
            kind,
            actor_device_id,
            subject_id,
            detail,
            stored_previous,
            event_hash,
        ) = row?;
        let sequence = host_timestamp(sequence)?;
        let event_id = AuditEventId::from_database(event_id)
            .map_err(|_| AuthError::CorruptDatabase("invalid stored audit event id"))?;
        let occurred_at_ms = host_timestamp(occurred_at_ms)?;
        validate_audit_text(&kind, MAX_AUDIT_KIND_BYTES)
            .map_err(|_| AuthError::CorruptDatabase("invalid stored audit kind"))?;
        validate_audit_text(&detail, MAX_AUDIT_DETAIL_BYTES)
            .map_err(|_| AuthError::CorruptDatabase("invalid stored audit detail"))?;
        let actor_device_id = actor_device_id
            .map(AuthenticatedDeviceId::from_database)
            .transpose()
            .map_err(|_| AuthError::CorruptDatabase("invalid stored audit actor"))?;
        if let Some(subject_id) = subject_id.as_deref() {
            validate_audit_subject(subject_id)?;
        }
        let stored_previous = digest_array(stored_previous)?;
        let event_hash = digest_array(event_hash)?;
        if stored_previous != previous {
            return Err(AuthError::AuditCorrupt);
        }
        let expected = audit_hash(AuditHashInput {
            sequence,
            event_id: event_id.as_str(),
            occurred_at_ms,
            kind: kind.as_str(),
            actor_device_id: actor_device_id.as_ref().map(AuthenticatedDeviceId::as_str),
            subject_id: subject_id.as_deref(),
            detail: detail.as_str(),
            previous_hash: &previous,
        });
        if expected != event_hash {
            return Err(AuthError::AuditCorrupt);
        }
        count = count.checked_add(1).ok_or(AuthError::AuditCorrupt)?;
        if count > maximum_events {
            return Err(AuthError::AuditCorrupt);
        }
        previous = event_hash;
    }
    Ok(count)
}

fn validate_strict_tables(
    connection: &Connection,
    expected_tables: &BTreeSet<String>,
) -> Result<(), AuthError> {
    let mut statement = connection.prepare(
        "SELECT name, type, wr, strict
         FROM pragma_table_list
         WHERE schema = 'main' AND name NOT LIKE 'sqlite_%'
         ORDER BY name",
    )?;
    let rows = statement.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, i64>(2)?,
            row.get::<_, i64>(3)?,
        ))
    })?;
    let mut actual = BTreeSet::new();
    for row in rows {
        let (name, kind, without_rowid, strict) = row?;
        if kind != "table" || without_rowid != 0 || strict != 1 {
            return Err(AuthError::CorruptDatabase(
                "auth tables must remain strict rowid tables",
            ));
        }
        actual.insert(name);
    }
    if &actual != expected_tables {
        return Err(AuthError::CorruptDatabase("unexpected strict auth tables"));
    }
    Ok(())
}

fn normalize_schema_sql(value: &str) -> String {
    value
        .split_ascii_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .trim_end_matches(';')
        .to_ascii_lowercase()
}

fn validate_table_definitions(connection: &Connection) -> Result<(), AuthError> {
    let expected = BTreeSet::from([
        (
            "audit_events".to_owned(),
            normalize_schema_sql(
                "CREATE TABLE audit_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    occurred_at_ms INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    actor_device_id TEXT,
                    subject_id TEXT,
                    detail TEXT NOT NULL,
                    previous_hash BLOB NOT NULL CHECK (length(previous_hash) = 32),
                    event_hash BLOB NOT NULL UNIQUE CHECK (length(event_hash) = 32)
                 ) STRICT",
            ),
        ),
        (
            "auth_metadata".to_owned(),
            normalize_schema_sql(
                "CREATE TABLE auth_metadata (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL,
                    host_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL
                 ) STRICT",
            ),
        ),
        (
            "auth_sessions".to_owned(),
            normalize_schema_sql(
                "CREATE TABLE auth_sessions (
                    session_id TEXT PRIMARY KEY,
                    secret_hash BLOB NOT NULL UNIQUE CHECK (length(secret_hash) = 32),
                    csrf_hash BLOB NOT NULL CHECK (length(csrf_hash) = 32),
                    device_id TEXT NOT NULL REFERENCES devices(device_id),
                    created_at_ms INTEGER NOT NULL,
                    expires_at_ms INTEGER NOT NULL,
                    revoked_at_ms INTEGER,
                    replaced_by_session_id TEXT REFERENCES auth_sessions(session_id)
                 ) STRICT",
            ),
        ),
        (
            "devices".to_owned(),
            normalize_schema_sql(
                "CREATE TABLE devices (
                    device_id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    revoked_at_ms INTEGER
                 ) STRICT",
            ),
        ),
        (
            "invitations".to_owned(),
            normalize_schema_sql(
                "CREATE TABLE invitations (
                    invitation_id TEXT PRIMARY KEY,
                    secret_hash BLOB NOT NULL UNIQUE CHECK (length(secret_hash) = 32),
                    device_label TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    expires_at_ms INTEGER NOT NULL,
                    consumed_at_ms INTEGER,
                    consumed_device_id TEXT REFERENCES devices(device_id)
                 ) STRICT",
            ),
        ),
    ]);
    let mut statement = connection.prepare(
        "SELECT name, sql FROM sqlite_schema
         WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
         ORDER BY name",
    )?;
    let rows = statement.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;
    let mut actual = BTreeSet::new();
    for row in rows {
        let (name, sql) = row?;
        actual.insert((name, normalize_schema_sql(&sql)));
    }
    if actual != expected {
        return Err(AuthError::CorruptDatabase(
            "unexpected authentication table definition",
        ));
    }
    Ok(())
}

fn validate_trigger_definitions(connection: &Connection) -> Result<(), AuthError> {
    let expected = BTreeSet::from([
        (
            "audit_events_no_delete".to_owned(),
            normalize_schema_sql(
                "CREATE TRIGGER audit_events_no_delete
                 BEFORE DELETE ON audit_events
                 BEGIN
                     SELECT RAISE(ABORT, 'audit events are immutable');
                 END",
            ),
        ),
        (
            "audit_events_no_update".to_owned(),
            normalize_schema_sql(
                "CREATE TRIGGER audit_events_no_update
                 BEFORE UPDATE ON audit_events
                 BEGIN
                     SELECT RAISE(ABORT, 'audit events are immutable');
                 END",
            ),
        ),
    ]);
    let mut statement = connection.prepare(
        "SELECT name, sql FROM sqlite_schema
         WHERE type = 'trigger' ORDER BY name",
    )?;
    let rows = statement.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;
    let mut actual = BTreeSet::new();
    for row in rows {
        let (name, sql) = row?;
        actual.insert((name, normalize_schema_sql(&sql)));
    }
    if actual != expected {
        return Err(AuthError::CorruptDatabase(
            "unexpected audit trigger definition",
        ));
    }
    Ok(())
}

fn validate_foreign_keys(connection: &Connection) -> Result<(), AuthError> {
    let expected = BTreeSet::from([
        (
            "invitations".to_owned(),
            "devices".to_owned(),
            "consumed_device_id".to_owned(),
            "device_id".to_owned(),
            "NO ACTION".to_owned(),
            "NO ACTION".to_owned(),
            "NONE".to_owned(),
        ),
        (
            "auth_sessions".to_owned(),
            "devices".to_owned(),
            "device_id".to_owned(),
            "device_id".to_owned(),
            "NO ACTION".to_owned(),
            "NO ACTION".to_owned(),
            "NONE".to_owned(),
        ),
        (
            "auth_sessions".to_owned(),
            "auth_sessions".to_owned(),
            "replaced_by_session_id".to_owned(),
            "session_id".to_owned(),
            "NO ACTION".to_owned(),
            "NO ACTION".to_owned(),
            "NONE".to_owned(),
        ),
    ]);
    let mut actual = BTreeSet::new();
    for (table, pragma) in [
        ("auth_metadata", "PRAGMA foreign_key_list(auth_metadata)"),
        ("devices", "PRAGMA foreign_key_list(devices)"),
        ("invitations", "PRAGMA foreign_key_list(invitations)"),
        ("auth_sessions", "PRAGMA foreign_key_list(auth_sessions)"),
        ("audit_events", "PRAGMA foreign_key_list(audit_events)"),
    ] {
        let mut statement = connection.prepare(pragma)?;
        let rows = statement.query_map([], |row| {
            Ok((
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
                row.get::<_, String>(4)?,
                row.get::<_, String>(5)?,
                row.get::<_, String>(6)?,
                row.get::<_, String>(7)?,
            ))
        })?;
        for row in rows {
            let (target, from, to, on_update, on_delete, matching) = row?;
            actual.insert((
                table.to_owned(),
                target,
                from,
                to,
                on_update,
                on_delete,
                matching,
            ));
        }
    }
    if actual != expected {
        return Err(AuthError::CorruptDatabase(
            "unexpected authentication foreign keys",
        ));
    }
    let mut statement = connection.prepare("PRAGMA foreign_key_check")?;
    let mut rows = statement.query([])?;
    if rows.next()?.is_some() {
        return Err(AuthError::CorruptDatabase(
            "authentication foreign key check failed",
        ));
    }
    Ok(())
}

fn schema_objects(connection: &Connection, kind: &str) -> Result<BTreeSet<String>, AuthError> {
    let mut statement = connection.prepare(
        "SELECT name FROM sqlite_schema
         WHERE type = ?1 AND name NOT LIKE 'sqlite_%' ORDER BY name",
    )?;
    let rows = statement.query_map(params![kind], |row| row.get::<_, String>(0))?;
    let mut names = BTreeSet::new();
    for row in rows {
        names.insert(row?);
    }
    Ok(names)
}

fn validate_table_columns(
    connection: &Connection,
    table: &str,
    expected: &[(&str, &str, i64)],
) -> Result<(), AuthError> {
    let pragma = match table {
        "auth_metadata" => "PRAGMA table_info(auth_metadata)",
        "devices" => "PRAGMA table_info(devices)",
        "invitations" => "PRAGMA table_info(invitations)",
        "auth_sessions" => "PRAGMA table_info(auth_sessions)",
        "audit_events" => "PRAGMA table_info(audit_events)",
        _ => return Err(AuthError::CorruptDatabase("unknown schema table")),
    };
    let mut statement = connection.prepare(pragma)?;
    let rows = statement.query_map([], |row| {
        Ok((
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, i64>(5)?,
        ))
    })?;
    let mut actual = Vec::new();
    for row in rows {
        actual.push(row?);
    }
    let expected = expected
        .iter()
        .map(|(name, kind, primary_key)| ((*name).to_owned(), (*kind).to_owned(), *primary_key))
        .collect::<Vec<_>>();
    if actual != expected {
        return Err(AuthError::CorruptDatabase("unexpected auth table columns"));
    }
    Ok(())
}

fn append_audit(
    transaction: &Transaction<'_>,
    limits: AuthLimits,
    occurred_at_ms: u64,
    kind: &str,
    actor_device_id: Option<&str>,
    subject_id: Option<&str>,
    detail: &str,
) -> Result<(), AuthError> {
    validate_audit_text(kind, MAX_AUDIT_KIND_BYTES)?;
    validate_audit_text(detail, MAX_AUDIT_DETAIL_BYTES)?;
    enforce_capacity(
        transaction,
        "audit_events",
        limits.max_audit_events,
        "audit events",
    )?;
    let previous = transaction
        .query_row(
            "SELECT sequence, event_hash FROM audit_events ORDER BY sequence DESC LIMIT 1",
            [],
            |row| Ok((row.get::<_, i64>(0)?, row.get::<_, Vec<u8>>(1)?)),
        )
        .optional()?;
    let (sequence, previous_hash) = match previous {
        Some((last_sequence, hash)) => (
            host_timestamp(last_sequence)?.saturating_add(1),
            digest_array(hash)?,
        ),
        None => (1, [0_u8; DIGEST_BYTES]),
    };
    let event_id = AuditEventId::generate()?;
    let event_hash = audit_hash(AuditHashInput {
        sequence,
        event_id: event_id.as_str(),
        occurred_at_ms,
        kind,
        actor_device_id,
        subject_id,
        detail,
        previous_hash: &previous_hash,
    });
    transaction.execute(
        "INSERT INTO audit_events (
            sequence, event_id, occurred_at_ms, kind, actor_device_id, subject_id,
            detail, previous_hash, event_hash
         ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
        params![
            db_timestamp(sequence)?,
            event_id.as_str(),
            db_timestamp(occurred_at_ms)?,
            kind,
            actor_device_id,
            subject_id,
            detail,
            previous_hash.as_slice(),
            event_hash.as_slice(),
        ],
    )?;
    Ok(())
}

struct AuditHashInput<'a> {
    sequence: u64,
    event_id: &'a str,
    occurred_at_ms: u64,
    kind: &'a str,
    actor_device_id: Option<&'a str>,
    subject_id: Option<&'a str>,
    detail: &'a str,
    previous_hash: &'a [u8; 32],
}

fn audit_hash(input: AuditHashInput<'_>) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(b"termdeck-web-auth/v1/audit\0");
    hasher.update(input.sequence.to_be_bytes());
    hash_field(&mut hasher, input.event_id);
    hasher.update(input.occurred_at_ms.to_be_bytes());
    hash_field(&mut hasher, input.kind);
    hash_optional_field(&mut hasher, input.actor_device_id);
    hash_optional_field(&mut hasher, input.subject_id);
    hash_field(&mut hasher, input.detail);
    hasher.update(input.previous_hash);
    hasher.finalize().into()
}

fn hash_field(hasher: &mut Sha256, value: &str) {
    hasher.update(u64::try_from(value.len()).unwrap_or(u64::MAX).to_be_bytes());
    hasher.update(value.as_bytes());
}

fn hash_optional_field(hasher: &mut Sha256, value: Option<&str>) {
    match value {
        Some(value) => {
            hasher.update([1]);
            hash_field(hasher, value);
        }
        None => hasher.update([0]),
    }
}

fn validate_label(label: &str) -> Result<(), AuthError> {
    if label.is_empty() || label.len() > MAX_LABEL_BYTES || label.chars().any(char::is_control) {
        return Err(AuthError::InvalidLabel);
    }
    Ok(())
}

fn validate_audit_text(value: &str, maximum: usize) -> Result<(), AuthError> {
    if value.is_empty() || value.len() > maximum || value.chars().any(char::is_control) {
        return Err(AuthError::InvalidAuditText);
    }
    Ok(())
}

fn credential_digest(kind: SecretKind, value: &str) -> Result<[u8; 32], AuthError> {
    digest_validated_secret(kind, value).map_err(|_error| AuthError::Unauthenticated)
}

fn checked_expiry(now_ms: u64, ttl_ms: u64, maximum_ttl_ms: u64) -> Result<u64, AuthError> {
    validate_timestamp(now_ms)?;
    if ttl_ms == 0 || ttl_ms > maximum_ttl_ms {
        return Err(AuthError::InvalidTime);
    }
    let expiry = now_ms.checked_add(ttl_ms).ok_or(AuthError::InvalidTime)?;
    validate_timestamp(expiry)?;
    Ok(expiry)
}

fn validate_timestamp(value: u64) -> Result<(), AuthError> {
    if value > i64::MAX as u64 {
        return Err(AuthError::InvalidTime);
    }
    Ok(())
}

fn db_timestamp(value: u64) -> Result<i64, AuthError> {
    i64::try_from(value).map_err(|_| AuthError::InvalidTime)
}

fn host_timestamp(value: i64) -> Result<u64, AuthError> {
    u64::try_from(value).map_err(|_| AuthError::CorruptDatabase("negative timestamp"))
}

fn db_count(value: usize) -> Result<i64, AuthError> {
    i64::try_from(value).map_err(|_| AuthError::InvalidLimits)
}

fn prune_expired_auth_state(transaction: &Transaction<'_>, now_ms: u64) -> Result<(), AuthError> {
    let now = db_timestamp(now_ms)?;
    transaction.execute(
        "DELETE FROM invitations
         WHERE consumed_at_ms IS NOT NULL OR expires_at_ms <= ?1",
        params![now],
    )?;
    transaction.execute(
        "UPDATE auth_sessions
         SET replaced_by_session_id = NULL
         WHERE replaced_by_session_id IN (
             SELECT session_id FROM auth_sessions
             WHERE revoked_at_ms IS NOT NULL OR expires_at_ms <= ?1
         )",
        params![now],
    )?;
    transaction.execute(
        "DELETE FROM auth_sessions
         WHERE revoked_at_ms IS NOT NULL OR expires_at_ms <= ?1",
        params![now],
    )?;
    transaction.execute(
        "DELETE FROM devices
         WHERE NOT EXISTS (
               SELECT 1 FROM auth_sessions
               WHERE auth_sessions.device_id = devices.device_id
           )
           AND NOT EXISTS (
               SELECT 1 FROM invitations
               WHERE invitations.consumed_device_id = devices.device_id
           )",
        [],
    )?;
    Ok(())
}

fn enforce_capacity(
    transaction: &Transaction<'_>,
    table: &str,
    maximum: usize,
    label: &'static str,
) -> Result<(), AuthError> {
    let sql = match table {
        "devices" => "SELECT COUNT(*) FROM devices",
        "invitations" => "SELECT COUNT(*) FROM invitations",
        "auth_sessions" => "SELECT COUNT(*) FROM auth_sessions",
        "audit_events" => "SELECT COUNT(*) FROM audit_events",
        _ => return Err(AuthError::CorruptDatabase("unknown capacity table")),
    };
    let count: i64 = transaction.query_row(sql, [], |row| row.get(0))?;
    if count >= db_count(maximum)? {
        return Err(AuthError::CapacityReached(label));
    }
    Ok(())
}

fn enforce_capacity_or_equal(
    connection: &Connection,
    table: &str,
    maximum: usize,
    label: &'static str,
) -> Result<(), AuthError> {
    let sql = match table {
        "devices" => "SELECT COUNT(*) FROM devices",
        "invitations" => "SELECT COUNT(*) FROM invitations",
        "auth_sessions" => "SELECT COUNT(*) FROM auth_sessions",
        "audit_events" => "SELECT COUNT(*) FROM audit_events",
        _ => return Err(AuthError::CorruptDatabase("unknown capacity table")),
    };
    let count: i64 = connection.query_row(sql, [], |row| row.get(0))?;
    if count > db_count(maximum)? {
        return Err(AuthError::CapacityReached(label));
    }
    Ok(())
}

fn digest_array(value: Vec<u8>) -> Result<[u8; 32], AuthError> {
    value
        .try_into()
        .map_err(|_value| AuthError::CorruptDatabase("invalid digest length"))
}

#[derive(Debug)]
pub enum AuthError {
    AuditCorrupt,
    CapacityReached(&'static str),
    CorruptDatabase(&'static str),
    CsrfMismatch,
    Database(rusqlite::Error),
    InsecurePath(&'static str),
    InvalidAuditLimit,
    InvalidAuditText,
    InvalidLabel,
    InvalidLimits,
    InvalidTime,
    InvitationUnavailable,
    Io(std::io::Error),
    LockPoisoned,
    Secret(SecretError),
    SessionUnavailable,
    Unauthenticated,
    WrongHost,
    WrongOwner,
}

impl fmt::Display for AuthError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::AuditCorrupt => formatter.write_str("authentication audit chain is corrupt"),
            Self::CapacityReached(label) => write!(formatter, "{label} capacity reached"),
            Self::CorruptDatabase(detail) => {
                write!(formatter, "auth database is invalid: {detail}")
            }
            Self::CsrfMismatch => formatter.write_str("CSRF capability is invalid"),
            Self::Database(_) => formatter.write_str("authentication database operation failed"),
            Self::InsecurePath(detail) => write!(formatter, "insecure auth path: {detail}"),
            Self::InvalidAuditLimit => formatter.write_str("invalid audit query limit"),
            Self::InvalidAuditText => formatter.write_str("invalid audit record text"),
            Self::InvalidLabel => formatter.write_str("invalid device label"),
            Self::InvalidLimits => formatter.write_str("invalid authentication limits"),
            Self::InvalidTime => formatter.write_str("invalid authentication timestamp or TTL"),
            Self::InvitationUnavailable => formatter.write_str("pairing invitation is unavailable"),
            Self::Io(_) => formatter.write_str("authentication filesystem operation failed"),
            Self::LockPoisoned => formatter.write_str("authentication store lock is unavailable"),
            Self::Secret(error) => error.fmt(formatter),
            Self::SessionUnavailable => {
                formatter.write_str("authentication session is unavailable")
            }
            Self::Unauthenticated => formatter.write_str("authentication failed"),
            Self::WrongHost => formatter.write_str("auth database belongs to another host"),
            Self::WrongOwner => formatter.write_str("auth database belongs to another owner"),
        }
    }
}

impl Error for AuthError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Database(error) => Some(error),
            Self::Io(error) => Some(error),
            Self::Secret(error) => Some(error),
            _ => None,
        }
    }
}

impl From<rusqlite::Error> for AuthError {
    fn from(value: rusqlite::Error) -> Self {
        Self::Database(value)
    }
}

impl From<std::io::Error> for AuthError {
    fn from(value: std::io::Error) -> Self {
        Self::Io(value)
    }
}

impl From<SecretError> for AuthError {
    fn from(value: SecretError) -> Self {
        Self::Secret(value)
    }
}

#[cfg(test)]
mod tests {
    use super::{
        AUTH_DATABASE_FILENAME, AuthError, AuthLimits, AuthSessionId, AuthStore,
        AuthenticatedDeviceId, MAX_AUTH_DATABASE_BYTES,
    };
    use rusqlite::{Connection, OpenFlags, params};
    use std::fs::{self, DirBuilder, OpenOptions};
    use std::path::{Path, PathBuf};
    use std::sync::{Arc, Barrier};
    use std::thread;
    use termdeck_web_core::{HostId, OwnerId};

    #[cfg(unix)]
    use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt, symlink};

    struct TestDirectory(PathBuf);

    impl TestDirectory {
        fn new(label: &str) -> Self {
            let identifier = match super::random_public_id("test_") {
                Ok(value) => value,
                Err(error) => panic!("could not generate test directory id: {error}"),
            };
            let path = std::env::temp_dir().join(format!("termdeck-{label}-{identifier}"));
            let mut builder = DirBuilder::new();
            #[cfg(unix)]
            builder.mode(0o700);
            if let Err(error) = builder.create(&path) {
                panic!("could not create test directory: {error}");
            }
            Self(path)
        }

        fn path(&self) -> &Path {
            self.0.as_path()
        }
    }

    impl Drop for TestDirectory {
        fn drop(&mut self) {
            let _result = fs::remove_dir_all(&self.0);
        }
    }

    fn host(value: &str) -> HostId {
        match HostId::new(value) {
            Ok(value) => value,
            Err(error) => panic!("invalid test host: {error}"),
        }
    }

    fn owner(value: &str) -> OwnerId {
        match OwnerId::new(value) {
            Ok(value) => value,
            Err(error) => panic!("invalid test owner: {error}"),
        }
    }

    fn store(directory: &TestDirectory) -> AuthStore {
        match AuthStore::open_or_create(
            directory.path(),
            host("host-test"),
            owner("owner-test"),
            1,
            AuthLimits::default(),
        ) {
            Ok(value) => value,
            Err(error) => panic!("could not open auth store: {error}"),
        }
    }

    #[test]
    fn invite_redeem_authenticate_csrf_rotate_and_revoke_are_fail_closed() {
        let directory = TestDirectory::new("lifecycle");
        let value = store(&directory);
        let invitation = match value.create_invitation("Laptop", 100, 1_000) {
            Ok(value) => value,
            Err(error) => panic!("could not create invitation: {error}"),
        };
        let raw_invitation = invitation.secret().expose_secret().to_owned();
        let session = match value.redeem_invitation(&raw_invitation, 200, 5_000) {
            Ok(value) => value,
            Err(error) => panic!("could not redeem invitation: {error}"),
        };
        assert!(matches!(
            value.redeem_invitation(&raw_invitation, 201, 5_000),
            Err(AuthError::InvitationUnavailable)
        ));
        let context = match value.authenticate(session.session_secret().expose_secret(), 300) {
            Ok(value) => value,
            Err(error) => panic!("could not authenticate session: {error}"),
        };
        assert_eq!(context.session_id(), session.session_id());
        assert_eq!(context.device_id(), session.device_id());
        assert!(
            value
                .verify_csrf(
                    session.session_id(),
                    session.csrf_secret().expose_secret(),
                    300,
                )
                .is_ok()
        );
        assert!(matches!(
            value.verify_csrf(session.session_id(), "tdc1_invalid", 300),
            Err(AuthError::CsrfMismatch | AuthError::Unauthenticated)
        ));
        let old_secret = session.session_secret().expose_secret().to_owned();
        let rotated = match value.rotate_session(&old_secret, 400, 5_000) {
            Ok(value) => value,
            Err(error) => panic!("could not rotate session: {error}"),
        };
        assert!(matches!(
            value.authenticate(&old_secret, 401),
            Err(AuthError::Unauthenticated)
        ));
        assert!(
            value
                .authenticate(rotated.session_secret().expose_secret(), 401)
                .is_ok()
        );
        assert!(
            value
                .revoke_device(rotated.device_id(), 500)
                .is_ok_and(|changed| changed)
        );
        assert!(matches!(
            value.authenticate(rotated.session_secret().expose_secret(), 501),
            Err(AuthError::Unauthenticated)
        ));
        assert!(matches!(value.verify_audit_chain(), Ok(4)));
    }

    #[test]
    fn expired_invites_sessions_and_cross_session_csrf_are_rejected() {
        let directory = TestDirectory::new("expiry");
        let value = store(&directory);
        let expired = match value.create_invitation("Expired", 100, 10) {
            Ok(value) => value,
            Err(error) => panic!("could not create invitation: {error}"),
        };
        assert!(matches!(
            value.redeem_invitation(expired.secret().expose_secret(), 110, 100),
            Err(AuthError::InvitationUnavailable)
        ));
        let first_invitation = match value.create_invitation("One", 200, 100) {
            Ok(value) => value,
            Err(error) => panic!("could not create first invitation: {error}"),
        };
        let second_invitation = match value.create_invitation("Two", 200, 100) {
            Ok(value) => value,
            Err(error) => panic!("could not create second invitation: {error}"),
        };
        let first =
            match value.redeem_invitation(first_invitation.secret().expose_secret(), 210, 20) {
                Ok(value) => value,
                Err(error) => panic!("could not redeem first invitation: {error}"),
            };
        let second =
            match value.redeem_invitation(second_invitation.secret().expose_secret(), 210, 100) {
                Ok(value) => value,
                Err(error) => panic!("could not redeem second invitation: {error}"),
            };
        assert!(matches!(
            value.verify_csrf(
                first.session_id(),
                second.csrf_secret().expose_secret(),
                211,
            ),
            Err(AuthError::CsrfMismatch)
        ));
        assert!(matches!(
            value.authenticate(first.session_secret().expose_secret(), 230),
            Err(AuthError::Unauthenticated)
        ));
    }

    #[test]
    fn concurrent_redemption_creates_exactly_one_device_and_session() {
        let directory = TestDirectory::new("concurrent");
        let value = Arc::new(store(&directory));
        let invitation = match value.create_invitation("Concurrent", 1, 10_000) {
            Ok(value) => value,
            Err(error) => panic!("could not create invitation: {error}"),
        };
        let secret = invitation.secret().expose_secret().to_owned();
        let barrier = Arc::new(Barrier::new(3));
        let mut handles = Vec::new();
        for _index in 0..2 {
            let clone = Arc::clone(&value);
            let gate = Arc::clone(&barrier);
            let candidate = secret.clone();
            handles.push(thread::spawn(move || {
                gate.wait();
                clone.redeem_invitation(&candidate, 2, 10_000)
            }));
        }
        barrier.wait();
        let mut successes = 0;
        let mut failures = 0;
        for handle in handles {
            match handle.join() {
                Ok(Ok(_session)) => successes += 1,
                Ok(Err(AuthError::InvitationUnavailable)) => failures += 1,
                Ok(Err(error)) => panic!("unexpected redemption error: {error}"),
                Err(_error) => panic!("redemption thread panicked"),
            }
        }
        assert_eq!(successes, 1);
        assert_eq!(failures, 1);
    }

    #[test]
    fn raw_capabilities_never_persist_and_database_is_owner_only() {
        let directory = TestDirectory::new("secrets");
        let value = store(&directory);
        let invitation = match value.create_invitation("Secret check", 1, 100) {
            Ok(value) => value,
            Err(error) => panic!("could not create invitation: {error}"),
        };
        let invitation_secret = invitation.secret().expose_secret().to_owned();
        let session = match value.redeem_invitation(&invitation_secret, 2, 100) {
            Ok(value) => value,
            Err(error) => panic!("could not redeem invitation: {error}"),
        };
        let session_secret = session.session_secret().expose_secret().to_owned();
        let csrf_secret = session.csrf_secret().expose_secret().to_owned();
        let path = value.database_path().to_owned();
        let connection = match value.lock_connection() {
            Ok(value) => value,
            Err(error) => panic!("could not inspect SQLite page bound: {error}"),
        };
        let page_size: i64 = match connection.query_row("PRAGMA page_size", [], |row| row.get(0)) {
            Ok(value) => value,
            Err(error) => panic!("could not read SQLite page size: {error}"),
        };
        let maximum_pages: i64 =
            match connection.query_row("PRAGMA max_page_count", [], |row| row.get(0)) {
                Ok(value) => value,
                Err(error) => panic!("could not read SQLite page bound: {error}"),
            };
        let maximum_bytes = match page_size.checked_mul(maximum_pages) {
            Some(value) => value,
            None => panic!("SQLite page bound overflowed"),
        };
        let hard_limit = match i64::try_from(MAX_AUTH_DATABASE_BYTES) {
            Ok(value) => value,
            Err(error) => panic!("invalid hard database limit: {error}"),
        };
        assert!(maximum_bytes > 0);
        assert!(maximum_bytes <= hard_limit);
        drop(connection);
        drop(value);
        let bytes = match fs::read(&path) {
            Ok(value) => value,
            Err(error) => panic!("could not read auth database: {error}"),
        };
        for secret in [&invitation_secret, &session_secret, &csrf_secret] {
            assert!(
                !bytes
                    .windows(secret.len())
                    .any(|window| window == secret.as_bytes())
            );
        }
        #[cfg(unix)]
        {
            let mode = match fs::metadata(&path) {
                Ok(value) => value.permissions().mode() & 0o777,
                Err(error) => panic!("could not inspect auth database: {error}"),
            };
            assert_eq!(mode, 0o600);
        }
    }

    #[test]
    fn wrong_host_owner_insecure_directory_and_symlink_database_fail_closed() {
        let directory = TestDirectory::new("path");
        let value = store(&directory);
        assert_eq!(value.host_id().as_str(), "host-test");
        drop(value);
        assert!(matches!(
            AuthStore::open_or_create(
                directory.path(),
                host("host-other"),
                owner("owner-test"),
                1,
                AuthLimits::default(),
            ),
            Err(AuthError::WrongHost)
        ));
        assert!(matches!(
            AuthStore::open_or_create(
                directory.path(),
                host("host-test"),
                owner("owner-other"),
                1,
                AuthLimits::default(),
            ),
            Err(AuthError::WrongOwner)
        ));
        #[cfg(unix)]
        {
            let database_path = directory.path().join(AUTH_DATABASE_FILENAME);
            let hard_link_path = directory.path().join("auth-hard-link.sqlite3");
            if let Err(error) = fs::hard_link(&database_path, &hard_link_path) {
                panic!("could not create auth database hard link: {error}");
            }
            assert!(matches!(
                AuthStore::open_or_create(
                    directory.path(),
                    host("host-test"),
                    owner("owner-test"),
                    1,
                    AuthLimits::default(),
                ),
                Err(AuthError::InsecurePath(
                    "auth database must not have additional hard links"
                ))
            ));
            if let Err(error) = fs::remove_file(&hard_link_path) {
                panic!("could not remove auth database hard link: {error}");
            }
            if let Err(error) =
                fs::set_permissions(directory.path(), fs::Permissions::from_mode(0o755))
            {
                panic!("could not weaken test permissions: {error}");
            }
            assert!(matches!(
                AuthStore::open_or_create(
                    directory.path(),
                    host("host-test"),
                    owner("owner-test"),
                    1,
                    AuthLimits::default(),
                ),
                Err(AuthError::InsecurePath(_))
            ));
            if let Err(error) =
                fs::set_permissions(directory.path(), fs::Permissions::from_mode(0o700))
            {
                panic!("could not restore test permissions: {error}");
            }

            let linked_directory = TestDirectory::new("symlink");
            let target = linked_directory.path().join("target");
            if let Err(error) = fs::write(&target, b"not sqlite") {
                panic!("could not create symlink target: {error}");
            }
            if let Err(error) = symlink(
                &target,
                linked_directory.path().join(AUTH_DATABASE_FILENAME),
            ) {
                panic!("could not create database symlink: {error}");
            }
            assert!(matches!(
                AuthStore::open_or_create(
                    linked_directory.path(),
                    host("host-test"),
                    owner("owner-test"),
                    1,
                    AuthLimits::default(),
                ),
                Err(AuthError::InsecurePath(_))
            ));
        }
    }

    #[test]
    fn oversized_database_is_rejected_before_sqlite_parsing() {
        let directory = TestDirectory::new("oversized");
        let path = directory.path().join(AUTH_DATABASE_FILENAME);
        let mut options = OpenOptions::new();
        options.read(true).write(true).create_new(true);
        #[cfg(unix)]
        options.mode(0o600);
        let file = match options.open(&path) {
            Ok(value) => value,
            Err(error) => panic!("could not create oversized auth database: {error}"),
        };
        if let Err(error) = file.set_len(MAX_AUTH_DATABASE_BYTES.saturating_add(1)) {
            panic!("could not size oversized auth database: {error}");
        }
        if let Err(error) = file.sync_all() {
            panic!("could not sync oversized auth database: {error}");
        }
        drop(file);
        assert!(matches!(
            AuthStore::open_or_create(
                directory.path(),
                host("host-test"),
                owner("owner-test"),
                1,
                AuthLimits::default(),
            ),
            Err(AuthError::InsecurePath(
                "auth database exceeds the configured size bound"
            ))
        ));
    }

    #[test]
    fn securely_reserved_empty_database_recovers_and_unknown_tables_fail_closed() {
        let reserved = TestDirectory::new("reserved");
        let path = reserved.path().join(AUTH_DATABASE_FILENAME);
        let mut options = OpenOptions::new();
        options.read(true).write(true).create_new(true);
        #[cfg(unix)]
        options.mode(0o600);
        let file = match options.open(&path) {
            Ok(value) => value,
            Err(error) => panic!("could not reserve empty auth database: {error}"),
        };
        if let Err(error) = file.sync_all() {
            panic!("could not sync empty auth database: {error}");
        }
        drop(file);
        let recovered = match AuthStore::open_or_create(
            reserved.path(),
            host("host-test"),
            owner("owner-test"),
            5,
            AuthLimits::default(),
        ) {
            Ok(value) => value,
            Err(error) => panic!("could not recover empty auth database: {error}"),
        };
        assert_eq!(recovered.owner_id().as_str(), "owner-test");
        assert!(matches!(recovered.verify_audit_chain(), Ok(0)));
        drop(recovered);

        let connection = match Connection::open_with_flags(
            &path,
            OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        ) {
            Ok(value) => value,
            Err(error) => panic!("could not reopen recovered auth database: {error}"),
        };
        if let Err(error) =
            connection.execute("CREATE TABLE unexpected_auth_state (value TEXT) STRICT", [])
        {
            panic!("could not add unexpected auth table: {error}");
        }
        drop(connection);
        assert!(matches!(
            AuthStore::open_or_create(
                reserved.path(),
                host("host-test"),
                owner("owner-test"),
                5,
                AuthLimits::default(),
            ),
            Err(AuthError::CorruptDatabase("unexpected auth tables"))
        ));
    }

    #[test]
    fn unexpected_auth_views_fail_closed() {
        let directory = TestDirectory::new("unexpected-view");
        let value = store(&directory);
        let path = value.database_path().to_owned();
        drop(value);
        let flags = OpenFlags::SQLITE_OPEN_READ_WRITE
            | OpenFlags::SQLITE_OPEN_NO_MUTEX
            | OpenFlags::SQLITE_OPEN_NOFOLLOW;
        let connection = match Connection::open_with_flags(&path, flags) {
            Ok(value) => value,
            Err(error) => panic!("could not open view test database: {error}"),
        };
        if let Err(error) = connection.execute(
            "CREATE VIEW leaked_auth_metadata AS
             SELECT host_id, owner_id FROM auth_metadata",
            [],
        ) {
            panic!("could not create unexpected auth view: {error}");
        }
        drop(connection);
        assert!(matches!(
            AuthStore::open_or_create(
                directory.path(),
                host("host-test"),
                owner("owner-test"),
                1,
                AuthLimits::default(),
            ),
            Err(AuthError::CorruptDatabase("unexpected auth views"))
        ));
    }

    #[test]
    fn malformed_schema_and_audit_mutation_are_rejected() {
        let directory = TestDirectory::new("schema");
        let value = store(&directory);
        let invitation = match value.create_invitation("Audit", 1, 100) {
            Ok(value) => value,
            Err(error) => panic!("could not create invitation: {error}"),
        };
        assert!(!invitation.secret().expose_secret().is_empty());
        let path = value.database_path().to_owned();
        assert!(matches!(value.verify_audit_chain(), Ok(1)));
        drop(value);
        let flags = OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX;
        let connection = match Connection::open_with_flags(&path, flags) {
            Ok(value) => value,
            Err(error) => panic!("could not open test database: {error}"),
        };
        assert!(connection.execute("DELETE FROM audit_events", []).is_err());
        assert!(
            connection
                .execute("UPDATE audit_events SET detail = 'changed'", [])
                .is_err()
        );
        drop(connection);

        let malformed = TestDirectory::new("malformed");
        let malformed_path = malformed.path().join(AUTH_DATABASE_FILENAME);
        let mut options = OpenOptions::new();
        options.read(true).write(true).create_new(true);
        #[cfg(unix)]
        options.mode(0o600);
        let malformed_file = match options.open(&malformed_path) {
            Ok(value) => value,
            Err(error) => panic!("could not reserve malformed database: {error}"),
        };
        if let Err(error) = malformed_file.sync_all() {
            panic!("could not sync malformed database reservation: {error}");
        }
        drop(malformed_file);
        let flags = OpenFlags::SQLITE_OPEN_READ_WRITE
            | OpenFlags::SQLITE_OPEN_NO_MUTEX
            | OpenFlags::SQLITE_OPEN_NOFOLLOW;
        let connection = match Connection::open_with_flags(&malformed_path, flags) {
            Ok(value) => value,
            Err(error) => panic!("could not create malformed database: {error}"),
        };
        if let Err(error) = connection.pragma_update(None, "user_version", 99) {
            panic!("could not set malformed version: {error}");
        }
        drop(connection);
        #[cfg(unix)]
        if let Err(error) = fs::set_permissions(&malformed_path, fs::Permissions::from_mode(0o600))
        {
            panic!("could not secure malformed database: {error}");
        }
        assert!(matches!(
            AuthStore::open_or_create(
                malformed.path(),
                host("host-test"),
                owner("owner-test"),
                1,
                AuthLimits::default(),
            ),
            Err(AuthError::CorruptDatabase(_))
        ));
    }

    #[test]
    fn oversized_stored_rows_and_forged_audit_appends_fail_on_reopen() {
        let oversized_directory = TestDirectory::new("oversized-row");
        let oversized_store = store(&oversized_directory);
        let invitation = match oversized_store.create_invitation("Bounded", 1, 100) {
            Ok(value) => value,
            Err(error) => panic!("could not create bounded invitation: {error}"),
        };
        if let Err(error) =
            oversized_store.redeem_invitation(invitation.secret().expose_secret(), 2, 100)
        {
            panic!("could not create bounded device: {error}");
        }
        let oversized_path = oversized_store.database_path().to_owned();
        drop(oversized_store);
        let flags = OpenFlags::SQLITE_OPEN_READ_WRITE
            | OpenFlags::SQLITE_OPEN_NO_MUTEX
            | OpenFlags::SQLITE_OPEN_NOFOLLOW;
        let connection = match Connection::open_with_flags(&oversized_path, flags) {
            Ok(value) => value,
            Err(error) => panic!("could not open oversized-row database: {error}"),
        };
        if let Err(error) =
            connection.execute("UPDATE devices SET label = lower(hex(zeroblob(129)))", [])
        {
            panic!("could not insert oversized device label: {error}");
        }
        drop(connection);
        assert!(matches!(
            AuthStore::open_or_create(
                oversized_directory.path(),
                host("host-test"),
                owner("owner-test"),
                1,
                AuthLimits::default(),
            ),
            Err(AuthError::CorruptDatabase("invalid stored device row"))
        ));

        let audit_directory = TestDirectory::new("forged-audit");
        let audit_store = store(&audit_directory);
        if let Err(error) = audit_store.create_invitation("Audit", 1, 100) {
            panic!("could not create audit event: {error}");
        }
        let audit_path = audit_store.database_path().to_owned();
        drop(audit_store);
        let connection = match Connection::open_with_flags(&audit_path, flags) {
            Ok(value) => value,
            Err(error) => panic!("could not open forged-audit database: {error}"),
        };
        if let Err(error) = connection.execute(
            "INSERT INTO audit_events (
                event_id, occurred_at_ms, kind, actor_device_id, subject_id,
                detail, previous_hash, event_hash
             ) VALUES (?1, 2, 'forged', NULL, NULL, 'forged', zeroblob(32), zeroblob(32))",
            params!["aud_00000000000000000000000000000000"],
        ) {
            panic!("could not append forged audit event: {error}");
        }
        drop(connection);
        assert!(matches!(
            AuthStore::open_or_create(
                audit_directory.path(),
                host("host-test"),
                owner("owner-test"),
                1,
                AuthLimits::default(),
            ),
            Err(AuthError::AuditCorrupt)
        ));
    }

    #[test]
    fn weakened_table_constraints_are_rejected_on_reopen() {
        let directory = TestDirectory::new("table-definition");
        let value = store(&directory);
        let path = value.database_path().to_owned();
        drop(value);
        let flags = OpenFlags::SQLITE_OPEN_READ_WRITE
            | OpenFlags::SQLITE_OPEN_NO_MUTEX
            | OpenFlags::SQLITE_OPEN_NOFOLLOW;
        let connection = match Connection::open_with_flags(&path, flags) {
            Ok(value) => value,
            Err(error) => panic!("could not open table-definition database: {error}"),
        };
        if let Err(error) = connection.execute_batch(
            "CREATE TABLE auth_metadata_weakened (
                 singleton INTEGER PRIMARY KEY,
                 schema_version INTEGER,
                 host_id TEXT,
                 owner_id TEXT,
                 created_at_ms INTEGER
             ) STRICT;
             INSERT INTO auth_metadata_weakened
             SELECT singleton, schema_version, host_id, owner_id, created_at_ms
             FROM auth_metadata;
             DROP TABLE auth_metadata;
             ALTER TABLE auth_metadata_weakened RENAME TO auth_metadata;",
        ) {
            panic!("could not weaken auth metadata table: {error}");
        }
        drop(connection);
        assert!(matches!(
            AuthStore::open_or_create(
                directory.path(),
                host("host-test"),
                owner("owner-test"),
                1,
                AuthLimits::default(),
            ),
            Err(AuthError::CorruptDatabase(
                "unexpected authentication table definition"
            ))
        ));
    }

    #[test]
    fn trigger_definitions_and_foreign_key_integrity_are_revalidated() {
        let trigger_directory = TestDirectory::new("trigger-definition");
        let trigger_store = store(&trigger_directory);
        let trigger_path = trigger_store.database_path().to_owned();
        drop(trigger_store);
        let flags = OpenFlags::SQLITE_OPEN_READ_WRITE
            | OpenFlags::SQLITE_OPEN_NO_MUTEX
            | OpenFlags::SQLITE_OPEN_NOFOLLOW;
        let connection = match Connection::open_with_flags(&trigger_path, flags) {
            Ok(value) => value,
            Err(error) => panic!("could not open trigger test database: {error}"),
        };
        if let Err(error) = connection.execute_batch(
            "DROP TRIGGER audit_events_no_update;
             CREATE TRIGGER audit_events_no_update
             BEFORE UPDATE ON audit_events BEGIN SELECT 1; END;",
        ) {
            panic!("could not replace audit trigger: {error}");
        }
        drop(connection);
        assert!(matches!(
            AuthStore::open_or_create(
                trigger_directory.path(),
                host("host-test"),
                owner("owner-test"),
                1,
                AuthLimits::default(),
            ),
            Err(AuthError::CorruptDatabase(
                "unexpected audit trigger definition"
            ))
        ));

        let foreign_key_directory = TestDirectory::new("foreign-key");
        let foreign_key_store = store(&foreign_key_directory);
        let foreign_key_path = foreign_key_store.database_path().to_owned();
        drop(foreign_key_store);
        let connection = match Connection::open_with_flags(&foreign_key_path, flags) {
            Ok(value) => value,
            Err(error) => panic!("could not open foreign-key test database: {error}"),
        };
        if let Err(error) = connection.pragma_update(None, "foreign_keys", false) {
            panic!("could not disable test foreign keys: {error}");
        }
        if let Err(error) = connection.execute(
            "INSERT INTO invitations (
                invitation_id, secret_hash, device_label, created_at_ms, expires_at_ms,
                consumed_at_ms, consumed_device_id
             ) VALUES (?1, zeroblob(32), 'Corrupt', 1, 2, 1, ?2)",
            params![
                "inv_00000000000000000000000000000000",
                "dev_00000000000000000000000000000000",
            ],
        ) {
            panic!("could not add foreign-key violation: {error}");
        }
        drop(connection);
        assert!(matches!(
            AuthStore::open_or_create(
                foreign_key_directory.path(),
                host("host-test"),
                owner("owner-test"),
                1,
                AuthLimits::default(),
            ),
            Err(AuthError::CorruptDatabase(
                "authentication foreign key check failed"
            ))
        ));
    }

    #[test]
    fn expired_rows_are_reclaimed_and_single_session_rotation_remains_atomic() {
        let directory = TestDirectory::new("reclaim");
        let limits = AuthLimits {
            max_devices: 1,
            max_invitations: 1,
            max_sessions: 1,
            max_audit_events: 32,
            max_invitation_ttl_ms: 100,
            max_session_ttl_ms: 100,
        };
        let value = match AuthStore::open_or_create(
            directory.path(),
            host("host-test"),
            owner("owner-test"),
            1,
            limits,
        ) {
            Ok(value) => value,
            Err(error) => panic!("could not open reclaim store: {error}"),
        };
        let expired_invitation = match value.create_invitation("Expired", 1, 10) {
            Ok(value) => value,
            Err(error) => panic!("could not create expiring invitation: {error}"),
        };
        assert!(!expired_invitation.secret().expose_secret().is_empty());
        let first = match value.create_invitation("First", 12, 20) {
            Ok(value) => value,
            Err(error) => panic!("expired invitation was not reclaimed: {error}"),
        };
        let first_session = match value.redeem_invitation(first.secret().expose_secret(), 13, 10) {
            Ok(value) => value,
            Err(error) => panic!("could not redeem first live invitation: {error}"),
        };
        let second = match value.create_invitation("Second", 14, 100) {
            Ok(value) => value,
            Err(error) => panic!("consumed invitation was not reclaimed: {error}"),
        };
        let second_session = match value.redeem_invitation(second.secret().expose_secret(), 24, 50)
        {
            Ok(value) => value,
            Err(error) => panic!("expired session was not reclaimed: {error}"),
        };
        assert!(matches!(
            value.authenticate(first_session.session_secret().expose_secret(), 24),
            Err(AuthError::Unauthenticated)
        ));
        let second_secret = second_session.session_secret().expose_secret().to_owned();
        let rotated = match value.rotate_session(&second_secret, 25, 50) {
            Ok(value) => value,
            Err(error) => panic!("single-capacity session rotation failed: {error}"),
        };
        assert!(matches!(
            value.authenticate(&second_secret, 26),
            Err(AuthError::Unauthenticated)
        ));
        assert!(
            value
                .authenticate(rotated.session_secret().expose_secret(), 26)
                .is_ok()
        );
        let connection = match value.lock_connection() {
            Ok(value) => value,
            Err(error) => panic!("could not inspect reclaim store: {error}"),
        };
        let invitation_count: i64 =
            match connection.query_row("SELECT COUNT(*) FROM invitations", [], |row| row.get(0)) {
                Ok(value) => value,
                Err(error) => panic!("could not count invitations: {error}"),
            };
        let session_count: i64 = match connection.query_row(
            "SELECT COUNT(*) FROM auth_sessions",
            [],
            |row| row.get(0),
        ) {
            Ok(value) => value,
            Err(error) => panic!("could not count sessions: {error}"),
        };
        assert_eq!(invitation_count, 0);
        assert_eq!(session_count, 1);
        drop(connection);
        assert!(matches!(value.verify_audit_chain(), Ok(6)));
    }

    #[test]
    fn configured_capacities_and_ttls_fail_closed() {
        let directory = TestDirectory::new("limits");
        let limits = AuthLimits {
            max_devices: 1,
            max_invitations: 1,
            max_sessions: 2,
            max_audit_events: 8,
            max_invitation_ttl_ms: 100,
            max_session_ttl_ms: 100,
        };
        let value = match AuthStore::open_or_create(
            directory.path(),
            host("host-test"),
            owner("owner-test"),
            1,
            limits,
        ) {
            Ok(value) => value,
            Err(error) => panic!("could not open limited store: {error}"),
        };
        assert!(value.create_invitation("One", 1, 101).is_err());
        let invitation = match value.create_invitation("One", 1, 100) {
            Ok(value) => value,
            Err(error) => panic!("could not create invitation: {error}"),
        };
        assert!(matches!(
            value.create_invitation("Two", 1, 100),
            Err(AuthError::CapacityReached("invitations"))
        ));
        assert!(
            value
                .redeem_invitation(invitation.secret().expose_secret(), 2, 101)
                .is_err()
        );
    }

    #[test]
    fn configuration_cannot_disable_hard_store_bounds() {
        let directory = TestDirectory::new("hard-limits");
        let base = AuthLimits::default();
        for limits in [
            AuthLimits {
                max_devices: super::HARD_MAX_DEVICES + 1,
                ..base
            },
            AuthLimits {
                max_invitations: super::HARD_MAX_INVITATIONS + 1,
                ..base
            },
            AuthLimits {
                max_sessions: super::HARD_MAX_SESSIONS + 1,
                ..base
            },
            AuthLimits {
                max_audit_events: super::HARD_MAX_AUDIT_EVENTS + 1,
                ..base
            },
            AuthLimits {
                max_invitation_ttl_ms: super::HARD_MAX_INVITATION_TTL_MS + 1,
                ..base
            },
            AuthLimits {
                max_session_ttl_ms: super::HARD_MAX_SESSION_TTL_MS + 1,
                ..base
            },
        ] {
            assert!(matches!(
                AuthStore::open_or_create(
                    directory.path(),
                    host("host-test"),
                    owner("owner-test"),
                    1,
                    limits,
                ),
                Err(AuthError::InvalidLimits)
            ));
        }
        assert!(!directory.path().join(AUTH_DATABASE_FILENAME).exists());
        let value = store(&directory);
        assert!(matches!(
            value.audit_records(super::MAX_AUDIT_QUERY_RECORDS + 1),
            Err(AuthError::InvalidAuditLimit)
        ));
    }

    #[test]
    fn identifier_parsers_reject_corrupt_database_values() {
        assert!(AuthSessionId::from_database("bad".to_owned()).is_err());
        assert!(AuthenticatedDeviceId::from_database("bad".to_owned()).is_err());
    }
}
