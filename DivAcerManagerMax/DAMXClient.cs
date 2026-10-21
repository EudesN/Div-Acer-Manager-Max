using System;
using System.Collections.Generic;
using System.IO;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading;
using System.Threading.Tasks;

namespace DivAcerManagerMax;

/// <summary>Connection state of the client, used for the header status chip.</summary>
public enum DaemonConnectionState
{
    Disconnected,
    Connected,
    NotRunning,
    PermissionDenied,
    Error
}

/// <summary>Result of a daemon command: { "success": bool, "data": ..., "error": "...", "message": "..." }.</summary>
public sealed class DaemonResult
{
    public DaemonResult(bool success, string? error, JsonElement data, string? message)
    {
        Success = success;
        Error = error;
        Data = data;
        Message = message;
    }

    public bool Success { get; }
    public string? Error { get; }

    /// <summary>Detached copy of the "data" member (ValueKind == Undefined when absent).</summary>
    public JsonElement Data { get; }

    public string? Message { get; }

    public bool IsPermissionDenied =>
        Error != null && Error.Contains("permission denied", StringComparison.OrdinalIgnoreCase);

    public static DaemonResult Fail(string error)
    {
        return new DaemonResult(false, error, default, null);
    }

    public string? GetDataString(string property)
    {
        if (Data.ValueKind != JsonValueKind.Object || !Data.TryGetProperty(property, out var value)) return null;
        return value.ValueKind switch
        {
            JsonValueKind.String => value.GetString(),
            JsonValueKind.Null or JsonValueKind.Undefined => null,
            _ => value.GetRawText()
        };
    }

    public bool GetDataBool(string property)
    {
        return Data.ValueKind == JsonValueKind.Object &&
               Data.TryGetProperty(property, out var value) &&
               value.ValueKind == JsonValueKind.True;
    }
}

/// <summary>Thrown when the socket exists but the user is not allowed to open it (EACCES).</summary>
public sealed class DaemonPermissionException : Exception
{
    public const string UserMessage =
        "Sem permissão para acessar o daemon. Adicione seu usuário ao grupo 'damx' " +
        "(sudo usermod -aG damx $USER) e faça logout/login.";

    public DaemonPermissionException(Exception? inner = null) : base(UserMessage, inner)
    {
    }
}

/// <summary>Thrown when a command reached the daemon but it answered success=false.</summary>
public sealed class DaemonCommandException : Exception
{
    public DaemonCommandException(string message, bool permissionDenied = false) : base(message)
    {
        PermissionDenied = permissionDenied;
    }

    public bool PermissionDenied { get; }
}

/// <summary>
///     Client for the DAMX-Daemon Unix socket.
///     Protocol: newline-delimited JSON. Each request is one JSON object followed by "\n";
///     each response is one JSON object followed by "\n".
/// </summary>
public class DAMXClient : IDisposable
{
    private const string SocketPath = "/var/run/DAMX.sock";
    private const int MaxRetryAttempts = 2;
    private const int RetryDelayMs = 300;
    private const int MaxResponseBytes = 64 * 1024;
    private static readonly TimeSpan CommandTimeout = TimeSpan.FromSeconds(5);
    private static readonly TimeSpan ConnectTimeout = TimeSpan.FromSeconds(3);

    // Serializes every socket operation: connect, send+receive, reset. Two callers must never
    // interleave requests on the socket or dispose each other's socket.
    private readonly SemaphoreSlim _commandLock = new(1, 1);
    private readonly byte[] _rxBuffer = new byte[MaxResponseBytes];
    private volatile HashSet<string> _availableFeatures = new();
    private volatile bool _disposed;
    private int _rxCount;
    private Socket? _socket;
    private volatile DaemonConnectionState _state = DaemonConnectionState.Disconnected;

    public DAMXClient()
    {
    }

    /// <summary>True while a socket is open and the last operation on it succeeded.</summary>
    public bool IsConnected => _state == DaemonConnectionState.Connected;

    public DaemonConnectionState State => _state;

    /// <summary>Human readable reason of the last connection failure (null when connected).</summary>
    public string? LastError { get; private set; }

    /// <summary>Daemon version reported by "ping" (null if unknown).</summary>
    public string? DaemonVersion { get; private set; }

    public void Dispose()
    {
        Dispose(true);
        GC.SuppressFinalize(this);
    }

    public bool IsFeatureAvailable(string featureName)
    {
        return _availableFeatures.Contains(featureName);
    }

    /// <summary>
    ///     Make sure we have a working connection. A working socket is never torn down because of a
    ///     single negative answer: only I/O errors reset the connection (inside SendCommandAsync).
    /// </summary>
    public async Task<bool> ConnectAsync()
    {
        try
        {
            if (!await ValidateConnectionAsync()) return false;
            await RefreshAvailableFeaturesAsync();
            return IsConnected;
        }
        catch (DaemonPermissionException ex)
        {
            LastError = ex.Message;
            return false;
        }
        catch (Exception ex)
        {
            LastError = ex.Message;
            Console.WriteLine($"Failed to connect to daemon: {ex.Message}");
            return false;
        }
    }

    /// <summary>Sends "ping". Any well-formed answer proves the link works (old daemons answer "Unknown command").</summary>
    private async Task<bool> ValidateConnectionAsync()
    {
        var result = await SendCommandResultAsync("ping");
        if (result.Success)
            DaemonVersion = result.GetDataString("version") ?? DaemonVersion;
        return IsConnected;
    }

    private async Task RefreshAvailableFeaturesAsync()
    {
        try
        {
            var result = await SendCommandResultAsync("get_supported_features");
            if (!result.Success || result.Data.ValueKind != JsonValueKind.Object) return;
            if (!result.Data.TryGetProperty("available_features", out var features) ||
                features.ValueKind != JsonValueKind.Array) return;

            var set = new HashSet<string>();
            foreach (var feature in features.EnumerateArray())
                if (feature.ValueKind == JsonValueKind.String && feature.GetString() is { } name)
                    set.Add(name);

            _availableFeatures = set;
            Console.WriteLine($"Available features: {string.Join(", ", set)}");
        }
        catch (DaemonPermissionException)
        {
            throw;
        }
        catch (Exception ex)
        {
            Console.WriteLine($"Failed to get available features: {ex.Message}");
        }
    }

    public void Disconnect()
    {
        // Take the lock so we never dispose a socket another caller is using. If an operation is
        // stuck we still reset after a short wait (its own timeout will fire anyway).
        var acquired = _commandLock.Wait(TimeSpan.FromSeconds(2));
        try
        {
            ResetConnectionCore();
            _state = DaemonConnectionState.Disconnected;
        }
        finally
        {
            if (acquired) _commandLock.Release();
        }
    }

    private static bool IsIdempotent(string command)
    {
        return command == "ping" || command.StartsWith("get_", StringComparison.Ordinal);
    }

    /// <summary>
    ///     Send a command and return the parsed response. The caller owns (and must dispose) the document.
    ///     Retries only when nothing was sent yet (or for read-only get_* / ping commands).
    /// </summary>
    public async Task<JsonDocument> SendCommandAsync(string command, Dictionary<string, object>? parameters = null,
        CancellationToken cancellationToken = default)
    {
        if (_disposed) throw new ObjectDisposedException(nameof(DAMXClient));

        var request = new Dictionary<string, object>
        {
            ["command"] = command,
            ["params"] = parameters ?? new Dictionary<string, object>()
        };
        var payload = Encoding.UTF8.GetBytes(JsonSerializer.Serialize(request) + "\n");
        var idempotent = IsIdempotent(command);

        for (var attempt = 1;; attempt++)
        {
            var bytesSent = 0;
            Exception failure = new IOException("Unknown daemon communication error");

            await _commandLock.WaitAsync(cancellationToken);
            try
            {
                if (_socket == null || _state != DaemonConnectionState.Connected)
                    await ConnectCoreAsync(cancellationToken);

                var socket = _socket!;
                using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
                timeout.CancelAfter(CommandTimeout);

                while (bytesSent < payload.Length)
                {
                    var n = await socket.SendAsync(payload.AsMemory(bytesSent), SocketFlags.None, timeout.Token);
                    if (n <= 0) throw new IOException("Daemon socket refused data");
                    bytesSent += n;
                }

                var line = await ReadLineAsync(socket, timeout.Token);
                return JsonDocument.Parse(line);
            }
            catch (DaemonPermissionException)
            {
                throw;
            }
            catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
            {
                ResetConnectionCore();
                _state = DaemonConnectionState.Error;
                failure = new TimeoutException($"Daemon did not answer '{command}' within {CommandTimeout.TotalSeconds:0} s");
            }
            catch (Exception ex) when (ex is SocketException or IOException or ObjectDisposedException
                                           or JsonException or InvalidOperationException)
            {
                ResetConnectionCore();
                if (_state == DaemonConnectionState.Connected) _state = DaemonConnectionState.Error;
                failure = ex;
            }
            finally
            {
                _commandLock.Release();
            }

            // Never resend a state-changing command that may already have reached the daemon.
            var mayRetry = bytesSent == 0 || idempotent;
            if (!mayRetry || attempt >= MaxRetryAttempts)
            {
                LastError = failure.Message;
                if (failure is TimeoutException or IOException) throw failure;
                throw new IOException($"Failed to communicate with daemon: {failure.Message}", failure);
            }

            await Task.Delay(RetryDelayMs, cancellationToken);
        }
    }

    /// <summary>Send a command and return a detached result (no JsonDocument to dispose).</summary>
    public async Task<DaemonResult> SendCommandResultAsync(string command,
        Dictionary<string, object>? parameters = null, CancellationToken cancellationToken = default)
    {
        using var doc = await SendCommandAsync(command, parameters, cancellationToken);
        return ToResult(doc);
    }

    private static DaemonResult ToResult(JsonDocument doc)
    {
        var root = doc.RootElement;
        if (root.ValueKind != JsonValueKind.Object)
            return DaemonResult.Fail("Malformed daemon response");

        var success = root.TryGetProperty("success", out var s) && s.ValueKind == JsonValueKind.True;
        string? error = null;
        if (root.TryGetProperty("error", out var e) && e.ValueKind == JsonValueKind.String)
            error = e.GetString();
        string? message = null;
        if (root.TryGetProperty("message", out var m) && m.ValueKind == JsonValueKind.String)
            message = m.GetString();
        var data = root.TryGetProperty("data", out var d) ? d.Clone() : default;

        if (!success && string.IsNullOrWhiteSpace(error))
            error = "Daemon reported failure";

        return new DaemonResult(success, error, data, message);
    }

    /// <summary>Reads one "\n"-terminated response. Must be called with the lock held.</summary>
    private async Task<string> ReadLineAsync(Socket socket, CancellationToken token)
    {
        while (true)
        {
            var newline = Array.IndexOf(_rxBuffer, (byte)'\n', 0, _rxCount);
            if (newline >= 0)
            {
                var line = Encoding.UTF8.GetString(_rxBuffer, 0, newline).Trim();
                var rest = _rxCount - (newline + 1);
                if (rest > 0) Buffer.BlockCopy(_rxBuffer, newline + 1, _rxBuffer, 0, rest);
                _rxCount = rest;
                if (line.Length == 0) continue; // tolerate blank keep-alive lines
                return line;
            }

            // Compatibility with daemons that do not terminate responses with "\n":
            // accept the buffer once it holds one complete JSON value.
            if (_rxCount > 0 && IsCompleteJson(_rxBuffer, _rxCount))
            {
                var line = Encoding.UTF8.GetString(_rxBuffer, 0, _rxCount).Trim();
                _rxCount = 0;
                return line;
            }

            if (_rxCount >= MaxResponseBytes)
                throw new IOException($"Daemon response exceeds {MaxResponseBytes / 1024} KiB");

            var received = await socket.ReceiveAsync(_rxBuffer.AsMemory(_rxCount, MaxResponseBytes - _rxCount),
                SocketFlags.None, token);
            if (received <= 0) throw new IOException("Daemon closed the connection");
            _rxCount += received;
        }
    }

    private static bool IsCompleteJson(byte[] buffer, int count)
    {
        try
        {
            var reader = new Utf8JsonReader(new ReadOnlySpan<byte>(buffer, 0, count), true, default);
            return reader.Read() && reader.TrySkip() && !reader.Read();
        }
        catch (JsonException)
        {
            return false;
        }
    }

    /// <summary>Opens a new socket. Must be called with the lock held.</summary>
    private async Task ConnectCoreAsync(CancellationToken cancellationToken)
    {
        ResetConnectionCore();

        var socket = new Socket(AddressFamily.Unix, SocketType.Stream, ProtocolType.Unspecified);
        try
        {
            using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            timeout.CancelAfter(ConnectTimeout);
            await socket.ConnectAsync(new UnixDomainSocketEndPoint(SocketPath), timeout.Token);

            _socket = socket;
            _rxCount = 0;
            _state = DaemonConnectionState.Connected;
            LastError = null;
        }
        catch (Exception ex) when (IsAccessDenied(ex))
        {
            socket.Dispose();
            _state = DaemonConnectionState.PermissionDenied;
            LastError = DaemonPermissionException.UserMessage;
            throw new DaemonPermissionException(ex);
        }
        catch (Exception ex) when (ex is SocketException or OperationCanceledException or IOException)
        {
            socket.Dispose();
            _state = DaemonConnectionState.NotRunning;
            LastError = File.Exists(SocketPath)
                ? $"Daemon not reachable at {SocketPath}: {ex.Message}"
                : $"{SocketPath} not found — is damx-daemon.service running?";
            throw new IOException(LastError, ex);
        }
        catch
        {
            socket.Dispose();
            _state = DaemonConnectionState.Error;
            throw;
        }
    }

    private static bool IsAccessDenied(Exception ex)
    {
        return ex is UnauthorizedAccessException ||
               (ex is SocketException se &&
                (se.SocketErrorCode == SocketError.AccessDenied || se.NativeErrorCode == 13));
    }

    /// <summary>Closes the current socket. Must be called with the lock held (or from Dispose).</summary>
    private void ResetConnectionCore()
    {
        var socket = _socket;
        _socket = null;
        _rxCount = 0;
        if (_state == DaemonConnectionState.Connected) _state = DaemonConnectionState.Disconnected;
        if (socket == null) return;

        try
        {
            socket.Shutdown(SocketShutdown.Both);
        }
        catch
        {
            // Socket may already be closed.
        }

        try
        {
            socket.Dispose();
        }
        catch
        {
            // Ignore cleanup failure.
        }
    }

    // ------------------------------------------------------------------ typed commands

    private static DaemonResult NotAvailable(string feature)
    {
        return DaemonResult.Fail($"Feature '{feature}' is not available on this device");
    }

    public async Task<DAMXSettings> GetAllSettingsAsync()
    {
        var result = await SendCommandResultAsync("get_all_settings");
        if (!result.Success)
            throw new DaemonCommandException($"Failed to get settings: {result.Error}", result.IsPermissionDenied);

        var settings = result.Data.ValueKind == JsonValueKind.Object
            ? result.Data.Deserialize<DAMXSettings>() ?? new DAMXSettings()
            : new DAMXSettings();

        if (settings.AvailableFeatures != null)
            _availableFeatures = new HashSet<string>(settings.AvailableFeatures);

        return settings;
    }

    public async Task<string?> GetModprobeParameterAsync()
    {
        var result = await SendCommandResultAsync("get_modprobe_parameter");
        if (!result.Success)
            throw new DaemonCommandException(result.Error ?? "get_modprobe_parameter failed",
                result.IsPermissionDenied);
        return result.GetDataString("parameter") ?? "";
    }

    public Task<DaemonResult> SetThermalProfileAsync(string profile)
    {
        if (!IsFeatureAvailable("thermal_profile")) return Task.FromResult(NotAvailable("thermal_profile"));
        return SendCommandResultAsync("set_thermal_profile", new Dictionary<string, object> { { "profile", profile } });
    }

    public Task<DaemonResult> SetFanSpeedAsync(int cpu, int gpu)
    {
        if (!IsFeatureAvailable("fan_speed")) return Task.FromResult(NotAvailable("fan_speed"));
        return SendCommandResultAsync("set_fan_speed",
            new Dictionary<string, object> { { "cpu", cpu }, { "gpu", gpu } });
    }

    public Task<DaemonResult> SetBacklightTimeoutAsync(bool enabled)
    {
        if (!IsFeatureAvailable("backlight_timeout")) return Task.FromResult(NotAvailable("backlight_timeout"));
        return SendCommandResultAsync("set_backlight_timeout",
            new Dictionary<string, object> { { "enabled", enabled } });
    }

    public Task<DaemonResult> SetBatteryCalibrationAsync(bool enabled)
    {
        if (!IsFeatureAvailable("battery_calibration")) return Task.FromResult(NotAvailable("battery_calibration"));
        return SendCommandResultAsync("set_battery_calibration",
            new Dictionary<string, object> { { "enabled", enabled } });
    }

    public Task<DaemonResult> SetBatteryLimiterAsync(bool enabled)
    {
        if (!IsFeatureAvailable("battery_limiter")) return Task.FromResult(NotAvailable("battery_limiter"));
        return SendCommandResultAsync("set_battery_limiter",
            new Dictionary<string, object> { { "enabled", enabled } });
    }

    public Task<DaemonResult> SetBootAnimationSoundAsync(bool enabled)
    {
        if (!IsFeatureAvailable("boot_animation_sound"))
            return Task.FromResult(NotAvailable("boot_animation_sound"));
        return SendCommandResultAsync("set_boot_animation_sound",
            new Dictionary<string, object> { { "enabled", enabled } });
    }

    public Task<DaemonResult> SetLcdOverrideAsync(bool enabled)
    {
        if (!IsFeatureAvailable("lcd_override")) return Task.FromResult(NotAvailable("lcd_override"));
        return SendCommandResultAsync("set_lcd_override", new Dictionary<string, object> { { "enabled", enabled } });
    }

    public Task<DaemonResult> SetUsbChargingAsync(int level)
    {
        if (!IsFeatureAvailable("usb_charging")) return Task.FromResult(NotAvailable("usb_charging"));
        return SendCommandResultAsync("set_usb_charging", new Dictionary<string, object> { { "level", level } });
    }

    public Task<DaemonResult> SetPerZoneModeAsync(string zone1, string zone2, string zone3, string zone4,
        int brightness)
    {
        if (!IsFeatureAvailable("per_zone_mode")) return Task.FromResult(NotAvailable("per_zone_mode"));
        return SendCommandResultAsync("set_per_zone_mode", new Dictionary<string, object>
        {
            { "zone1", zone1 },
            { "zone2", zone2 },
            { "zone3", zone3 },
            { "zone4", zone4 },
            { "brightness", brightness }
        });
    }

    public Task<DaemonResult> SetFourZoneModeAsync(int mode, int speed, int brightness, int direction, int red,
        int green, int blue)
    {
        if (!IsFeatureAvailable("four_zone_mode")) return Task.FromResult(NotAvailable("four_zone_mode"));
        return SendCommandResultAsync("set_four_zone_mode", new Dictionary<string, object>
        {
            { "mode", mode },
            { "speed", speed },
            { "brightness", brightness },
            { "direction", direction },
            { "red", red },
            { "green", green },
            { "blue", blue }
        });
    }

    protected virtual void Dispose(bool disposing)
    {
        if (_disposed) return;
        _disposed = true;

        if (disposing)
            Disconnect();
    }
}

/// <summary>Reads a JSON string, number, bool or null into a string (daemon versions differ).</summary>
public sealed class FlexibleStringConverter : JsonConverter<string>
{
    public override string? Read(ref Utf8JsonReader reader, Type typeToConvert, JsonSerializerOptions options)
    {
        switch (reader.TokenType)
        {
            case JsonTokenType.String:
                return reader.GetString();
            case JsonTokenType.Number:
                using (var doc = JsonDocument.ParseValue(ref reader))
                {
                    return doc.RootElement.GetRawText();
                }
            case JsonTokenType.True:
                return "1";
            case JsonTokenType.False:
                return "0";
            case JsonTokenType.Null:
                return null;
            default:
                reader.Skip();
                return null;
        }
    }

    public override void Write(Utf8JsonWriter writer, string value, JsonSerializerOptions options)
    {
        writer.WriteStringValue(value);
    }
}

/// <summary>
///     Models for DAMX settings
/// </summary>
public class DAMXSettings
{
    [JsonPropertyName("laptop_type")]
    [JsonConverter(typeof(FlexibleStringConverter))]
    public string LaptopType { get; set; } = "UNKNOWN";

    [JsonPropertyName("has_four_zone_kb")] public bool HasFourZoneKb { get; set; }

    [JsonPropertyName("available_features")]
    public List<string> AvailableFeatures { get; set; } = new();

    [JsonPropertyName("version")]
    [JsonConverter(typeof(FlexibleStringConverter))]
    public string Version { get; set; } = "NOT CONNECTED PROPERLY";

    [JsonPropertyName("driver_version")]
    [JsonConverter(typeof(FlexibleStringConverter))]
    public string DriverVersion { get; set; } = "DRIVER VERSION NOT FOUND";

    [JsonPropertyName("thermal_profile")] public ThermalProfileSettings ThermalProfile { get; set; } = new();

    [JsonPropertyName("backlight_timeout")]
    [JsonConverter(typeof(FlexibleStringConverter))]
    public string BacklightTimeout { get; set; } = "0";

    [JsonPropertyName("battery_calibration")]
    [JsonConverter(typeof(FlexibleStringConverter))]
    public string BatteryCalibration { get; set; } = "0";

    [JsonPropertyName("battery_limiter")]
    [JsonConverter(typeof(FlexibleStringConverter))]
    public string BatteryLimiter { get; set; } = "0";

    [JsonPropertyName("boot_animation_sound")]
    [JsonConverter(typeof(FlexibleStringConverter))]
    public string BootAnimationSound { get; set; } = "0";

    [JsonPropertyName("fan_speed")] public FanSpeedSettings FanSpeed { get; set; } = new();

    [JsonPropertyName("lcd_override")]
    [JsonConverter(typeof(FlexibleStringConverter))]
    public string LcdOverride { get; set; } = "0";

    [JsonPropertyName("usb_charging")]
    [JsonConverter(typeof(FlexibleStringConverter))]
    public string UsbCharging { get; set; } = "0";

    [JsonPropertyName("per_zone_mode")]
    [JsonConverter(typeof(FlexibleStringConverter))]
    public string PerZoneMode { get; set; } = "";

    [JsonPropertyName("four_zone_mode")]
    [JsonConverter(typeof(FlexibleStringConverter))]
    public string FourZoneMode { get; set; } = "";

    [JsonPropertyName("modprobe_parameter")]
    [JsonConverter(typeof(FlexibleStringConverter))]
    public string ModprobeParameter { get; set; } = "";
}

public class ThermalProfileSettings
{
    [JsonPropertyName("current")]
    [JsonConverter(typeof(FlexibleStringConverter))]
    public string Current { get; set; } = "";

    [JsonPropertyName("available")] public List<string> Available { get; set; } = new();
}

public class FanSpeedSettings
{
    [JsonPropertyName("cpu")]
    [JsonConverter(typeof(FlexibleStringConverter))]
    public string Cpu { get; set; } = "0";

    [JsonPropertyName("gpu")]
    [JsonConverter(typeof(FlexibleStringConverter))]
    public string Gpu { get; set; } = "0";
}
