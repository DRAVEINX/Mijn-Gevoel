# MoodPress Cloud Portal: private deployment preview

This is a self-hostable preview of the cloud sync API and the online support portal. It is not automatically online when committed to GitHub.

## Requirements

- A Linux server you control, Docker Engine and the Docker Compose plugin.
- A domain name with DNS A/AAAA records pointing to that server.
- Inbound TCP ports 80 and 443 available so Caddy can provision HTTPS.
- Persistent storage and backups for the Docker volume `moodpress_data`.

## Start the service

From the repository root:

```sh
cp deploy/.env.example deploy/.env
# Edit deploy/.env and set MOODPRESS_DOMAIN to your real domain.
docker compose --env-file deploy/.env -f deploy/docker-compose.yml up --build -d
docker compose --env-file deploy/.env -f deploy/docker-compose.yml logs -f moodpress-cloud
```

Open `https://your-domain.example` in a browser. The portal's health endpoint is `/health`. Configure that same HTTPS base URL in MoodPress on each device. Use the online portal for owner account registration and invitation management.

The service process is not exposed directly on the public network; Caddy handles public HTTPS and forwards requests to the internal API port. The SQLite database is stored on a persistent named volume. Back up that volume regularly using your hosting provider's snapshots or a tested database-backup job.

## Before using real personal data

The cloud service currently does not send e-mail, verify e-mail ownership or deliver password-reset links. Invitation codes must be shared out of band. Do not advertise this preview as a public healthcare service, and do not invite real care teams or store sensitive data until account recovery, identity verification, rate-limit/abuse monitoring, data retention/deletion, privacy notices/consent, controller/processor responsibilities, and a security/privacy review have been completed. If the service is internet-facing before then, restrict it to a trusted pilot group at the reverse-proxy or network layer.

The cloud sync archive holds an encrypted account file and encrypted data-record files. The API validates the archive digest and version but does not decrypt those records. The support portal separately encrypts shared text in the browser with AES-GCM; its key is not stored by this server.

## Operational checks

- Confirm `GET /health` returns HTTP 200 through HTTPS.
- Register a test owner, make a second test account, and use the invitation flow.
- Test that category permissions hide unrelated shared items and that revoking a member blocks future reads.
- Sync an empty test account from two devices, then create different records on each and verify the merged result.
- Change the same record on both devices and confirm the conflict is reported without overwriting either version.
- Test restore, database backup/restore, owner account deletion, and loss of a device before any production use.
