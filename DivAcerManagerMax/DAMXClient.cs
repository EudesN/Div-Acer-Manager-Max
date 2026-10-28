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
        CancellationToken cancellationToken = default, TimeSpan? timeout = null)
    {
        var commandTimeout = timeout ?? CommandTimeout;
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
                using var timeoutCts = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
                timeoutCts.CancelAfter(commandTimeout);

                while (bytesSent < payload.Length)
                {
                    var n = await socket.SendAsync(payload.AsMemory(bytesSent), SocketFlags.None, timeoutCts.Token);
                    if (n <= 0) throw new IOException("Daemon socket refused data");
                    bytesSent += n;
                }

                var line = await ReadLineAsync(socket, timeoutCts.Token);
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
                failure = new TimeoutException($"Daemon did not answer '{command}' within {commandTimeout.TotalSeconds:0} s");
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
        Dictionary<string, object>? parameters = null, CancellationToken cancellationToken = default,
        TimeSpan? timeout = null)
    {
        using var doc = await SendCommandAsync(command, parameters, cancellationToken, timeout);
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

    // ------------------------------------------------------------------ fan mode / curve / persistence (v5)

    /// <summary>set_fan_mode: "auto" | "max" | "manual" (cpu/gpu 1..100) | "curve".</summary>
    public Task<DaemonResult> SetFanModeAsync(string mode, int? cpu = null, int? gpu = null)
    {
        var parameters = new Dictionary<string, object> { { "mode", mode } };
        if (cpu.HasValue) parameters["cpu"] = cpu.Value;
        if (gpu.HasValue) parameters["gpu"] = gpu.Value;
        return SendCommandResultAsync("set_fan_mode", parameters);
    }

    /// <summary>get_fan_curve: saved curve, engine state, presets and the last engine sample.</summary>
    public async Task<FanCurveInfo> GetFanCurveAsync()
    {
        var result = await SendCommandResultAsync("get_fan_curve");
        if (!result.Success)
            throw new DaemonCommandException(result.Error ?? "get_fan_curve failed", result.IsPermissionDenied);
        return FanCurveInfo.FromJson(result.Data);
    }

    /// <summary>set_fan_curve: validates + saves; activate=true also switches fan_mode to "curve".</summary>
    public Task<DaemonResult> SetFanCurveAsync(FanCurve curve, bool activate)
    {
        return SendCommandResultAsync("set_fan_curve", new Dictionary<string, object>
        {
            { "curve", curve.ToParams() },
            { "activate", activate }
        });
    }

    public async Task<PersistenceInfo> GetPersistenceAsync()
    {
        var result = await SendCommandResultAsync("get_persistence");
        if (!result.Success)
            throw new DaemonCommandException(result.Error ?? "get_persistence failed", result.IsPermissionDenied);
        return PersistenceInfo.FromJson(result.Data);
    }

    public Task<DaemonResult> SetPersistenceAsync(bool restoreOnStart)
    {
        return SendCommandResultAsync("set_persistence",
            new Dictionary<string, object> { { "restore_on_start", restoreOnStart } });
    }

    public Task<DaemonResult> ResetPersistenceAsync()
    {
        return SendCommandResultAsync("reset_persistence");
    }

    // ------------------------------------------------------------------ GPU power / ppd sync (v6)

    public async Task<GpuPowerInfo> GetGpuPowerInfoAsync()
    {
        var result = await SendCommandResultAsync("get_gpu_power_info");
        if (!result.Success)
            throw new DaemonCommandException(result.Error ?? "get_gpu_power_info failed", result.IsPermissionDenied);
        return GpuPowerInfo.FromJson(result.Data);
    }

    /// <summary>Privileged: enables/disables nvidia-powerd.service (data: {"powerd": {...}}).</summary>
    public Task<DaemonResult> SetNvidiaPowerdAsync(bool enabled)
    {
        // The daemon replies at once ({"scheduled": true}) and runs systemctl in the background;
        // 10 s is only a safety margin for older daemons that still wait for systemctl.
        return SendCommandResultAsync("set_nvidia_powerd", new Dictionary<string, object> { { "enabled", enabled } },
            default, TimeSpan.FromSeconds(10));
    }

    public async Task<PpdSyncInfo> GetPpdSyncAsync()
    {
        var result = await SendCommandResultAsync("get_ppd_sync");
        if (!result.Success)
            throw new DaemonCommandException(result.Error ?? "get_ppd_sync failed", result.IsPermissionDenied);
        return PpdSyncInfo.FromJson(result.Data);
    }

    /// <summary>Partial update: only the non-null parts are sent. Result data has the get_ppd_sync shape.</summary>
    public Task<DaemonResult> SetPpdSyncAsync(bool? enabled,
        Dictionary<string, Dictionary<string, string>>? toAcer = null, Dictionary<string, string>? toPpd = null)
    {
        var parameters = new Dictionary<string, object>();
        if (enabled.HasValue) parameters["enabled"] = enabled.Value;
        if (toAcer != null) parameters["to_acer"] = toAcer;
        if (toPpd != null) parameters["to_ppd"] = toPpd;
        return SendCommandResultAsync("set_ppd_sync", parameters);
    }

    /// <summary>get_thermal_profile: (current, available). Cheap; used to follow profile changes made elsewhere.</summary>
    public async Task<(string current, List<string> available)> GetThermalProfileAsync()
    {
        var result = await SendCommandResultAsync("get_thermal_profile");
        if (!result.Success)
            throw new DaemonCommandException(result.Error ?? "get_thermal_profile failed", result.IsPermissionDenied);
        var current = result.GetDataString("current") ?? "";
        var available = new List<string>();
        if (result.Data.ValueKind == JsonValueKind.Object && result.Data.TryGetProperty("available", out var a) &&
            a.ValueKind == JsonValueKind.Array)
            foreach (var item in a.EnumerateArray())
                if (item.ValueKind == JsonValueKind.String && item.GetString() is { } name)
                    available.Add(name);
        return (current.Trim(), available);
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

    /// <summary>"auto" | "max" | "manual" | "curve" | "unknown" (absent on older daemons).</summary>
    [JsonPropertyName("fan_mode")]
    [JsonConverter(typeof(FlexibleStringConverter))]
    public string FanMode { get; set; } = "";

    [JsonPropertyName("fan_curve_active")] public bool? FanCurveActive { get; set; }

    /// <summary>True while the daemon pauses fan control (Quiet profile: firmware auto, fan commands rejected).</summary>
    [JsonPropertyName("fan_control_paused")] public bool? FanControlPaused { get; set; }

    [JsonPropertyName("restore_on_start")] public bool? RestoreOnStart { get; set; }

    [JsonPropertyName("power_source")]
    [JsonConverter(typeof(FlexibleStringConverter))]
    public string PowerSource { get; set; } = "";

    [JsonPropertyName("ppd_sync")] public PpdSyncSummary? PpdSync { get; set; }

    [JsonPropertyName("gpu_power")] public GpuPowerSummary? GpuPower { get; set; }
}

public class PpdSyncSummary
{
    [JsonPropertyName("available")] public bool? Available { get; set; }
    [JsonPropertyName("enabled")] public bool? Enabled { get; set; }

    [JsonPropertyName("active_profile")]
    [JsonConverter(typeof(FlexibleStringConverter))]
    public string? ActiveProfile { get; set; }
}

public class GpuPowerSummary
{
    [JsonPropertyName("powerd_active")] public bool? PowerdActive { get; set; }
    [JsonPropertyName("default_limit_w")] public double? DefaultLimitW { get; set; }
    [JsonPropertyName("max_limit_w")] public double? MaxLimitW { get; set; }
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

/// <summary>One fan curve: points are [temp °C, percent]; see feature-contract.md.</summary>
public sealed class FanCurve
{
    public const int MinTemp = 20;
    public const int MaxTemp = 105;
    public const int SafetyTemp = 95;
    public const int MinPoints = 2;
    public const int MaxPoints = 8;

    public List<int[]> Cpu { get; set; } = new();
    public List<int[]> Gpu { get; set; } = new();
    public int Hysteresis { get; set; } = 4;
    public int Interval { get; set; } = 2;

    public static FanCurve Default()
    {
        return new FanCurve
        {
            Cpu = new List<int[]> { new[] { 40, 20 }, new[] { 60, 40 }, new[] { 75, 65 }, new[] { 88, 100 } },
            Gpu = new List<int[]> { new[] { 40, 20 }, new[] { 60, 40 }, new[] { 75, 65 }, new[] { 85, 100 } },
            Hysteresis = 4,
            Interval = 2
        };
    }

    public FanCurve Clone()
    {
        return new FanCurve
        {
            Cpu = ClonePoints(Cpu),
            Gpu = ClonePoints(Gpu),
            Hysteresis = Hysteresis,
            Interval = Interval
        };
    }

    public static List<int[]> ClonePoints(List<int[]> points)
    {
        var copy = new List<int[]>(points.Count);
        foreach (var p in points) copy.Add(new[] { p[0], p[1] });
        return copy;
    }

    public List<int[]> Series(bool gpu)
    {
        return gpu ? Gpu : Cpu;
    }

    /// <summary>Linear interpolation, flat outside the points, 100 % at/above the safety temperature.</summary>
    public static int Evaluate(List<int[]> points, double temp)
    {
        if (points.Count == 0) return 0;
        if (temp >= SafetyTemp) return 100;
        if (temp <= points[0][0]) return points[0][1];
        for (var i = 1; i < points.Count; i++)
            if (temp <= points[i][0])
            {
                var (t0, p0, t1, p1) = (points[i - 1][0], points[i - 1][1], points[i][0], points[i][1]);
                return (int)Math.Round(p0 + (p1 - p0) * (temp - t0) / Math.Max(1, t1 - t0));
            }

        return points[^1][1];
    }

    /// <summary>Same rules the daemon enforces. Returns null when valid, otherwise the error text.</summary>
    public string? Validate()
    {
        if (Hysteresis is < 0 or > 10) return "Hysteresis must be 0–10 °C.";
        if (Interval is < 1 or > 10) return "Interval must be 1–10 s.";
        return ValidateSeries("CPU", Cpu) ?? ValidateSeries("GPU", Gpu);
    }

    private static string? ValidateSeries(string name, List<int[]> points)
    {
        if (points.Count is < MinPoints or > MaxPoints) return $"{name}: the curve needs 2–8 points.";
        for (var i = 0; i < points.Count; i++)
        {
            var (t, p) = (points[i][0], points[i][1]);
            if (t is < MinTemp or > MaxTemp) return $"{name}: temperatures must be {MinTemp}–{MaxTemp} °C.";
            if (p is < 1 or > 100) return $"{name}: fan speed must be 1–100 % (0 means firmware auto).";
            if (i > 0 && t <= points[i - 1][0]) return $"{name}: temperatures must be strictly increasing.";
            if (i > 0 && p < points[i - 1][1]) return $"{name}: fan speed must not decrease as temperature rises.";
        }

        return null;
    }

    /// <summary>Object serialized as the "curve" parameter.</summary>
    public Dictionary<string, object> ToParams()
    {
        return new Dictionary<string, object>
        {
            { "cpu", ClonePoints(Cpu) },
            { "gpu", ClonePoints(Gpu) },
            { "hysteresis", Hysteresis },
            { "interval", Interval }
        };
    }

    /// <summary>Lenient parser (accepts 45 or 45.0); returns null if the shape is unusable.</summary>
    public static FanCurve? FromJson(JsonElement e)
    {
        if (e.ValueKind != JsonValueKind.Object) return null;
        var curve = new FanCurve
        {
            Cpu = ParsePoints(e, "cpu"),
            Gpu = ParsePoints(e, "gpu"),
            Hysteresis = ParseInt(e, "hysteresis") ?? 4,
            Interval = ParseInt(e, "interval") ?? 2
        };
        return curve.Cpu.Count >= MinPoints && curve.Gpu.Count >= MinPoints ? curve : null;
    }

    private static List<int[]> ParsePoints(JsonElement e, string name)
    {
        var list = new List<int[]>();
        if (!e.TryGetProperty(name, out var arr) || arr.ValueKind != JsonValueKind.Array) return list;
        foreach (var pt in arr.EnumerateArray())
        {
            if (pt.ValueKind != JsonValueKind.Array || pt.GetArrayLength() < 2) continue;
            var t = pt[0];
            var p = pt[1];
            if (t.ValueKind == JsonValueKind.Number && p.ValueKind == JsonValueKind.Number)
                list.Add(new[] { (int)Math.Round(t.GetDouble()), (int)Math.Round(p.GetDouble()) });
        }

        return list;
    }

    internal static int? ParseInt(JsonElement e, string name)
    {
        return e.ValueKind == JsonValueKind.Object && e.TryGetProperty(name, out var v) &&
               v.ValueKind == JsonValueKind.Number
            ? (int)Math.Round(v.GetDouble())
            : null;
    }

    internal static double? ParseDouble(JsonElement e, string name)
    {
        return e.ValueKind == JsonValueKind.Object && e.TryGetProperty(name, out var v) &&
               v.ValueKind == JsonValueKind.Number
            ? v.GetDouble()
            : null;
    }
}

/// <summary>Last sample of the daemon's curve engine.</summary>
public sealed class FanCurveSample
{
    public double? CpuTemp { get; init; }
    public double? GpuTemp { get; init; }
    public int? CpuPercent { get; init; }
    public int? GpuPercent { get; init; }
    public bool GpuAsleep { get; init; }

    public static FanCurveSample FromJson(JsonElement e)
    {
        return new FanCurveSample
        {
            CpuTemp = FanCurve.ParseDouble(e, "cpu_temp"),
            GpuTemp = FanCurve.ParseDouble(e, "gpu_temp"),
            CpuPercent = FanCurve.ParseInt(e, "cpu_percent"),
            GpuPercent = FanCurve.ParseInt(e, "gpu_percent"),
            GpuAsleep = e.ValueKind == JsonValueKind.Object && e.TryGetProperty("gpu_asleep", out var a) &&
                        a.ValueKind == JsonValueKind.True
        };
    }

    /// <summary>"CPU 62 °C → 45 % · GPU sleep" (culture independent).</summary>
    public string Summary()
    {
        var inv = System.Globalization.CultureInfo.InvariantCulture;
        string Part(string name, double? temp, int? pct, bool asleep)
        {
            if (asleep) return $"{name} sleep" + (pct.HasValue ? $" → {pct.Value.ToString(inv)} %" : "");
            var t = temp.HasValue ? temp.Value.ToString("0", inv) + " °C" : "— °C";
            var p = pct.HasValue ? pct.Value.ToString(inv) + " %" : "— %";
            return $"{name} {t} → {p}";
        }

        return Part("CPU", CpuTemp, CpuPercent, false) + " · " + Part("GPU", GpuTemp, GpuPercent, GpuAsleep);
    }
}

public sealed class FanCurveInfo
{
    public FanCurve Curve { get; init; } = FanCurve.Default();
    public bool Active { get; init; }
    public Dictionary<string, FanCurve> Presets { get; init; } = new(StringComparer.OrdinalIgnoreCase);
    public FanCurveSample Last { get; init; } = new();

    public static FanCurveInfo FromJson(JsonElement data)
    {
        var presets = new Dictionary<string, FanCurve>(StringComparer.OrdinalIgnoreCase);
        if (data.ValueKind == JsonValueKind.Object && data.TryGetProperty("presets", out var pr) &&
            pr.ValueKind == JsonValueKind.Object)
            foreach (var prop in pr.EnumerateObject())
                if (FanCurve.FromJson(prop.Value) is { } c)
                    presets[prop.Name] = c;

        return new FanCurveInfo
        {
            Curve = data.ValueKind == JsonValueKind.Object && data.TryGetProperty("curve", out var cv)
                ? FanCurve.FromJson(cv) ?? FanCurve.Default()
                : FanCurve.Default(),
            Active = data.ValueKind == JsonValueKind.Object && data.TryGetProperty("active", out var a) &&
                     a.ValueKind == JsonValueKind.True,
            Presets = presets,
            Last = data.ValueKind == JsonValueKind.Object && data.TryGetProperty("last", out var l)
                ? FanCurveSample.FromJson(l)
                : new FanCurveSample()
        };
    }
}

/// <summary>Saved per-power-source state (get_persistence).</summary>
public sealed class SavedSourceState
{
    public string? ThermalProfile { get; init; }
    public string? FanMode { get; init; }
    public int[]? FanManual { get; init; }

    public static SavedSourceState FromJson(JsonElement state, string source)
    {
        if (state.ValueKind != JsonValueKind.Object || !state.TryGetProperty(source, out var s) ||
            s.ValueKind != JsonValueKind.Object)
            return new SavedSourceState();

        string? Str(string n)
        {
            return s.TryGetProperty(n, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;
        }

        int[]? manual = null;
        if (s.TryGetProperty("fan_manual", out var m) && m.ValueKind == JsonValueKind.Array &&
            m.GetArrayLength() >= 2 && m[0].ValueKind == JsonValueKind.Number && m[1].ValueKind == JsonValueKind.Number)
            manual = new[] { (int)Math.Round(m[0].GetDouble()), (int)Math.Round(m[1].GetDouble()) };

        return new SavedSourceState { ThermalProfile = Str("thermal_profile"), FanMode = Str("fan_mode"), FanManual = manual };
    }
}

public sealed class PersistenceInfo
{
    public bool RestoreOnStart { get; init; }
    public string PowerSource { get; init; } = "";
    public SavedSourceState Ac { get; init; } = new();
    public SavedSourceState Battery { get; init; } = new();

    public static PersistenceInfo FromJson(JsonElement data)
    {
        var state = data.ValueKind == JsonValueKind.Object && data.TryGetProperty("state", out var st)
            ? st
            : default;
        return new PersistenceInfo
        {
            RestoreOnStart = data.ValueKind == JsonValueKind.Object && data.TryGetProperty("restore_on_start", out var r) &&
                             r.ValueKind == JsonValueKind.True,
            PowerSource = data.ValueKind == JsonValueKind.Object && data.TryGetProperty("power_source", out var ps) &&
                          ps.ValueKind == JsonValueKind.String
                ? ps.GetString() ?? ""
                : "",
            Ac = SavedSourceState.FromJson(state, "ac"),
            Battery = SavedSourceState.FromJson(state, "battery")
        };
    }
}

internal static class JsonRead
{
    public static bool Bool(JsonElement e, string name)
    {
        return e.ValueKind == JsonValueKind.Object && e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.True;
    }

    public static bool? NullableBool(JsonElement e, string name)
    {
        if (e.ValueKind != JsonValueKind.Object || !e.TryGetProperty(name, out var v)) return null;
        return v.ValueKind switch { JsonValueKind.True => true, JsonValueKind.False => false, _ => null };
    }

    public static string? Str(JsonElement e, string name)
    {
        return e.ValueKind == JsonValueKind.Object && e.TryGetProperty(name, out var v) &&
               v.ValueKind == JsonValueKind.String
            ? v.GetString()
            : null;
    }

    public static JsonElement Obj(JsonElement e, string name)
    {
        return e.ValueKind == JsonValueKind.Object && e.TryGetProperty(name, out var v) &&
               v.ValueKind == JsonValueKind.Object
            ? v
            : default;
    }

    public static Dictionary<string, string> StringMap(JsonElement e)
    {
        var map = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        if (e.ValueKind != JsonValueKind.Object) return map;
        foreach (var p in e.EnumerateObject())
            if (p.Value.ValueKind == JsonValueKind.String && p.Value.GetString() is { } v)
                map[p.Name] = v;
        return map;
    }
}

/// <summary>get_gpu_power_info (feature-contract-2.md §A).</summary>
public sealed class GpuPowerInfo
{
    public bool NvidiaPresent { get; init; }
    public bool GpuAsleep { get; init; }
    public bool PowerdInstalled { get; init; }
    public bool PowerdEnabled { get; init; }
    public bool PowerdActive { get; init; }
    public double? DefaultLimitW { get; init; }
    public double? MaxLimitW { get; init; }
    public bool? DynamicBoostSupported { get; init; }

    public static GpuPowerInfo FromJson(JsonElement data)
    {
        var powerd = JsonRead.Obj(data, "powerd");
        return new GpuPowerInfo
        {
            NvidiaPresent = JsonRead.Bool(data, "nvidia_present"),
            GpuAsleep = JsonRead.Bool(data, "gpu_asleep"),
            PowerdInstalled = JsonRead.Bool(powerd, "installed"),
            PowerdEnabled = JsonRead.Bool(powerd, "enabled"),
            PowerdActive = JsonRead.Bool(powerd, "active"),
            DefaultLimitW = FanCurve.ParseDouble(data, "default_limit_w"),
            MaxLimitW = FanCurve.ParseDouble(data, "max_limit_w"),
            DynamicBoostSupported = JsonRead.NullableBool(data, "dynamic_boost_supported")
        };
    }
}

/// <summary>get_ppd_sync / set_ppd_sync data (feature-contract-2.md §B).</summary>
public sealed class PpdSyncInfo
{
    public static readonly string[] PpdProfiles = { "power-saver", "balanced", "performance" };

    public bool Available { get; init; }
    public string? BusName { get; init; }
    public string? ActiveProfile { get; init; }
    public bool Enabled { get; init; }
    public string PowerSource { get; init; } = "";

    /// <summary>to_acer[source][ppd profile] = Acer profile; sources "ac" / "battery".</summary>
    public Dictionary<string, Dictionary<string, string>> ToAcer { get; init; } = DefaultToAcer();

    /// <summary>to_ppd[Acer profile] = ppd profile.</summary>
    public Dictionary<string, string> ToPpd { get; init; } = DefaultToPpd();

    public static Dictionary<string, Dictionary<string, string>> DefaultToAcer()
    {
        return new Dictionary<string, Dictionary<string, string>>(StringComparer.OrdinalIgnoreCase)
        {
            ["ac"] = new(StringComparer.OrdinalIgnoreCase)
            {
                ["power-saver"] = "quiet", ["balanced"] = "balanced", ["performance"] = "balanced-performance"
            },
            ["battery"] = new(StringComparer.OrdinalIgnoreCase)
            {
                ["power-saver"] = "low-power", ["balanced"] = "balanced", ["performance"] = "balanced"
            }
        };
    }

    public static Dictionary<string, string> DefaultToPpd()
    {
        return new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["low-power"] = "power-saver", ["quiet"] = "power-saver", ["balanced"] = "balanced",
            ["balanced-performance"] = "performance", ["performance"] = "performance"
        };
    }

    public static PpdSyncInfo FromJson(JsonElement data)
    {
        var toAcer = DefaultToAcer();
        var toAcerJson = JsonRead.Obj(data, "to_acer");
        foreach (var source in new[] { "ac", "battery" })
        {
            var map = JsonRead.StringMap(JsonRead.Obj(toAcerJson, source));
            foreach (var kv in map) toAcer[source][kv.Key] = kv.Value;
        }

        var toPpd = DefaultToPpd();
        foreach (var kv in JsonRead.StringMap(JsonRead.Obj(data, "to_ppd"))) toPpd[kv.Key] = kv.Value;

        return new PpdSyncInfo
        {
            Available = JsonRead.Bool(data, "available"),
            BusName = JsonRead.Str(data, "bus_name"),
            ActiveProfile = JsonRead.Str(data, "active_profile"),
            Enabled = JsonRead.Bool(data, "enabled"),
            PowerSource = JsonRead.Str(data, "power_source") ?? "",
            ToAcer = toAcer,
            ToPpd = toPpd
        };
    }
}
