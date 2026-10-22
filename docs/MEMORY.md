# Local identity and memory

Companion identity and memories remain on the machine running the brain, under
`PET_HOME` (default `~/.picrawler_pet`). No memory content is sent to a cloud API.
The optional mind accepts only a loopback/LAN/local-hostname model endpoint. Name,
original temperament traits, birth timestamp and existing configuration fields
survive the milestone upgrade. Generated character text is narrative; it cannot
change the coordinator or Pi safety policy.

Memory schema 2 separates:

- `observed_facts`: captured input from a named source. A microphone transcript is
  captured model output, not a guarantee that the person said those exact words.
- `inferred_context`: tentative model interpretation.
- `narrative`: generated dialogue or character description.
- Legacy `observation` and `note`: retained verbatim and labeled unverified.

Old identity label tallies could contain generated descriptions, so they are kept
for recovery/export and excluded from grounded observation counts. Detection of
`person` is a class detection, not recognition of an individual. Greeting and play
counts restore bounded interaction variation after a restart; they do not identify
who is speaking. Pending actions, headings and targets are not restored.

## Inspect, export and reset

Run from the repository root with the brain environment. These commands access
local files only and never contact a robot:

```bash
brain/.venv/bin/python -m brain.pet.memory_cli inspect
brain/.venv/bin/python -m brain.pet.memory_cli export /tmp/crab-memory-export.json
brain/.venv/bin/python -m brain.pet.memory_cli reset --confirm
```

`inspect` prints identity, count and the last 20 memory records. Export contains all
records and refuses to overwrite an existing file. Reset clears episodic records
but keeps name and temperament. Add `--include-identity` only to intentionally reset
identity too. Explicit `--memory-db PATH --identity-file PATH` options precede the
subcommand. Stop the companion before reset or restoration to avoid concurrent
writers.

## Migration and recovery

Opening a legacy database runs a versioned, transactional schema migration. Before
changing it, SQLite's backup API writes `memory.db.pre-v2-<timestamp>.bak`, including
committed WAL data. Identity migration first copies the original JSON to
`identity.json.pre-v2-<timestamp>.bak`, then atomically replaces it with version 2.
Unknown identity fields are preserved. Newer schema versions and corrupt identity
JSON fail without overwriting originals. No migration touches `world.db`, model
weights, vendor servo calibration, or `crab.env`.

Reset similarly writes a `pre-reset-<timestamp>.bak` before removing data. To undo a
reset or upgrade, stop the companion, retain a copy of the current files, and copy
the selected backup to the configured memory/identity path. Restoring a legacy
backup and launching this version will migrate it again with another backup. Do not
restore over a running SQLite process or leave a newer `-wal`/`-shm` alongside a
restored main database; stop all writers and preserve the whole old file set first.
Backups intentionally retain personal data until the operator removes them.

## Model boundary

`shared/companion.py:MindOutput` is the prompt and validation schema. The model can
suggest dialogue, attention, listening, play invitation or settling. Unknown fields,
invalid gestures, motor headings, safety values and model resume requests are
rejected. `wag` is canonical; the former `wiggle` mismatch is removed. Targets must
match current detector labels and are attention only. Results expire eight seconds
after inference starts and are cancelled by newer engagement or stop. Malformed
output is never silently spoken. Quiet periods do not solicit periodic narration.

The default companion no longer rewrites its character from generated visual
prose. Useful captured interactions persist without converting hallucinations to
facts. Jetson model latency, resident RAM and hardware microphone accuracy remain
hardware acceptance work; simulated tests do not establish those properties.
