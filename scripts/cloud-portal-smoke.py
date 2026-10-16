#!/usr/bin/env python3
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("MOODPRESS_TEST_URL", "http://127.0.0.1:5080").rstrip("/")

def request(path, method="GET", token=None, workspace=None, body=None, expected=(200,)):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    if workspace:
        headers["X-MoodPress-Workspace"] = workspace
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            code, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        code, raw = error.code, error.read()
    if code not in expected:
        raise AssertionError(f"{method} {path}: expected {expected}, got {code}: {raw[:600]!r}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.decode("utf-8", "replace")

def wait_ready():
    for _ in range(40):
        try:
            if request("/health").get("status") == "ok":
                return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("Cloud portal did not become healthy")

def register(tag):
    email = f"{tag}-{secrets.token_hex(5)}@example.invalid"
    return email, request("/api/v1/auth/register", "POST", body={
        "email": email, "displayName": tag, "password": "MoodPress-Cloud-Test-Only-2026!"
    })

def main():
    wait_ready()
    owner_email, owner = register("owner")
    owner_token = owner["accessToken"]
    owner_workspace = request("/api/v1/me", token=owner_token)["workspaces"][0]["id"]

    # The server stores arbitrary opaque bytes; the API must not inspect or decrypt a vault backup.
    archive = b"opaque-encrypted-test-archive-" + secrets.token_bytes(64)
    archive64 = __import__("base64").b64encode(archive).decode()
    digest = hashlib.sha256(archive).hexdigest().upper()
    saved = request("/api/v1/sync", "PUT", owner_token, owner_workspace, {
        "expectedVersion": 0, "sha256": digest, "vaultId": "a" * 64, "encryptedArchiveBase64": archive64, "deviceId": "ci-phone"
    })
    assert saved["version"] == 1
    current = request("/api/v1/sync", token=owner_token, workspace=owner_workspace)
    assert current["version"] == 1 and current["sha256"].lower() == digest.lower()
    request("/api/v1/sync", "PUT", owner_token, owner_workspace, {
        "expectedVersion": 0, "sha256": digest, "vaultId": "a" * 64, "encryptedArchiveBase64": archive64, "deviceId": "stale-tablet"
    }, expected=(409,))

    support_email = f"support-{secrets.token_hex(5)}@example.invalid"
    invite = request("/api/v1/support/invitations", "POST", owner_token, owner_workspace, {
        "email": support_email, "role": "begeleider", "categories": ["agenda", "helpkaart"], "expiresInHours": 24
    })
    _, support = register("support")
    support_token = support["accessToken"]
    # The smoke suite substitutes the invited address only through the request's primary identity, so create the invited account directly with that email.
    request("/api/v1/auth/logout", "POST", token=support_token)
    # The invitation is deliberately bound to its address; create a fresh invited account with that exact address.
    invited = request("/api/v1/auth/register", "POST", body={
        "email": support_email, "displayName": "Invited supporter", "password": "MoodPress-Cloud-Test-Only-2026!"
    })
    invited_token = invited["accessToken"]
    request("/api/v1/support/invitations/accept", "POST", invited_token, owner_workspace, {"code": invite["code"]})
    own_key = {"Authorization": "Bearer " + owner_token, "X-MoodPress-Workspace": owner_workspace}

    def post_encrypted_share(category, salt):
        iv = __import__("base64").b64encode(secrets.token_bytes(12)).decode()
        cipher = __import__("base64").b64encode(secrets.token_bytes(24)).decode()
        request("/api/v1/support/shares", "POST", owner_token, owner_workspace, {
            "category": category, "ivBase64": iv, "ciphertextBase64": cipher
        })

    post_encrypted_share("gevoel", "a")
    post_encrypted_share("agenda", "b")
    visible = request("/api/v1/support/shares", token=invited_token, workspace=owner_workspace)
    assert len(visible) == 1 and visible[0]["category"] == "agenda", visible

    # A supporter can submit an encrypted proposal only for a category allowed by their role.
    suggestion_iv = __import__("base64").b64encode(secrets.token_bytes(12)).decode()
    suggestion_cipher = __import__("base64").b64encode(secrets.token_bytes(24)).decode()
    suggestion = request("/api/v1/support/suggestions", "POST", invited_token, owner_workspace, {
        "category": "agenda", "ivBase64": suggestion_iv, "ciphertextBase64": suggestion_cipher
    })
    listed = request("/api/v1/support/suggestions", token=owner_token, workspace=owner_workspace)
    assert any(item["id"] == suggestion["id"] and item["status"] == "pending" for item in listed)
    request(f"/api/v1/support/suggestions/{suggestion['id']}/decision", "POST", owner_token, owner_workspace, {"decision": "accepted"})
    members = request("/api/v1/support/members", token=owner_token, workspace=owner_workspace)
    assert any(item["email"] == support_email for item in members)
    invited_membership = next(item for item in members if item["email"] == support_email)
    request(f"/api/v1/support/members/{invited_membership['id']}", "DELETE", owner_token, owner_workspace)
    request("/api/v1/support/shares", token=invited_token, workspace=owner_workspace, expected=(404,))
    request("/api/v1/auth/logout", "POST", token=owner_token)
    request("/api/v1/me", token=owner_token, expected=(401,))
    print("Cloud portal smoke tests passed: accounts, version conflicts, role ACL, encrypted shares, suggestions, revocation and logout.")

if __name__ == "__main__":
    main()
