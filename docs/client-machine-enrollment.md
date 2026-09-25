# TermDeck client machine enrollment

TermDeck's owner login, browser login device, enrolled machine and terminal
Codex account are distinct identities. This protocol uses only TermDeck's own
web authentication. It has no relationship to CloudMCP authentication or the
independently installed local Codex login.

## Protocol

An authenticated user explicitly enrolls a client installation. The browser
generates 32 random bytes with Web Crypto and retains the credential in its own
origin's IndexedDB. The wire representation is `tdm1_` followed by 64 lowercase
hex digits. Enrollment sends that credential, a human label and a random
32-hex-digit request ID to `POST /_termdeck/api/v1/machines` with operation
`enroll`. TermDeck generates an immutable `mach_...` ID; it stores only a
domain-separated credential digest. Responses contain metadata, never the
credential. An account namespace is derived from the configured host and owner
IDs, independently of client labels or paths.

The same endpoint supports `rename`, `rotate` and `revoke`. Rename and rotation
require the exact current machine credential. Rotation accepts a new random
credential, preserves machine identity and increments credential generation.
Keep a pending new credential until the response is confirmed; an identical
rotation retry returns the same generation. An owner may explicitly revoke any
of their enrolled machines. Revocation is permanent for that ID; re-enrollment
creates a new identity. Concurrent rotation cannot undo revocation.

`GET /_termdeck/api/v1/machines` lists the owner's enrolled machines.
`GET /_termdeck/api/v1/machines/current` additionally requires
`X-TermDeck-Machine-Key`. It returns the authenticated machine and updates its
last-seen time. Every operation revalidates the active login session. Mutations
also pass the normal HTTP origin and CSRF checks. The body is bounded to 4096
bytes and accepts only documented fields. Request IDs are scoped to the login
session; reuse with a different payload fails with conflict. Credentials from
a revoked machine fail even while the browser login remains valid. Login
expiration does not revoke the machine: an explicitly presented credential may
be used after a new login to the same account.

The enrollment represents possession of a credential. It does not attest a
hardware serial number or prevent an owner from copying a credential. No IP,
user agent, fingerprint, hostname or cookie supplies machine authority. A new
browser profile or incognito context must explicitly enroll; it must not choose
an existing machine merely because a login or label matches. A companion may
retain the credential in its OS credential store and use the same identity
protocol, subject to the same login and request authorization.

## Storage and rollback

Machine metadata is held in the owner-only
`termdeck-machines-v1.sqlite3` beside TermDeck's web authentication database.
This separate, versioned database preserves the existing login schema. The
database is mode 0600 inside a mode 0700 direct directory. Opening and subsequent
operations reject identity changes, symlinks, hardlinks and unsafe permissions.
SQLite transactions serialize enrollment, rotation, revocation and replay
receipts. Limits are 1024 machine records and 32768 mutation receipts; reaching
a limit fails explicitly and does not discard revocations or replay history.

Back up both authentication databases consistently with the service quiescent.
An older release ignores the new database; preserve it on rollback. Do not
restore an older machine database into a running installation: that could
revive credentials revoked after the backup. Disabling staging on rollback must
also disable every attachment resolver using these identities. Browser controls
and staged-object authorization are tracked by the dependent BR tasks.
