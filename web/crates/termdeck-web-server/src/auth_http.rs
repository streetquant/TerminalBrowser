use axum::http::{HeaderMap, HeaderValue, header};
use std::error::Error;
use std::fmt;
use std::sync::{Mutex, MutexGuard};
use termdeck_web_auth::{
    AuthError, AuthSessionId, AuthStore, AuthenticatedDeviceId, AuthenticatedSession,
    BoundedRateLimiter, DeviceRecord, MachineStore, OwnerPasswordStore, PasswordError,
    RateDecision, RateLimitConfig, RateLimitError, SecretKind, SessionContext,
    VerifiedOwnerPassword, rate_subject_digest, validate_secret,
};
use zeroize::Zeroizing;

pub const PAIRING_PATH: &str = "/_termdeck/pair";
pub const SETUP_PATH: &str = "/_termdeck/setup";
pub const LOGIN_PATH: &str = "/_termdeck/login";
pub const SESSION_PATH: &str = "/_termdeck/api/v1/session";
pub const LOGOUT_PATH: &str = "/_termdeck/api/v1/logout";
pub const DEVICES_PATH: &str = "/_termdeck/api/v1/devices";
pub const CSRF_HEADER_NAME: &str = "x-termdeck-csrf";
pub const MAX_AUTH_REQUEST_BYTES: usize = 1_024;
pub const MAX_COOKIE_HEADER_BYTES: usize = 8 * 1024;

const INSECURE_SESSION_COOKIE: &str = "termdeck_session";
const INSECURE_CSRF_COOKIE: &str = "termdeck_csrf";
const SECURE_SESSION_COOKIE: &str = "__Host-termdeck_session";
const SECURE_CSRF_COOKIE: &str = "__Host-termdeck_csrf";
const PAIRING_RATE_SCOPE: &str = "pairing";
const PASSWORD_RATE_SCOPE: &str = "password";
const SESSION_RATE_SCOPE: &str = "session";
const RATE_SUBJECT: &[u8] = b"loopback-owner";
const DEFAULT_RATE_SUBJECTS: usize = 8;
const DEFAULT_RATE_ATTEMPTS: u32 = 8;
const DEFAULT_RATE_WINDOW_MS: u64 = 60_000;
const DEFAULT_RATE_BLOCK_MS: u64 = 5 * 60_000;

pub struct HttpAuth {
    store: AuthStore,
    machines: MachineStore,
    password_store: OwnerPasswordStore,
    limiter: Mutex<BoundedRateLimiter>,
    session_ttl_ms: u64,
    secure_cookies: bool,
}

impl HttpAuth {
    pub fn new(
        store: AuthStore,
        session_ttl_ms: u64,
        secure_cookies: bool,
    ) -> Result<Self, HttpAuthError> {
        Self::new_with_rate_config(
            store,
            session_ttl_ms,
            secure_cookies,
            RateLimitConfig {
                max_subjects: DEFAULT_RATE_SUBJECTS,
                attempts_per_window: DEFAULT_RATE_ATTEMPTS,
                window_ms: DEFAULT_RATE_WINDOW_MS,
                block_ms: DEFAULT_RATE_BLOCK_MS,
            },
        )
    }

    fn new_with_rate_config(
        store: AuthStore,
        session_ttl_ms: u64,
        secure_cookies: bool,
        rate_config: RateLimitConfig,
    ) -> Result<Self, HttpAuthError> {
        if session_ttl_ms == 0 {
            return Err(HttpAuthError::InvalidConfiguration);
        }
        let secure_directory = store
            .database_path()
            .parent()
            .ok_or(HttpAuthError::InvalidConfiguration)?;
        let password_store = OwnerPasswordStore::new(
            secure_directory,
            store.host_id().clone(),
            store.owner_id().clone(),
        )
        .map_err(HttpAuthError::Password)?;
        let machines =
            MachineStore::open(&store).map_err(|_| HttpAuthError::InvalidConfiguration)?;
        Ok(Self {
            store,
            machines,
            password_store,
            limiter: Mutex::new(BoundedRateLimiter::new(rate_config)?),
            session_ttl_ms,
            secure_cookies,
        })
    }

    pub fn now_ms(&self, wall_clock_ms: u64) -> Result<u64, HttpAuthError> {
        self.store
            .observe_time(wall_clock_ms)
            .map_err(HttpAuthError::Auth)
    }

    pub(crate) fn machine_stores(&self) -> (&AuthStore, &MachineStore) {
        (&self.store, &self.machines)
    }

    /// Revalidate an upgraded connection without retaining its cookie secret
    /// or charging the HTTP login rate limiter for terminal traffic.
    pub(crate) fn require_session(
        &self,
        context: &SessionContext,
        wall_clock_ms: u64,
    ) -> Result<(), HttpAuthError> {
        self.store
            .require_context(context, wall_clock_ms)
            .map_err(HttpAuthError::Auth)
    }

    pub fn authenticate(
        &self,
        headers: &HeaderMap,
        wall_clock_ms: u64,
    ) -> Result<SessionContext, HttpAuthError> {
        let session_secret = cookie_value(headers, self.session_cookie_name())?
            .ok_or(HttpAuthError::Unauthenticated)?;
        let now_ms = self.now_ms(wall_clock_ms)?;
        self.check_rate(SESSION_RATE_SCOPE, now_ms)?;
        if validate_secret(SecretKind::Session, session_secret.as_str()).is_err() {
            self.record_failure(SESSION_RATE_SCOPE, now_ms)?;
            return Err(HttpAuthError::Unauthenticated);
        }
        match self.store.authenticate(session_secret.as_str(), now_ms) {
            Ok(context) => {
                self.clear_rate(SESSION_RATE_SCOPE)?;
                Ok(context)
            }
            Err(AuthError::Unauthenticated | AuthError::Secret(_)) => {
                self.record_failure(SESSION_RATE_SCOPE, now_ms)?;
                Err(HttpAuthError::Unauthenticated)
            }
            Err(error) => Err(HttpAuthError::Auth(error)),
        }
    }

    pub fn pair(
        &self,
        invitation_secret: &str,
        wall_clock_ms: u64,
    ) -> Result<AuthenticatedSession, HttpAuthError> {
        let now_ms = self.now_ms(wall_clock_ms)?;
        self.check_rate(PAIRING_RATE_SCOPE, now_ms)?;
        match self
            .store
            .redeem_invitation(invitation_secret, now_ms, self.session_ttl_ms)
        {
            Ok(session) => {
                self.clear_rate(PAIRING_RATE_SCOPE)?;
                Ok(session)
            }
            Err(
                AuthError::InvitationUnavailable
                | AuthError::Unauthenticated
                | AuthError::Secret(_),
            ) => {
                self.record_failure(PAIRING_RATE_SCOPE, now_ms)?;
                Err(HttpAuthError::Unauthenticated)
            }
            Err(error) => Err(HttpAuthError::Auth(error)),
        }
    }

    pub fn password_configured(&self) -> Result<bool, HttpAuthError> {
        self.password_store
            .is_configured()
            .map_err(HttpAuthError::Password)
    }

    pub fn setup_password(
        &self,
        password: &str,
        device_label: &str,
        wall_clock_ms: u64,
    ) -> Result<AuthenticatedSession, HttpAuthError> {
        let now_ms = self.now_ms(wall_clock_ms)?;
        self.check_rate(PASSWORD_RATE_SCOPE, now_ms)?;
        let proof = match self.password_store.configure_once(password, now_ms) {
            Ok(value) => value,
            Err(PasswordError::AlreadyConfigured) => {
                return Err(HttpAuthError::PasswordAlreadyConfigured);
            }
            Err(PasswordError::InvalidPassword) => return Err(HttpAuthError::InvalidPassword),
            Err(error) => return Err(HttpAuthError::Password(error)),
        };
        let session = self
            .store
            .create_password_session(&proof, device_label, now_ms, self.session_ttl_ms)
            .map_err(HttpAuthError::Auth)?;
        self.clear_rate(PASSWORD_RATE_SCOPE)?;
        Ok(session)
    }

    pub fn login_password(
        &self,
        password: &str,
        device_label: &str,
        wall_clock_ms: u64,
    ) -> Result<AuthenticatedSession, HttpAuthError> {
        let now_ms = self.now_ms(wall_clock_ms)?;
        self.check_rate(PASSWORD_RATE_SCOPE, now_ms)?;
        let proof: VerifiedOwnerPassword = match self.password_store.verify(password) {
            Ok(value) => value,
            Err(
                PasswordError::InvalidPassword
                | PasswordError::NotConfigured
                | PasswordError::Unauthenticated,
            ) => {
                self.record_failure(PASSWORD_RATE_SCOPE, now_ms)?;
                return Err(HttpAuthError::Unauthenticated);
            }
            Err(error) => return Err(HttpAuthError::Password(error)),
        };
        let session = self
            .store
            .create_password_session(&proof, device_label, now_ms, self.session_ttl_ms)
            .map_err(HttpAuthError::Auth)?;
        self.clear_rate(PASSWORD_RATE_SCOPE)?;
        Ok(session)
    }

    pub fn verify_csrf(
        &self,
        headers: &HeaderMap,
        context: &SessionContext,
        wall_clock_ms: u64,
    ) -> Result<(), HttpAuthError> {
        let now_ms = self.now_ms(wall_clock_ms)?;
        let cookie =
            cookie_value(headers, self.csrf_cookie_name())?.ok_or(HttpAuthError::CsrfRejected)?;
        let header_value =
            one_header(headers, CSRF_HEADER_NAME)?.ok_or(HttpAuthError::CsrfRejected)?;
        let header_value = header_value
            .to_str()
            .map_err(|_| HttpAuthError::CsrfRejected)?;
        if cookie.as_str() != header_value {
            return Err(HttpAuthError::CsrfRejected);
        }
        match self
            .store
            .verify_csrf(context.session_id(), header_value, now_ms)
        {
            Ok(()) => Ok(()),
            Err(AuthError::CsrfMismatch | AuthError::Unauthenticated | AuthError::Secret(_)) => {
                Err(HttpAuthError::CsrfRejected)
            }
            Err(error) => Err(HttpAuthError::Auth(error)),
        }
    }

    pub fn session_cookie_headers(
        &self,
        session: &AuthenticatedSession,
        wall_clock_ms: u64,
    ) -> Result<[HeaderValue; 2], HttpAuthError> {
        let now_ms = self.now_ms(wall_clock_ms)?;
        let remaining_ms = session.expires_at_ms().saturating_sub(now_ms);
        if remaining_ms == 0 {
            return Err(HttpAuthError::ExpiredSession);
        }
        let max_age_seconds = remaining_ms.div_ceil(1_000);
        let session_cookie = format_cookie(
            self.session_cookie_name(),
            session.session_secret().expose_secret(),
            max_age_seconds,
            true,
            self.secure_cookies,
        )?;
        let csrf_cookie = format_cookie(
            self.csrf_cookie_name(),
            session.csrf_secret().expose_secret(),
            max_age_seconds,
            false,
            self.secure_cookies,
        )?;
        Ok([session_cookie, csrf_cookie])
    }

    pub fn clearing_cookie_headers(&self) -> Result<[HeaderValue; 2], HttpAuthError> {
        let session = clear_cookie(self.session_cookie_name(), true, self.secure_cookies)?;
        let csrf = clear_cookie(self.csrf_cookie_name(), false, self.secure_cookies)?;
        Ok([session, csrf])
    }

    pub fn revoke_session(
        &self,
        session_id: &AuthSessionId,
        wall_clock_ms: u64,
    ) -> Result<bool, HttpAuthError> {
        let now_ms = self.now_ms(wall_clock_ms)?;
        self.store
            .revoke_session(session_id, now_ms)
            .map_err(HttpAuthError::Auth)
    }

    pub fn revoke_device(
        &self,
        device_id: &AuthenticatedDeviceId,
        wall_clock_ms: u64,
    ) -> Result<bool, HttpAuthError> {
        let now_ms = self.now_ms(wall_clock_ms)?;
        self.store
            .revoke_device(device_id, now_ms)
            .map_err(HttpAuthError::Auth)
    }

    pub fn devices(&self) -> Result<Vec<DeviceRecord>, HttpAuthError> {
        self.store.devices().map_err(HttpAuthError::Auth)
    }

    pub fn secure_cookies(&self) -> bool {
        self.secure_cookies
    }

    fn session_cookie_name(&self) -> &'static str {
        if self.secure_cookies {
            SECURE_SESSION_COOKIE
        } else {
            INSECURE_SESSION_COOKIE
        }
    }

    fn csrf_cookie_name(&self) -> &'static str {
        if self.secure_cookies {
            SECURE_CSRF_COOKIE
        } else {
            INSECURE_CSRF_COOKIE
        }
    }

    fn check_rate(&self, scope: &str, now_ms: u64) -> Result<(), HttpAuthError> {
        let subject = rate_subject_digest(scope, RATE_SUBJECT)?;
        match self.lock_limiter()?.check(scope, subject, now_ms)? {
            RateDecision::Allowed { .. } => Ok(()),
            RateDecision::Blocked { retry_after_ms } => {
                Err(HttpAuthError::RateLimited { retry_after_ms })
            }
        }
    }

    fn record_failure(&self, scope: &str, now_ms: u64) -> Result<(), HttpAuthError> {
        let subject = rate_subject_digest(scope, RATE_SUBJECT)?;
        match self
            .lock_limiter()?
            .record_failure(scope, subject, now_ms)?
        {
            RateDecision::Allowed { .. } => Ok(()),
            RateDecision::Blocked { retry_after_ms } => {
                Err(HttpAuthError::RateLimited { retry_after_ms })
            }
        }
    }

    fn clear_rate(&self, scope: &str) -> Result<(), HttpAuthError> {
        let subject = rate_subject_digest(scope, RATE_SUBJECT)?;
        let _cleared = self.lock_limiter()?.clear(scope, subject)?;
        Ok(())
    }

    fn lock_limiter(&self) -> Result<MutexGuard<'_, BoundedRateLimiter>, HttpAuthError> {
        self.limiter.lock().map_err(|_| HttpAuthError::LockPoisoned)
    }
}

fn one_header<'a>(
    headers: &'a HeaderMap,
    name: &'static str,
) -> Result<Option<&'a HeaderValue>, HttpAuthError> {
    let mut values = headers.get_all(name).iter();
    let first = values.next();
    if values.next().is_some() {
        return Err(HttpAuthError::MalformedHeader);
    }
    Ok(first)
}

fn cookie_value(
    headers: &HeaderMap,
    name: &str,
) -> Result<Option<Zeroizing<String>>, HttpAuthError> {
    let mut total = 0_usize;
    let mut found: Option<Zeroizing<String>> = None;
    for value in headers.get_all(header::COOKIE).iter() {
        total = total
            .checked_add(value.as_bytes().len())
            .ok_or(HttpAuthError::MalformedCookie)?;
        if total > MAX_COOKIE_HEADER_BYTES {
            return Err(HttpAuthError::MalformedCookie);
        }
        let value = value.to_str().map_err(|_| HttpAuthError::MalformedCookie)?;
        for pair in value.split(';') {
            let pair = pair.trim();
            let Some((key, value)) = pair.split_once('=') else {
                if !pair.is_empty() {
                    return Err(HttpAuthError::MalformedCookie);
                }
                continue;
            };
            if key.trim() != name {
                continue;
            }
            if found.is_some()
                || value.is_empty()
                || value.len() > 256
                || !value.as_bytes().iter().all(u8::is_ascii)
            {
                return Err(HttpAuthError::MalformedCookie);
            }
            found = Some(Zeroizing::new(value.to_owned()));
        }
    }
    Ok(found)
}

fn format_cookie(
    name: &str,
    value: &str,
    max_age_seconds: u64,
    http_only: bool,
    secure: bool,
) -> Result<HeaderValue, HttpAuthError> {
    let mut cookie = format!("{name}={value}; Path=/; SameSite=Strict; Max-Age={max_age_seconds}");
    if http_only {
        cookie.push_str("; HttpOnly");
    }
    if secure {
        cookie.push_str("; Secure");
    }
    HeaderValue::from_str(&cookie).map_err(|_| HttpAuthError::MalformedHeader)
}

fn clear_cookie(name: &str, http_only: bool, secure: bool) -> Result<HeaderValue, HttpAuthError> {
    let mut cookie = format!("{name}=; Path=/; SameSite=Strict; Max-Age=0");
    if http_only {
        cookie.push_str("; HttpOnly");
    }
    if secure {
        cookie.push_str("; Secure");
    }
    HeaderValue::from_str(&cookie).map_err(|_| HttpAuthError::MalformedHeader)
}

#[derive(Debug)]
pub enum HttpAuthError {
    Auth(AuthError),
    CsrfRejected,
    ExpiredSession,
    InvalidConfiguration,
    InvalidPassword,
    LockPoisoned,
    MalformedCookie,
    MalformedHeader,
    Password(PasswordError),
    PasswordAlreadyConfigured,
    Rate(RateLimitError),
    RateLimited { retry_after_ms: u64 },
    Secret(termdeck_web_auth::SecretError),
    Unauthenticated,
}

impl HttpAuthError {
    pub fn retry_after_ms(&self) -> Option<u64> {
        match self {
            Self::RateLimited { retry_after_ms } => Some(*retry_after_ms),
            _ => None,
        }
    }
}

impl fmt::Display for HttpAuthError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Auth(error) => write!(formatter, "authentication store failed: {error}"),
            Self::CsrfRejected => formatter.write_str("CSRF validation failed"),
            Self::ExpiredSession => formatter.write_str("authentication session is expired"),
            Self::InvalidConfiguration => {
                formatter.write_str("invalid HTTP authentication configuration")
            }
            Self::InvalidPassword => formatter.write_str("invalid owner password"),
            Self::LockPoisoned => formatter.write_str("authentication rate limiter is unavailable"),
            Self::MalformedCookie => formatter.write_str("malformed authentication cookie"),
            Self::MalformedHeader => formatter.write_str("malformed authentication header"),
            Self::Password(error) => write!(formatter, "owner password failed: {error}"),
            Self::PasswordAlreadyConfigured => {
                formatter.write_str("owner password is already configured")
            }
            Self::Rate(error) => write!(formatter, "authentication rate limiter failed: {error}"),
            Self::RateLimited { .. } => formatter.write_str("authentication rate limit reached"),
            Self::Secret(error) => error.fmt(formatter),
            Self::Unauthenticated => formatter.write_str("authentication failed"),
        }
    }
}

impl Error for HttpAuthError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Auth(error) => Some(error),
            Self::Password(error) => Some(error),
            Self::Rate(error) => Some(error),
            Self::Secret(error) => Some(error),
            _ => None,
        }
    }
}

impl From<RateLimitError> for HttpAuthError {
    fn from(value: RateLimitError) -> Self {
        Self::Rate(value)
    }
}

impl From<termdeck_web_auth::SecretError> for HttpAuthError {
    fn from(value: termdeck_web_auth::SecretError) -> Self {
        Self::Secret(value)
    }
}

#[cfg(test)]
mod tests {
    use super::{
        CSRF_HEADER_NAME, HttpAuth, HttpAuthError, INSECURE_CSRF_COOKIE, INSECURE_SESSION_COOKIE,
        SECURE_CSRF_COOKIE, SECURE_SESSION_COOKIE,
    };
    use axum::http::{HeaderMap, HeaderValue, header};
    use std::fs::{self, DirBuilder};
    use std::path::{Path, PathBuf};
    use termdeck_web_auth::{AuthLimits, AuthStore, RateLimitConfig};
    use termdeck_web_core::{HostId, OwnerId};

    #[cfg(unix)]
    use std::os::unix::fs::DirBuilderExt;

    struct TestDirectory(PathBuf);

    impl TestDirectory {
        fn new(label: &str) -> Self {
            let path = std::env::temp_dir().join(format!(
                "termdeck-http-auth-{label}-{}-{}",
                std::process::id(),
                unique_test_id()
            ));
            let mut builder = DirBuilder::new();
            #[cfg(unix)]
            builder.mode(0o700);
            if let Err(error) = builder.create(&path) {
                panic!("could not create auth test directory: {error}");
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

    fn unique_test_id() -> u128 {
        use std::time::{SystemTime, UNIX_EPOCH};
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0, |duration| duration.as_nanos())
    }

    fn id<T>(value: Result<T, termdeck_web_core::IdError>) -> T {
        match value {
            Ok(value) => value,
            Err(error) => panic!("invalid test identifier: {error}"),
        }
    }

    fn auth(directory: &TestDirectory, secure: bool) -> HttpAuth {
        let store = match AuthStore::open_or_create(
            directory.path(),
            id(HostId::new("host-test")),
            id(OwnerId::new("owner-test")),
            1,
            AuthLimits::default(),
        ) {
            Ok(value) => value,
            Err(error) => panic!("could not open auth test store: {error}"),
        };
        match HttpAuth::new(store, 10_000, secure) {
            Ok(value) => value,
            Err(error) => panic!("could not create HTTP auth: {error}"),
        }
    }

    fn cookie_headers(values: &[HeaderValue]) -> HeaderMap {
        let mut headers = HeaderMap::new();
        let joined = values
            .iter()
            .filter_map(|value| value.to_str().ok())
            .filter_map(|value| value.split_once(';').map(|(pair, _rest)| pair))
            .collect::<Vec<_>>()
            .join("; ");
        let value = match HeaderValue::from_str(&joined) {
            Ok(value) => value,
            Err(error) => panic!("invalid test cookie header: {error}"),
        };
        headers.insert(header::COOKIE, value);
        headers
    }

    #[test]
    fn upgraded_context_observes_logout_revocation_and_expiry_without_cross_session_changes() {
        let directory = TestDirectory::new("upgraded-context");
        let auth = auth(&directory, false);
        let mut contexts = Vec::new();
        for label in ["Logout", "Revoke", "Unchanged"] {
            let invitation = auth
                .store
                .create_invitation(label, 10, 1_000)
                .unwrap_or_else(|error| panic!("invitation failed: {error}"));
            let session = auth
                .pair(invitation.secret().expose_secret(), 20)
                .unwrap_or_else(|error| panic!("pairing failed: {error}"));
            let headers = cookie_headers(
                &auth
                    .session_cookie_headers(&session, 20)
                    .unwrap_or_else(|error| panic!("cookies failed: {error}")),
            );
            contexts.push(
                auth.authenticate(&headers, 21)
                    .unwrap_or_else(|error| panic!("authentication failed: {error}")),
            );
        }
        for context in &contexts {
            assert!(auth.require_session(context, 22).is_ok());
        }
        assert!(auth.revoke_session(contexts[0].session_id(), 23).is_ok());
        assert!(auth.require_session(&contexts[0], 24).is_err());
        assert!(auth.require_session(&contexts[1], 24).is_ok());
        assert!(auth.require_session(&contexts[2], 24).is_ok());
        assert!(auth.revoke_device(contexts[1].device_id(), 25).is_ok());
        assert!(auth.require_session(&contexts[1], 26).is_err());
        assert!(auth.require_session(&contexts[2], 26).is_ok());
        assert!(
            auth.require_session(&contexts[2], contexts[2].expires_at_ms())
                .is_err()
        );
        // A wall-clock rewind cannot restore an expired upgraded session.
        assert!(auth.require_session(&contexts[2], 27).is_err());
    }

    #[test]
    fn missing_session_cookie_does_not_touch_the_authentication_database() {
        let directory = TestDirectory::new("missing-cookie");
        let auth = auth(&directory, false);
        let database_path = auth.store.database_path().to_owned();
        fs::remove_file(&database_path).unwrap_or_else(|error| {
            panic!("could not remove test authentication database: {error}")
        });
        assert!(matches!(
            auth.authenticate(&HeaderMap::new(), 2),
            Err(HttpAuthError::Unauthenticated)
        ));
    }

    #[test]
    fn pairing_sets_host_only_strict_cookies_and_authenticates() {
        let directory = TestDirectory::new("pair");
        let auth = auth(&directory, false);
        let invitation = match auth.store.create_invitation("Laptop", 10, 1_000) {
            Ok(value) => value,
            Err(error) => panic!("could not create invitation: {error}"),
        };
        let session = match auth.pair(invitation.secret().expose_secret(), 20) {
            Ok(value) => value,
            Err(error) => panic!("could not pair: {error}"),
        };
        let cookies = match auth.session_cookie_headers(&session, 20) {
            Ok(value) => value,
            Err(error) => panic!("could not create cookies: {error}"),
        };
        let session_text = match cookies[0].to_str() {
            Ok(value) => value,
            Err(error) => panic!("invalid session cookie: {error}"),
        };
        let csrf_text = match cookies[1].to_str() {
            Ok(value) => value,
            Err(error) => panic!("invalid CSRF cookie: {error}"),
        };
        assert!(session_text.starts_with(INSECURE_SESSION_COOKIE));
        assert!(csrf_text.starts_with(INSECURE_CSRF_COOKIE));
        assert!(session_text.contains("HttpOnly"));
        assert!(session_text.contains("SameSite=Strict"));
        assert!(!session_text.contains("Domain="));
        assert!(!session_text.contains("Secure"));
        let headers = cookie_headers(&cookies);
        let context = match auth.authenticate(&headers, 21) {
            Ok(value) => value,
            Err(error) => panic!("could not authenticate cookie: {error}"),
        };
        assert_eq!(context.device_label(), "Laptop");
        assert_eq!(context.device_id(), session.device_id());
    }

    #[test]
    fn secure_mode_uses_host_prefix_and_secure_attribute() {
        let directory = TestDirectory::new("secure");
        let auth = auth(&directory, true);
        let invitation = match auth.store.create_invitation("Phone", 10, 1_000) {
            Ok(value) => value,
            Err(error) => panic!("could not create invitation: {error}"),
        };
        let session = match auth.pair(invitation.secret().expose_secret(), 20) {
            Ok(value) => value,
            Err(error) => panic!("could not pair: {error}"),
        };
        let cookies = match auth.session_cookie_headers(&session, 20) {
            Ok(value) => value,
            Err(error) => panic!("could not create cookies: {error}"),
        };
        let session_text = cookies[0].to_str().unwrap_or("");
        let csrf_text = cookies[1].to_str().unwrap_or("");
        assert!(session_text.starts_with(SECURE_SESSION_COOKIE));
        assert!(csrf_text.starts_with(SECURE_CSRF_COOKIE));
        assert!(session_text.contains("; Secure"));
        assert!(csrf_text.contains("; Secure"));
    }

    #[test]
    fn csrf_is_bound_to_the_authenticated_session_and_duplicate_cookies_fail() {
        let directory = TestDirectory::new("csrf");
        let auth = auth(&directory, false);
        let first_invitation = match auth.store.create_invitation("One", 10, 1_000) {
            Ok(value) => value,
            Err(error) => panic!("could not create first invitation: {error}"),
        };
        let second_invitation = match auth.store.create_invitation("Two", 10, 1_000) {
            Ok(value) => value,
            Err(error) => panic!("could not create second invitation: {error}"),
        };
        let first = match auth.pair(first_invitation.secret().expose_secret(), 20) {
            Ok(value) => value,
            Err(error) => panic!("could not pair first device: {error}"),
        };
        let second = match auth.pair(second_invitation.secret().expose_secret(), 20) {
            Ok(value) => value,
            Err(error) => panic!("could not pair second device: {error}"),
        };
        let first_cookies = auth
            .session_cookie_headers(&first, 20)
            .unwrap_or_else(|error| panic!("could not create first cookies: {error}"));
        let context = match auth.authenticate(&cookie_headers(&first_cookies), 21) {
            Ok(value) => value,
            Err(error) => panic!("could not authenticate first session: {error}"),
        };
        let mut headers = cookie_headers(&first_cookies);
        headers.insert(
            CSRF_HEADER_NAME,
            HeaderValue::from_str(second.csrf_secret().expose_secret())
                .unwrap_or_else(|error| panic!("invalid test CSRF header: {error}")),
        );
        assert!(matches!(
            auth.verify_csrf(&headers, &context, 21),
            Err(HttpAuthError::CsrfRejected)
        ));
        let duplicate = format!(
            "{INSECURE_SESSION_COOKIE}={}; {INSECURE_SESSION_COOKIE}={}",
            first.session_secret().expose_secret(),
            first.session_secret().expose_secret()
        );
        let mut duplicate_headers = HeaderMap::new();
        duplicate_headers.insert(
            header::COOKIE,
            HeaderValue::from_str(&duplicate)
                .unwrap_or_else(|error| panic!("invalid duplicate cookie header: {error}")),
        );
        assert!(matches!(
            auth.authenticate(&duplicate_headers, 22),
            Err(HttpAuthError::MalformedCookie)
        ));
    }

    #[test]
    fn repeated_pairing_failures_are_blocked_before_store_access() {
        let directory = TestDirectory::new("rate");
        let store = AuthStore::open_or_create(
            directory.path(),
            id(HostId::new("host-test")),
            id(OwnerId::new("owner-test")),
            1,
            AuthLimits::default(),
        )
        .unwrap_or_else(|error| panic!("could not open rate store: {error}"));
        let auth = HttpAuth::new_with_rate_config(
            store,
            10_000,
            false,
            RateLimitConfig {
                max_subjects: 2,
                attempts_per_window: 1,
                window_ms: 1_000,
                block_ms: 5_000,
            },
        )
        .unwrap_or_else(|error| panic!("could not create rate auth: {error}"));
        assert!(matches!(
            auth.pair("tdi1_invalid", 10),
            Err(HttpAuthError::Unauthenticated)
        ));
        assert!(matches!(
            auth.pair("tdi1_invalid", 11),
            Err(HttpAuthError::RateLimited { .. })
        ));
        assert!(matches!(
            auth.pair("tdi1_invalid", 12),
            Err(HttpAuthError::RateLimited { .. })
        ));
    }
}
