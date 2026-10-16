# MoodPress Cloud Portal (4.1)

This project is the server-side foundation for cross-device encrypted backup sync and the online support portal. It is separate from the working Android 4.0 release branch while integration proceeds.

## Security design
- The cloud sync endpoint accepts an opaque backup archive produced by MoodPress. It verifies the SHA-256 checksum and uses version compare-and-swap; it never extracts or decrypts the archive.
- Support portal entries are AES-GCM ciphertext with a 12-byte IV. Encryption/decryption happens in the browser with Web Crypto. The server stores category ACL metadata, IV, ciphertext, timestamps and membership/account metadata, not the share key or plaintext note.
- Bearer sessions are random, stored only as SHA-256 hashes server-side, expire after 30 days and can be revoked.
- Support membership is invitation-based and role/category scoped. Support suggestions remain pending until the account owner accepts or rejects them.
- The portal uses same-origin calls, restrictive security headers, no localStorage for bearer tokens, and rate limits on authentication and writes.

## Run locally
Requires .NET 10 SDK. From this directory:
```sh
dotnet restore
ASPNETCORE_ENVIRONMENT=Development dotnet run --urls http://127.0.0.1:5080
```
Then open http://127.0.0.1:5080. The local SQLite database defaults to `data/moodpress-cloud.db`; set `MOODPRESS_DATABASE_PATH` to choose another path.

## Deployment requirements
Deploy behind HTTPS, configure a persistent writable disk for the SQLite database, back up that database, and set `MOODPRESS_DATABASE_PATH` to the persistent path. Use a single server instance until the database is moved to a shared transactional database. This version does not send e-mail, verify e-mail addresses, or provide password-reset e-mail; share invitation codes out-of-band. Those requirements must be completed before inviting real care teams or publishing a public portal.

## API summary
- `POST /api/v1/auth/register`, `POST /api/v1/auth/login`, `POST /api/v1/auth/logout`, `GET /api/v1/me`
- `GET /api/v1/sync`, `PUT /api/v1/sync` (owner only, opaque archive and expected version)
- `POST /api/v1/support/invitations`, `POST /api/v1/support/invitations/accept`
- `GET /api/v1/support/members`, `DELETE /api/v1/support/members/{membershipId}`
- `POST/GET/DELETE /api/v1/support/shares`
- `POST/GET /api/v1/support/suggestions`, `POST /api/v1/support/suggestions/{id}/decision`

The mobile/desktop client integration is a separate step. Do not treat this project alone as proof that automatic device sync has been enabled in the apps.


## Client integration status (2026-10-16)
Android cloud-sync client code is now included in the separate cloud integration patch branch and is being validated by the Android phone/tablet compile job. Do not install this branch as a release until both platform builds pass and the service has been deployed behind HTTPS.
