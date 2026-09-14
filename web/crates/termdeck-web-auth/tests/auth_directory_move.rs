//! Disposable end-to-end proof that the web-auth directory can be moved and
//! reopened without losing authentication state.
//!
//! The fixture is deliberately independent of TermDeck's live state and
//! services. It creates only synthetic credentials below the OS temporary
//! directory, closes every handle, renames the whole directory on one device,
//! and then performs the same opens a new process would perform.

use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use termdeck_web_auth::{
    AUTH_DATABASE_FILENAME, AuthLimits, AuthStore, OWNER_PASSWORD_FILENAME, OwnerPasswordStore,
};
use termdeck_web_core::{HostId, OwnerId};

const SYNTHETIC_PASSWORD: &str = "fixture-owner-password-2026";

struct FixtureRoot {
    path: PathBuf,
}

impl FixtureRoot {
    fn new() -> Self {
        let now = match SystemTime::now().duration_since(UNIX_EPOCH) {
            Ok(value) => value.as_nanos(),
            Err(error) => panic!("system clock is before the Unix epoch: {error}"),
        };
        let path = std::env::temp_dir().join(format!(
            "termdeck-web-auth-move-{}-{now}",
            std::process::id()
        ));
        if let Err(error) = fs::create_dir(&path) {
            panic!("could not create disposable auth fixture: {error}");
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;

            if let Err(error) = fs::set_permissions(&path, fs::Permissions::from_mode(0o700)) {
                panic!("could not protect disposable auth fixture: {error}");
            }
        }
        Self { path }
    }

    fn source(&self) -> PathBuf {
        self.path.join("source-auth")
    }

    fn moved(&self) -> PathBuf {
        self.path.join("moved-auth")
    }
}

impl Drop for FixtureRoot {
    fn drop(&mut self) {
        let _result = fs::remove_dir_all(&self.path);
    }
}

fn host() -> HostId {
    match HostId::new("fixture-host") {
        Ok(value) => value,
        Err(error) => panic!("invalid fixture host: {error}"),
    }
}

fn owner() -> OwnerId {
    match OwnerId::new("fixture-owner") {
        Ok(value) => value,
        Err(error) => panic!("invalid fixture owner: {error}"),
    }
}

#[test]
fn populated_auth_directory_survives_physical_move_and_restart_equivalent_reopen() {
    let fixture = FixtureRoot::new();
    let source = fixture.source();
    let moved = fixture.moved();
    if !source.starts_with(std::env::temp_dir()) || !moved.starts_with(std::env::temp_dir()) {
        panic!("auth move fixture escaped the disposable temporary root");
    }
    if let Err(error) = fs::create_dir(&source) {
        panic!("could not create source auth directory: {error}");
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;

        if let Err(error) = fs::set_permissions(&source, fs::Permissions::from_mode(0o700)) {
            panic!("could not protect source auth directory: {error}");
        }
    }

    let host_id = host();
    let owner_id = owner();
    let password_store = match OwnerPasswordStore::new(&source, host_id.clone(), owner_id.clone()) {
        Ok(value) => value,
        Err(error) => panic!("could not create fixture owner-password store: {error}"),
    };
    if let Err(error) = password_store.configure_once(SYNTHETIC_PASSWORD, 100) {
        panic!("could not configure synthetic owner password: {error}");
    }
    let password_file = password_store.path().to_owned();
    let password_metadata = match fs::metadata(&password_file) {
        Ok(value) => value,
        Err(error) => panic!("could not inspect fixture owner-password file: {error}"),
    };
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;

        assert_eq!(password_metadata.permissions().mode() & 0o777, 0o600);
    }

    let store = match AuthStore::open_or_create(
        &source,
        host_id.clone(),
        owner_id.clone(),
        100,
        AuthLimits::default(),
    ) {
        Ok(value) => value,
        Err(error) => panic!("could not open fixture auth store: {error}"),
    };
    let owner_proof = match password_store.verify(SYNTHETIC_PASSWORD) {
        Ok(value) => value,
        Err(error) => panic!("could not verify synthetic owner password: {error}"),
    };
    let password_session =
        match store.create_password_session(&owner_proof, "fixture-owner-device", 200, 5_000) {
            Ok(value) => value,
            Err(error) => panic!("could not create fixture password session: {error}"),
        };
    let invitation = match store.create_invitation("fixture-paired-device", 300, 5_000) {
        Ok(value) => value,
        Err(error) => panic!("could not create fixture pairing invitation: {error}"),
    };
    let invitation_secret = invitation.secret().expose_secret().to_owned();
    let paired_session = match store.redeem_invitation(&invitation_secret, 400, 5_000) {
        Ok(value) => value,
        Err(error) => panic!("could not redeem fixture pairing invitation: {error}"),
    };

    let devices_before = match store.devices() {
        Ok(value) => value,
        Err(error) => panic!("could not read fixture devices: {error}"),
    };
    let owner_context_before =
        match store.authenticate(password_session.session_secret().expose_secret(), 500) {
            Ok(value) => value,
            Err(error) => panic!("could not authenticate fixture owner session: {error}"),
        };
    let paired_context_before =
        match store.authenticate(paired_session.session_secret().expose_secret(), 500) {
            Ok(value) => value,
            Err(error) => panic!("could not authenticate fixture paired session: {error}"),
        };
    let audit_before = match store.audit_records(64) {
        Ok(value) => value,
        Err(error) => panic!("could not read fixture audit records: {error}"),
    };
    let audit_count_before = match store.verify_audit_chain() {
        Ok(value) => value,
        Err(error) => panic!("fixture audit chain was invalid before move: {error}"),
    };
    assert_eq!(audit_count_before, audit_before.len());
    assert_eq!(devices_before.len(), 2);
    assert!(!audit_before.is_empty());
    let database_before = store.database_path().to_owned();

    drop(store);
    drop(password_store);

    let source_metadata = match fs::metadata(&source) {
        Ok(value) => value,
        Err(error) => panic!("could not inspect source auth directory before move: {error}"),
    };
    if let Err(error) = fs::rename(&source, &moved) {
        panic!("could not physically move disposable auth directory: {error}");
    }
    assert!(!source.exists());
    assert!(moved.is_dir());
    let moved_metadata = match fs::metadata(&moved) {
        Ok(value) => value,
        Err(error) => panic!("could not inspect moved auth directory: {error}"),
    };
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;

        assert_eq!(source_metadata.dev(), moved_metadata.dev());
        assert_eq!(source_metadata.ino(), moved_metadata.ino());
    }

    let moved_password_store =
        match OwnerPasswordStore::new(&moved, host_id.clone(), owner_id.clone()) {
            Ok(value) => value,
            Err(error) => panic!("could not reopen moved owner-password store: {error}"),
        };
    assert_eq!(
        moved_password_store
            .path()
            .file_name()
            .and_then(|value| value.to_str()),
        Some(OWNER_PASSWORD_FILENAME)
    );
    let moved_password_metadata = match fs::metadata(moved_password_store.path()) {
        Ok(value) => value,
        Err(error) => panic!("could not inspect moved owner-password file: {error}"),
    };
    assert_eq!(moved_password_metadata.len(), password_metadata.len());
    #[cfg(unix)]
    {
        use std::os::unix::fs::{MetadataExt, PermissionsExt};

        assert_eq!(moved_password_metadata.permissions().mode() & 0o777, 0o600);
        assert_eq!(password_metadata.dev(), moved_password_metadata.dev());
        assert_eq!(password_metadata.ino(), moved_password_metadata.ino());
    }
    if let Err(error) = moved_password_store.verify(SYNTHETIC_PASSWORD) {
        panic!("synthetic owner password did not survive the move: {error}");
    }

    // Dropping and reopening both stores above is the process-restart
    // equivalent. No service, live TermDeck path, or live secret is opened.
    let reopened =
        match AuthStore::open_or_create(&moved, host_id, owner_id, 600, AuthLimits::default()) {
            Ok(value) => value,
            Err(error) => panic!("could not reopen moved auth store: {error}"),
        };
    assert_eq!(
        reopened
            .database_path()
            .file_name()
            .and_then(|value| value.to_str()),
        Some(AUTH_DATABASE_FILENAME)
    );
    let devices_after = match reopened.devices() {
        Ok(value) => value,
        Err(error) => panic!("could not read moved devices: {error}"),
    };
    assert_eq!(devices_after, devices_before);
    let owner_context_after =
        match reopened.authenticate(password_session.session_secret().expose_secret(), 600) {
            Ok(value) => value,
            Err(error) => panic!("owner session did not survive move: {error}"),
        };
    assert_eq!(owner_context_after, owner_context_before);
    let paired_context_after =
        match reopened.authenticate(paired_session.session_secret().expose_secret(), 600) {
            Ok(value) => value,
            Err(error) => panic!("paired session did not survive move: {error}"),
        };
    assert_eq!(paired_context_after, paired_context_before);
    if let Err(error) = reopened.verify_csrf(
        password_session.session_id(),
        password_session.csrf_secret().expose_secret(),
        600,
    ) {
        panic!("owner CSRF state did not survive move: {error}");
    }
    if let Err(error) = reopened.verify_csrf(
        paired_session.session_id(),
        paired_session.csrf_secret().expose_secret(),
        600,
    ) {
        panic!("paired CSRF state did not survive move: {error}");
    }
    let audit_after = match reopened.audit_records(64) {
        Ok(value) => value,
        Err(error) => panic!("could not read moved audit records: {error}"),
    };
    assert_eq!(audit_after, audit_before);
    let audit_count_after = match reopened.verify_audit_chain() {
        Ok(value) => value,
        Err(error) => panic!("moved audit chain is invalid: {error}"),
    };
    assert_eq!(audit_count_after, audit_count_before);
    assert!(database_before.ends_with(AUTH_DATABASE_FILENAME));
    assert_eq!(
        reopened.database_path(),
        Path::new(&moved).join(AUTH_DATABASE_FILENAME).as_path()
    );
}
