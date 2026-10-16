namespace MoodPress.CloudPortal;

public sealed record RegisterRequest(string Email, string DisplayName, string Password);
public sealed record LoginRequest(string Email, string Password);
public sealed record UserProfile(string Id, string Email, string DisplayName);
public sealed record SessionResponse(string AccessToken, DateTimeOffset ExpiresAt, UserProfile User);
public sealed record InvitationRequest(string Email, string Role, string[] Categories, int ExpiresInHours = 168);
public sealed record AcceptInvitationRequest(string Code);
public sealed record InvitationResponse(string Id, string Code, string Email, string Role, string[] Categories, DateTimeOffset ExpiresAt);
public sealed record MemberResponse(string Id, string Email, string DisplayName, string Role, string[] Categories, DateTimeOffset CreatedAt);
public sealed record SyncUploadRequest(long ExpectedVersion, string Sha256, string EncryptedArchiveBase64, string DeviceId);
public sealed record SyncSnapshotResponse(long Version, string Sha256, string EncryptedArchiveBase64, DateTimeOffset UpdatedAt, string DeviceId);
public sealed record ShareUploadRequest(string Category, string IvBase64, string CiphertextBase64);
public sealed record SharedItemResponse(string Id, string Category, string IvBase64, string CiphertextBase64, DateTimeOffset CreatedAt);
public sealed record SuggestionRequest(string Category, string IvBase64, string CiphertextBase64);
public sealed record SuggestionResponse(string Id, string Category, string IvBase64, string CiphertextBase64, string Status, DateTimeOffset CreatedAt, DateTimeOffset? DecidedAt);
public sealed record SuggestionDecisionRequest(string Decision);
