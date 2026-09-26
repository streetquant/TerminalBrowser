# Browser window configuration and client files

The Windows page includes **New window** and **Client files**. An empty window
requires only a name. Expand **Configure workspace, runtime and client files**
to choose a saved workspace or **Enter server directory…**, a supported runtime,
one to eight initial tabs, and immutable staged attachments. A server directory
must already exist and be accessible to the installed host account. Paths with
spaces are supported; read-only directories remain read-only. Protected private
storage and system control trees are excluded. The server validates and pins the
canonical directory before launch; errors stay in the dialog for correction.
The server policy is described in [web-agent-launch.md](web-agent-launch.md).
Selecting a directory does not register a new runtime or executable command.

**Server directory** refers to the machine running TermDeck. It does not read a
path on the browser's machine. Select local files below to upload copies and
attach their immutable references to the remote task.

Machine enrollment is explicit and separate from web login. The browser creates
a random credential and stores it in its origin/base-path-scoped IndexedDB.
This is an enrolled client installation identity, not hardware attestation.
Copying a browser profile copies its credential; a new or incognito profile has
no enrollment even with the same login cookie. Enrollment/rotation requests are
saved before submission for interrupted-request recovery. Keep browser storage
private. Rename, list, rotate and revoke controls use the same machine API as
other clients. Revocation disables future staging and launch authorization;
it does not terminate existing user tasks.

## Explicit local selection

Use the file picker, multiple-file picker, directory picker where supported, or
drag and drop. Only selected `File` objects are read. A typed client path never
grants filesystem access. Browser relative paths and names are display metadata;
the server chooses destinations. Directory copies preserve selected relative
structure. Archives remain ordinary data and are never extracted automatically.

The UI shows upload progress, machine label, display path, lifecycle state,
generation, digest and attachment details. Refresh requires another explicit
selection and publishes a new generation. The old generation becomes `changed`
and remains immutable for existing attachments. The browser cannot detect changes
to files it has not been authorized to read again. A failed upload can be retried
while the selection remains open, using the same initiation/finalization request
IDs. Reloading drops file handles; it does not grant background sync. Expired
partial copies have explicit cleanup. Attachment removal requires detachment.

Creation failures remain visible in the dialog. A retry retains its creation ID.
Successful creation refreshes inventory and navigates to the server's canonical
window route. Pending/failed launch receipts do not create replacement windows
on replay. Existing windows and terminals are retained.

See [client-machine-enrollment.md](client-machine-enrollment.md) and
[client-file-staging.md](client-file-staging.md) for authentication, namespace,
limits, immutable references and lifecycle details.

## Optional persistent local companion design

No companion is required or shipped for browser selection. Persistent browsing
or synchronization outside authorized browser handles would require a separately
installed client with explicit local path grants. It must use the existing
machine enrollment and staging protocol, not an alternate namespace or direct
server writes. The client validates the host TLS identity; the host validates
the enrolled machine credential plus a valid account session. A loopback/local
socket control interface defaults to deny and never listens publicly by default.

Grants must be user-selected real directories with no-follow descriptor-based
traversal, bounded file counts/bytes, no symlink escapes, revocable local grants,
and server machine revocation checked on every operation. Upload generations
use the same server-generated object IDs, digests and immutable references.
Credential rotation/re-enrollment is explicit. The host cannot request arbitrary
paths beyond grants. Implementation and physical-client acceptance of such a
companion are separate future work, not a capability of this browser UI.

## Source qualification

`scripts/check_client_files_integration.py --browser` starts an isolated server,
authentication database, registry, tmux server and Chromium profiles. It exercises
enrollment, selection, progress, refresh, retry, directory upload, rotation,
configured window navigation, attachments and cross-profile revocation. It does
not validate a second physical machine or the production `/t1000/` deployment.
