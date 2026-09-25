# Client-file staging

This source protocol uses TermDeck web login plus the separately enrolled machine
credential described in [machine enrollment](client-machine-enrollment.md).
It does not use CloudMCP authentication or change an independently installed
Codex runtime. Production activation follows the existing release workflow.

## Storage and identity

The host selects all storage identities. With the production session database,
content resides under:

```
.termdeck/state/client-files/<account-id>/<machine-id>/objects/<object-id>/g<generation>/content/<relative-file>
```

Directories are owner-only 0700; partial files and metadata are 0600; published
content is 0400. The state root must already be private. No symlink component,
foreign owner/device, hardlinked file, or replaced recorded inode is accepted.
Source, `.beads`, auth, TLS, releases, rollback and service paths are outside
this tree. User-provided absolute paths are display metadata only.

An immutable reference consists of account, machine, object ID, generation,
authoritative manifest SHA-256, and total byte count. Every file also records its
SHA-256, size, relative name, uploader session, client modification metadata and
declared media type. Media type is descriptive; downloads are bounded JSON/base64
data and uploads are never executed or extracted.

## API

POST `/_termdeck/api/v1/client-files` uses the normal login cookie, same-origin
checks, `X-TermDeck-CSRF`, JSON content type, and `X-TermDeck-Machine-Key`.
The server constructs the account/machine/session actor. The CLI receives the
bounded envelope on stdin, never as shell text or command-line credentials.
Its fixed `client-files --db ... --auth-dir ...` bridge revalidates TermDeck's
login database and machine credential generation. Machine mutations serialize
against staging through the enrollment database transaction; revocation that
commits first prevents staging. Login expiration and machine revocation remain
distinct states.

Operations:

| Operation | Payload in addition to `operation` |
| --- | --- |
| `list` | optional bounded `offset`; 50 metadata summaries per page |
| `inspect` | exact `reference` |
| `initiate` | 32-hex `request_id`, `files`, optional label/display path/previous reference |
| `chunk` | upload reference, file index, byte offset, base64 data |
| `finalize` | request ID, original upload reference |
| `download` | published reference, file index, byte offset |
| `remove` | request ID, exact reference; refuses attached generations |
| `attach` | request ID, ready reference, target identity; records intent only |
| `detach` | request ID, reference, attachment ID |
| `cleanup` | request ID; expires unfinished generations older than 24 hours |

`files` contains relative `path`, integer `size`, optional SHA-256 `digest`,
`media_type`, and `modified_ms`. Limits are 256 files, 16 MiB per file,
64 MiB per object, 512 MiB reserved per machine, 4096 generations per machine,
and 256 KiB per chunk. The metadata database is separately size-bounded.
Archives remain opaque files. Empty directories are not represented.

The internal `resolve` operation is unavailable over HTTP. It requires an
authorized attachment and rechecks account, machine, login, revocation, exact
generation/digest, file identity and content before returning a canonical host
path to an internal launch caller. Target launch policy is additionally owned by
the agent attachment integration; an inert attachment record grants no execution.

## Lifecycle, retries, and interruption

`allocating` → `uploading` → `finalizing` → `ready` is durable. No resolver can
read unpublished generations. The authoritative metadata commit publishes the
whole generation. A refresh creates a new generation, then marks the previous
generation `changed`; existing attachments continue to resolve the original
verified bytes. New attachments require the current ready generation.

Initiation/finalization/removal/attachment retries are scoped to the login session
and request ID. Changed payload reuse conflicts. Chunk retries must reproduce
the already written bytes at that offset. Replayed receipts report current
lifecycle, so a removed object is never reported ready from an old receipt.

Removal records `removing` before unlinking receipted content; expiry records
`expiring`, then `failed`. A restarted request or explicit cleanup resumes these
operations. Attached generations are retained. A process death between empty
file creation and its inode receipt leaves an unreceipted leaf in a private,
quota-counted allocation. It is quarantined: neither adopted nor deleted
automatically. Inspection and logs identify the object without disclosing keys;
an administrator must establish ownership before removing unexpected content.
Unexpected substitutions likewise fail closed. Empty metadata directories and
receipts remain for audit; rollback must preserve this separate staging tree.

Run `python -m pytest -q tests/test_client_files.py` and the real bridge proof:

```
cargo build --manifest-path web/Cargo.toml -p termdeck-web-server
python scripts/check_client_files_integration.py --binary web/target/debug/termdeck-web-server --output /tmp/staging-proof.json
```

These use disposable state. They do not establish production activation or
validation from another physical machine.
