use argon2::password_hash::{PasswordHash, PasswordHasher, PasswordVerifier, SaltString};
use argon2::{Algorithm, Argon2, Params, Version};
use getrandom::fill;
use std::error::Error;
use std::fmt;
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use termdeck_web_core::{HostId, OwnerId};
use zeroize::Zeroize;

use crate::store::{effective_uid, sync_directory, validate_secure_directory};

#[cfg(unix)]
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};

pub const OWNER_PASSWORD_FILENAME: &str = "termdeck-owner-password-v1.bin";
pub const MIN_OWNER_PASSWORD_BYTES: usize = 8;
pub const MAX_OWNER_PASSWORD_BYTES: usize = 256;

const MAGIC: &[u8; 8] = b"TDPASS1\0";
const HEADER_BYTES: usize = 24;
const MAX_PASSWORD_FILE_BYTES: u64 = 4 * 1024;
const MAX_PHC_BYTES: usize = 512;
const SALT_BYTES: usize = 16;
const HASH_BYTES: usize = 32;
const ARGON2_MEMORY_KIB: u32 = 19 * 1024;
const ARGON2_ITERATIONS: u32 = 2;
const ARGON2_PARALLELISM: u32 = 1;
const DUMMY_SALT: [u8; SALT_BYTES] = [0x54; SALT_BYTES];

#[derive(Debug)]
pub struct VerifiedOwnerPassword {
    host_id: HostId,
    owner_id: OwnerId,
}

impl VerifiedOwnerPassword {
    pub(crate) fn authorizes(&self, host_id: &HostId, owner_id: &OwnerId) -> bool {
        &self.host_id == host_id && &self.owner_id == owner_id
    }
}

#[derive(Debug)]
pub struct OwnerPasswordStore {
    path: PathBuf,
    secure_directory: PathBuf,
    host_id: HostId,
    owner_id: OwnerId,
}

impl OwnerPasswordStore {
    pub fn new(
        secure_directory: &Path,
        host_id: HostId,
        owner_id: OwnerId,
    ) -> Result<Self, PasswordError> {
        validate_secure_directory(secure_directory).map_err(PasswordError::Auth)?;
        let store = Self {
            path: secure_directory.join(OWNER_PASSWORD_FILENAME),
            secure_directory: secure_directory.to_owned(),
            host_id,
            owner_id,
        };
        if store.path.exists() {
            let _record = store.read_record()?;
        }
        Ok(store)
    }

    #[must_use]
    pub fn path(&self) -> &Path {
        self.path.as_path()
    }

    pub fn is_configured(&self) -> Result<bool, PasswordError> {
        match fs::symlink_metadata(&self.path) {
            Ok(_metadata) => {
                let _record = self.read_record()?;
                Ok(true)
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
            Err(error) => Err(PasswordError::Io(error)),
        }
    }

    pub fn configure_once(
        &self,
        password: &str,
        created_at_ms: u64,
    ) -> Result<VerifiedOwnerPassword, PasswordError> {
        validate_password_for_setup(password)?;
        if created_at_ms > i64::MAX as u64 {
            return Err(PasswordError::InvalidTimestamp);
        }
        validate_secure_directory(&self.secure_directory).map_err(PasswordError::Auth)?;
        if self.path.exists() {
            let _record = self.read_record()?;
            return Err(PasswordError::AlreadyConfigured);
        }

        let phc = hash_password(password)?;
        let record = PasswordRecord {
            host_id: self.host_id.clone(),
            owner_id: self.owner_id.clone(),
            phc,
            created_at_ms,
        };
        let mut bytes = encode_record(&record)?;
        let mut options = OpenOptions::new();
        options.read(true).write(true).create_new(true);
        #[cfg(unix)]
        options.mode(0o600);
        let mut file = match options.open(&self.path) {
            Ok(value) => value,
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                bytes.zeroize();
                let _record = self.read_record()?;
                return Err(PasswordError::AlreadyConfigured);
            }
            Err(error) => {
                bytes.zeroize();
                return Err(PasswordError::Io(error));
            }
        };
        let descriptor_identity = file_identity(&file.metadata()?)?;
        let result = (|| {
            file.write_all(&bytes)?;
            file.flush()?;
            file.sync_all()?;
            validate_password_file(&self.path)?;
            let path_identity = file_identity(&fs::symlink_metadata(&self.path)?)?;
            if descriptor_identity != path_identity {
                return Err(PasswordError::InsecurePath(
                    "password file identity changed during creation",
                ));
            }
            sync_directory(&self.secure_directory).map_err(PasswordError::Auth)?;
            Ok::<(), PasswordError>(())
        })();
        bytes.zeroize();
        if let Err(error) = result {
            drop(file);
            remove_if_identity_matches(&self.path, descriptor_identity);
            return Err(error);
        }
        let _record = self.read_record()?;
        Ok(self.proof())
    }

    pub fn verify(&self, password: &str) -> Result<VerifiedOwnerPassword, PasswordError> {
        let valid_shape = password_shape_is_valid(password);
        let record = match self.read_record() {
            Ok(value) => value,
            Err(PasswordError::Io(error)) if error.kind() == std::io::ErrorKind::NotFound => {
                dummy_hash(password.as_bytes())?;
                return Err(PasswordError::NotConfigured);
            }
            Err(error) => return Err(error),
        };
        if !valid_shape {
            dummy_hash(password.as_bytes())?;
            return Err(PasswordError::Unauthenticated);
        }
        let parsed = parse_and_validate_phc(&record.phc)?;
        if argon2()?
            .verify_password(password.as_bytes(), &parsed)
            .is_err()
        {
            return Err(PasswordError::Unauthenticated);
        }
        Ok(self.proof())
    }

    fn proof(&self) -> VerifiedOwnerPassword {
        VerifiedOwnerPassword {
            host_id: self.host_id.clone(),
            owner_id: self.owner_id.clone(),
        }
    }

    fn read_record(&self) -> Result<PasswordRecord, PasswordError> {
        validate_secure_directory(&self.secure_directory).map_err(PasswordError::Auth)?;
        validate_password_file(&self.path)?;
        let mut file = OpenOptions::new().read(true).open(&self.path)?;
        let descriptor_identity = file_identity(&file.metadata()?)?;
        let path_identity = file_identity(&fs::symlink_metadata(&self.path)?)?;
        if descriptor_identity != path_identity {
            return Err(PasswordError::InsecurePath(
                "password file identity changed while opening",
            ));
        }
        let length = usize::try_from(file.metadata()?.len())
            .map_err(|_| PasswordError::CorruptFile("password file length overflow"))?;
        if length < HEADER_BYTES || length as u64 > MAX_PASSWORD_FILE_BYTES {
            return Err(PasswordError::CorruptFile(
                "password file has an invalid length",
            ));
        }
        let mut bytes = vec![0_u8; length];
        file.read_exact(&mut bytes)?;
        let result = decode_record(&bytes);
        bytes.zeroize();
        let record = result?;
        if record.host_id != self.host_id {
            return Err(PasswordError::WrongHost);
        }
        if record.owner_id != self.owner_id {
            return Err(PasswordError::WrongOwner);
        }
        Ok(record)
    }
}

#[derive(Debug)]
struct PasswordRecord {
    host_id: HostId,
    owner_id: OwnerId,
    phc: String,
    created_at_ms: u64,
}

fn password_shape_is_valid(password: &str) -> bool {
    let bytes = password.as_bytes();
    (MIN_OWNER_PASSWORD_BYTES..=MAX_OWNER_PASSWORD_BYTES).contains(&bytes.len())
        && !password.chars().any(char::is_control)
}

fn validate_password_for_setup(password: &str) -> Result<(), PasswordError> {
    if password_shape_is_valid(password) {
        Ok(())
    } else {
        Err(PasswordError::InvalidPassword)
    }
}

fn argon2() -> Result<Argon2<'static>, PasswordError> {
    let params = Params::new(
        ARGON2_MEMORY_KIB,
        ARGON2_ITERATIONS,
        ARGON2_PARALLELISM,
        Some(HASH_BYTES),
    )
    .map_err(|_| PasswordError::HashingUnavailable)?;
    Ok(Argon2::new(Algorithm::Argon2id, Version::V0x13, params))
}

fn hash_password(password: &str) -> Result<String, PasswordError> {
    let mut salt_bytes = [0_u8; SALT_BYTES];
    fill(&mut salt_bytes).map_err(|_| PasswordError::RandomUnavailable)?;
    let salt =
        SaltString::encode_b64(&salt_bytes).map_err(|_| PasswordError::HashingUnavailable)?;
    salt_bytes.zeroize();
    let phc = argon2()?
        .hash_password(password.as_bytes(), salt.as_salt())
        .map_err(|_| PasswordError::HashingUnavailable)?
        .to_string();
    let _parsed = parse_and_validate_phc(&phc)?;
    Ok(phc)
}

fn dummy_hash(password: &[u8]) -> Result<(), PasswordError> {
    let mut output = [0_u8; HASH_BYTES];
    let result = argon2()?.hash_password_into(password, &DUMMY_SALT, &mut output);
    output.zeroize();
    result.map_err(|_| PasswordError::HashingUnavailable)
}

fn parse_and_validate_phc(value: &str) -> Result<PasswordHash<'_>, PasswordError> {
    if value.is_empty() || value.len() > MAX_PHC_BYTES {
        return Err(PasswordError::CorruptFile(
            "invalid password verifier length",
        ));
    }
    let parsed = PasswordHash::new(value)
        .map_err(|_| PasswordError::CorruptFile("invalid password verifier"))?;
    let parameter_count = parsed.params.iter().count();
    let mut decoded_salt = [0_u8; 64];
    let salt_length = parsed
        .salt
        .as_ref()
        .ok_or(PasswordError::CorruptFile("password verifier has no salt"))?
        .decode_b64(&mut decoded_salt)
        .map_err(|_| PasswordError::CorruptFile("invalid password verifier salt"))?
        .len();
    decoded_salt.zeroize();
    let hash_length = parsed
        .hash
        .as_ref()
        .ok_or(PasswordError::CorruptFile(
            "password verifier has no output",
        ))?
        .len();
    if parsed.algorithm.as_str() != "argon2id"
        || parsed.version != Some(0x13)
        || parameter_count != 3
        || parsed.params.get_decimal("m") != Some(ARGON2_MEMORY_KIB)
        || parsed.params.get_decimal("t") != Some(ARGON2_ITERATIONS)
        || parsed.params.get_decimal("p") != Some(ARGON2_PARALLELISM)
        || salt_length != SALT_BYTES
        || hash_length != HASH_BYTES
    {
        return Err(PasswordError::CorruptFile(
            "password verifier parameters are not approved",
        ));
    }
    Ok(parsed)
}

fn encode_record(record: &PasswordRecord) -> Result<Vec<u8>, PasswordError> {
    let host = record.host_id.as_str().as_bytes();
    let owner = record.owner_id.as_str().as_bytes();
    let phc = record.phc.as_bytes();
    let host_length = u16::try_from(host.len())
        .map_err(|_| PasswordError::CorruptFile("host identity is too long"))?;
    let owner_length = u16::try_from(owner.len())
        .map_err(|_| PasswordError::CorruptFile("owner identity is too long"))?;
    let phc_length = u16::try_from(phc.len())
        .map_err(|_| PasswordError::CorruptFile("password verifier is too long"))?;
    let total = HEADER_BYTES
        .checked_add(host.len())
        .and_then(|value| value.checked_add(owner.len()))
        .and_then(|value| value.checked_add(phc.len()))
        .ok_or(PasswordError::CorruptFile(
            "password record length overflow",
        ))?;
    if total as u64 > MAX_PASSWORD_FILE_BYTES {
        return Err(PasswordError::CorruptFile("password record is too large"));
    }
    let mut bytes = Vec::with_capacity(total);
    bytes.extend_from_slice(MAGIC);
    bytes.extend_from_slice(&host_length.to_be_bytes());
    bytes.extend_from_slice(&owner_length.to_be_bytes());
    bytes.extend_from_slice(&phc_length.to_be_bytes());
    bytes.extend_from_slice(&0_u16.to_be_bytes());
    bytes.extend_from_slice(&record.created_at_ms.to_be_bytes());
    bytes.extend_from_slice(host);
    bytes.extend_from_slice(owner);
    bytes.extend_from_slice(phc);
    Ok(bytes)
}

fn decode_record(bytes: &[u8]) -> Result<PasswordRecord, PasswordError> {
    if bytes.len() < HEADER_BYTES || &bytes[..MAGIC.len()] != MAGIC {
        return Err(PasswordError::CorruptFile("invalid password record header"));
    }
    let host_length = usize::from(read_u16(bytes, 8)?);
    let owner_length = usize::from(read_u16(bytes, 10)?);
    let phc_length = usize::from(read_u16(bytes, 12)?);
    if read_u16(bytes, 14)? != 0 {
        return Err(PasswordError::CorruptFile(
            "password record reserved field is nonzero",
        ));
    }
    let created_at_ms = read_u64(bytes, 16)?;
    if created_at_ms > i64::MAX as u64 {
        return Err(PasswordError::CorruptFile(
            "password record timestamp is invalid",
        ));
    }
    let expected = HEADER_BYTES
        .checked_add(host_length)
        .and_then(|value| value.checked_add(owner_length))
        .and_then(|value| value.checked_add(phc_length))
        .ok_or(PasswordError::CorruptFile(
            "password record length overflow",
        ))?;
    if expected != bytes.len() {
        return Err(PasswordError::CorruptFile(
            "password record length does not match its header",
        ));
    }
    let host_start = HEADER_BYTES;
    let owner_start = host_start + host_length;
    let phc_start = owner_start + owner_length;
    let host = std::str::from_utf8(&bytes[host_start..owner_start])
        .map_err(|_| PasswordError::CorruptFile("password host identity is not UTF-8"))?;
    let owner = std::str::from_utf8(&bytes[owner_start..phc_start])
        .map_err(|_| PasswordError::CorruptFile("password owner identity is not UTF-8"))?;
    let phc = std::str::from_utf8(&bytes[phc_start..])
        .map_err(|_| PasswordError::CorruptFile("password verifier is not UTF-8"))?;
    let host_id = HostId::new(host)
        .map_err(|_| PasswordError::CorruptFile("password host identity is invalid"))?;
    let owner_id = OwnerId::new(owner)
        .map_err(|_| PasswordError::CorruptFile("password owner identity is invalid"))?;
    let _parsed = parse_and_validate_phc(phc)?;
    Ok(PasswordRecord {
        host_id,
        owner_id,
        phc: phc.to_owned(),
        created_at_ms,
    })
}

fn read_u16(bytes: &[u8], offset: usize) -> Result<u16, PasswordError> {
    let value = bytes
        .get(offset..offset + 2)
        .ok_or(PasswordError::CorruptFile("truncated password record"))?;
    Ok(u16::from_be_bytes([value[0], value[1]]))
}

fn read_u64(bytes: &[u8], offset: usize) -> Result<u64, PasswordError> {
    let value = bytes
        .get(offset..offset + 8)
        .ok_or(PasswordError::CorruptFile("truncated password record"))?;
    let mut array = [0_u8; 8];
    array.copy_from_slice(value);
    Ok(u64::from_be_bytes(array))
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct FileIdentity {
    device: u64,
    inode: u64,
}

fn file_identity(metadata: &fs::Metadata) -> Result<FileIdentity, PasswordError> {
    #[cfg(unix)]
    {
        Ok(FileIdentity {
            device: metadata.dev(),
            inode: metadata.ino(),
        })
    }
    #[cfg(not(unix))]
    {
        let _metadata = metadata;
        Err(PasswordError::InsecurePath(
            "password storage requires Unix file identity semantics",
        ))
    }
}

fn validate_password_file(path: &Path) -> Result<(), PasswordError> {
    if path.file_name().and_then(|value| value.to_str()) != Some(OWNER_PASSWORD_FILENAME) {
        return Err(PasswordError::InsecurePath(
            "unexpected owner password filename",
        ));
    }
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err(PasswordError::InsecurePath(
            "owner password record must be a non-symlink regular file",
        ));
    }
    if metadata.len() > MAX_PASSWORD_FILE_BYTES {
        return Err(PasswordError::InsecurePath(
            "owner password record exceeds its size bound",
        ));
    }
    #[cfg(unix)]
    {
        if metadata.uid() != effective_uid().map_err(PasswordError::Auth)? {
            return Err(PasswordError::InsecurePath(
                "owner password record must be owned by the current user",
            ));
        }
        if metadata.nlink() != 1 {
            return Err(PasswordError::InsecurePath(
                "owner password record must not have additional hard links",
            ));
        }
        if metadata.permissions().mode() & 0o777 != 0o600 {
            return Err(PasswordError::InsecurePath(
                "owner password record permissions must be exactly 0600",
            ));
        }
    }
    Ok(())
}

fn remove_if_identity_matches(path: &Path, expected: FileIdentity) {
    let matches = fs::symlink_metadata(path)
        .ok()
        .and_then(|metadata| file_identity(&metadata).ok())
        .is_some_and(|identity| identity == expected);
    if matches {
        let _removed = fs::remove_file(path);
    }
}

#[derive(Debug)]
pub enum PasswordError {
    AlreadyConfigured,
    Auth(crate::store::AuthError),
    CorruptFile(&'static str),
    HashingUnavailable,
    InsecurePath(&'static str),
    InvalidPassword,
    InvalidTimestamp,
    Io(std::io::Error),
    NotConfigured,
    RandomUnavailable,
    Unauthenticated,
    WrongHost,
    WrongOwner,
}

impl fmt::Display for PasswordError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::AlreadyConfigured => formatter.write_str("owner password is already configured"),
            Self::Auth(error) => write!(formatter, "authentication storage failed: {error}"),
            Self::CorruptFile(detail) => {
                write!(formatter, "owner password record is corrupt: {detail}")
            }
            Self::HashingUnavailable => {
                formatter.write_str("owner password hashing is unavailable")
            }
            Self::InsecurePath(detail) => {
                write!(formatter, "insecure owner password path: {detail}")
            }
            Self::InvalidPassword => {
                formatter.write_str("owner password does not satisfy the configured bounds")
            }
            Self::InvalidTimestamp => formatter.write_str("owner password timestamp is invalid"),
            Self::Io(error) => write!(formatter, "owner password I/O failed: {error}"),
            Self::NotConfigured => formatter.write_str("owner password is not configured"),
            Self::RandomUnavailable => formatter.write_str("owner password salt generation failed"),
            Self::Unauthenticated => formatter.write_str("owner password authentication failed"),
            Self::WrongHost => {
                formatter.write_str("owner password record belongs to a different host")
            }
            Self::WrongOwner => {
                formatter.write_str("owner password record belongs to a different owner")
            }
        }
    }
}

impl Error for PasswordError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Auth(error) => Some(error),
            Self::Io(error) => Some(error),
            _ => None,
        }
    }
}

impl From<std::io::Error> for PasswordError {
    fn from(error: std::io::Error) -> Self {
        Self::Io(error)
    }
}

#[cfg(test)]
mod tests {
    use super::{
        MAX_OWNER_PASSWORD_BYTES, MIN_OWNER_PASSWORD_BYTES, OWNER_PASSWORD_FILENAME,
        OwnerPasswordStore, PasswordError,
    };
    use crate::store::{AuthError, AuthLimits, AuthStore};
    use std::fs::{self, DirBuilder, OpenOptions};
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicU64, Ordering};
    use termdeck_web_core::{HostId, OwnerId};

    #[cfg(unix)]
    use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt};

    static NEXT_DIRECTORY: AtomicU64 = AtomicU64::new(1);

    struct TestDirectory(PathBuf);

    impl TestDirectory {
        fn new(label: &str) -> Self {
            let sequence = NEXT_DIRECTORY.fetch_add(1, Ordering::Relaxed);
            let path = std::env::temp_dir().join(format!(
                "termdeck-password-{label}-{}-{sequence}",
                std::process::id()
            ));
            let mut builder = DirBuilder::new();
            #[cfg(unix)]
            builder.mode(0o700);
            if let Err(error) = builder.create(&path) {
                panic!("could not create password test directory: {error}");
            }
            Self(path)
        }

        fn path(&self) -> &Path {
            self.0.as_path()
        }
    }

    impl Drop for TestDirectory {
        fn drop(&mut self) {
            let _removed = fs::remove_dir_all(&self.0);
        }
    }

    fn id<T>(result: Result<T, termdeck_web_core::IdError>) -> T {
        match result {
            Ok(value) => value,
            Err(error) => panic!("invalid password test identity: {error}"),
        }
    }

    fn password_store(directory: &TestDirectory) -> OwnerPasswordStore {
        OwnerPasswordStore::new(
            directory.path(),
            id(HostId::new("host-test")),
            id(OwnerId::new("owner-test")),
        )
        .unwrap_or_else(|error| panic!("could not create password store: {error}"))
    }

    #[test]
    fn configure_verify_and_reopen_store_only_a_bounded_verifier() {
        let directory = TestDirectory::new("roundtrip");
        let store = password_store(&directory);
        assert!(matches!(store.is_configured(), Ok(false)));
        let password = "correct horse battery staple";
        if let Err(error) = store.configure_once(password, 100) {
            panic!("could not configure owner password: {error}");
        }
        assert!(matches!(store.is_configured(), Ok(true)));
        assert!(store.verify(password).is_ok());
        assert!(matches!(
            store.verify("wrong password"),
            Err(PasswordError::Unauthenticated)
        ));
        assert!(matches!(
            store.configure_once(password, 101),
            Err(PasswordError::AlreadyConfigured)
        ));
        let bytes = fs::read(store.path())
            .unwrap_or_else(|error| panic!("could not read password record: {error}"));
        assert!(
            !bytes
                .windows(password.len())
                .any(|window| window == password.as_bytes())
        );
        assert!(bytes.len() < 1_024);
        #[cfg(unix)]
        assert_eq!(
            fs::metadata(store.path())
                .unwrap_or_else(|error| panic!("could not inspect password record: {error}"))
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
        let reopened = password_store(&directory);
        assert!(reopened.verify(password).is_ok());
    }

    #[test]
    fn verified_password_proof_creates_only_host_owner_bound_sessions() {
        let directory = TestDirectory::new("session-proof");
        let password_store = password_store(&directory);
        let proof = password_store
            .configure_once("proof-bound-password", 1)
            .unwrap_or_else(|error| panic!("could not configure password proof: {error}"));
        let auth_store = AuthStore::open_or_create(
            directory.path(),
            id(HostId::new("host-test")),
            id(OwnerId::new("owner-test")),
            2,
            AuthLimits::default(),
        )
        .unwrap_or_else(|error| panic!("could not open matching auth store: {error}"));
        let session = auth_store
            .create_password_session(&proof, "Laptop", 3, 60_000)
            .unwrap_or_else(|error| panic!("could not create password session: {error}"));
        let context = auth_store
            .authenticate(session.session_secret().expose_secret(), 4)
            .unwrap_or_else(|error| panic!("could not authenticate password session: {error}"));
        assert_eq!(context.device_id(), session.device_id());
        assert_eq!(context.device_label(), "Laptop");

        let foreign_directory = TestDirectory::new("foreign-proof");
        let foreign_store = AuthStore::open_or_create(
            foreign_directory.path(),
            id(HostId::new("foreign-host")),
            id(OwnerId::new("owner-test")),
            2,
            AuthLimits::default(),
        )
        .unwrap_or_else(|error| panic!("could not open foreign auth store: {error}"));
        assert!(matches!(
            foreign_store.create_password_session(&proof, "Foreign", 3, 60_000),
            Err(AuthError::Unauthenticated)
        ));
    }

    #[test]
    fn password_bounds_and_unconfigured_verification_fail_closed() {
        let directory = TestDirectory::new("bounds");
        let store = password_store(&directory);
        assert!(matches!(
            store.verify("not configured"),
            Err(PasswordError::NotConfigured)
        ));
        assert!(matches!(
            store.configure_once(&"a".repeat(MIN_OWNER_PASSWORD_BYTES - 1), 1),
            Err(PasswordError::InvalidPassword)
        ));
        assert!(matches!(
            store.configure_once(&"a".repeat(MAX_OWNER_PASSWORD_BYTES + 1), 1),
            Err(PasswordError::InvalidPassword)
        ));
        assert!(matches!(
            store.configure_once("valid\npassword", 1),
            Err(PasswordError::InvalidPassword)
        ));
    }

    #[test]
    fn host_owner_symlink_hardlink_and_mode_substitution_fail_closed() {
        let directory = TestDirectory::new("identity");
        let store = password_store(&directory);
        if let Err(error) = store.configure_once("one secure password", 1) {
            panic!("could not configure password: {error}");
        }
        assert!(matches!(
            OwnerPasswordStore::new(
                directory.path(),
                id(HostId::new("other-host")),
                id(OwnerId::new("owner-test")),
            ),
            Err(PasswordError::WrongHost)
        ));
        assert!(matches!(
            OwnerPasswordStore::new(
                directory.path(),
                id(HostId::new("host-test")),
                id(OwnerId::new("other-owner")),
            ),
            Err(PasswordError::WrongOwner)
        ));

        #[cfg(unix)]
        {
            fs::set_permissions(store.path(), fs::Permissions::from_mode(0o644))
                .unwrap_or_else(|error| panic!("could not weaken password mode: {error}"));
            assert!(matches!(
                store.is_configured(),
                Err(PasswordError::InsecurePath(_))
            ));
            fs::set_permissions(store.path(), fs::Permissions::from_mode(0o600))
                .unwrap_or_else(|error| panic!("could not restore password mode: {error}"));

            let hardlink = directory.path().join("password-hardlink");
            fs::hard_link(store.path(), &hardlink)
                .unwrap_or_else(|error| panic!("could not create password hardlink: {error}"));
            assert!(matches!(
                store.is_configured(),
                Err(PasswordError::InsecurePath(_))
            ));
            fs::remove_file(&hardlink)
                .unwrap_or_else(|error| panic!("could not remove password hardlink: {error}"));
        }

        let original = fs::read(store.path())
            .unwrap_or_else(|error| panic!("could not preserve password record: {error}"));
        fs::remove_file(store.path())
            .unwrap_or_else(|error| panic!("could not remove password record: {error}"));
        let target = directory.path().join("password-target");
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        options.mode(0o600);
        let mut target_file = options
            .open(&target)
            .unwrap_or_else(|error| panic!("could not create password target: {error}"));
        use std::io::Write as _;
        target_file
            .write_all(&original)
            .unwrap_or_else(|error| panic!("could not write password target: {error}"));
        target_file
            .sync_all()
            .unwrap_or_else(|error| panic!("could not sync password target: {error}"));
        #[cfg(unix)]
        std::os::unix::fs::symlink(&target, directory.path().join(OWNER_PASSWORD_FILENAME))
            .unwrap_or_else(|error| panic!("could not create password symlink: {error}"));
        assert!(matches!(
            store.is_configured(),
            Err(PasswordError::InsecurePath(_))
        ));
    }

    #[test]
    fn corrupted_record_fails_without_falling_back_to_password_setup() {
        let directory = TestDirectory::new("corrupt");
        let store = password_store(&directory);
        if let Err(error) = store.configure_once("another secure password", 1) {
            panic!("could not configure password: {error}");
        }
        let mut bytes = fs::read(store.path())
            .unwrap_or_else(|error| panic!("could not read password record: {error}"));
        let last = bytes
            .last_mut()
            .unwrap_or_else(|| panic!("password record unexpectedly empty"));
        *last ^= 1;
        fs::write(store.path(), bytes)
            .unwrap_or_else(|error| panic!("could not corrupt password record: {error}"));
        #[cfg(unix)]
        fs::set_permissions(store.path(), fs::Permissions::from_mode(0o600))
            .unwrap_or_else(|error| panic!("could not restore password permissions: {error}"));
        assert!(matches!(
            store.is_configured(),
            Err(PasswordError::CorruptFile(_))
        ));
        assert!(matches!(
            store.configure_once("replacement password", 2),
            Err(PasswordError::CorruptFile(_))
        ));
    }
}
