using System.Collections.Concurrent;
using System.Net.Mail;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Threading.RateLimiting;
using Microsoft.AspNetCore.RateLimiting;
using Microsoft.Data.Sqlite;

namespace MoodPress.CloudPortal;

public static class Program
{
    private const int PasswordIterations = 600_000;
    private const int SessionDays = 30;
    private const int MaxArchiveBytes = 100 * 1024 * 1024;
    private const int MaxShareBytes = 1 * 1024 * 1024;
    private static readonly string[] ShareCategories =
        ["gevoel", "agenda", "routine", "welzijn", "helpkaart", "weekrooster", "communicatie"];

    public static async Task Main(string[] args)
    {
        var builder = WebApplication.CreateBuilder(args);
        builder.WebHost.ConfigureKestrel(options => options.Limits.MaxRequestBodySize = 140L * 1024 * 1024);
        builder.Services.AddSingleton<CloudDatabase>();
        builder.Services.AddRateLimiter(options =>
        {
            options.RejectionStatusCode = StatusCodes.Status429TooManyRequests;
            options.AddPolicy("auth", context => RateLimitPartition.GetFixedWindowLimiter(
                context.Connection.RemoteIpAddress?.ToString() ?? "unknown",
                _ => new FixedWindowRateLimiterOptions { PermitLimit = 10, Window = TimeSpan.FromMinutes(1), QueueLimit = 0, AutoReplenishment = true }));
            options.AddPolicy("write", context => RateLimitPartition.GetFixedWindowLimiter(
                context.Connection.RemoteIpAddress?.ToString() ?? "unknown",
                _ => new FixedWindowRateLimiterOptions { PermitLimit = 60, Window = TimeSpan.FromMinutes(1), QueueLimit = 0, AutoReplenishment = true }));
        });

        var app = builder.Build();
        await app.Services.GetRequiredService<CloudDatabase>().InitializeAsync();

        app.Use(async (context, next) =>
        {
            context.Response.Headers.CacheControl = "no-store";
            context.Response.Headers.Pragma = "no-cache";
            context.Response.Headers["X-Content-Type-Options"] = "nosniff";
            context.Response.Headers["X-Frame-Options"] = "DENY";
            context.Response.Headers["Referrer-Policy"] = "no-referrer";
            context.Response.Headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()";
            context.Response.Headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'";
            if (!app.Environment.IsDevelopment()) context.Response.Headers.StrictTransportSecurity = "max-age=31536000; includeSubDomains";
            await next();
        });

        app.UseRateLimiter();
        app.UseDefaultFiles();
        app.UseStaticFiles();

        app.MapGet("/health", () => Results.Ok(new { status = "ok", service = "MoodPress Cloud Portal", utc = DateTimeOffset.UtcNow }));

        app.MapPost("/api/v1/auth/register", RegisterAsync).RequireRateLimiting("auth");
        app.MapPost("/api/v1/auth/login", LoginAsync).RequireRateLimiting("auth");
        app.MapPost("/api/v1/auth/logout", LogoutAsync).RequireRateLimiting("write");
        app.MapGet("/api/v1/me", MeAsync).RequireRateLimiting("write");

        app.MapGet("/api/v1/sync", GetSnapshotAsync).RequireRateLimiting("write");
        app.MapPut("/api/v1/sync", PutSnapshotAsync).RequireRateLimiting("write");

        app.MapPost("/api/v1/support/invitations", CreateInvitationAsync).RequireRateLimiting("write");
        app.MapPost("/api/v1/support/invitations/accept", AcceptInvitationAsync).RequireRateLimiting("write");
        app.MapGet("/api/v1/support/members", ListMembersAsync).RequireRateLimiting("write");
        app.MapDelete("/api/v1/support/members/{membershipId}", RevokeMemberAsync).RequireRateLimiting("write");

        app.MapPost("/api/v1/support/shares", CreateShareAsync).RequireRateLimiting("write");
        app.MapGet("/api/v1/support/shares", ListSharesAsync).RequireRateLimiting("write");
        app.MapDelete("/api/v1/support/shares/{shareId}", RevokeShareAsync).RequireRateLimiting("write");

        app.MapPost("/api/v1/support/suggestions", CreateSuggestionAsync).RequireRateLimiting("write");
        app.MapGet("/api/v1/support/suggestions", ListSuggestionsAsync).RequireRateLimiting("write");
        app.MapPost("/api/v1/support/suggestions/{suggestionId}/decision", DecideSuggestionAsync).RequireRateLimiting("write");

        await app.RunAsync();
    }

    private static async Task<IResult> RegisterAsync(RegisterRequest request, HttpContext context, CloudDatabase database)
    {
        var email = NormalizeEmail(request.Email);
        if (email is null) return Results.BadRequest(new { error = "Vul een geldig e-mailadres in." });
        if (string.IsNullOrWhiteSpace(request.DisplayName) || request.DisplayName.Trim().Length > 100)
            return Results.BadRequest(new { error = "Vul een naam van maximaal 100 tekens in." });
        if (string.IsNullOrEmpty(request.Password) || request.Password.Length < 12 || request.Password.Length > 256)
            return Results.BadRequest(new { error = "Gebruik een wachtwoord van minimaal 12 tekens." });

        var salt = RandomNumberGenerator.GetBytes(16);
        var hash = HashPassword(request.Password, salt);
        var userId = Guid.NewGuid().ToString("N");
        var workspaceId = Guid.NewGuid().ToString("N");
        var now = DateTimeOffset.UtcNow.ToString("O");
        try
        {
            await using var connection = await database.OpenAsync(context.RequestAborted);
            using var transaction = connection.BeginTransaction();
            await using (var command = connection.CreateCommand())
            {
                command.Transaction = transaction;
                command.CommandText = "INSERT INTO users(id,email,display_name,password_salt,password_hash,created_at) VALUES($id,$email,$name,$salt,$hash,$now);";
                command.Parameters.AddWithValue("$id", userId);
                command.Parameters.AddWithValue("$email", email);
                command.Parameters.AddWithValue("$name", request.DisplayName.Trim());
                command.Parameters.AddWithValue("$salt", Convert.ToBase64String(salt));
                command.Parameters.AddWithValue("$hash", Convert.ToBase64String(hash));
                command.Parameters.AddWithValue("$now", now);
                await command.ExecuteNonQueryAsync(context.RequestAborted);
            }
            await using (var command = connection.CreateCommand())
            {
                command.Transaction = transaction;
                command.CommandText = "INSERT INTO workspaces(id,owner_user_id,name,created_at) VALUES($id,$owner,$name,$now);";
                command.Parameters.AddWithValue("$id", workspaceId);
                command.Parameters.AddWithValue("$owner", userId);
                command.Parameters.AddWithValue("$name", "Mijn MoodPress");
                command.Parameters.AddWithValue("$now", now);
                await command.ExecuteNonQueryAsync(context.RequestAborted);
            }
            await using (var command = connection.CreateCommand())
            {
                command.Transaction = transaction;
                command.CommandText = "INSERT INTO memberships(id,workspace_id,user_id,role,permissions_json,created_at,active) VALUES($id,$workspace,$user,'owner',$permissions,$now,1);";
                command.Parameters.AddWithValue("$id", Guid.NewGuid().ToString("N"));
                command.Parameters.AddWithValue("$workspace", workspaceId);
                command.Parameters.AddWithValue("$user", userId);
                command.Parameters.AddWithValue("$permissions", JsonSerializer.Serialize(ShareCategories));
                command.Parameters.AddWithValue("$now", now);
                await command.ExecuteNonQueryAsync(context.RequestAborted);
            }
            transaction.Commit();
            transaction.Dispose();
            var profile = new UserProfile(userId, email, request.DisplayName.Trim());
            return Results.Ok(await CreateSessionAsync(connection, profile, context.RequestAborted));
        }
        catch (SqliteException ex) when (ex.SqliteErrorCode == 19)
        {
            return Results.Conflict(new { error = "Er bestaat al een account met dit e-mailadres." });
        }
        finally
        {
            CryptographicOperations.ZeroMemory(salt);
            CryptographicOperations.ZeroMemory(hash);
        }
    }

    private static async Task<IResult> LoginAsync(LoginRequest request, HttpContext context, CloudDatabase database)
    {
        var email = NormalizeEmail(request.Email);
        if (email is null || string.IsNullOrEmpty(request.Password) || request.Password.Length > 256)
            return Results.Unauthorized();
        await using var connection = await database.OpenAsync(context.RequestAborted);
        UserProfile? profile = null;
        byte[]? salt = null;
        byte[]? expectedHash = null;
        await using (var command = connection.CreateCommand())
        {
            command.CommandText = "SELECT id,email,display_name,password_salt,password_hash FROM users WHERE email=$email;";
            command.Parameters.AddWithValue("$email", email);
            await using var reader = await command.ExecuteReaderAsync(context.RequestAborted);
            if (await reader.ReadAsync(context.RequestAborted))
            {
                profile = new UserProfile(reader.GetString(0), reader.GetString(1), reader.GetString(2));
                salt = Convert.FromBase64String(reader.GetString(3));
                expectedHash = Convert.FromBase64String(reader.GetString(4));
            }
        }

        // Run the same KDF for unknown accounts, limiting account-enumeration timing differences.
        salt ??= Encoding.UTF8.GetBytes("MoodPress-cloud-dummy-salt");
        var actualHash = HashPassword(request.Password, salt);
        try
        {
            if (profile is null || expectedHash is null || !CryptographicOperations.FixedTimeEquals(actualHash, expectedHash))
                return Results.Unauthorized();
            return Results.Ok(await CreateSessionAsync(connection, profile, context.RequestAborted));
        }
        finally
        {
            CryptographicOperations.ZeroMemory(salt);
            CryptographicOperations.ZeroMemory(actualHash);
            if (expectedHash is not null) CryptographicOperations.ZeroMemory(expectedHash);
        }
    }

    private static async Task<IResult> LogoutAsync(HttpContext context, CloudDatabase database)
    {
        var tokenHash = GetBearerHash(context);
        if (tokenHash is null) return Results.Unauthorized();
        await using var connection = await database.OpenAsync(context.RequestAborted);
        await using var command = connection.CreateCommand();
        command.CommandText = "UPDATE sessions SET revoked=1 WHERE token_hash=$hash;";
        command.Parameters.AddWithValue("$hash", tokenHash);
        await command.ExecuteNonQueryAsync(context.RequestAborted);
        return Results.Ok(new { loggedOut = true });
    }

    private static async Task<IResult> MeAsync(HttpContext context, CloudDatabase database)
    {
        var userId = await RequireUserIdAsync(context, database);
        if (userId is null) return Results.Unauthorized();
        await using var connection = await database.OpenAsync(context.RequestAborted);
        UserProfile? profile = null;
        await using (var command = connection.CreateCommand())
        {
            command.CommandText = "SELECT id,email,display_name FROM users WHERE id=$id;";
            command.Parameters.AddWithValue("$id", userId);
            await using var reader = await command.ExecuteReaderAsync(context.RequestAborted);
            if (await reader.ReadAsync(context.RequestAborted))
                profile = new UserProfile(reader.GetString(0), reader.GetString(1), reader.GetString(2));
        }
        if (profile is null) return Results.Unauthorized();

        var workspaces = new List<object>();
        await using (var command = connection.CreateCommand())
        {
            command.CommandText = """
                SELECT w.id,w.name,m.role,m.permissions_json,m.id
                FROM workspaces w JOIN memberships m ON m.workspace_id=w.id
                WHERE m.user_id=$user AND m.active=1 ORDER BY w.created_at;
                """;
            command.Parameters.AddWithValue("$user", userId);
            await using var reader = await command.ExecuteReaderAsync(context.RequestAborted);
            while (await reader.ReadAsync(context.RequestAborted))
                workspaces.Add(new { id = reader.GetString(0), name = reader.GetString(1), role = reader.GetString(2),
                    categories = JsonSerializer.Deserialize<string[]>(reader.GetString(3)) ?? Array.Empty<string>(),
                    membershipId = reader.GetString(4) });
        }
        return Results.Ok(new { user = profile, workspaces });
    }

    private static async Task<IResult> GetSnapshotAsync(HttpContext context, CloudDatabase database)
    {
        var userId = await RequireUserIdAsync(context, database);
        if (userId is null) return Results.Unauthorized();
        await using var connection = await database.OpenAsync(context.RequestAborted);
        var access = await GetWorkspaceAccessAsync(connection, context, userId, context.RequestAborted);
        if (access is null) return Results.NotFound(new { error = "Er is geen gekoppelde werkruimte gevonden." });
        if (!access.IsOwner) return Results.Forbid();

        await using var command = connection.CreateCommand();
        command.CommandText = "SELECT version,sha256,vault_id,encrypted_archive,updated_at,device_id FROM sync_snapshots WHERE workspace_id=$workspace;";
        command.Parameters.AddWithValue("$workspace", access.WorkspaceId);
        await using var reader = await command.ExecuteReaderAsync(context.RequestAborted);
        if (!await reader.ReadAsync(context.RequestAborted)) return Results.NotFound(new { error = "Er staat nog geen cloudsnapshot." });
        return Results.Ok(new SyncSnapshotResponse(reader.GetInt64(0), reader.GetString(1), reader.GetString(2),
            Convert.ToBase64String((byte[])reader[3]), DateTimeOffset.Parse(reader.GetString(4)), reader.GetString(5)));
    }

    private static async Task<IResult> PutSnapshotAsync(SyncUploadRequest request, HttpContext context, CloudDatabase database)
    {
        var userId = await RequireUserIdAsync(context, database);
        if (userId is null) return Results.Unauthorized();
        if (request.ExpectedVersion < 0 || string.IsNullOrWhiteSpace(request.DeviceId) || request.DeviceId.Length > 128)
            return Results.BadRequest(new { error = "De synchronisatiegegevens zijn ongeldig." });
        if (request.VaultId is null || request.VaultId.Length != 64 || request.VaultId.Any(c => !Uri.IsHexDigit(c)))
            return Results.BadRequest(new { error = "De lokale kluis-ID is ongeldig." });
        if (request.EncryptedArchiveBase64 is null || request.EncryptedArchiveBase64.Length > ((MaxArchiveBytes + 2) / 3) * 4)
            return Results.BadRequest(new { error = "De versleutelde back-up is te groot (maximum 100 MB)." });
        byte[] archive;
        try { archive = Convert.FromBase64String(request.EncryptedArchiveBase64); }
        catch (FormatException) { return Results.BadRequest(new { error = "Het back-upbestand is ongeldig gecodeerd." }); }
        if (archive.Length == 0 || archive.Length > MaxArchiveBytes)
        {
            CryptographicOperations.ZeroMemory(archive);
            return Results.BadRequest(new { error = "De versleutelde back-up is leeg of groter dan 100 MB." });
        }

        var actualSha = Convert.ToHexString(SHA256.HashData(archive));
        if (!string.Equals(actualSha, request.Sha256, StringComparison.OrdinalIgnoreCase))
        {
            CryptographicOperations.ZeroMemory(archive);
            return Results.BadRequest(new { error = "De controlesom komt niet overeen; er is niets opgeslagen." });
        }

        try
        {
            await using var connection = await database.OpenAsync(context.RequestAborted);
            var access = await GetWorkspaceAccessAsync(connection, context, userId, context.RequestAborted);
            if (access is null) return Results.NotFound(new { error = "Er is geen gekoppelde werkruimte gevonden." });
            if (!access.IsOwner) return Results.Forbid();

            using var transaction = connection.BeginTransaction();
            long? currentVersion = null;
            string? currentVaultId = null;
            await using (var command = connection.CreateCommand())
            {
                command.Transaction = transaction;
                command.CommandText = "SELECT version,vault_id FROM sync_snapshots WHERE workspace_id=$workspace;";
                command.Parameters.AddWithValue("$workspace", access.WorkspaceId);
                var value = await command.ExecuteScalarAsync(context.RequestAborted);
                if (value is not null && value is not DBNull)
                {
                    currentVersion = Convert.ToInt64(value);
                    await using var vaultCommand = connection.CreateCommand();
                    vaultCommand.Transaction = transaction;
                    vaultCommand.CommandText = "SELECT vault_id FROM sync_snapshots WHERE workspace_id=$workspace;";
                    vaultCommand.Parameters.AddWithValue("$workspace", access.WorkspaceId);
                    currentVaultId = (string?)await vaultCommand.ExecuteScalarAsync(context.RequestAborted);
                }
            }

            if (currentVersion is not null && !string.Equals(currentVaultId, request.VaultId, StringComparison.OrdinalIgnoreCase))
            {
                transaction.Rollback();
                return Results.Json(new { error = "Deze cloudwerkruimte is al gekoppeld aan een andere lokale MoodPress-kluis. Er is niets overschreven." }, statusCode: StatusCodes.Status409Conflict);
            }
            if ((currentVersion ?? 0) != request.ExpectedVersion || (currentVersion is null && request.ExpectedVersion != 0))
            {
                transaction.Rollback();
                return Results.Conflict(new { error = "Er is intussen een andere synchronisatie opgeslagen. Er is niets overschreven.", currentVersion = currentVersion ?? 0 });
            }

            var nextVersion = (currentVersion ?? 0) + 1;
            if (currentVersion is null)
            {
                await using var command = connection.CreateCommand();
                command.Transaction = transaction;
                command.CommandText = "INSERT INTO sync_snapshots(workspace_id,version,sha256,vault_id,encrypted_archive,updated_at,updated_by,device_id) VALUES($workspace,$version,$sha,$vault,$archive,$now,$user,$device);";
                command.Parameters.AddWithValue("$workspace", access.WorkspaceId);
                command.Parameters.AddWithValue("$version", nextVersion);
                command.Parameters.AddWithValue("$sha", actualSha);
                command.Parameters.AddWithValue("$vault", request.VaultId.ToUpperInvariant());
                command.Parameters.AddWithValue("$archive", archive);
                command.Parameters.AddWithValue("$now", DateTimeOffset.UtcNow.ToString("O"));
                command.Parameters.AddWithValue("$user", userId);
                command.Parameters.AddWithValue("$device", request.DeviceId.Trim());
                await command.ExecuteNonQueryAsync(context.RequestAborted);
            }
            else
            {
                await using var command = connection.CreateCommand();
                command.Transaction = transaction;
                command.CommandText = """
                    UPDATE sync_snapshots SET version=$version,sha256=$sha,vault_id=$vault,encrypted_archive=$archive,updated_at=$now,updated_by=$user,device_id=$device
                    WHERE workspace_id=$workspace AND version=$expected;
                    """;
                command.Parameters.AddWithValue("$workspace", access.WorkspaceId);
                command.Parameters.AddWithValue("$version", nextVersion);
                command.Parameters.AddWithValue("$sha", actualSha);
                command.Parameters.AddWithValue("$archive", archive);
                command.Parameters.AddWithValue("$now", DateTimeOffset.UtcNow.ToString("O"));
                command.Parameters.AddWithValue("$user", userId);
                command.Parameters.AddWithValue("$device", request.DeviceId.Trim());
                command.Parameters.AddWithValue("$expected", request.ExpectedVersion);
                if (await command.ExecuteNonQueryAsync(context.RequestAborted) != 1)
                {
                    transaction.Rollback();
                    return Results.Conflict(new { error = "Er is intussen een andere synchronisatie opgeslagen. Er is niets overschreven." });
                }
            }

            transaction.Commit();
            return Results.Ok(new { version = nextVersion, sha256 = actualSha, updatedAt = DateTimeOffset.UtcNow });
        }
        finally { CryptographicOperations.ZeroMemory(archive); }
    }

    private static async Task<IResult> CreateInvitationAsync(InvitationRequest request, HttpContext context, CloudDatabase database)
    {
        var userId = await RequireUserIdAsync(context, database);
        if (userId is null) return Results.Unauthorized();
        var email = NormalizeEmail(request.Email);
        var role = (request.Role ?? "").Trim().ToLowerInvariant();
        if (email is null) return Results.BadRequest(new { error = "Vul een geldig e-mailadres in." });
        if (role is not ("begeleider" or "ouder_voogd"))
            return Results.BadRequest(new { error = "Kies de rol begeleider of ouder/voogd." });
        if (request.ExpiresInHours is < 1 or > 168)
            return Results.BadRequest(new { error = "Een uitnodiging mag maximaal 7 dagen geldig zijn." });
        var categories = (request.Categories ?? Array.Empty<string>()).Distinct(StringComparer.Ordinal).ToArray();
        if (categories.Length == 0 || categories.Length > ShareCategories.Length || categories.Any(c => !ShareCategories.Contains(c, StringComparer.Ordinal)))
            return Results.BadRequest(new { error = "Kies minimaal één geldige gegevenscategorie." });

        await using var connection = await database.OpenAsync(context.RequestAborted);
        var access = await GetWorkspaceAccessAsync(connection, context, userId, context.RequestAborted);
        if (access is null) return Results.NotFound(new { error = "Er is geen werkruimte gevonden." });
        if (!access.IsOwner) return Results.Forbid();

        var code = Base64Url(RandomNumberGenerator.GetBytes(32));
        var codeHash = Sha256Hex(Encoding.UTF8.GetBytes(code));
        var id = Guid.NewGuid().ToString("N");
        var now = DateTimeOffset.UtcNow;
        var expires = now.AddHours(request.ExpiresInHours);
        await using var command = connection.CreateCommand();
        command.CommandText = "INSERT INTO invitations(id,workspace_id,email,role,permissions_json,code_hash,created_at,expires_at) VALUES($id,$workspace,$email,$role,$permissions,$hash,$now,$expires);";
        command.Parameters.AddWithValue("$id", id);
        command.Parameters.AddWithValue("$workspace", access.WorkspaceId);
        command.Parameters.AddWithValue("$email", email);
        command.Parameters.AddWithValue("$role", role);
        command.Parameters.AddWithValue("$permissions", JsonSerializer.Serialize(categories));
        command.Parameters.AddWithValue("$hash", codeHash);
        command.Parameters.AddWithValue("$now", now.ToString("O"));
        command.Parameters.AddWithValue("$expires", expires.ToString("O"));
        await command.ExecuteNonQueryAsync(context.RequestAborted);
        return Results.Ok(new InvitationResponse(id, code, email, role, categories, expires));
    }

    private static async Task<IResult> AcceptInvitationAsync(AcceptInvitationRequest request, HttpContext context, CloudDatabase database)
    {
        var userId = await RequireUserIdAsync(context, database);
        if (userId is null) return Results.Unauthorized();
        if (string.IsNullOrWhiteSpace(request.Code) || request.Code.Length > 128)
            return Results.BadRequest(new { error = "De uitnodigingscode is ongeldig." });

        await using var connection = await database.OpenAsync(context.RequestAborted);
        string email;
        await using (var command = connection.CreateCommand())
        {
            command.CommandText = "SELECT email FROM users WHERE id=$id;";
            command.Parameters.AddWithValue("$id", userId);
            email = (string?)await command.ExecuteScalarAsync(context.RequestAborted) ?? "";
        }
        var codeHash = Sha256Hex(Encoding.UTF8.GetBytes(request.Code.Trim()));
        string invitationId, workspaceId, role, permissionsJson, inviteEmail, expiresAt;
        await using (var command = connection.CreateCommand())
        {
            command.CommandText = "SELECT id,workspace_id,role,permissions_json,email,expires_at FROM invitations WHERE code_hash=$hash AND revoked=0 AND used_by IS NULL;";
            command.Parameters.AddWithValue("$hash", codeHash);
            await using var reader = await command.ExecuteReaderAsync(context.RequestAborted);
            if (!await reader.ReadAsync(context.RequestAborted)) return Results.BadRequest(new { error = "De uitnodiging is ongeldig, al gebruikt of ingetrokken." });
            invitationId = reader.GetString(0);
            workspaceId = reader.GetString(1);
            role = reader.GetString(2);
            permissionsJson = reader.GetString(3);
            inviteEmail = reader.GetString(4);
            expiresAt = reader.GetString(5);
        }
        if (!string.Equals(email, inviteEmail, StringComparison.OrdinalIgnoreCase))
            return Results.Forbid();
        if (!DateTimeOffset.TryParse(expiresAt, out var expiry) || expiry <= DateTimeOffset.UtcNow)
            return Results.BadRequest(new { error = "Deze uitnodiging is verlopen." });

        using var transaction = connection.BeginTransaction();
        await using (var command = connection.CreateCommand())
        {
            command.Transaction = transaction;
            command.CommandText = "INSERT INTO memberships(id,workspace_id,user_id,role,permissions_json,created_at,active) VALUES($id,$workspace,$user,$role,$permissions,$now,1) ON CONFLICT(workspace_id,user_id) DO UPDATE SET role=excluded.role,permissions_json=excluded.permissions_json,created_at=excluded.created_at,active=1;";
            command.Parameters.AddWithValue("$id", Guid.NewGuid().ToString("N"));
            command.Parameters.AddWithValue("$workspace", workspaceId);
            command.Parameters.AddWithValue("$user", userId);
            command.Parameters.AddWithValue("$role", role);
            command.Parameters.AddWithValue("$permissions", permissionsJson);
            command.Parameters.AddWithValue("$now", DateTimeOffset.UtcNow.ToString("O"));
            await command.ExecuteNonQueryAsync(context.RequestAborted);
        }
        await using (var command = connection.CreateCommand())
        {
            command.Transaction = transaction;
            command.CommandText = "UPDATE invitations SET used_by=$user WHERE id=$id AND used_by IS NULL AND revoked=0;";
            command.Parameters.AddWithValue("$user", userId);
            command.Parameters.AddWithValue("$id", invitationId);
            if (await command.ExecuteNonQueryAsync(context.RequestAborted) != 1)
            {
                transaction.Rollback();
                return Results.Conflict(new { error = "De uitnodiging is intussen gebruikt." });
            }
        }
        transaction.Commit();
        return Results.Ok(new { accepted = true, workspaceId, role });
    }

    private static async Task<IResult> ListMembersAsync(HttpContext context, CloudDatabase database)
    {
        var userId = await RequireUserIdAsync(context, database);
        if (userId is null) return Results.Unauthorized();
        await using var connection = await database.OpenAsync(context.RequestAborted);
        var access = await GetWorkspaceAccessAsync(connection, context, userId, context.RequestAborted);
        if (access is null) return Results.NotFound();
        if (!access.IsOwner) return Results.Forbid();

        var members = new List<MemberResponse>();
        await using var command = connection.CreateCommand();
        command.CommandText = """
            SELECT m.id,u.email,u.display_name,m.role,m.permissions_json,m.created_at
            FROM memberships m JOIN users u ON u.id=m.user_id
            WHERE m.workspace_id=$workspace AND m.active=1 AND m.role<>'owner'
            ORDER BY m.created_at;
            """;
        command.Parameters.AddWithValue("$workspace", access.WorkspaceId);
        await using var reader = await command.ExecuteReaderAsync(context.RequestAborted);
        while (await reader.ReadAsync(context.RequestAborted))
            members.Add(new MemberResponse(reader.GetString(0), reader.GetString(1), reader.GetString(2),
                reader.GetString(3), JsonSerializer.Deserialize<string[]>(reader.GetString(4)) ?? Array.Empty<string>(),
                DateTimeOffset.Parse(reader.GetString(5))));
        return Results.Ok(members);
    }

    private static async Task<IResult> RevokeMemberAsync(string membershipId, HttpContext context, CloudDatabase database)
    {
        var userId = await RequireUserIdAsync(context, database);
        if (userId is null) return Results.Unauthorized();
        await using var connection = await database.OpenAsync(context.RequestAborted);
        var access = await GetWorkspaceAccessAsync(connection, context, userId, context.RequestAborted);
        if (access is null) return Results.NotFound();
        if (!access.IsOwner) return Results.Forbid();
        await using var command = connection.CreateCommand();
        command.CommandText = "UPDATE memberships SET active=0 WHERE id=$id AND workspace_id=$workspace AND role<>'owner' AND active=1;";
        command.Parameters.AddWithValue("$id", membershipId);
        command.Parameters.AddWithValue("$workspace", access.WorkspaceId);
        if (await command.ExecuteNonQueryAsync(context.RequestAborted) != 1) return Results.NotFound(new { error = "Lid niet gevonden." });
        return Results.Ok(new { revoked = true });
    }

    private static async Task<IResult> CreateShareAsync(ShareUploadRequest request, HttpContext context, CloudDatabase database)
    {
        var userId = await RequireUserIdAsync(context, database);
        if (userId is null) return Results.Unauthorized();
        var parsed = ParseCipher(request.IvBase64, request.CiphertextBase64, MaxShareBytes);
        if (parsed.Error is not null) return Results.BadRequest(new { error = parsed.Error });
        if (!ShareCategories.Contains(request.Category, StringComparer.Ordinal)) return Results.BadRequest(new { error = "Onbekende gegevenscategorie." });

        await using var connection = await database.OpenAsync(context.RequestAborted);
        var access = await GetWorkspaceAccessAsync(connection, context, userId, context.RequestAborted);
        if (access is null) return Results.NotFound();
        if (!access.IsOwner) return Results.Forbid();
        var id = Guid.NewGuid().ToString("N");
        var now = DateTimeOffset.UtcNow.ToString("O");
        await using var command = connection.CreateCommand();
        command.CommandText = "INSERT INTO shared_items(id,workspace_id,category,iv,ciphertext,created_at,created_by,revoked) VALUES($id,$workspace,$category,$iv,$cipher,$now,$user,0);";
        command.Parameters.AddWithValue("$id", id);
        command.Parameters.AddWithValue("$workspace", access.WorkspaceId);
        command.Parameters.AddWithValue("$category", request.Category);
        command.Parameters.AddWithValue("$iv", parsed.Iv!);
        command.Parameters.AddWithValue("$cipher", parsed.Ciphertext!);
        command.Parameters.AddWithValue("$now", now);
        command.Parameters.AddWithValue("$user", userId);
        await command.ExecuteNonQueryAsync(context.RequestAborted);
        CryptographicOperations.ZeroMemory(parsed.Iv!);
        CryptographicOperations.ZeroMemory(parsed.Ciphertext!);
        return Results.Ok(new { id, category = request.Category, createdAt = now });
    }

    private static async Task<IResult> ListSharesAsync(HttpContext context, CloudDatabase database)
    {
        var userId = await RequireUserIdAsync(context, database);
        if (userId is null) return Results.Unauthorized();
        await using var connection = await database.OpenAsync(context.RequestAborted);
        var access = await GetWorkspaceAccessAsync(connection, context, userId, context.RequestAborted);
        if (access is null) return Results.NotFound();
        var items = new List<SharedItemResponse>();
        await using var command = connection.CreateCommand();
        command.CommandText = "SELECT id,category,iv,ciphertext,created_at FROM shared_items WHERE workspace_id=$workspace AND revoked=0 ORDER BY created_at DESC LIMIT 500;";
        command.Parameters.AddWithValue("$workspace", access.WorkspaceId);
        await using var reader = await command.ExecuteReaderAsync(context.RequestAborted);
        while (await reader.ReadAsync(context.RequestAborted))
        {
            var category = reader.GetString(1);
            if (!access.IsOwner && !access.Categories.Contains(category, StringComparer.Ordinal)) continue;
            items.Add(new SharedItemResponse(reader.GetString(0), category, Convert.ToBase64String((byte[])reader[2]),
                Convert.ToBase64String((byte[])reader[3]), DateTimeOffset.Parse(reader.GetString(4))));
        }
        return Results.Ok(items);
    }

    private static async Task<IResult> RevokeShareAsync(string shareId, HttpContext context, CloudDatabase database)
    {
        var userId = await RequireUserIdAsync(context, database);
        if (userId is null) return Results.Unauthorized();
        await using var connection = await database.OpenAsync(context.RequestAborted);
        var access = await GetWorkspaceAccessAsync(connection, context, userId, context.RequestAborted);
        if (access is null) return Results.NotFound();
        if (!access.IsOwner) return Results.Forbid();
        await using var command = connection.CreateCommand();
        command.CommandText = "UPDATE shared_items SET revoked=1 WHERE id=$id AND workspace_id=$workspace AND revoked=0;";
        command.Parameters.AddWithValue("$id", shareId);
        command.Parameters.AddWithValue("$workspace", access.WorkspaceId);
        if (await command.ExecuteNonQueryAsync(context.RequestAborted) != 1) return Results.NotFound();
        return Results.Ok(new { revoked = true });
    }

    private static async Task<IResult> CreateSuggestionAsync(SuggestionRequest request, HttpContext context, CloudDatabase database)
    {
        var userId = await RequireUserIdAsync(context, database);
        if (userId is null) return Results.Unauthorized();
        var parsed = ParseCipher(request.IvBase64, request.CiphertextBase64, MaxShareBytes);
        if (parsed.Error is not null) return Results.BadRequest(new { error = parsed.Error });
        if (!ShareCategories.Contains(request.Category, StringComparer.Ordinal)) return Results.BadRequest(new { error = "Onbekende gegevenscategorie." });

        await using var connection = await database.OpenAsync(context.RequestAborted);
        var access = await GetWorkspaceAccessAsync(connection, context, userId, context.RequestAborted);
        if (access is null) return Results.NotFound();
        if (access.IsOwner) return Results.BadRequest(new { error = "Een eigenaar kan geen ondersteunersvoorstel bij zichzelf indienen." });
        if (!access.Categories.Contains(request.Category, StringComparer.Ordinal)) return Results.Forbid();

        var id = Guid.NewGuid().ToString("N");
        var now = DateTimeOffset.UtcNow.ToString("O");
        await using var command = connection.CreateCommand();
        command.CommandText = "INSERT INTO support_suggestions(id,workspace_id,member_id,category,iv,ciphertext,status,created_at) VALUES($id,$workspace,$member,$category,$iv,$cipher,'pending',$now);";
        command.Parameters.AddWithValue("$id", id);
        command.Parameters.AddWithValue("$workspace", access.WorkspaceId);
        command.Parameters.AddWithValue("$member", access.MembershipId);
        command.Parameters.AddWithValue("$category", request.Category);
        command.Parameters.AddWithValue("$iv", parsed.Iv!);
        command.Parameters.AddWithValue("$cipher", parsed.Ciphertext!);
        command.Parameters.AddWithValue("$now", now);
        await command.ExecuteNonQueryAsync(context.RequestAborted);
        CryptographicOperations.ZeroMemory(parsed.Iv!);
        CryptographicOperations.ZeroMemory(parsed.Ciphertext!);
        return Results.Ok(new { id, status = "pending", createdAt = now });
    }

    private static async Task<IResult> ListSuggestionsAsync(HttpContext context, CloudDatabase database)
    {
        var userId = await RequireUserIdAsync(context, database);
        if (userId is null) return Results.Unauthorized();
        await using var connection = await database.OpenAsync(context.RequestAborted);
        var access = await GetWorkspaceAccessAsync(connection, context, userId, context.RequestAborted);
        if (access is null) return Results.NotFound();
        var items = new List<SuggestionResponse>();
        await using var command = connection.CreateCommand();
        command.CommandText = access.IsOwner
            ? "SELECT id,category,iv,ciphertext,status,created_at,decided_at FROM support_suggestions WHERE workspace_id=$workspace ORDER BY created_at DESC LIMIT 500;"
            : "SELECT id,category,iv,ciphertext,status,created_at,decided_at FROM support_suggestions WHERE workspace_id=$workspace AND member_id=$member ORDER BY created_at DESC LIMIT 500;";
        command.Parameters.AddWithValue("$workspace", access.WorkspaceId);
        if (!access.IsOwner) command.Parameters.AddWithValue("$member", access.MembershipId);
        await using var reader = await command.ExecuteReaderAsync(context.RequestAborted);
        while (await reader.ReadAsync(context.RequestAborted))
        {
            var category = reader.GetString(1);
            if (!access.IsOwner && !access.Categories.Contains(category, StringComparer.Ordinal)) continue;
            items.Add(new SuggestionResponse(reader.GetString(0), category, Convert.ToBase64String((byte[])reader[2]),
                Convert.ToBase64String((byte[])reader[3]), reader.GetString(4), DateTimeOffset.Parse(reader.GetString(5)),
                reader.IsDBNull(6) ? null : DateTimeOffset.Parse(reader.GetString(6))));
        }
        return Results.Ok(items);
    }

    private static async Task<IResult> DecideSuggestionAsync(string suggestionId, SuggestionDecisionRequest request, HttpContext context, CloudDatabase database)
    {
        var userId = await RequireUserIdAsync(context, database);
        if (userId is null) return Results.Unauthorized();
        var decision = (request.Decision ?? "").Trim().ToLowerInvariant();
        if (decision is not ("accepted" or "rejected")) return Results.BadRequest(new { error = "Kies accepted of rejected." });

        await using var connection = await database.OpenAsync(context.RequestAborted);
        var access = await GetWorkspaceAccessAsync(connection, context, userId, context.RequestAborted);
        if (access is null) return Results.NotFound();
        if (!access.IsOwner) return Results.Forbid();
        await using var command = connection.CreateCommand();
        command.CommandText = "UPDATE support_suggestions SET status=$status,decided_at=$now,decided_by=$user WHERE id=$id AND workspace_id=$workspace AND status='pending';";
        command.Parameters.AddWithValue("$status", decision);
        command.Parameters.AddWithValue("$now", DateTimeOffset.UtcNow.ToString("O"));
        command.Parameters.AddWithValue("$user", userId);
        command.Parameters.AddWithValue("$id", suggestionId);
        command.Parameters.AddWithValue("$workspace", access.WorkspaceId);
        if (await command.ExecuteNonQueryAsync(context.RequestAborted) != 1) return Results.NotFound(new { error = "Voorstel niet gevonden of al beoordeeld." });
        return Results.Ok(new { id = suggestionId, status = decision });
    }

    private static async Task<SessionResponse> CreateSessionAsync(SqliteConnection connection, UserProfile profile, CancellationToken cancellationToken)
    {
        var token = Base64Url(RandomNumberGenerator.GetBytes(32));
        var tokenHash = Sha256Hex(Encoding.UTF8.GetBytes(token));
        var now = DateTimeOffset.UtcNow;
        var expiry = now.AddDays(SessionDays);
        await using var command = connection.CreateCommand();
        command.CommandText = "INSERT INTO sessions(token_hash,user_id,created_at,expires_at,revoked) VALUES($hash,$user,$now,$expiry,0);";
        command.Parameters.AddWithValue("$hash", tokenHash);
        command.Parameters.AddWithValue("$user", profile.Id);
        command.Parameters.AddWithValue("$now", now.ToString("O"));
        command.Parameters.AddWithValue("$expiry", expiry.ToString("O"));
        await command.ExecuteNonQueryAsync(cancellationToken);
        return new SessionResponse(token, expiry, profile);
    }

    private static async Task<string?> RequireUserIdAsync(HttpContext context, CloudDatabase database)
    {
        var tokenHash = GetBearerHash(context);
        if (tokenHash is null) return null;
        await using var connection = await database.OpenAsync(context.RequestAborted);
        await using var command = connection.CreateCommand();
        command.CommandText = "SELECT user_id FROM sessions WHERE token_hash=$hash AND revoked=0 AND expires_at>$now;";
        command.Parameters.AddWithValue("$hash", tokenHash);
        command.Parameters.AddWithValue("$now", DateTimeOffset.UtcNow.ToString("O"));
        return (string?)await command.ExecuteScalarAsync(context.RequestAborted);
    }

    private static string? GetBearerHash(HttpContext context)
    {
        var header = context.Request.Headers.Authorization.ToString();
        if (!header.StartsWith("Bearer ", StringComparison.OrdinalIgnoreCase)) return null;
        var token = header["Bearer ".Length..].Trim();
        if (token.Length is < 30 or > 256) return null;
        return Sha256Hex(Encoding.UTF8.GetBytes(token));
    }

    private static async Task<WorkspaceAccess?> GetWorkspaceAccessAsync(SqliteConnection connection, HttpContext context, string userId, CancellationToken cancellationToken)
    {
        var requestedId = context.Request.Headers["X-MoodPress-Workspace"].ToString();
        string? workspaceId = null;
        string? ownerId = null;
        if (!string.IsNullOrWhiteSpace(requestedId))
        {
            await using var byId = connection.CreateCommand();
            byId.CommandText = "SELECT id,owner_user_id FROM workspaces WHERE id=$id;";
            byId.Parameters.AddWithValue("$id", requestedId);
            await using var reader = await byId.ExecuteReaderAsync(cancellationToken);
            if (await reader.ReadAsync(cancellationToken)) { workspaceId = reader.GetString(0); ownerId = reader.GetString(1); }
        }
        else
        {
            await using var owner = connection.CreateCommand();
            owner.CommandText = "SELECT id,owner_user_id FROM workspaces WHERE owner_user_id=$user ORDER BY created_at LIMIT 1;";
            owner.Parameters.AddWithValue("$user", userId);
            await using (var reader = await owner.ExecuteReaderAsync(cancellationToken))
            {
                if (await reader.ReadAsync(cancellationToken)) { workspaceId = reader.GetString(0); ownerId = reader.GetString(1); }
            }
            if (workspaceId is null)
            {
                await using var membership = connection.CreateCommand();
                membership.CommandText = "SELECT w.id,w.owner_user_id FROM memberships m JOIN workspaces w ON w.id=m.workspace_id WHERE m.user_id=$user AND m.active=1 ORDER BY m.created_at LIMIT 1;";
                membership.Parameters.AddWithValue("$user", userId);
                await using var memberReader = await membership.ExecuteReaderAsync(cancellationToken);
                if (await memberReader.ReadAsync(cancellationToken)) { workspaceId = memberReader.GetString(0); ownerId = memberReader.GetString(1); }
            }
        }
        if (workspaceId is null || ownerId is null) return null;
        if (ownerId == userId) return new WorkspaceAccess(workspaceId, true, "owner", ShareCategories, null);

        await using var command = connection.CreateCommand();
        command.CommandText = "SELECT id,role,permissions_json FROM memberships WHERE workspace_id=$workspace AND user_id=$user AND active=1;";
        command.Parameters.AddWithValue("$workspace", workspaceId);
        command.Parameters.AddWithValue("$user", userId);
        await using var result = await command.ExecuteReaderAsync(cancellationToken);
        if (!await result.ReadAsync(cancellationToken)) return null;
        var categories = JsonSerializer.Deserialize<string[]>(result.GetString(2)) ?? Array.Empty<string>();
        return new WorkspaceAccess(workspaceId, false, result.GetString(1), categories, result.GetString(0));
    }

    private static (byte[]? Iv, byte[]? Ciphertext, string? Error) ParseCipher(string? ivBase64, string? ciphertextBase64, int maxBytes)
    {
        try
        {
            var iv = Convert.FromBase64String(ivBase64 ?? "");
            var ciphertext = Convert.FromBase64String(ciphertextBase64 ?? "");
            if (iv.Length != 12 || ciphertext.Length < 16 || ciphertext.Length > maxBytes)
            {
                CryptographicOperations.ZeroMemory(iv);
                CryptographicOperations.ZeroMemory(ciphertext);
                return (null, null, "De versleutelde inhoud heeft een ongeldige lengte.");
            }
            return (iv, ciphertext, null);
        }
        catch (FormatException) { return (null, null, "De versleutelde inhoud is ongeldig gecodeerd."); }
    }

    private static string? NormalizeEmail(string? value)
    {
        var input = (value ?? "").Trim();
        if (input.Length is < 5 or > 254) return null;
        try
        {
            var address = new MailAddress(input);
            return string.Equals(address.Address, input, StringComparison.OrdinalIgnoreCase) ? address.Address.ToLowerInvariant() : null;
        }
        catch (FormatException) { return null; }
    }

    private static byte[] HashPassword(string password, byte[] salt) =>
        Rfc2898DeriveBytes.Pbkdf2(password, salt, PasswordIterations, HashAlgorithmName.SHA256, 32);

    private static string Sha256Hex(byte[] bytes) => Convert.ToHexString(SHA256.HashData(bytes));
    private static string Base64Url(byte[] bytes) => Convert.ToBase64String(bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_');

    private sealed record WorkspaceAccess(string WorkspaceId, bool IsOwner, string Role, string[] Categories, string? MembershipId);
}
