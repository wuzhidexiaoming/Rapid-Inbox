# rapid-inbox-ingestd

`rapid-inbox-ingestd` is the C++ SMTP ingest process for Rapid Inbox.

Phase 1 accepts SMTP mail, returns `250 queued as <message_id>` after the
message enters the in-memory queue, and batch-writes raw mail, recovery
manifests, and SQLite pending records. The Python HTTP app remains responsible
for admin/public UI and parsing pending messages.

## Build

```bash
cmake -S cpp/ingestd -B cpp/ingestd/build
cmake --build cpp/ingestd/build
ctest --test-dir cpp/ingestd/build --output-on-failure
```

## Run

```bash
SMTP_HOST=127.0.0.1 SMTP_PORT=2525 \
  cpp/ingestd/build/rapid-inbox-ingestd --base-dir .
```

## Durability Semantics

`250 queued` means the message is in the ingestd process memory queue. A normal
SIGTERM/SIGINT stops accepting new connections and drains returned-250 mail to
storage and SQLite before exit. A crash, kill -9, machine reboot, or power loss
can lose messages that have not yet been written.
