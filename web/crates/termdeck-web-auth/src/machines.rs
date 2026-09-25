//! Explicit machine enrollment, independent of browser login records.
//! The browser retains a CSPRNG credential; only its domain-separated digest
//! is persisted. No cookie, label, path, IP address or fingerprint is identity.

use crate::secret::{SecretKind, digest_validated_secret, random_public_id};
use crate::store::{
    configure_connection, sync_directory, validate_private_database_file, validate_secure_directory,
};
use crate::{AuthError, AuthStore, SessionContext};
use rusqlite::{Connection, OpenFlags, OptionalExtension, TransactionBehavior, params};
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::fs::{self, OpenOptions};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::PathBuf;
use std::sync::{Mutex, MutexGuard};

const FILENAME: &str = "termdeck-machines-v1.sqlite3";
const SCHEMA: &str = "
CREATE TABLE identity(singleton INTEGER PRIMARY KEY CHECK(singleton=1), host TEXT NOT NULL, owner TEXT NOT NULL, version INTEGER NOT NULL CHECK(version=1)) STRICT;
CREATE TABLE machines(machine_id TEXT PRIMARY KEY, credential_hash BLOB NOT NULL UNIQUE CHECK(length(credential_hash)=32), label TEXT NOT NULL, created_at_ms INTEGER NOT NULL, last_seen_at_ms INTEGER NOT NULL, revoked_at_ms INTEGER, generation INTEGER NOT NULL CHECK(generation>0)) STRICT;
CREATE TABLE requests(session_id TEXT NOT NULL, request_id TEXT NOT NULL, digest BLOB NOT NULL CHECK(length(digest)=32), machine_id TEXT NOT NULL REFERENCES machines(machine_id), PRIMARY KEY(session_id,request_id)) STRICT;
";

#[derive(Clone, Debug, Serialize)]
pub struct MachineRecord {
    pub account_id: String,
    pub machine_id: String,
    pub label: String,
    pub created_at_ms: i64,
    pub last_seen_at_ms: i64,
    pub revoked_at_ms: Option<i64>,
    pub credential_generation: i64,
}

// Intentionally no Debug: callers must never log credential-bearing requests.
pub struct MachineMutation<'a> {
    pub operation: &'a str,
    pub request_id: &'a str,
    pub machine_id: Option<&'a str>,
    pub credential: Option<&'a str>,
    pub new_credential: Option<&'a str>,
    pub label: Option<&'a str>,
}

#[derive(Debug)]
pub enum MachineError {
    Invalid,
    Unauthorized,
    Conflict,
    Capacity,
    Store(AuthError),
}

impl std::fmt::Display for MachineError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::Invalid => "invalid machine request",
            Self::Unauthorized => "machine authorization failed",
            Self::Conflict => "machine request conflicts with an earlier operation",
            Self::Capacity => "machine enrollment capacity reached",
            Self::Store(_) => "machine store is unavailable",
        })
    }
}
impl std::error::Error for MachineError {}
impl From<AuthError> for MachineError {
    fn from(e: AuthError) -> Self {
        Self::Store(e)
    }
}
impl From<rusqlite::Error> for MachineError {
    fn from(e: rusqlite::Error) -> Self {
        Self::Store(e.into())
    }
}
impl From<std::io::Error> for MachineError {
    fn from(e: std::io::Error) -> Self {
        Self::Store(e.into())
    }
}

pub struct MachineStore {
    path: PathBuf,
    file_identity: (u64, u64),
    directory_identity: (u64, u64),
    account_id: String,
    host_id: String,
    owner_id: String,
    connection: Mutex<Connection>,
}

impl MachineStore {
    pub fn open(auth: &AuthStore) -> Result<Self, MachineError> {
        let directory = auth.database_path().parent().ok_or(MachineError::Invalid)?;
        validate_secure_directory(directory)?;
        let parent = fs::symlink_metadata(directory)?;
        let path = directory.join(FILENAME);
        let fresh = match OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&path)
        {
            Ok(file) => {
                file.sync_all()?;
                true
            }
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => false,
            Err(e) => return Err(e.into()),
        };
        validate_private_database_file(&path, FILENAME)?;
        let before = fs::symlink_metadata(&path)?;
        let mut connection = Connection::open_with_flags(
            &path,
            OpenFlags::SQLITE_OPEN_READ_WRITE
                | OpenFlags::SQLITE_OPEN_NO_MUTEX
                | OpenFlags::SQLITE_OPEN_NOFOLLOW,
        )?;
        configure_connection(&connection)?;
        let count: i64 = connection.query_row(
            "SELECT count(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'",
            [],
            |row| row.get(0),
        )?;
        if fresh || count == 0 {
            let tx = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
            tx.execute_batch(SCHEMA)?;
            tx.execute(
                "INSERT INTO identity VALUES(1,?1,?2,1)",
                params![auth.host_id().as_str(), auth.owner_id().as_str()],
            )?;
            tx.commit()?;
            sync_directory(directory)?;
        }
        let identity: (String, String, i64) = connection.query_row(
            "SELECT host,owner,version FROM identity WHERE singleton=1",
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )?;
        if identity
            != (
                auth.host_id().as_str().to_owned(),
                auth.owner_id().as_str().to_owned(),
                1,
            )
        {
            return Err(MachineError::Unauthorized);
        }
        // Validate the complete schema rather than accepting a weakened table.
        let expected = Connection::open_in_memory()?;
        expected.execute_batch(SCHEMA)?;
        fn definitions(db: &Connection) -> Result<Vec<(String, String)>, rusqlite::Error> {
            db.prepare(
                "SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name",
            )?
            .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?
            .collect()
        }
        if definitions(&connection)? != definitions(&expected)? {
            return Err(MachineError::Invalid);
        }
        let mut hash = Sha256::new();
        hash.update(b"termdeck/account-namespace/v1\0");
        hash.update(auth.host_id().as_str().as_bytes());
        hash.update([0]);
        hash.update(auth.owner_id().as_str().as_bytes());
        let store = Self {
            path,
            file_identity: (before.dev(), before.ino()),
            directory_identity: (parent.dev(), parent.ino()),
            account_id: format!(
                "acc_{}",
                hash.finalize()
                    .iter()
                    .map(|b| format!("{b:02x}"))
                    .collect::<String>()
            ),
            host_id: auth.host_id().as_str().to_owned(),
            owner_id: auth.owner_id().as_str().to_owned(),
            connection: Mutex::new(connection),
        };
        drop(store.lock()?);
        Ok(store)
    }

    fn lock(&self) -> Result<MutexGuard<'_, Connection>, MachineError> {
        let guard = self
            .connection
            .lock()
            .map_err(|_| MachineError::Store(AuthError::LockPoisoned))?;
        let directory = self.path.parent().ok_or(MachineError::Invalid)?;
        validate_secure_directory(directory)?;
        validate_private_database_file(&self.path, FILENAME)?;
        let file = fs::symlink_metadata(&self.path)?;
        let parent = fs::symlink_metadata(directory)?;
        if (file.dev(), file.ino()) != self.file_identity
            || (parent.dev(), parent.ino()) != self.directory_identity
        {
            return Err(MachineError::Unauthorized);
        }
        Ok(guard)
    }

    fn authorize(
        &self,
        auth: &AuthStore,
        session: &SessionContext,
        now: u64,
    ) -> Result<i64, MachineError> {
        if self.host_id != auth.host_id().as_str() || self.owner_id != auth.owner_id().as_str() {
            return Err(MachineError::Unauthorized);
        }
        auth.require_context(session, now)?;
        i64::try_from(now).map_err(|_| MachineError::Invalid)
    }

    fn read(&self, db: &Connection, id: &str) -> Result<MachineRecord, MachineError> {
        db.query_row("SELECT machine_id,label,created_at_ms,last_seen_at_ms,revoked_at_ms,generation FROM machines WHERE machine_id=?1", [id], |row| {
            Ok(MachineRecord {account_id: self.account_id.clone(), machine_id: row.get(0)?, label: row.get(1)?,
                created_at_ms: row.get(2)?, last_seen_at_ms: row.get(3)?, revoked_at_ms: row.get(4)?, credential_generation: row.get(5)?})
        }).optional()?.ok_or(MachineError::Unauthorized)
    }

    pub fn list(
        &self,
        auth: &AuthStore,
        session: &SessionContext,
        now: u64,
    ) -> Result<Vec<MachineRecord>, MachineError> {
        self.authorize(auth, session, now)?;
        let db = self.lock()?;
        let ids: Vec<String> = db
            .prepare(
                "SELECT machine_id FROM machines ORDER BY created_at_ms,machine_id LIMIT 1025",
            )?
            .query_map([], |row| row.get(0))?
            .collect::<Result<_, _>>()?;
        if ids.len() > 1024 {
            return Err(MachineError::Capacity);
        }
        ids.iter().map(|id| self.read(&db, id)).collect()
    }

    pub fn identify(
        &self,
        auth: &AuthStore,
        session: &SessionContext,
        credential: &str,
        now: u64,
    ) -> Result<MachineRecord, MachineError> {
        let now = self.authorize(auth, session, now)?;
        let digest = key_digest(credential)?;
        let mut db = self.lock()?;
        let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let id: String = tx.query_row("SELECT machine_id FROM machines WHERE credential_hash=?1 AND revoked_at_ms IS NULL", [digest.as_slice()], |row| row.get(0))
            .optional()?.ok_or(MachineError::Unauthorized)?;
        tx.execute(
            "UPDATE machines SET last_seen_at_ms=max(last_seen_at_ms,?1) WHERE machine_id=?2",
            params![now, id],
        )?;
        let record = self.read(&tx, &id)?;
        tx.commit()?;
        Ok(record)
    }

    pub fn mutate(
        &self,
        auth: &AuthStore,
        session: &SessionContext,
        request: MachineMutation<'_>,
        now: u64,
    ) -> Result<MachineRecord, MachineError> {
        let now = self.authorize(auth, session, now)?;
        validate_request(&request)?;
        let digest = request_digest(&request);
        let mut db = self.lock()?;
        let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let prior: Option<(Vec<u8>, String)> = tx
            .query_row(
                "SELECT digest,machine_id FROM requests WHERE session_id=?1 AND request_id=?2",
                params![session.session_id().as_str(), request.request_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;
        if let Some((previous, id)) = prior {
            if previous != digest {
                return Err(MachineError::Conflict);
            }
            let record = self.read(&tx, &id)?;
            if request.operation != "revoke" {
                let key = request
                    .new_credential
                    .or(request.credential)
                    .ok_or(MachineError::Unauthorized)?;
                require_key(&tx, &id, key)?;
            }
            return Ok(record);
        }
        let requests: i64 = tx.query_row("SELECT count(*) FROM requests", [], |r| r.get(0))?;
        if requests >= 32768 {
            return Err(MachineError::Capacity);
        }
        let id = if request.operation == "enroll" {
            let count: i64 = tx.query_row("SELECT count(*) FROM machines", [], |r| r.get(0))?;
            if count >= 1024 {
                return Err(MachineError::Capacity);
            }
            let key = key_digest(request.credential.ok_or(MachineError::Invalid)?)?;
            let exists: bool = tx.query_row(
                "SELECT EXISTS(SELECT 1 FROM machines WHERE credential_hash=?1)",
                [key.as_slice()],
                |r| r.get(0),
            )?;
            if exists {
                return Err(MachineError::Conflict);
            }
            let id = random_public_id("mach_").map_err(|_| MachineError::Invalid)?;
            tx.execute(
                "INSERT INTO machines VALUES(?1,?2,?3,?4,?4,NULL,1)",
                params![id, key.as_slice(), request.label, now],
            )?;
            id
        } else {
            let id = request.machine_id.ok_or(MachineError::Invalid)?;
            let _record = self.read(&tx, id)?;
            if request.operation != "revoke" {
                require_key(
                    &tx,
                    id,
                    request.credential.ok_or(MachineError::Unauthorized)?,
                )?;
            }
            match request.operation {
                "rename" => {
                    tx.execute(
                        "UPDATE machines SET label=?1 WHERE machine_id=?2",
                        params![request.label, id],
                    )?;
                }
                "rotate" => {
                    let key = key_digest(request.new_credential.ok_or(MachineError::Invalid)?)?;
                    let exists: bool = tx.query_row(
                        "SELECT EXISTS(SELECT 1 FROM machines WHERE credential_hash=?1)",
                        [key.as_slice()],
                        |r| r.get(0),
                    )?;
                    if exists {
                        return Err(MachineError::Conflict);
                    }
                    tx.execute("UPDATE machines SET credential_hash=?1,generation=generation+1 WHERE machine_id=?2", params![key.as_slice(),id])?;
                }
                "revoke" => {
                    tx.execute("UPDATE machines SET revoked_at_ms=coalesce(revoked_at_ms,?1) WHERE machine_id=?2", params![now,id])?;
                }
                _ => return Err(MachineError::Invalid),
            }
            id.to_owned()
        };
        tx.execute(
            "INSERT INTO requests VALUES(?1,?2,?3,?4)",
            params![
                session.session_id().as_str(),
                request.request_id,
                digest.as_slice(),
                id
            ],
        )?;
        let record = self.read(&tx, &id)?;
        tx.commit()?;
        Ok(record)
    }
}

fn key_digest(key: &str) -> Result<[u8; 32], MachineError> {
    digest_validated_secret(SecretKind::Machine, key).map_err(|_| MachineError::Unauthorized)
}
fn require_key(db: &Connection, id: &str, key: &str) -> Result<(), MachineError> {
    let digest = key_digest(key)?;
    let valid: bool = db.query_row("SELECT EXISTS(SELECT 1 FROM machines WHERE machine_id=?1 AND credential_hash=?2 AND revoked_at_ms IS NULL)",
        params![id,digest.as_slice()], |r| r.get(0))?;
    if valid {
        Ok(())
    } else {
        Err(MachineError::Unauthorized)
    }
}
fn validate_request(r: &MachineMutation<'_>) -> Result<(), MachineError> {
    if r.request_id.len() != 32
        || !r
            .request_id
            .bytes()
            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
    {
        return Err(MachineError::Invalid);
    }
    if let Some(id) = r.machine_id
        && (id.len() != 37
            || !id.starts_with("mach_")
            || !id[5..]
                .bytes()
                .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()))
    {
        return Err(MachineError::Invalid);
    }
    if let Some(label) = r.label
        && (label.trim().is_empty() || label.len() > 128 || label.chars().any(char::is_control))
    {
        return Err(MachineError::Invalid);
    }
    let fields = (
        r.machine_id.is_some(),
        r.credential.is_some(),
        r.new_credential.is_some(),
        r.label.is_some(),
    );
    let valid = match r.operation {
        "enroll" => fields == (false, true, false, true),
        "rename" => fields == (true, true, false, true),
        "rotate" => fields == (true, true, true, false),
        "revoke" => fields == (true, false, false, false),
        _ => false,
    };
    if valid {
        Ok(())
    } else {
        Err(MachineError::Invalid)
    }
}
fn request_digest(r: &MachineMutation<'_>) -> [u8; 32] {
    let mut hash = Sha256::new();
    hash.update(b"termdeck/machine-request/v1\0");
    for field in [
        Some(r.operation),
        r.machine_id,
        r.credential,
        r.new_credential,
        r.label,
    ] {
        match field {
            Some(value) => {
                hash.update([1]);
                hash.update((value.len() as u64).to_be_bytes());
                hash.update(value.as_bytes());
            }
            None => hash.update([0]),
        }
    }
    hash.finalize().into()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{AuthLimits, SecretToken};
    use std::os::unix::fs::{PermissionsExt, symlink};
    use termdeck_web_core::{HostId, OwnerId};

    struct Fixture {
        directory: PathBuf,
        auth: AuthStore,
        session: SessionContext,
        machines: MachineStore,
    }
    impl Fixture {
        fn new(owner: &str) -> Result<Self, Box<dyn std::error::Error>> {
            let directory = std::env::temp_dir().join(random_public_id("td_machine_")?);
            fs::create_dir(&directory)?;
            fs::set_permissions(&directory, fs::Permissions::from_mode(0o700))?;
            let auth = AuthStore::open_or_create(
                &directory,
                HostId::new("fixture")?,
                OwnerId::new(owner)?,
                100,
                AuthLimits::default(),
            )?;
            let invite = auth.create_invitation("browser", 100, 10000)?;
            let login = auth.redeem_invitation(invite.secret().expose_secret(), 100, 10000)?;
            let session = auth.authenticate(login.session_secret().expose_secret(), 100)?;
            let machines = MachineStore::open(&auth)?;
            Ok(Self {
                directory,
                auth,
                session,
                machines,
            })
        }
        fn enroll(
            &self,
            key: &SecretToken,
            request_id: &str,
        ) -> Result<MachineRecord, MachineError> {
            self.machines.mutate(
                &self.auth,
                &self.session,
                MachineMutation {
                    operation: "enroll",
                    request_id,
                    machine_id: None,
                    credential: Some(key.expose_secret()),
                    new_credential: None,
                    label: Some("Laptop"),
                },
                100,
            )
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _removed = fs::remove_dir_all(&self.directory);
        }
    }

    #[test]
    fn enrollment_rename_rotate_revoke_and_retry() -> Result<(), Box<dyn std::error::Error>> {
        let f = Fixture::new("owner")?;
        let old = SecretToken::generate(SecretKind::Machine)?;
        let new = SecretToken::generate(SecretKind::Machine)?;
        let id = "a".repeat(32);
        let m = f.enroll(&old, &id)?;
        assert_eq!(m.machine_id, f.enroll(&old, &id)?.machine_id);
        assert_eq!(f.machines.list(&f.auth, &f.session, 100)?.len(), 1);
        let renamed = f.machines.mutate(
            &f.auth,
            &f.session,
            MachineMutation {
                operation: "rename",
                request_id: &"b".repeat(32),
                machine_id: Some(&m.machine_id),
                credential: Some(old.expose_secret()),
                new_credential: None,
                label: Some("Work laptop"),
            },
            101,
        )?;
        assert_eq!(renamed.label, "Work laptop");
        for _ in 0..2 {
            let rotated = f.machines.mutate(
                &f.auth,
                &f.session,
                MachineMutation {
                    operation: "rotate",
                    request_id: &"c".repeat(32),
                    machine_id: Some(&m.machine_id),
                    credential: Some(old.expose_secret()),
                    new_credential: Some(new.expose_secret()),
                    label: None,
                },
                102,
            )?;
            assert_eq!(rotated.credential_generation, 2);
        }
        assert!(matches!(
            f.machines
                .identify(&f.auth, &f.session, old.expose_secret(), 102),
            Err(MachineError::Unauthorized)
        ));
        assert_eq!(
            f.machines
                .identify(&f.auth, &f.session, new.expose_secret(), 103)?
                .last_seen_at_ms,
            103
        );
        assert!(f.enroll(&old, &id).is_err());
        for _ in 0..2 {
            let revoked = f.machines.mutate(
                &f.auth,
                &f.session,
                MachineMutation {
                    operation: "revoke",
                    request_id: &"d".repeat(32),
                    machine_id: Some(&m.machine_id),
                    credential: None,
                    new_credential: None,
                    label: None,
                },
                104,
            )?;
            assert_eq!(revoked.revoked_at_ms, Some(104));
        }
        assert!(
            f.machines
                .identify(&f.auth, &f.session, new.expose_secret(), 105)
                .is_err()
        );
        f.auth.require_context(&f.session, 105)?;
        let database = fs::read(f.directory.join(FILENAME))?;
        for key in [&old, &new] {
            assert!(
                !database
                    .windows(key.expose_secret().len())
                    .any(|v| v == key.expose_secret().as_bytes())
            );
        }
        Ok(())
    }

    #[test]
    fn account_machine_session_and_replay_boundaries() -> Result<(), Box<dyn std::error::Error>> {
        let a = Fixture::new("account-a")?;
        let b = Fixture::new("account-b")?;
        let ka = SecretToken::generate(SecretKind::Machine)?;
        let kb = SecretToken::generate(SecretKind::Machine)?;
        let ma = a.enroll(&ka, &"a".repeat(32))?;
        let mb = a.enroll(&kb, &"b".repeat(32))?;
        assert_ne!(ma.machine_id, mb.machine_id);
        assert!(matches!(
            a.enroll(&kb, &"a".repeat(32)),
            Err(MachineError::Conflict)
        ));
        assert!(
            a.machines
                .identify(&b.auth, &b.session, ka.expose_secret(), 100)
                .is_err()
        );
        assert!(
            b.machines
                .identify(&b.auth, &b.session, ka.expose_secret(), 100)
                .is_err()
        );
        assert!(
            a.machines
                .mutate(
                    &a.auth,
                    &a.session,
                    MachineMutation {
                        operation: "rename",
                        request_id: &"c".repeat(32),
                        machine_id: Some(&ma.machine_id),
                        credential: Some(kb.expose_secret()),
                        new_credential: None,
                        label: Some("wrong machine")
                    },
                    100
                )
                .is_err()
        );
        assert!(
            a.machines
                .identify(&a.auth, &a.session, ka.expose_secret(), 10101)
                .is_err()
        );
        assert!(
            a.machines
                .identify(&a.auth, &a.session, ka.expose_secret(), 100)
                .is_err()
        );
        // A new login on the same account can explicitly present the enrolled
        // credential; a login by itself does not acquire machine authority.
        let invite = a.auth.create_invitation("new browser", 10101, 10000)?;
        let login = a
            .auth
            .redeem_invitation(invite.secret().expose_secret(), 10101, 10000)?;
        let context = a
            .auth
            .authenticate(login.session_secret().expose_secret(), 10101)?;
        assert_eq!(
            a.machines
                .identify(&a.auth, &context, ka.expose_secret(), 10101)?
                .machine_id,
            ma.machine_id
        );
        a.auth.revoke_session(context.session_id(), 10102)?;
        assert!(
            a.machines
                .identify(&a.auth, &context, ka.expose_secret(), 10102)
                .is_err()
        );
        Ok(())
    }

    #[test]
    fn concurrent_enrollment_and_rotation_have_single_result()
    -> Result<(), Box<dyn std::error::Error>> {
        let f = Fixture::new("owner")?;
        let old = SecretToken::generate(SecretKind::Machine)?;
        let new = SecretToken::generate(SecretKind::Machine)?;
        std::thread::scope(|scope| {
            let handles: Vec<_> = (0..8)
                .map(|_| scope.spawn(|| f.enroll(&old, &"a".repeat(32))))
                .collect();
            let ids: Vec<_> = handles
                .into_iter()
                .map(|h| {
                    h.join()
                        .unwrap_or_else(|_| panic!("enrollment thread failed"))
                        .unwrap_or_else(|e| panic!("enrollment failed: {e}"))
                        .machine_id
                })
                .collect();
            assert!(ids.iter().all(|id| id == &ids[0]));
        });
        let m = f
            .machines
            .identify(&f.auth, &f.session, old.expose_secret(), 100)?;
        std::thread::scope(|scope| {
            let handles: Vec<_> = (0..8)
                .map(|n| {
                    let machine_id = &m.machine_id;
                    let f = &f;
                    let old = &old;
                    let new = &new;
                    scope.spawn(move || {
                        f.machines.mutate(
                            &f.auth,
                            &f.session,
                            MachineMutation {
                                operation: "rotate",
                                request_id: &format!("{n:032x}"),
                                machine_id: Some(machine_id),
                                credential: Some(old.expose_secret()),
                                new_credential: Some(new.expose_secret()),
                                label: None,
                            },
                            100,
                        )
                    })
                })
                .collect();
            let successes = handles
                .into_iter()
                .map(|h| {
                    h.join()
                        .unwrap_or_else(|_| panic!("rotation thread failed"))
                })
                .filter(Result::is_ok)
                .count();
            assert_eq!(successes, 1);
        });
        assert_eq!(
            f.machines
                .identify(&f.auth, &f.session, new.expose_secret(), 100)?
                .credential_generation,
            2
        );
        let record = f
            .machines
            .identify(&f.auth, &f.session, new.expose_secret(), 100)?;
        f.machines.mutate(
            &f.auth,
            &f.session,
            MachineMutation {
                operation: "revoke",
                request_id: &"d".repeat(32),
                machine_id: Some(&record.machine_id),
                credential: None,
                new_credential: None,
                label: None,
            },
            100,
        )?;
        assert!(
            f.machines
                .identify(&f.auth, &f.session, new.expose_secret(), 100)
                .is_err()
        );
        Ok(())
    }

    #[test]
    fn malformed_inputs_and_unsafe_store_paths_fail_closed()
    -> Result<(), Box<dyn std::error::Error>> {
        let f = Fixture::new("owner")?;
        let key = SecretToken::generate(SecretKind::Machine)?;
        for label in ["", " ", "bad\nlabel"] {
            assert!(
                f.machines
                    .mutate(
                        &f.auth,
                        &f.session,
                        MachineMutation {
                            operation: "enroll",
                            request_id: &"a".repeat(32),
                            machine_id: None,
                            credential: Some(key.expose_secret()),
                            new_credential: None,
                            label: Some(label)
                        },
                        100
                    )
                    .is_err()
            );
        }
        assert!(f.enroll(&key, "../invalid").is_err());
        let path = f.directory.join(FILENAME);
        let saved = f.directory.join("saved.sqlite3");
        fs::rename(&path, &saved)?;
        symlink(&saved, &path)?;
        assert!(f.machines.list(&f.auth, &f.session, 100).is_err());
        fs::remove_file(&path)?;
        fs::hard_link(&saved, &path)?;
        assert!(MachineStore::open(&f.auth).is_err());
        fs::remove_file(&path)?;
        fs::rename(&saved, &path)?;
        fs::set_permissions(&path, fs::Permissions::from_mode(0o644))?;
        assert!(MachineStore::open(&f.auth).is_err());
        Ok(())
    }

    #[test]
    fn concurrent_revocation_cannot_be_undone_by_rotation() -> Result<(), Box<dyn std::error::Error>>
    {
        let f = Fixture::new("owner")?;
        let old = SecretToken::generate(SecretKind::Machine)?;
        let new = SecretToken::generate(SecretKind::Machine)?;
        let m = f.enroll(&old, &"a".repeat(32))?;
        std::thread::scope(|scope| {
            let rotate = scope.spawn(|| {
                f.machines.mutate(
                    &f.auth,
                    &f.session,
                    MachineMutation {
                        operation: "rotate",
                        request_id: &"b".repeat(32),
                        machine_id: Some(&m.machine_id),
                        credential: Some(old.expose_secret()),
                        new_credential: Some(new.expose_secret()),
                        label: None,
                    },
                    100,
                )
            });
            let revoke = scope.spawn(|| {
                f.machines.mutate(
                    &f.auth,
                    &f.session,
                    MachineMutation {
                        operation: "revoke",
                        request_id: &"c".repeat(32),
                        machine_id: Some(&m.machine_id),
                        credential: None,
                        new_credential: None,
                        label: None,
                    },
                    100,
                )
            });
            let rotation = rotate
                .join()
                .unwrap_or_else(|_| panic!("rotation thread failed"));
            assert!(rotation.is_ok() || matches!(rotation, Err(MachineError::Unauthorized)));
            assert!(
                revoke
                    .join()
                    .unwrap_or_else(|_| panic!("revocation thread failed"))
                    .is_ok()
            );
        });
        assert!(
            f.machines
                .identify(&f.auth, &f.session, old.expose_secret(), 100)
                .is_err()
        );
        assert!(
            f.machines
                .identify(&f.auth, &f.session, new.expose_secret(), 100)
                .is_err()
        );
        assert_eq!(
            f.machines.list(&f.auth, &f.session, 100)?[0].revoked_at_ms,
            Some(100)
        );
        Ok(())
    }

    #[test]
    fn separate_store_preserves_legacy_login_and_reopens() -> Result<(), Box<dyn std::error::Error>>
    {
        let f = Fixture::new("owner")?;
        let key = SecretToken::generate(SecretKind::Machine)?;
        let record = f.enroll(&key, &"a".repeat(32))?;
        let again = MachineStore::open(&f.auth)?;
        assert_eq!(
            again
                .identify(&f.auth, &f.session, key.expose_secret(), 100)?
                .machine_id,
            record.machine_id
        );
        f.auth.verify_audit_chain()?;
        f.auth.require_context(&f.session, 100)?;
        Ok(())
    }
}
