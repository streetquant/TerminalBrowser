use getrandom::fill;
use sha2::{Digest, Sha256};
use std::error::Error;
use std::fmt;
use zeroize::Zeroize;

const SECRET_RANDOM_BYTES: usize = 32;
const ID_RANDOM_BYTES: usize = 16;
const HEX_DIGITS: &[u8; 16] = b"0123456789abcdef";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SecretKind {
    Invitation,
    Session,
    Csrf,
    Machine,
}

impl SecretKind {
    const fn prefix(self) -> &'static str {
        match self {
            Self::Invitation => "tdi1_",
            Self::Session => "tds1_",
            Self::Csrf => "tdc1_",
            Self::Machine => "tdm1_",
        }
    }

    const fn domain(self) -> &'static [u8] {
        match self {
            Self::Invitation => b"termdeck-web-auth/v1/invitation\0",
            Self::Session => b"termdeck-web-auth/v1/session\0",
            Self::Csrf => b"termdeck-web-auth/v1/csrf\0",
            Self::Machine => b"termdeck-web-auth/v1/machine\0",
        }
    }
}

#[derive(Eq, PartialEq)]
pub struct SecretToken {
    kind: SecretKind,
    value: String,
}

impl SecretToken {
    pub fn generate(kind: SecretKind) -> Result<Self, SecretError> {
        let mut random = [0_u8; SECRET_RANDOM_BYTES];
        fill(&mut random).map_err(|error| SecretError::Random(error.to_string()))?;
        let mut value = String::with_capacity(kind.prefix().len() + SECRET_RANDOM_BYTES * 2);
        value.push_str(kind.prefix());
        encode_hex_into(&mut value, &random);
        random.zeroize();
        Ok(Self { kind, value })
    }

    pub fn kind(&self) -> SecretKind {
        self.kind
    }

    pub fn expose_secret(&self) -> &str {
        self.value.as_str()
    }

    pub fn digest(&self) -> [u8; 32] {
        digest_secret(self.kind, self.value.as_str())
    }
}

impl fmt::Debug for SecretToken {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("SecretToken")
            .field("kind", &self.kind)
            .field("value", &"[REDACTED]")
            .finish()
    }
}

impl Drop for SecretToken {
    fn drop(&mut self) {
        self.value.zeroize();
    }
}

pub fn validate_secret(kind: SecretKind, value: &str) -> Result<(), SecretError> {
    let expected_len = kind.prefix().len() + SECRET_RANDOM_BYTES * 2;
    if value.len() != expected_len || !value.starts_with(kind.prefix()) {
        return Err(SecretError::Malformed);
    }
    if !value.as_bytes()[kind.prefix().len()..]
        .iter()
        .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        return Err(SecretError::Malformed);
    }
    Ok(())
}

pub fn digest_validated_secret(kind: SecretKind, value: &str) -> Result<[u8; 32], SecretError> {
    validate_secret(kind, value)?;
    Ok(digest_secret(kind, value))
}

pub fn rate_subject_digest(scope: &str, value: &[u8]) -> Result<[u8; 32], SecretError> {
    if scope.is_empty()
        || scope.len() > 32
        || !scope
            .as_bytes()
            .iter()
            .all(|byte| byte.is_ascii_lowercase() || *byte == b'-')
        || value.len() > 16 * 1024
    {
        return Err(SecretError::Malformed);
    }
    let mut hasher = Sha256::new();
    hasher.update(b"termdeck-web-auth/v1/rate-subject\0");
    hasher.update(u64::try_from(scope.len()).unwrap_or(u64::MAX).to_be_bytes());
    hasher.update(scope.as_bytes());
    hasher.update(u64::try_from(value.len()).unwrap_or(u64::MAX).to_be_bytes());
    hasher.update(value);
    Ok(hasher.finalize().into())
}

pub(crate) fn random_public_id(prefix: &str) -> Result<String, SecretError> {
    if prefix.is_empty()
        || prefix.len() > 12
        || !prefix
            .as_bytes()
            .iter()
            .all(|byte| byte.is_ascii_lowercase() || *byte == b'_')
    {
        return Err(SecretError::InvalidPrefix);
    }
    let mut random = [0_u8; ID_RANDOM_BYTES];
    fill(&mut random).map_err(|error| SecretError::Random(error.to_string()))?;
    let mut value = String::with_capacity(prefix.len() + ID_RANDOM_BYTES * 2);
    value.push_str(prefix);
    encode_hex_into(&mut value, &random);
    random.zeroize();
    Ok(value)
}

fn digest_secret(kind: SecretKind, value: &str) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(kind.domain());
    hasher.update(value.as_bytes());
    hasher.finalize().into()
}

fn encode_hex_into(output: &mut String, bytes: &[u8]) {
    for byte in bytes {
        output.push(char::from(HEX_DIGITS[usize::from(byte >> 4)]));
        output.push(char::from(HEX_DIGITS[usize::from(byte & 0x0f)]));
    }
}

#[derive(Debug, Eq, PartialEq)]
pub enum SecretError {
    InvalidPrefix,
    Malformed,
    Random(String),
}

impl fmt::Display for SecretError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidPrefix => formatter.write_str("invalid public identifier prefix"),
            Self::Malformed => formatter.write_str("malformed opaque capability"),
            Self::Random(_) => formatter.write_str("operating-system randomness is unavailable"),
        }
    }
}

impl Error for SecretError {}

#[cfg(test)]
mod tests {
    use super::{
        SecretKind, SecretToken, digest_validated_secret, random_public_id, rate_subject_digest,
    };

    #[test]
    fn generated_capabilities_are_bounded_url_safe_and_domain_separated() {
        let invitation = match SecretToken::generate(SecretKind::Invitation) {
            Ok(value) => value,
            Err(error) => panic!("could not generate invitation: {error}"),
        };
        let session = match SecretToken::generate(SecretKind::Session) {
            Ok(value) => value,
            Err(error) => panic!("could not generate session: {error}"),
        };
        let csrf = match SecretToken::generate(SecretKind::Csrf) {
            Ok(value) => value,
            Err(error) => panic!("could not generate CSRF capability: {error}"),
        };
        for (token, prefix) in [
            (&invitation, "tdi1_"),
            (&session, "tds1_"),
            (&csrf, "tdc1_"),
        ] {
            let exposed = token.expose_secret();
            assert_eq!(exposed.len(), 69);
            assert!(exposed.starts_with(prefix));
            assert!(
                exposed
                    .as_bytes()
                    .iter()
                    .all(|byte| byte.is_ascii_alphanumeric() || *byte == b'_')
            );
            assert!(!format!("{token:?}").contains(exposed));
        }
        assert_ne!(invitation.digest(), session.digest());
        assert_ne!(session.digest(), csrf.digest());
    }

    #[test]
    fn malformed_or_cross_kind_capabilities_fail_closed() {
        let invitation = match SecretToken::generate(SecretKind::Invitation) {
            Ok(value) => value,
            Err(error) => panic!("could not generate invitation: {error}"),
        };
        assert!(
            digest_validated_secret(SecretKind::Invitation, invitation.expose_secret()).is_ok()
        );
        assert!(digest_validated_secret(SecretKind::Session, invitation.expose_secret()).is_err());
        assert!(digest_validated_secret(SecretKind::Invitation, "tdi1_short").is_err());
        let mut malformed = invitation.expose_secret().to_owned();
        malformed.replace_range(12..13, "-");
        assert!(digest_validated_secret(SecretKind::Invitation, &malformed).is_err());
    }

    #[test]
    fn public_ids_are_random_bounded_and_non_secret() {
        let first = match random_public_id("dev_") {
            Ok(value) => value,
            Err(error) => panic!("could not generate device id: {error}"),
        };
        let second = match random_public_id("dev_") {
            Ok(value) => value,
            Err(error) => panic!("could not generate device id: {error}"),
        };
        assert_eq!(first.len(), 36);
        assert_ne!(first, second);
        assert!(random_public_id("BAD-").is_err());
        let pairing = match rate_subject_digest("pairing", first.as_bytes()) {
            Ok(value) => value,
            Err(error) => panic!("could not hash pairing rate subject: {error}"),
        };
        let session = match rate_subject_digest("session", first.as_bytes()) {
            Ok(value) => value,
            Err(error) => panic!("could not hash session rate subject: {error}"),
        };
        assert_ne!(pairing, session);
        assert!(rate_subject_digest("BAD", first.as_bytes()).is_err());
    }
}
