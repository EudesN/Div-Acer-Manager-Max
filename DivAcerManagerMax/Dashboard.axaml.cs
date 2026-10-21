using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Runtime.CompilerServices;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Media;
using Avalonia.Threading;
using LiveChartsCore;
using LiveChartsCore.SkiaSharpView;
using LiveChartsCore.SkiaSharpView.Avalonia;
using LiveChartsCore.SkiaSharpView.Painting;
using Material.Icons.Avalonia;
using SkiaSharp;

namespace DivAcerManagerMax;

public partial class Dashboard : UserControl, INotifyPropertyChanged
{
    private const int RefreshIntervalMs = 2000;
    private const int MaxHistoryPoints = 60; // 2 minutes at one sample every 2 s
    private const int CommandTimeoutMs = 2000;
    private const double HotTemperatureC = 85;

    // Visual fan speed: 3000 RPM -> one revolution per second. 0 RPM -> stopped.
    private const double DegreesPerSecondPerRpm = 0.12;

    private const string Dash = "—";
    private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

    private readonly Stopwatch _animationClock = Stopwatch.StartNew();
    private readonly RotateTransform _cpuFanRotateTransform = new();
    private readonly DispatcherTimer _fanAnimationTimer;
    private readonly RotateTransform _gpuFanRotateTransform = new();
    private readonly HashSet<string> _missingCommands = new();
    private readonly DispatcherTimer _refreshTimer;

    // Cache for sysfs paths. Only touched by the static-info task and then by one refresh at a
    // time (the in-flight guard serializes them), so no locking is needed.
    private readonly Dictionary<string, string> _systemInfoPaths = new();

    private string? _batteryDir;
    private int _batteryPercentageInt;
    private double _batteryLevel;
    private string _batteryLimiterText = Dash;
    private string _batteryPercentText = Dash;
    private string _batteryStatus = "UNKNOWN";
    private string _batteryTimeRemainingString = Dash;
    private double _cpuFanAngle;
    private double _cpuFanLevel;
    private int _cpuFanSpeedRpm;
    private string _cpuFanText = Dash;
    private string _cpuName = "Detecting…";
    private double _cpuTemp;
    private ObservableCollection<double> _cpuTempHistory = new();
    private string _cpuTempText = Dash;
    private double _cpuUsage;
    private string _cpuUsageText = Dash;

    public bool _fanPathsSearched;
    private double _gpuFanAngle;
    private double _gpuFanLevel;
    private int _gpuFanSpeedRpm;
    private string _gpuFanText = Dash;
    private string _gpuDriverText = Dash;
    private string _gpuName = "Detecting…";
    private string _gpuStateText = "CORE TEMP";
    private double _gpuTemp;
    private ObservableCollection<double> _gpuTempHistory = new();
    private string _gpuTempText = Dash;
    private GpuType _gpuType = GpuType.Unknown;
    private double _gpuUsage;
    private string _gpuUsageText = Dash;
    private bool _hasBattery;
    private volatile bool _hasBatteryInternal;
    private bool _isAttached;
    private bool _isCpuHot;
    private bool _isGpuAsleep;
    private bool _isGpuHot;
    private bool _isMinimized;
    private string _kernelVersion = "";
    private double _lastAnimationSeconds;
    private double _lastCpuUsage;
    private double _lastGpuTemp = double.NaN;
    private string? _nvidiaPciPath;
    private string _osVersion = "";
    private long _prevCpuIdle;
    private long _prevCpuTotal;
    private string _ramTotal = "";
    private double _ramUsage;
    private bool _refreshInFlight;
    private bool _staticInfoReady;
    private CartesianChart? _temperatureChart;
    private ObservableCollection<ISeries> _tempSeries = new();
    private Window? _window;

    public Dashboard()
    {
        InitializeComponent();
        DataContext = this;

        var cpuFanIcon = this.FindControl<MaterialIcon>("CpuFanIcon");
        if (cpuFanIcon != null) cpuFanIcon.RenderTransform = _cpuFanRotateTransform;
        var gpuFanIcon = this.FindControl<MaterialIcon>("GpuFanIcon");
        if (gpuFanIcon != null) gpuFanIcon.RenderTransform = _gpuFanRotateTransform;

        InitializeTemperatureGraph();

        _refreshTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(RefreshIntervalMs) };
        _refreshTimer.Tick += OnRefreshTick;

        _fanAnimationTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(33) };
        _fanAnimationTimer.Tick += OnFanAnimationTick;

        // Static info (lspci / nvidia / glxinfo / sysfs scans) runs off the UI thread.
        _ = InitializeStaticSystemInfoAsync();
    }

    // ------------------------------------------------------------------ bindable properties

    public string CpuName { get => _cpuName; set => SetProperty(ref _cpuName, value); }
    public string GpuName { get => _gpuName; set => SetProperty(ref _gpuName, value); }
    public string GpuDriverText { get => _gpuDriverText; set => SetProperty(ref _gpuDriverText, value); }
    public int CpuFanSpeedRPM { get => _cpuFanSpeedRpm; set => SetProperty(ref _cpuFanSpeedRpm, value); }
    public int GpuFanSpeedRPM { get => _gpuFanSpeedRpm; set => SetProperty(ref _gpuFanSpeedRpm, value); }
    public double CpuFanLevel { get => _cpuFanLevel; set => SetProperty(ref _cpuFanLevel, value); }
    public double GpuFanLevel { get => _gpuFanLevel; set => SetProperty(ref _gpuFanLevel, value); }
    public string CpuFanText { get => _cpuFanText; set => SetProperty(ref _cpuFanText, value); }
    public string GpuFanText { get => _gpuFanText; set => SetProperty(ref _gpuFanText, value); }
    public string OsVersion { get => _osVersion; set => SetProperty(ref _osVersion, value); }
    public string KernelVersion { get => _kernelVersion; set => SetProperty(ref _kernelVersion, value); }
    public string RamTotal { get => _ramTotal; set => SetProperty(ref _ramTotal, value); }
    public double CpuTemp { get => _cpuTemp; set => SetProperty(ref _cpuTemp, value); }
    public double GpuTemp { get => _gpuTemp; set => SetProperty(ref _gpuTemp, value); }
    public string CpuTempText { get => _cpuTempText; set => SetProperty(ref _cpuTempText, value); }
    public string GpuTempText { get => _gpuTempText; set => SetProperty(ref _gpuTempText, value); }
    public string GpuStateText { get => _gpuStateText; set => SetProperty(ref _gpuStateText, value); }
    public double CpuUsage { get => _cpuUsage; set => SetProperty(ref _cpuUsage, value); }
    public string CpuUsageText { get => _cpuUsageText; set => SetProperty(ref _cpuUsageText, value); }
    public double RamUsage { get => _ramUsage; set => SetProperty(ref _ramUsage, value); }
    public double GpuUsage { get => _gpuUsage; set => SetProperty(ref _gpuUsage, value); }
    public string GpuUsageText { get => _gpuUsageText; set => SetProperty(ref _gpuUsageText, value); }
    public bool IsCpuHot { get => _isCpuHot; set => SetProperty(ref _isCpuHot, value); }
    public bool IsGpuHot { get => _isGpuHot; set => SetProperty(ref _isGpuHot, value); }
    public bool IsGpuAsleep { get => _isGpuAsleep; set => SetProperty(ref _isGpuAsleep, value); }
    public string BatteryStatus { get => _batteryStatus; set => SetProperty(ref _batteryStatus, value); }
    public int BatteryPercentageInt { get => _batteryPercentageInt; set => SetProperty(ref _batteryPercentageInt, value); }
    public string BatteryPercentText { get => _batteryPercentText; set => SetProperty(ref _batteryPercentText, value); }
    public double BatteryLevel { get => _batteryLevel; set => SetProperty(ref _batteryLevel, value); }

    public string BatteryTimeRemainingString
    {
        get => _batteryTimeRemainingString;
        set => SetProperty(ref _batteryTimeRemainingString, value);
    }

    public string BatteryLimiterText
    {
        get => _batteryLimiterText;
        set => SetProperty(ref _batteryLimiterText, value);
    }

    public bool HasBattery { get => _hasBattery; set => SetProperty(ref _hasBattery, value); }

    // INotifyPropertyChanged implementation (hides AvaloniaObject.PropertyChanged on purpose,
    // the compiled bindings in Dashboard.axaml use it).
    public new event PropertyChangedEventHandler? PropertyChanged;

    /// <summary>Called by MainWindow after reading settings from the daemon.</summary>
    public void SetBatteryLimiterState(bool? enabled)
    {
        BatteryLimiterText = enabled switch
        {
            true => "ON · 80%",
            false => "OFF",
            null => Dash
        };
    }

    // ------------------------------------------------------------------ lifecycle / timers

    protected override void OnAttachedToVisualTree(VisualTreeAttachmentEventArgs e)
    {
        base.OnAttachedToVisualTree(e);
        _isAttached = true;

        _window = TopLevel.GetTopLevel(this) as Window;
        if (_window != null)
        {
            _window.PropertyChanged += OnWindowPropertyChanged;
            _isMinimized = _window.WindowState == WindowState.Minimized;
        }

        UpdateTimers();
    }

    protected override void OnDetachedFromVisualTree(VisualTreeAttachmentEventArgs e)
    {
        base.OnDetachedFromVisualTree(e);
        _isAttached = false;

        if (_window != null)
        {
            _window.PropertyChanged -= OnWindowPropertyChanged;
            _window = null;
        }

        UpdateTimers();
    }

    private void OnWindowPropertyChanged(object? sender, AvaloniaPropertyChangedEventArgs e)
    {
        if (e.Property != Window.WindowStateProperty || _window == null) return;
        _isMinimized = _window.WindowState == WindowState.Minimized;
        UpdateTimers();
    }

    /// <summary>Polling only runs while the dashboard is on screen and the window is not minimized.</summary>
    private void UpdateTimers()
    {
        var shouldRun = _isAttached && _staticInfoReady && !_isMinimized;

        if (shouldRun && !_refreshTimer.IsEnabled)
        {
            _refreshTimer.Start();
            _ = RefreshDynamicMetricsAsync();
        }
        else if (!shouldRun && _refreshTimer.IsEnabled)
        {
            _refreshTimer.Stop();
        }

        UpdateFanAnimationState();
    }

    private void UpdateFanAnimationState()
    {
        var spin = _isAttached && !_isMinimized && (_cpuFanSpeedRpm > 0 || _gpuFanSpeedRpm > 0);
        if (spin && !_fanAnimationTimer.IsEnabled)
        {
            _lastAnimationSeconds = _animationClock.Elapsed.TotalSeconds;
            _fanAnimationTimer.Start();
        }
        else if (!spin && _fanAnimationTimer.IsEnabled)
        {
            _fanAnimationTimer.Stop();
        }
    }

    private void OnFanAnimationTick(object? sender, EventArgs e)
    {
        var now = _animationClock.Elapsed.TotalSeconds;
        var dt = Math.Clamp(now - _lastAnimationSeconds, 0, 0.1);
        _lastAnimationSeconds = now;

        _cpuFanAngle = (_cpuFanAngle + _cpuFanSpeedRpm * DegreesPerSecondPerRpm * dt) % 360;
        _gpuFanAngle = (_gpuFanAngle + _gpuFanSpeedRpm * DegreesPerSecondPerRpm * dt) % 360;
        _cpuFanRotateTransform.Angle = _cpuFanAngle;
        _gpuFanRotateTransform.Angle = _gpuFanAngle;
    }

    // ------------------------------------------------------------------ refresh

    private void OnRefreshTick(object? sender, EventArgs e)
    {
        _ = RefreshDynamicMetricsAsync();
    }

    private async Task RefreshDynamicMetricsAsync()
    {
        // Never let two refreshes overlap (a slow nvidia-smi / sensors call would pile them up).
        if (_refreshInFlight || !_staticInfoReady) return;
        _refreshInFlight = true;

        try
        {
            var metrics = await Task.Run(CollectMetrics);

            // The await resumes on the UI thread (Avalonia sync context); only marshal if not.
            if (Dispatcher.UIThread.CheckAccess())
                ApplyMetrics(metrics);
            else
                Dispatcher.UIThread.Post(() => ApplyMetrics(metrics));
        }
        catch (Exception ex)
        {
            Console.WriteLine($"Error updating metrics: {ex.Message}");
        }
        finally
        {
            _refreshInFlight = false;
        }
    }

    private MetricsData CollectMetrics()
    {
        var data = new MetricsData
        {
            CpuUsage = GetCpuUsage(),
            CpuTemp = GetCpuTemperature(),
            RamUsage = GetRamUsage()
        };

        var fans = GetFanSpeeds();
        data.CpuFanSpeedRPM = fans.cpuFan;
        data.GpuFanSpeedRPM = fans.gpuFan;

        var gpu = GetGpuMetrics();
        data.GpuTemp = gpu.temperature;
        data.GpuUsage = gpu.usage;
        data.GpuAsleep = gpu.asleep;

        var battery = GetBatteryInfo();
        data.BatteryPercentage = battery.percentage;
        data.BatteryStatus = battery.status;
        data.BatteryTimeRemainingHours = battery.timeRemaining;
        return data;
    }

    private void ApplyMetrics(MetricsData m)
    {
        try
        {
            CpuUsage = m.CpuUsage;
            CpuUsageText = FormatNumber(m.CpuUsage);
            CpuTemp = m.CpuTemp;
            CpuTempText = m.CpuTemp > 0 ? FormatNumber(m.CpuTemp) : Dash;
            IsCpuHot = m.CpuTemp >= HotTemperatureC;
            RamUsage = m.RamUsage;

            CpuFanSpeedRPM = m.CpuFanSpeedRPM;
            GpuFanSpeedRPM = m.GpuFanSpeedRPM;
            CpuFanLevel = m.CpuFanSpeedRPM;
            GpuFanLevel = m.GpuFanSpeedRPM;
            CpuFanText = m.CpuFanSpeedRPM.ToString(Inv);
            GpuFanText = m.GpuFanSpeedRPM.ToString(Inv);

            IsGpuAsleep = m.GpuAsleep;
            if (m.GpuAsleep)
            {
                GpuTempText = "SLEEP";
                GpuUsageText = Dash;
                GpuUsage = 0;
                IsGpuHot = false;
                GpuStateText = "dGPU ASLEEP · NOT POLLED";
            }
            else
            {
                var validTemp = !double.IsNaN(m.GpuTemp) && m.GpuTemp > 0;
                GpuTemp = validTemp ? m.GpuTemp : 0;
                GpuTempText = validTemp ? FormatNumber(m.GpuTemp) : Dash;
                IsGpuHot = validTemp && m.GpuTemp >= HotTemperatureC;
                GpuUsage = double.IsNaN(m.GpuUsage) ? 0 : m.GpuUsage;
                GpuUsageText = double.IsNaN(m.GpuUsage) ? Dash : FormatNumber(m.GpuUsage);
                GpuStateText = "CORE TEMP";
                if (validTemp) _lastGpuTemp = m.GpuTemp;
            }

            if (_hasBatteryInternal)
            {
                BatteryPercentageInt = m.BatteryPercentage;
                BatteryPercentText = m.BatteryPercentage.ToString(Inv);
                BatteryLevel = m.BatteryPercentage;
                BatteryStatus = string.IsNullOrWhiteSpace(m.BatteryStatus)
                    ? "UNKNOWN"
                    : m.BatteryStatus.ToUpperInvariant();
                BatteryTimeRemainingString = FormatHoursMinutes(m.BatteryTimeRemainingHours);
            }

            UpdateFanAnimationState();

            AddHistoryPoint(_cpuTempHistory, m.CpuTemp);
            // While the dGPU sleeps we keep the last known value so the two series stay aligned.
            AddHistoryPoint(_gpuTempHistory, double.IsNaN(_lastGpuTemp) ? 0 : _lastGpuTemp);
        }
        catch (Exception ex)
        {
            Console.WriteLine($"Error applying metrics: {ex.Message}");
        }
    }

    private static void AddHistoryPoint(ObservableCollection<double> history, double value)
    {
        if (double.IsNaN(value) || double.IsInfinity(value)) value = 0;
        if (history.Count >= MaxHistoryPoints)
            history.RemoveAt(0);
        history.Add(value);
    }

    private static string FormatNumber(double value)
    {
        return double.IsNaN(value) ? Dash : value.ToString("0", Inv);
    }

    /// <summary>Hours as "h:mm" (culture independent); "—" when unknown.</summary>
    private static string FormatHoursMinutes(double hours)
    {
        if (double.IsNaN(hours) || double.IsInfinity(hours) || hours <= 0 || hours > 99) return Dash;
        var totalMinutes = (int)Math.Round(hours * 60);
        return string.Format(Inv, "{0}:{1:00}", totalMinutes / 60, totalMinutes % 60);
    }

    // ------------------------------------------------------------------ static info (background)

    private async Task InitializeStaticSystemInfoAsync()
    {
        try
        {
            var info = await Task.Run(CollectStaticInfo);

            CpuName = info.CpuName;
            GpuName = info.GpuName;
            GpuDriverText = info.GpuDriver;
            OsVersion = info.OsVersion;
            KernelVersion = info.KernelVersion;
            RamTotal = info.RamTotal;
            HasBattery = info.HasBattery;
        }
        catch (Exception ex)
        {
            Console.WriteLine($"Error during initialization: {ex.Message}");
        }
        finally
        {
            _staticInfoReady = true;
            UpdateTimers();
        }
    }

    private StaticInfo CollectStaticInfo()
    {
        var info = new StaticInfo();
        try
        {
            info.CpuName = GetCpuName();
            DetectGpuType();
            info.GpuName = GetGpuName();
            FindSystemPaths();
            info.GpuDriver = GetGpuDriverVersion();
            info.OsVersion = GetOsVersion();
            info.KernelVersion = GetKernelVersion();
            info.RamTotal = GetTotalRam();
            info.HasBattery = CheckForBattery();
            _hasBatteryInternal = info.HasBattery;

            // Prime the /proc/stat sample so the first refresh already has a delta.
            GetCpuUsage();
        }
        catch (Exception ex)
        {
            Console.WriteLine($"Error collecting static info: {ex.Message}");
        }

        return info;
    }

    private static string? ReadTrim(string path)
    {
        try
        {
            return File.Exists(path) ? File.ReadAllText(path).Trim() : null;
        }
        catch
        {
            return null;
        }
    }

    private string GetCpuName()
    {
        try
        {
            var cpuInfo = File.ReadAllText("/proc/cpuinfo");
            var modelNameMatch = Regex.Match(cpuInfo, @"model name\s+:\s+(.+)");
            if (modelNameMatch.Success) return modelNameMatch.Groups[1].Value.Trim();
            return "Unknown CPU";
        }
        catch
        {
            return "CPU Information Unavailable";
        }
    }

    /// <summary>
    ///     Detect the GPU from sysfs (vendor/class attributes are cached by the kernel, reading them
    ///     never wakes a runtime-suspended device — unlike lspci or nvidia-smi).
    /// </summary>
    private void DetectGpuType()
    {
        string? nvidia = null, amd = null, intel = null;
        try
        {
            const string pciRoot = "/sys/bus/pci/devices";
            if (Directory.Exists(pciRoot))
                foreach (var dev in Directory.GetDirectories(pciRoot))
                {
                    var cls = ReadTrim(Path.Combine(dev, "class"));
                    if (cls == null || !cls.StartsWith("0x03", StringComparison.OrdinalIgnoreCase)) continue;

                    switch (ReadTrim(Path.Combine(dev, "vendor"))?.ToLowerInvariant())
                    {
                        case "0x10de":
                            nvidia ??= dev;
                            break;
                        case "0x1002":
                            amd ??= dev;
                            break;
                        case "0x8086":
                            intel ??= dev;
                            break;
                    }
                }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"PCI scan failed: {ex.Message}");
        }

        _nvidiaPciPath = nvidia;

        if (nvidia != null) _gpuType = GpuType.Nvidia;
        else if (amd != null) _gpuType = GpuType.Amd;
        else if (intel != null) _gpuType = GpuType.Intel;
        else
        {
            // No sysfs (unusual): fall back to a single lspci call.
            var lspci = RunCommand("lspci", "");
            if (lspci.Contains("NVIDIA")) _gpuType = GpuType.Nvidia;
            else if (lspci.Contains("AMD") || lspci.Contains("ATI")) _gpuType = GpuType.Amd;
            else if (lspci.Contains("Intel")) _gpuType = GpuType.Intel;
            else _gpuType = GpuType.Unknown;
        }
    }

    /// <summary>True when the NVIDIA dGPU is runtime-suspended (D3cold); querying it would wake it.</summary>
    private bool IsNvidiaSuspended()
    {
        if (_nvidiaPciPath == null) return false;
        var status = ReadTrim(Path.Combine(_nvidiaPciPath, "power", "runtime_status"));
        return string.Equals(status, "suspended", StringComparison.OrdinalIgnoreCase);
    }

    private string GetGpuName()
    {
        try
        {
            return _gpuType switch
            {
                GpuType.Nvidia => GetNvidiaGpuName(),
                GpuType.Amd => GetAmdGpuName(),
                GpuType.Intel => GetIntelGpuName(),
                _ => GetFallbackGpuName()
            };
        }
        catch
        {
            return "GPU Information Unavailable";
        }
    }

    private string GetNvidiaGpuName()
    {
        // The proprietary driver exposes the model in procfs; reading it does not need the GPU awake.
        try
        {
            const string gpusRoot = "/proc/driver/nvidia/gpus";
            if (Directory.Exists(gpusRoot))
            {
                var candidates = new List<string>();
                if (_nvidiaPciPath != null)
                    candidates.Add(Path.Combine(gpusRoot, Path.GetFileName(_nvidiaPciPath), "information"));
                candidates.AddRange(Directory.GetDirectories(gpusRoot).Select(d => Path.Combine(d, "information")));

                foreach (var file in candidates)
                {
                    var text = ReadTrim(file);
                    if (text == null) continue;
                    var match = Regex.Match(text, @"^Model:\s+(.+)$", RegexOptions.Multiline);
                    if (match.Success) return match.Groups[1].Value.Trim();
                }
            }
        }
        catch
        {
            // fall through
        }

        if (!IsNvidiaSuspended())
        {
            var nvidiaSmiOutput = RunCommand("nvidia-smi", "--query-gpu=name --format=csv,noheader");
            var firstLine = FirstNonEmptyLine(nvidiaSmiOutput);
            if (firstLine != null) return firstLine;
        }

        return "NVIDIA GPU";
    }

    private string GetAmdGpuName()
    {
        var rocmOutput = RunCommand("rocm-smi", "--showproductname");
        var match = Regex.Match(rocmOutput, @"Product Name:\s+(.+)");
        if (match.Success) return match.Groups[1].Value.Trim();

        var renderer = GetGlxRenderer();
        if (renderer != null) return renderer;

        return GetLspciDisplayName("1002") ?? "AMD GPU";
    }

    private string GetIntelGpuName()
    {
        return GetLspciDisplayName("8086") ?? GetGlxRenderer() ?? "Intel Graphics";
    }

    private string GetFallbackGpuName()
    {
        return GetGlxRenderer() ?? "Unknown GPU";
    }

    /// <summary>Name of the first display controller of a vendor via "lspci -vmm -d vendor::0300".</summary>
    private string? GetLspciDisplayName(string vendorHex)
    {
        var output = RunCommand("lspci", $"-vmm -d {vendorHex}::0300");
        var match = Regex.Match(output, @"^Device:\s+(.+)$", RegexOptions.Multiline);
        return match.Success ? match.Groups[1].Value.Trim() : null;
    }

    private string? GetGlxRenderer()
    {
        var glxOutput = RunCommand("glxinfo", "-B");
        var match = Regex.Match(glxOutput, @"OpenGL renderer string:\s+(.+)");
        if (!match.Success) return null;
        var cleaned = Regex.Replace(match.Groups[1].Value, @"(\(.*?\)|LLVM.*|DRM.*)", "").Trim();
        return cleaned.Length > 0 ? cleaned : null;
    }

    private string GetGpuDriverVersion()
    {
        try
        {
            if (_gpuType == GpuType.Nvidia)
            {
                var moduleVersion = ReadTrim("/sys/module/nvidia/version");
                if (!string.IsNullOrEmpty(moduleVersion)) return moduleVersion;

                var procVersion = ReadTrim("/proc/driver/nvidia/version");
                if (procVersion != null)
                {
                    var m = Regex.Match(procVersion, @"Kernel Module(?:\s+for\s+\S+)?\s+(\d+(?:\.\d+)+)");
                    if (m.Success) return m.Groups[1].Value;
                }

                if (!IsNvidiaSuspended())
                {
                    var smi = FirstNonEmptyLine(RunCommand("nvidia-smi",
                        "--query-gpu=driver_version --format=csv,noheader"));
                    if (smi != null) return smi;
                }

                return "Unknown Driver";
            }

            // Mesa (AMD / Intel): "glxinfo -B" and parse here (no shell pipes).
            var glx = RunCommand("glxinfo", "-B");
            var mesa = Regex.Match(glx, @"OpenGL version string:.*?Mesa\s+(\d+(?:\.\d+)+)");
            if (mesa.Success) return "Mesa " + mesa.Groups[1].Value;

            var generic = Regex.Match(glx, @"OpenGL version string:.*?(\d+\.\d+\.\d+)");
            if (generic.Success) return generic.Groups[1].Value;

            return "Unknown Driver";
        }
        catch
        {
            return "Driver Information Unavailable";
        }
    }

    private string GetOsVersion()
    {
        try
        {
            if (File.Exists("/etc/os-release"))
            {
                var osRelease = File.ReadAllText("/etc/os-release");
                var prettyNameMatch = Regex.Match(osRelease, @"PRETTY_NAME=""(.+?)""");
                if (prettyNameMatch.Success) return prettyNameMatch.Groups[1].Value;
            }

            return "Unknown Linux Distribution";
        }
        catch
        {
            return "OS Information Unavailable";
        }
    }

    private string GetKernelVersion()
    {
        return ReadTrim("/proc/sys/kernel/osrelease") ?? RunCommand("uname", "-r").Trim();
    }

    private string GetTotalRam()
    {
        try
        {
            var memInfo = File.ReadAllText("/proc/meminfo");
            var match = Regex.Match(memInfo, @"MemTotal:\s+(\d+) kB");
            if (match.Success && long.TryParse(match.Groups[1].Value, NumberStyles.Integer, Inv, out var kbytes))
                return string.Format(Inv, "{0:F2} GB", kbytes / (1024.0 * 1024.0));

            return "Unknown";
        }
        catch
        {
            return "RAM Information Unavailable";
        }
    }

    private bool CheckForBattery()
    {
        try
        {
            if (!Directory.Exists("/sys/class/power_supply"))
                return false;

            var batteryDirs = Directory.GetDirectories("/sys/class/power_supply")
                .Where(dir => ReadTrim(Path.Combine(dir, "type")) == "Battery")
                .OrderBy(dir => dir, StringComparer.Ordinal)
                .ToList();

            if (batteryDirs.Count == 0) return false;

            _batteryDir = batteryDirs[0];

            if (File.Exists(Path.Combine(_batteryDir, "energy_now")))
                _systemInfoPaths["energy_now"] = Path.Combine(_batteryDir, "energy_now");
            else if (File.Exists(Path.Combine(_batteryDir, "charge_now")))
                _systemInfoPaths["energy_now"] = Path.Combine(_batteryDir, "charge_now");

            if (File.Exists(Path.Combine(_batteryDir, "power_now")))
                _systemInfoPaths["power_now"] = Path.Combine(_batteryDir, "power_now");
            else if (File.Exists(Path.Combine(_batteryDir, "current_now")))
                _systemInfoPaths["power_now"] = Path.Combine(_batteryDir, "current_now");

            if (File.Exists(Path.Combine(_batteryDir, "energy_full")))
                _systemInfoPaths["energy_full"] = Path.Combine(_batteryDir, "energy_full");
            else if (File.Exists(Path.Combine(_batteryDir, "charge_full")))
                _systemInfoPaths["energy_full"] = Path.Combine(_batteryDir, "charge_full");

            _systemInfoPaths["capacity"] = Path.Combine(_batteryDir, "capacity");
            _systemInfoPaths["status"] = Path.Combine(_batteryDir, "status");
            return true;
        }
        catch (Exception ex)
        {
            Console.WriteLine($"Error checking battery: {ex.Message}");
            return false;
        }
    }

    private void InitializeTemperatureGraph()
    {
        _cpuTempHistory = new ObservableCollection<double>();
        _gpuTempHistory = new ObservableCollection<double>();

        // CPU = off-white ink, GPU = the accent (see HudPalette / App.axaml).
        var cpuColor = new SKColor(0xEC, 0xE7, 0xDD);
        var gpuColor = new SKColor(0xE8, 0xF2, 0x3A);

        _tempSeries = new ObservableCollection<ISeries>
        {
            new LineSeries<double>
            {
                Values = _cpuTempHistory,
                Name = "CPU",
                Stroke = new SolidColorPaint(cpuColor) { StrokeThickness = 1.5f },
                GeometryStroke = null,
                GeometryFill = null,
                GeometrySize = 0,
                Fill = AreaFill(cpuColor)
            },
            new LineSeries<double>
            {
                Values = _gpuTempHistory,
                Name = "GPU",
                Stroke = new SolidColorPaint(gpuColor) { StrokeThickness = 1.5f },
                GeometryStroke = null,
                GeometryFill = null,
                GeometrySize = 0,
                Fill = AreaFill(gpuColor)
            }
        };

        _temperatureChart = this.FindControl<CartesianChart>("TemperatureChart");
        if (_temperatureChart == null) return;

        _temperatureChart.Series = _tempSeries;
        _temperatureChart.XAxes = new List<Axis>
        {
            new()
            {
                Name = "Time",
                IsVisible = false
            }
        };
        _temperatureChart.YAxes = new List<Axis>
        {
            new()
            {
                LabelsPaint = new SolidColorPaint(new SKColor(0x77, 0x73, 0x6C)),
                TextSize = 11,
                Labeler = value => value.ToString("0", CultureInfo.InvariantCulture) + "°",
                SeparatorsPaint = new SolidColorPaint(new SKColor(0x2A, 0x2E, 0x33)) { StrokeThickness = 1 }
            }
        };
    }

    // Vertical gradient from a faint line colour down to transparent.
    private static LinearGradientPaint AreaFill(SKColor color)
    {
        return new LinearGradientPaint(
            new[] { color.WithAlpha(28), color.WithAlpha(0) },
            new SKPoint(0.5f, 0),
            new SKPoint(0.5f, 1));
    }

    // ------------------------------------------------------------------ dynamic metrics (background)

    /// <summary>
    ///     CPU usage from the aggregate "cpu" line of /proc/stat, using the previous sample (no sleep).
    ///     Total includes user, nice, system, idle, iowait, irq, softirq and steal; idle includes iowait.
    /// </summary>
    private double GetCpuUsage()
    {
        try
        {
            var line = File.ReadLines("/proc/stat").FirstOrDefault(l => l.StartsWith("cpu ", StringComparison.Ordinal));
            if (line == null) return _lastCpuUsage;

            var parts = line.Split(' ', StringSplitOptions.RemoveEmptyEntries);
            var values = new long[8];
            for (var i = 0; i < values.Length && i + 1 < parts.Length; i++)
                long.TryParse(parts[i + 1], NumberStyles.Integer, Inv, out values[i]);

            var idle = values[3] + values[4];
            long total = 0;
            foreach (var v in values) total += v;

            var prevTotal = _prevCpuTotal;
            var prevIdle = _prevCpuIdle;
            _prevCpuTotal = total;
            _prevCpuIdle = idle;

            if (prevTotal == 0) return _lastCpuUsage;

            var totalDelta = total - prevTotal;
            var idleDelta = idle - prevIdle;
            if (totalDelta <= 0) return _lastCpuUsage;

            var usage = Math.Clamp((totalDelta - idleDelta) * 100.0 / totalDelta, 0, 100);
            _lastCpuUsage = Math.Round(usage, 1);
            return _lastCpuUsage;
        }
        catch
        {
            return _lastCpuUsage;
        }
    }

    private double GetCpuTemperature()
    {
        try
        {
            if (_systemInfoPaths.TryGetValue("cpu_temp", out var cpuTempPath) &&
                TryReadMillidegreeTemperature(cpuTempPath, out var tempC))
                return Math.Round(tempC, 1);

            cpuTempPath = FindCpuTemperaturePath();
            if (cpuTempPath != null)
            {
                _systemInfoPaths["cpu_temp"] = cpuTempPath;
                if (TryReadMillidegreeTemperature(cpuTempPath, out tempC))
                    return Math.Round(tempC, 1);
            }

            // Fallback to lm-sensors if available
            var output = RunCommand("sensors", "");
            var match = Regex.Match(output, @"Package id \d+:\s+\+?(\d+(?:\.\d+)?)°C");
            if (match.Success &&
                double.TryParse(match.Groups[1].Value, NumberStyles.Float, Inv, out tempC))
                return Math.Round(tempC, 1);

            match = Regex.Match(output, @"(?:Tctl|Tdie):\s+\+?(\d+(?:\.\d+)?)°C");
            if (match.Success &&
                double.TryParse(match.Groups[1].Value, NumberStyles.Float, Inv, out tempC))
                return Math.Round(tempC, 1);

            return 0;
        }
        catch (Exception ex)
        {
            Console.WriteLine($"Error getting CPU temperature: {ex.Message}");
            return 0;
        }
    }

    private static bool TryReadMillidegreeTemperature(string path, out double temperatureC)
    {
        temperatureC = 0;

        var temperatureStr = ReadTrim(path);
        if (temperatureStr == null ||
            !int.TryParse(temperatureStr, NumberStyles.Integer, Inv, out var tempValue))
            return false;

        temperatureC = tempValue / 1000.0;
        return true;
    }

    private static string? FindCpuTemperaturePath()
    {
        var intelPackagePath = FindHwmonTemperaturePath(
            "coretemp",
            label => Regex.IsMatch(label, @"^Package id \d+$", RegexOptions.IgnoreCase));
        if (intelPackagePath != null)
            return intelPackagePath;

        var amdPackagePath = FindHwmonTemperaturePath(
            "k10temp",
            label => string.Equals(label, "Tctl", StringComparison.OrdinalIgnoreCase) ||
                     string.Equals(label, "Tdie", StringComparison.OrdinalIgnoreCase));
        if (amdPackagePath != null)
            return amdPackagePath;

        return FindHwmonTemperaturePath(
            "zenpower",
            label => string.Equals(label, "Tctl", StringComparison.OrdinalIgnoreCase) ||
                     string.Equals(label, "Tdie", StringComparison.OrdinalIgnoreCase));
    }

    private static string? FindHwmonTemperaturePath(string hwmonName, Func<string, bool> labelMatches)
    {
        const string hwmonRoot = "/sys/class/hwmon";
        if (!Directory.Exists(hwmonRoot))
            return null;

        foreach (var hwmonDir in Directory.GetDirectories(hwmonRoot).OrderBy(path => path, StringComparer.Ordinal))
        {
            var name = ReadTrim(Path.Combine(hwmonDir, "name"));
            if (!string.Equals(name, hwmonName, StringComparison.OrdinalIgnoreCase))
                continue;

            foreach (var labelFile in Directory.GetFiles(hwmonDir, "temp*_label")
                         .OrderBy(path => path, StringComparer.Ordinal))
            {
                var label = ReadTrim(labelFile);
                if (label == null || !labelMatches(label))
                    continue;

                var inputFile = Path.Combine(
                    hwmonDir,
                    Path.GetFileName(labelFile).Replace("_label", "_input", StringComparison.Ordinal));
                if (File.Exists(inputFile))
                    return inputFile;
            }

            var temp1Input = Path.Combine(hwmonDir, "temp1_input");
            if (File.Exists(temp1Input))
                return temp1Input;
        }

        return null;
    }

    private double GetRamUsage()
    {
        try
        {
            var memInfo = File.ReadAllText("/proc/meminfo");

            var totalMatch = Regex.Match(memInfo, @"MemTotal:\s+(\d+) kB");
            var availableMatch = Regex.Match(memInfo, @"MemAvailable:\s+(\d+) kB");

            if (totalMatch.Success && availableMatch.Success &&
                long.TryParse(totalMatch.Groups[1].Value, NumberStyles.Integer, Inv, out var totalKb) &&
                long.TryParse(availableMatch.Groups[1].Value, NumberStyles.Integer, Inv, out var availableKb) &&
                totalKb > 0)
                return Math.Round((totalKb - availableKb) / (double)totalKb * 100.0, 1);

            return 0;
        }
        catch
        {
            return 0;
        }
    }

    private (double temperature, double usage, bool asleep) GetGpuMetrics()
    {
        try
        {
            return _gpuType switch
            {
                GpuType.Nvidia => GetNvidiaGpuMetrics(),
                GpuType.Amd => GetAmdGpuMetrics(),
                GpuType.Intel => GetIntelGpuMetrics(),
                _ => (double.NaN, double.NaN, false)
            };
        }
        catch
        {
            return (double.NaN, double.NaN, false);
        }
    }

    private (double temperature, double usage, bool asleep) GetNvidiaGpuMetrics()
    {
        // Never spawn nvidia-smi while the dGPU is runtime-suspended: it would wake it up
        // (and keep it awake on battery) just to draw a number.
        if (IsNvidiaSuspended())
            return (double.NaN, double.NaN, true);

        var output = RunCommand("nvidia-smi",
            "--query-gpu=temperature.gpu,utilization.gpu --format=csv,noheader,nounits");
        var line = FirstNonEmptyLine(output);
        if (line == null) return (double.NaN, double.NaN, false);

        var parts = line.Split(',', StringSplitOptions.TrimEntries);
        var temp = parts.Length > 0 && double.TryParse(parts[0], NumberStyles.Float, Inv, out var t)
            ? t
            : double.NaN;
        var usage = parts.Length > 1 && double.TryParse(parts[1], NumberStyles.Float, Inv, out var u)
            ? u
            : double.NaN;
        return (temp, usage, false);
    }

    private (double temperature, double usage, bool asleep) GetAmdGpuMetrics()
    {
        var temp = double.NaN;
        var usage = double.NaN;

        if (_systemInfoPaths.TryGetValue("gpu_temp", out var tempPath) &&
            TryReadMillidegreeTemperature(tempPath, out var tempC))
            temp = tempC;

        if (_systemInfoPaths.TryGetValue("gpu_usage", out var usagePath) &&
            int.TryParse(ReadTrim(usagePath), NumberStyles.Integer, Inv, out var usageValue))
            usage = usageValue;

        if (double.IsNaN(temp) || double.IsNaN(usage))
        {
            var radeontopOutput = RunCommand("radeontop", "-d - -l 1");
            var tempMatch = Regex.Match(radeontopOutput, @"Temperature:\s+(\d+)");
            var usageMatch = Regex.Match(radeontopOutput, @"gpu\s+(\d+(?:\.\d+)?)%", RegexOptions.IgnoreCase);

            if (double.IsNaN(temp) && tempMatch.Success &&
                double.TryParse(tempMatch.Groups[1].Value, NumberStyles.Float, Inv, out var tv))
                temp = tv;

            if (double.IsNaN(usage) && usageMatch.Success &&
                double.TryParse(usageMatch.Groups[1].Value, NumberStyles.Float, Inv, out var uv))
                usage = uv;
        }

        return (temp, usage, false);
    }

    private (double temperature, double usage, bool asleep) GetIntelGpuMetrics()
    {
        var temp = double.NaN;
        if (_systemInfoPaths.TryGetValue("gpu_temp", out var tempPath) &&
            TryReadMillidegreeTemperature(tempPath, out var tempC))
            temp = tempC;

        // intel_gpu_top needs root and never exits on its own: usage is not polled.
        return (temp, double.NaN, false);
    }

    private void FindSystemPaths()
    {
        try
        {
            var cpuTempPath = FindCpuTemperaturePath();
            if (cpuTempPath != null)
            {
                _systemInfoPaths["cpu_temp"] = cpuTempPath;
                Console.WriteLine($"Found CPU package temperature at {cpuTempPath}");
            }
            else
            {
                Console.WriteLine("CPU package temperature hwmon sensor not found; sensors fallback will be used");
            }

            FindFanSpeedPaths();

            switch (_gpuType)
            {
                case GpuType.Amd:
                    var amdTemp = FindHwmonTemperaturePath("amdgpu", label =>
                        string.Equals(label, "edge", StringComparison.OrdinalIgnoreCase));
                    if (amdTemp != null) _systemInfoPaths["gpu_temp"] = amdTemp;

                    foreach (var card in SafeGetDirectories("/sys/class/drm"))
                    {
                        var busy = Path.Combine(card, "device", "gpu_busy_percent");
                        if (File.Exists(busy))
                        {
                            _systemInfoPaths["gpu_usage"] = busy;
                            break;
                        }
                    }

                    break;

                case GpuType.Intel:
                    foreach (var zone in SafeGetDirectories("/sys/class/thermal"))
                    {
                        var type = ReadTrim(Path.Combine(zone, "type"));
                        var tempFile = Path.Combine(zone, "temp");
                        if (type != null && type.Contains("gpu", StringComparison.OrdinalIgnoreCase) &&
                            File.Exists(tempFile))
                        {
                            _systemInfoPaths["gpu_temp"] = tempFile;
                            break;
                        }
                    }

                    break;
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"Error finding system paths: {ex.Message}");
        }
    }

    private static string[] SafeGetDirectories(string path)
    {
        try
        {
            return Directory.Exists(path) ? Directory.GetDirectories(path) : Array.Empty<string>();
        }
        catch
        {
            return Array.Empty<string>();
        }
    }

    private void FindFanSpeedPaths()
    {
        try
        {
            if (_fanPathsSearched)
                return;

            _fanPathsSearched = true;

            var hwmonDirs = SafeGetDirectories("/sys/class/hwmon").OrderBy(d => d, StringComparer.Ordinal).ToArray();

            // Known fan controllers first (acer / linuwu_sense, generic "fan" drivers, ...)
            foreach (var hwmonDir in hwmonDirs)
            {
                var deviceName = ReadTrim(Path.Combine(hwmonDir, "name"))?.ToLowerInvariant();
                if (deviceName == null) continue;

                if (deviceName.Contains("acer") || deviceName.Contains("fan") ||
                    deviceName.Contains("acpi") || deviceName.Contains("thinkpad"))
                {
                    var fan1File = Path.Combine(hwmonDir, "fan1_input");
                    var fan2File = Path.Combine(hwmonDir, "fan2_input");

                    if (File.Exists(fan1File) && !_systemInfoPaths.ContainsKey("cpu_fan"))
                        _systemInfoPaths["cpu_fan"] = fan1File;

                    if (File.Exists(fan2File) && !_systemInfoPaths.ContainsKey("gpu_fan"))
                        _systemInfoPaths["gpu_fan"] = fan2File;

                    if (_systemInfoPaths.ContainsKey("cpu_fan") && _systemInfoPaths.ContainsKey("gpu_fan"))
                        return;
                }
            }

            // Acer multi-value files ("CPU: 2400 GPU: 3100")
            string[] possibleAcerMultiValuePaths =
            {
                "/sys/devices/platform/acer-wmi/fan_speed",
                "/proc/acpi/acer-wmi/fans"
            };

            foreach (var path in possibleAcerMultiValuePaths)
            {
                var content = ReadTrim(path);
                if (content != null && (content.Contains("CPU") || content.Contains("GPU")))
                {
                    _systemInfoPaths["cpu_fan_special"] = path;
                    _systemInfoPaths["gpu_fan_special"] = path;
                    return;
                }
            }

            // Any hwmon exposing numeric fanN_input
            foreach (var hwmonDir in hwmonDirs)
            foreach (var fanName in new[] { "fan1_input", "fan2_input" })
            {
                var file = Path.Combine(hwmonDir, fanName);
                if (!int.TryParse(ReadTrim(file), NumberStyles.Integer, Inv, out _)) continue;

                if (!_systemInfoPaths.ContainsKey("cpu_fan"))
                    _systemInfoPaths["cpu_fan"] = file;
                else if (!_systemInfoPaths.ContainsKey("gpu_fan") && _systemInfoPaths["cpu_fan"] != file)
                    _systemInfoPaths["gpu_fan"] = file;
            }

            // NVIDIA: fan.speed via nvidia-smi (only probe while the dGPU is awake)
            if (_gpuType == GpuType.Nvidia && !_systemInfoPaths.ContainsKey("gpu_fan") && !IsNvidiaSuspended())
            {
                var nvidiaSmiOutput = RunCommand("nvidia-smi", "--query-gpu=fan.speed --format=csv,noheader");
                if (nvidiaSmiOutput.Contains('%'))
                    _systemInfoPaths["gpu_fan_nvidia_smi"] = "true";
            }

            if (_gpuType == GpuType.Amd && !_systemInfoPaths.ContainsKey("gpu_fan"))
            {
                var rocmSmiOutput = RunCommand("rocm-smi", "--showfan");
                if (rocmSmiOutput.Contains("Fan Speed (%)"))
                    _systemInfoPaths["gpu_fan_rocm_smi"] = "true";
            }

            if (!_systemInfoPaths.ContainsKey("cpu_fan"))
                _systemInfoPaths["cpu_fan_sensors"] = "fan1";

            if (!_systemInfoPaths.ContainsKey("gpu_fan"))
                _systemInfoPaths["gpu_fan_sensors"] = "fan2";
        }
        catch (Exception ex)
        {
            Console.WriteLine($"Error finding fan speed paths: {ex.Message}");
            _fanPathsSearched = true;
        }
    }

    /// <summary>Reads fan RPMs. Runs on a worker thread: returns values only, never sets properties.</summary>
    private (int cpuFan, int gpuFan) GetFanSpeeds()
    {
        try
        {
            if (!_fanPathsSearched) FindFanSpeedPaths();

            var cpuFanSpeed = 0;
            var gpuFanSpeed = 0;
            string? sensorsOutput = null;
            string Sensors() => sensorsOutput ??= RunCommand("sensors", "");

            if (_systemInfoPaths.TryGetValue("cpu_fan", out var cpuFanPath))
            {
                int.TryParse(ReadTrim(cpuFanPath), NumberStyles.Integer, Inv, out cpuFanSpeed);
            }
            else if (_systemInfoPaths.TryGetValue("cpu_fan_special", out var cpuSpecial))
            {
                var match = Regex.Match(ReadTrim(cpuSpecial) ?? "", @"CPU:?\s*(\d+)");
                if (match.Success) int.TryParse(match.Groups[1].Value, NumberStyles.Integer, Inv, out cpuFanSpeed);
            }
            else if (_systemInfoPaths.ContainsKey("cpu_fan_sensors"))
            {
                var match = Regex.Match(Sensors(), @"fan1:\s+(\d+) RPM");
                if (match.Success) int.TryParse(match.Groups[1].Value, NumberStyles.Integer, Inv, out cpuFanSpeed);
            }

            if (_systemInfoPaths.TryGetValue("gpu_fan", out var gpuFanPath))
            {
                int.TryParse(ReadTrim(gpuFanPath), NumberStyles.Integer, Inv, out gpuFanSpeed);
            }
            else if (_systemInfoPaths.TryGetValue("gpu_fan_special", out var gpuSpecial))
            {
                var match = Regex.Match(ReadTrim(gpuSpecial) ?? "", @"GPU:?\s*(\d+)");
                if (match.Success) int.TryParse(match.Groups[1].Value, NumberStyles.Integer, Inv, out gpuFanSpeed);
            }
            else if (_systemInfoPaths.ContainsKey("gpu_fan_nvidia_smi"))
            {
                if (!IsNvidiaSuspended())
                {
                    var match = Regex.Match(RunCommand("nvidia-smi", "--query-gpu=fan.speed --format=csv,noheader"),
                        @"(\d+)\s*%");
                    if (match.Success && int.TryParse(match.Groups[1].Value, NumberStyles.Integer, Inv, out var pct))
                        gpuFanSpeed = pct * 60; // rough % -> RPM approximation
                }
            }
            else if (_systemInfoPaths.ContainsKey("gpu_fan_rocm_smi"))
            {
                var match = Regex.Match(RunCommand("rocm-smi", "--showfan"), @"Fan Speed \(%\)\s*:\s*(\d+)");
                if (match.Success && int.TryParse(match.Groups[1].Value, NumberStyles.Integer, Inv, out var pct))
                    gpuFanSpeed = pct * 60; // rough % -> RPM approximation
            }
            else if (_systemInfoPaths.ContainsKey("gpu_fan_sensors"))
            {
                var match = Regex.Match(Sensors(), @"fan2:\s+(\d+) RPM");
                if (match.Success) int.TryParse(match.Groups[1].Value, NumberStyles.Integer, Inv, out gpuFanSpeed);
            }

            return (Math.Max(0, cpuFanSpeed), Math.Max(0, gpuFanSpeed));
        }
        catch (Exception ex)
        {
            Console.WriteLine($"Error in GetFanSpeeds: {ex.Message}");
            return (0, 0);
        }
    }

    private (int percentage, string status, double timeRemaining) GetBatteryInfo()
    {
        if (!_hasBatteryInternal) return (0, "No Battery", double.NaN);

        try
        {
            var percentage = 0;
            var status = "Unknown";
            var timeRemaining = double.NaN;

            if (_systemInfoPaths.TryGetValue("capacity", out var capacityPath) &&
                int.TryParse(ReadTrim(capacityPath), NumberStyles.Integer, Inv, out var capacity))
                percentage = Math.Clamp(capacity, 0, 100);

            if (_systemInfoPaths.TryGetValue("status", out var statusPath))
                status = ReadTrim(statusPath) ?? "Unknown";

            if (_systemInfoPaths.TryGetValue("energy_now", out var nowPath) &&
                _systemInfoPaths.TryGetValue("power_now", out var powerPath) &&
                _systemInfoPaths.TryGetValue("energy_full", out var fullPath) &&
                double.TryParse(ReadTrim(nowPath), NumberStyles.Float, Inv, out var energyNow) &&
                double.TryParse(ReadTrim(powerPath), NumberStyles.Float, Inv, out var powerNow) &&
                double.TryParse(ReadTrim(fullPath), NumberStyles.Float, Inv, out var energyFull) &&
                powerNow > 0)
            {
                if (status == "Discharging")
                    timeRemaining = energyNow / powerNow;
                else if (status == "Charging")
                    timeRemaining = Math.Max(0, energyFull - energyNow) / powerNow;
            }

            return (percentage, status, timeRemaining);
        }
        catch
        {
            return (0, "Error", double.NaN);
        }
    }

    private static string? FirstNonEmptyLine(string? text)
    {
        if (string.IsNullOrWhiteSpace(text)) return null;
        foreach (var raw in text.Split('\n'))
        {
            var line = raw.Trim();
            if (line.Length > 0) return line;
        }

        return null;
    }

    /// <summary>
    ///     Runs a tool and returns its stdout. Bounded by a timeout: on expiry the process tree is
    ///     killed and "" is returned. Tools that are not installed are remembered and not retried.
    /// </summary>
    private string RunCommand(string command, string arguments, int timeoutMs = CommandTimeoutMs)
    {
        lock (_missingCommands)
        {
            if (_missingCommands.Contains(command)) return string.Empty;
        }

        Process? process = null;
        try
        {
            process = new Process
            {
                StartInfo = new ProcessStartInfo
                {
                    FileName = command,
                    Arguments = arguments,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                    UseShellExecute = false,
                    CreateNoWindow = true
                }
            };

            try
            {
                if (!process.Start()) return string.Empty;
            }
            catch (Win32Exception)
            {
                lock (_missingCommands)
                {
                    _missingCommands.Add(command);
                }

                return string.Empty;
            }

            // Drain both pipes asynchronously so a chatty stderr cannot block the child.
            var stdout = process.StandardOutput.ReadToEndAsync();
            _ = process.StandardError.ReadToEndAsync();

            if (!process.WaitForExit(timeoutMs))
            {
                try
                {
                    process.Kill(true);
                }
                catch
                {
                    // already gone
                }

                Console.WriteLine($"'{command} {arguments}' timed out after {timeoutMs} ms and was killed");
                return string.Empty;
            }

            return stdout.Wait(500) ? stdout.Result : string.Empty;
        }
        catch
        {
            return string.Empty;
        }
        finally
        {
            process?.Dispose();
        }
    }

    protected bool SetProperty<T>(ref T field, T value, [CallerMemberName] string? propertyName = null)
    {
        if (EqualityComparer<T>.Default.Equals(field, value)) return false;
        field = value;
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(propertyName));
        return true;
    }

    protected virtual void OnPropertyChanged([CallerMemberName] string? propertyName = null)
    {
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(propertyName));
    }

    private sealed class StaticInfo
    {
        public string CpuName { get; set; } = "Unknown CPU";
        public string GpuName { get; set; } = "Unknown GPU";
        public string GpuDriver { get; set; } = "Unknown Driver";
        public string OsVersion { get; set; } = "";
        public string KernelVersion { get; set; } = "";
        public string RamTotal { get; set; } = "";
        public bool HasBattery { get; set; }
    }

    private sealed class MetricsData
    {
        public double CpuUsage { get; set; }
        public double CpuTemp { get; set; }
        public double RamUsage { get; set; }
        public double GpuTemp { get; set; } = double.NaN;
        public double GpuUsage { get; set; } = double.NaN;
        public bool GpuAsleep { get; set; }
        public int BatteryPercentage { get; set; }
        public string BatteryStatus { get; set; } = "Unknown";
        public double BatteryTimeRemainingHours { get; set; } = double.NaN;
        public int CpuFanSpeedRPM { get; set; }
        public int GpuFanSpeedRPM { get; set; }
    }

    private enum GpuType
    {
        Unknown,
        Nvidia,
        Amd,
        Intel
    }
}
