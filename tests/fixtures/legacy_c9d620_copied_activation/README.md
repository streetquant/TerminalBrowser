# Captured copied activation

These are exact read-only captures from the refused activation on 2026-09-26.
`provenance.json` records their original paths, modes, sizes, and SHA-256 values.
The fixtures are stored without executable permission and contain no credentials
or user session data.

The installed bootstrap omits the state-root environment assignment emitted by
the exact c9d620 wheel. Its managed state names the matching older launcher hash.
The captured public launcher already points at the colocated managed root; this
is an existing supported migration input. The captured desktop points at the
older user launcher and predates the managed autostart receipt. Its exact bytes,
mode, owner, group, and single-link status are admitted only within the same
authenticated historical migration.

Tests retain all four captures byte for byte and mount their copies at the
recorded current paths in a private namespace before migration. Historical
paths are rendering inputs only. Current user service files are also captured
read-only into the disposable unit directory; their bytes, modes, and the absent
watchdog baseline must survive the inverse. The namespace has an independently validated
empty control directory because it does not map the host's root signer. The
disposable candidate validates that fixture's path, mode, owner, group, and
contents; storage admission uses a synthetic fixture. These tests prove package
publication, launcher verification, and exact inverse behavior, and do not
claim production storage, root-signer, or service-start acceptance.

For the full artifact-backed regression, set `TERMDECK_TEST_LEGACY_RELEASE` to a
read-only copy of release `c9d6204ee6dbe01dc4225970cb41014e759d38146bef824b5fbda71cb3ed707c`.
The ordinary tests also exercise the closed rendering gate with these golden
bytes; those fast checks do not substitute for verification of the real wheel.
