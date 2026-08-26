use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;

const MAX_SCOPE_BYTES: usize = 32;
const MAX_RATE_SUBJECTS: usize = 65_536;
const MAX_ATTEMPTS_PER_WINDOW: u32 = 10_000;
const MAX_RATE_WINDOW_MS: u64 = 24 * 60 * 60 * 1_000;
const MAX_BLOCK_MS: u64 = 7 * 24 * 60 * 60 * 1_000;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RateLimitConfig {
    pub max_subjects: usize,
    pub attempts_per_window: u32,
    pub window_ms: u64,
    pub block_ms: u64,
}

impl RateLimitConfig {
    pub fn validate(self) -> Result<Self, RateLimitError> {
        if self.max_subjects == 0
            || self.attempts_per_window == 0
            || self.window_ms == 0
            || self.block_ms == 0
            || self.max_subjects > MAX_RATE_SUBJECTS
            || self.attempts_per_window > MAX_ATTEMPTS_PER_WINDOW
            || self.window_ms > MAX_RATE_WINDOW_MS
            || self.block_ms > MAX_BLOCK_MS
        {
            return Err(RateLimitError::InvalidConfiguration);
        }
        Ok(self)
    }
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
struct RateKey {
    scope: String,
    subject_digest: [u8; 32],
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct RateEntry {
    window_started_at_ms: u64,
    attempts: u32,
    blocked_until_ms: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RateDecision {
    Allowed { remaining: u32 },
    Blocked { retry_after_ms: u64 },
}

#[derive(Debug)]
pub struct BoundedRateLimiter {
    config: RateLimitConfig,
    entries: BTreeMap<RateKey, RateEntry>,
}

impl BoundedRateLimiter {
    pub fn new(config: RateLimitConfig) -> Result<Self, RateLimitError> {
        Ok(Self {
            config: config.validate()?,
            entries: BTreeMap::new(),
        })
    }

    pub fn record_failure(
        &mut self,
        scope: &str,
        subject_digest: [u8; 32],
        now_ms: u64,
    ) -> Result<RateDecision, RateLimitError> {
        validate_scope(scope)?;
        self.remove_expired(now_ms);
        let key = RateKey {
            scope: scope.to_owned(),
            subject_digest,
        };
        if let Some(entry) = self.entries.get_mut(&key) {
            if entry.blocked_until_ms > now_ms {
                return Ok(RateDecision::Blocked {
                    retry_after_ms: entry.blocked_until_ms.saturating_sub(now_ms),
                });
            }
            if now_ms.saturating_sub(entry.window_started_at_ms) >= self.config.window_ms {
                *entry = RateEntry {
                    window_started_at_ms: now_ms,
                    attempts: 0,
                    blocked_until_ms: 0,
                };
            }
            entry.attempts = entry.attempts.saturating_add(1);
            if entry.attempts > self.config.attempts_per_window {
                entry.blocked_until_ms = now_ms.saturating_add(self.config.block_ms);
                return Ok(RateDecision::Blocked {
                    retry_after_ms: self.config.block_ms,
                });
            }
            return Ok(RateDecision::Allowed {
                remaining: self.config.attempts_per_window - entry.attempts,
            });
        }
        if self.entries.len() >= self.config.max_subjects {
            return Err(RateLimitError::CapacityReached);
        }
        self.entries.insert(
            key,
            RateEntry {
                window_started_at_ms: now_ms,
                attempts: 1,
                blocked_until_ms: 0,
            },
        );
        Ok(RateDecision::Allowed {
            remaining: self.config.attempts_per_window.saturating_sub(1),
        })
    }

    pub fn clear(&mut self, scope: &str, subject_digest: [u8; 32]) -> Result<bool, RateLimitError> {
        validate_scope(scope)?;
        Ok(self
            .entries
            .remove(&RateKey {
                scope: scope.to_owned(),
                subject_digest,
            })
            .is_some())
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    fn remove_expired(&mut self, now_ms: u64) {
        let window_ms = self.config.window_ms;
        self.entries.retain(|_key, entry| {
            entry.blocked_until_ms > now_ms
                || now_ms.saturating_sub(entry.window_started_at_ms) < window_ms
        });
    }
}

fn validate_scope(scope: &str) -> Result<(), RateLimitError> {
    if scope.is_empty()
        || scope.len() > MAX_SCOPE_BYTES
        || !scope
            .as_bytes()
            .iter()
            .all(|byte| byte.is_ascii_lowercase() || *byte == b'-')
    {
        return Err(RateLimitError::InvalidScope);
    }
    Ok(())
}

#[derive(Debug, Eq, PartialEq)]
pub enum RateLimitError {
    CapacityReached,
    InvalidConfiguration,
    InvalidScope,
}

impl fmt::Display for RateLimitError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::CapacityReached => {
                formatter.write_str("rate-limit subject capacity reached; request denied")
            }
            Self::InvalidConfiguration => formatter.write_str("invalid rate-limit configuration"),
            Self::InvalidScope => formatter.write_str("invalid rate-limit scope"),
        }
    }
}

impl Error for RateLimitError {}

#[cfg(test)]
mod tests {
    use super::{BoundedRateLimiter, RateDecision, RateLimitConfig, RateLimitError};

    fn limiter(max_subjects: usize) -> BoundedRateLimiter {
        match BoundedRateLimiter::new(RateLimitConfig {
            max_subjects,
            attempts_per_window: 2,
            window_ms: 1_000,
            block_ms: 5_000,
        }) {
            Ok(value) => value,
            Err(error) => panic!("invalid test limiter: {error}"),
        }
    }

    #[test]
    fn attempts_are_bounded_and_blocking_is_deterministic() {
        let mut value = limiter(4);
        let digest = [7_u8; 32];
        assert_eq!(
            value.record_failure("pairing", digest, 100),
            Ok(RateDecision::Allowed { remaining: 1 })
        );
        assert_eq!(
            value.record_failure("pairing", digest, 101),
            Ok(RateDecision::Allowed { remaining: 0 })
        );
        assert_eq!(
            value.record_failure("pairing", digest, 102),
            Ok(RateDecision::Blocked {
                retry_after_ms: 5_000
            })
        );
        assert_eq!(
            value.record_failure("pairing", digest, 103),
            Ok(RateDecision::Blocked {
                retry_after_ms: 4_999
            })
        );
        assert_eq!(
            value.record_failure("pairing", digest, 5_102),
            Ok(RateDecision::Allowed { remaining: 1 })
        );
    }

    #[test]
    fn subject_capacity_fails_closed_until_entries_expire() {
        let mut value = limiter(1);
        assert!(value.record_failure("session", [1_u8; 32], 1).is_ok());
        assert_eq!(
            value.record_failure("session", [2_u8; 32], 2),
            Err(RateLimitError::CapacityReached)
        );
        assert_eq!(value.len(), 1);
        assert!(value.record_failure("session", [2_u8; 32], 1_001).is_ok());
        assert_eq!(value.len(), 1);
    }

    #[test]
    fn successful_authentication_can_clear_only_the_exact_subject() {
        let mut value = limiter(4);
        assert!(value.record_failure("session", [3_u8; 32], 1).is_ok());
        assert!(value.record_failure("session", [4_u8; 32], 1).is_ok());
        assert_eq!(value.clear("session", [3_u8; 32]), Ok(true));
        assert_eq!(value.clear("session", [3_u8; 32]), Ok(false));
        assert_eq!(value.len(), 1);
        assert!(value.clear("BAD", [4_u8; 32]).is_err());
    }

    #[test]
    fn configuration_cannot_disable_hard_resource_bounds() {
        for config in [
            RateLimitConfig {
                max_subjects: super::MAX_RATE_SUBJECTS + 1,
                attempts_per_window: 1,
                window_ms: 1,
                block_ms: 1,
            },
            RateLimitConfig {
                max_subjects: 1,
                attempts_per_window: super::MAX_ATTEMPTS_PER_WINDOW + 1,
                window_ms: 1,
                block_ms: 1,
            },
            RateLimitConfig {
                max_subjects: 1,
                attempts_per_window: 1,
                window_ms: super::MAX_RATE_WINDOW_MS + 1,
                block_ms: 1,
            },
            RateLimitConfig {
                max_subjects: 1,
                attempts_per_window: 1,
                window_ms: 1,
                block_ms: super::MAX_BLOCK_MS + 1,
            },
        ] {
            assert_eq!(
                BoundedRateLimiter::new(config).err(),
                Some(RateLimitError::InvalidConfiguration)
            );
        }
    }
}
