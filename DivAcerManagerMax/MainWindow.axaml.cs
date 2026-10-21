using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Runtime.CompilerServices;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Shapes;
using Avalonia.Input;
using Avalonia.Interactivity;
using Avalonia.Layout;
using Avalonia.Markup.Xaml;
using Avalonia.Media;
using Avalonia.Styling;
using Avalonia.Threading;
using Material.Icons;
using Material.Icons.Avalonia;
using MsBox.Avalonia;

namespace DivAcerManagerMax;

public partial class MainWindow : Window, INotifyPropertyChanged
{
    private const string ProjectVersion = "1.0.2";
    private const string DefaultEffectColor = "#0078D7";
    private const string DefaultZone1Color = "#4287f5";
    private const string DefaultZone2Color = "#ff5733";
    private const string DefaultZone3Color = "#33ff57";
    private const string DefaultZone4Color = "#ffff01";
    private const int DirectionLeftToRight = 1;
    private const int DirectionRightToLeft = 2;
    private const string AppDataFolderName = "DivAcerManagerMax";
    private const string KeyboardZonePresetFileName = "keyboard-zone-colors.conf";
    private const string KeyboardLightingEffectPresetFileName = "keyboard-lighting-effect.conf";
    private const string QuietProfile = "quiet";

    private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

    private static readonly string AppDataFolderPath =
        System.IO.Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
            AppDataFolderName
        );

    private static readonly string KeyboardZonePresetPath =
        System.IO.Path.Combine(AppDataFolderPath, KeyboardZonePresetFileName);

    private static readonly string KeyboardLightingEffectPresetPath =
        System.IO.Path.Combine(AppDataFolderPath, KeyboardLightingEffectPresetFileName);

    /// <summary>
    ///     Presentation of the platform_profile names the kernel may report. Only the names the daemon
    ///     actually reports are turned into buttons; unknown names get a generic segment.
    /// </summary>
    private static readonly Dictionary<string, ProfileInfo> KnownProfiles = new(StringComparer.OrdinalIgnoreCase)
    {
        ["low-power"] = new ProfileInfo("Low Power", MaterialIconKind.Leaf,
            "Prioritizes energy efficiency, reduces performance to extend battery life.", true, false),
        ["cool"] = new ProfileInfo("Cool", MaterialIconKind.Fan,
            "Keeps the chassis cool by limiting power.", true, true),
        ["quiet"] = new ProfileInfo("Quiet", MaterialIconKind.FanMinus,
            "Minimizes noise, prioritizes low power and cooling.", false, true),
        ["balanced"] = new ProfileInfo("Balanced", MaterialIconKind.ScaleBalance,
            "Optimal mix of performance and noise for everyday tasks.", true, true),
        ["balanced-performance"] = new ProfileInfo("Performance", MaterialIconKind.SpeedometerMedium,
            "Maximizes speed for demanding workloads, higher fan noise.", false, true),
        ["performance"] = new ProfileInfo("Turbo", MaterialIconKind.RocketLaunch,
            "Unleashes peak power for extreme tasks, loudest fans.", false, true),
        ["custom"] = new ProfileInfo("Custom", MaterialIconKind.TuneVariant,
            "Firmware custom profile.", true, true)
    };

    private readonly DispatcherTimer _toastTimer;

    // UI Controls (bound via the window's and the dashboard's name scopes)
    private Button? _applyKeyboardColorsButton;
    private RadioButton? _autoFanSpeedRadioButton;
    private CheckBox? _backlightTimeoutCheckBox;
    private CheckBox? _batteryLimitCheckBox;
    private CheckBox? _bootAnimAndSoundCheckBox;
    private TextBlock? _calibrationStatusTextBlock;
    public DAMXClient _client;
    private Slider? _cpuFanSlider;
    private int _cpuFanSpeed = 50;
    private TextBlock? _cpuFanTextBlock;
    private string _currentProfile = "";
    private Grid? _daemonErrorGrid;
    private TextBlock? _daemonErrorMessage;
    private TextBlock? _daemonErrorTitle;
    private Ellipse? _daemonStatusDot;
    private TextBlock? _daemonStatusText;
    private TextBlock? _daemonVersionText;
    private Dashboard? _dashboard;
    private INameScope? _dashScope;
    private TextBlock? _driverVersionText;
    private Slider? _gpuFanSlider;
    private int _gpuFanSpeed = 70;
    private TextBlock? _gpuFanTextBlock;
    private TextBlock? _guiVersionTextBlock;
    private TextBlock? _headerModelText;
    private bool _applyingSettings;
    private bool _isCalibrating;
    private bool _isConnected;
    private bool _isManualFanControl;
    private bool _isSettingFanSpeed;
    private int _keyboardBrightness = 100;
    private Slider? _keyBrightnessSlider;
    private TextBlock? _keyBrightnessText;
    private TextBlock? _laptopTypeText;
    private CheckBox? _lcdOverrideCheckBox;
    private RadioButton? _leftToRightRadioButton;
    private ColorPicker? _lightEffectColorPicker;
    private Button? _lightingEffectsApplyButton;
    private ComboBox? _lightingModeComboBox;
    private int _lightingSpeed = 5;
    private Slider? _lightingSpeedSlider;
    private TextBlock? _lightSpeedTextBlock;
    private INameScope? _mainScope;
    private TextBlock? _manualDisabledInfoText;
    private RadioButton? _manualFanSpeedRadioButton;
    private RadioButton? _maxFanSpeedRadioButton;
    private string _modelName = "Unknown";
    private TextBlock? _modelNameText;
    private PowerSourceDetection? _powerDetection;
    private ToggleSwitch? _powerToggleSwitch;
    private Panel? _profileButtonsPanel;
    private bool _profileChangeInFlight;
    private bool _reloadInFlight;
    private RadioButton? _rightToLeftRadioButton;
    private Button? _setManualSpeedButton;
    public DAMXSettings? _settings;
    private Button? _startCalibrationButton;
    private HudPanel? _statusToast;
    private Ellipse? _statusToastDot;
    private TextBlock? _statusToastText;
    private Button? _stopCalibrationButton;
    private TextBlock? _supportedFeaturesTextBlock;
    private TextBlock? _thermalProfileInfoText;
    private ComboBox? _usbChargingComboBox;
    private ColorPicker? _zone1ColorPicker;
    private ColorPicker? _zone2ColorPicker;
    private ColorPicker? _zone3ColorPicker;
    private ColorPicker? _zone4ColorPicker;

    public MainWindow()
    {
        InitializeComponent();
        DataContext = this;
        _client = new DAMXClient();

        _toastTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(5) };
        _toastTimer.Tick += (_, _) =>
        {
            _toastTimer.Stop();
            if (_statusToast != null) _statusToast.IsVisible = false;
        };

        Loaded += MainWindow_Loaded;
    }

    public bool IsCalibrating
    {
        get => _isCalibrating;
        set => SetField(ref _isCalibrating, value);
    }

    private void InitializeComponent()
    {
        AvaloniaXamlLoader.Load(this);
    }

    private void MainWindow_Loaded(object? sender, RoutedEventArgs e)
    {
        try
        {
            BindControls();
            AttachEventHandlers();

            _modelName = GetLinuxLaptopModel();
            SetText(_headerModelText, _modelName.ToUpperInvariant());
            SetText(_modelNameText, _modelName);
            UpdateProfileButtons();
        }
        catch (Exception ex)
        {
            Console.WriteLine($"Error while setting up the window: {ex}");
        }

        InitializeAsync();
    }

    protected override void OnClosed(EventArgs e)
    {
        base.OnClosed(e);
        _toastTimer.Stop();
        _powerDetection?.Dispose();
        _powerDetection = null;
        _client.Dispose();
    }

    // ------------------------------------------------------------------ control lookup

    /// <summary>Finds a named control in the window's scope, then in the dashboard's own scope.</summary>
    private T? FindInScopes<T>(string name) where T : class
    {
        return _mainScope?.Find<T>(name) ?? _dashScope?.Find<T>(name);
    }

    private void BindControls()
    {
        _mainScope = this.FindNameScope();
        _dashboard = _mainScope?.Find<Dashboard>("DashboardView");
        _dashScope = _dashboard != null ? NameScope.GetNameScope(_dashboard) : null;

        // Header / overlay / toast
        _headerModelText = FindInScopes<TextBlock>("HeaderModelText");
        _daemonStatusDot = FindInScopes<Ellipse>("DaemonStatusDot");
        _daemonStatusText = FindInScopes<TextBlock>("DaemonStatusText");
        _daemonErrorGrid = FindInScopes<Grid>("DaemonErrorGrid");
        _daemonErrorTitle = FindInScopes<TextBlock>("DaemonErrorTitle");
        _daemonErrorMessage = FindInScopes<TextBlock>("DaemonErrorMessage");
        _statusToast = FindInScopes<HudPanel>("StatusToast");
        _statusToastDot = FindInScopes<Ellipse>("StatusToastDot");
        _statusToastText = FindInScopes<TextBlock>("StatusToastText");

        // Thermal profile (dashboard)
        _profileButtonsPanel = FindInScopes<Panel>("ProfileButtonsPanel");
        _powerToggleSwitch = FindInScopes<ToggleSwitch>("PluggedInToggleSwitch");
        _thermalProfileInfoText = FindInScopes<TextBlock>("ThermalProfileInfoText");

        // Fan control (dashboard)
        _autoFanSpeedRadioButton = FindInScopes<RadioButton>("AutoFanSpeedRadioButton");
        _maxFanSpeedRadioButton = FindInScopes<RadioButton>("MaxFanSpeedRadioButton");
        _manualFanSpeedRadioButton = FindInScopes<RadioButton>("ManualFanSpeedRadioButton");
        _cpuFanSlider = FindInScopes<Slider>("CpuFanSlider");
        _gpuFanSlider = FindInScopes<Slider>("GpuFanSlider");
        _cpuFanTextBlock = FindInScopes<TextBlock>("CpuFanTextBlock");
        _gpuFanTextBlock = FindInScopes<TextBlock>("GpuFanTextBlock");
        _setManualSpeedButton = FindInScopes<Button>("SetManualSpeedButton");
        _manualDisabledInfoText = FindInScopes<TextBlock>("ManualDisabledInfoText");

        // Battery
        _startCalibrationButton = FindInScopes<Button>("StartCalibrationButton");
        _stopCalibrationButton = FindInScopes<Button>("StopCalibrationButton");
        _calibrationStatusTextBlock = FindInScopes<TextBlock>("CalibrationStatusTextBlock");
        _batteryLimitCheckBox = FindInScopes<CheckBox>("BatteryLimitCheckBox");
        _usbChargingComboBox = FindInScopes<ComboBox>("UsbChargingComboBox");

        // Keyboard lighting zones
        _zone1ColorPicker = FindInScopes<ColorPicker>("Zone1ColorPicker");
        _zone2ColorPicker = FindInScopes<ColorPicker>("Zone2ColorPicker");
        _zone3ColorPicker = FindInScopes<ColorPicker>("Zone3ColorPicker");
        _zone4ColorPicker = FindInScopes<ColorPicker>("Zone4ColorPicker");
        _keyBrightnessSlider = FindInScopes<Slider>("KeyBrightnessSlider");
        _keyBrightnessText = FindInScopes<TextBlock>("KeyBrightnessText");
        _applyKeyboardColorsButton = FindInScopes<Button>("ApplyKeyboardColorsButton");

        // Lighting effects
        _lightingModeComboBox = FindInScopes<ComboBox>("LightingModeComboBox");
        _lightingSpeedSlider = FindInScopes<Slider>("LightingSpeedSlider");
        _lightSpeedTextBlock = FindInScopes<TextBlock>("LightSpeedTextBlock");
        _lightEffectColorPicker = FindInScopes<ColorPicker>("LightEffectColorPicker");
        _leftToRightRadioButton = FindInScopes<RadioButton>("LeftToRightRadioButton");
        _rightToLeftRadioButton = FindInScopes<RadioButton>("RightToLeftRadioButton");
        _lightingEffectsApplyButton = FindInScopes<Button>("LightingEffectsApplyButton");

        // System settings
        _backlightTimeoutCheckBox = FindInScopes<CheckBox>("BacklightTimeoutCheckBox");
        _lcdOverrideCheckBox = FindInScopes<CheckBox>("LcdOverrideCheckBox");
        _bootAnimAndSoundCheckBox = FindInScopes<CheckBox>("BootAnimAndSoundCheckBox");

        // Info texts
        _modelNameText = FindInScopes<TextBlock>("ModelNameText");
        _laptopTypeText = FindInScopes<TextBlock>("LaptopTypeText");
        _supportedFeaturesTextBlock = FindInScopes<TextBlock>("SupportedFeaturesTextBlock");
        _daemonVersionText = FindInScopes<TextBlock>("DaemonVersionText");
        _driverVersionText = FindInScopes<TextBlock>("DriverVersionText");
        _guiVersionTextBlock = FindInScopes<TextBlock>("ProjectVersionText");

        SetText(_guiVersionTextBlock, $"v{ProjectVersion}");
    }

    private void AttachEventHandlers()
    {
        // Power source indicator: rebuild the profile segments when AC state changes
        if (_powerToggleSwitch != null)
        {
            _powerDetection = new PowerSourceDetection(_powerToggleSwitch);
            _powerToggleSwitch.PropertyChanged += (_, args) =>
            {
                if (args.Property.Name == "IsChecked") UpdateProfileButtons();
            };
        }

        // Fan control (each handler wired exactly once, here)
        if (_autoFanSpeedRadioButton != null) _autoFanSpeedRadioButton.Click += AutoFanSpeedRadioButtonClick;
        if (_maxFanSpeedRadioButton != null) _maxFanSpeedRadioButton.Click += MaxFanSpeedRadioButton_OnClick;
        if (_manualFanSpeedRadioButton != null) _manualFanSpeedRadioButton.Click += ManualFanControlRadioBox_Click;
        if (_cpuFanSlider != null) _cpuFanSlider.PropertyChanged += CpuFanSlider_ValueChanged;
        if (_gpuFanSlider != null) _gpuFanSlider.PropertyChanged += GpuFanSlider_ValueChanged;
        if (_setManualSpeedButton != null) _setManualSpeedButton.Click += SetManualSpeedButton_OnClick;

        // Battery
        if (_startCalibrationButton != null) _startCalibrationButton.Click += StartCalibrationButton_Click;
        if (_stopCalibrationButton != null) _stopCalibrationButton.Click += StopCalibrationButton_Click;
        if (_batteryLimitCheckBox != null) _batteryLimitCheckBox.Click += BatteryLimitCheckBox_Click;

        // Keyboard lighting
        if (_keyBrightnessSlider != null) _keyBrightnessSlider.PropertyChanged += KeyboardBrightnessSlider_ValueChanged;
        if (_applyKeyboardColorsButton != null) _applyKeyboardColorsButton.Click += ApplyKeyboardColorsButton_Click;
        if (_lightingSpeedSlider != null) _lightingSpeedSlider.PropertyChanged += LightingSpeedSlider_ValueChanged;
        if (_lightingEffectsApplyButton != null) _lightingEffectsApplyButton.Click += LightingEffectsApplyButton_Click;

        // System settings
        if (_backlightTimeoutCheckBox != null) _backlightTimeoutCheckBox.Click += BacklightTimeoutCheckBox_Click;
        if (_lcdOverrideCheckBox != null) _lcdOverrideCheckBox.Click += LcdOverrideCheckBox_Click;
        if (_bootAnimAndSoundCheckBox != null) _bootAnimAndSoundCheckBox.Click += BootSoundCheckBox_Click;
    }

    // ------------------------------------------------------------------ error handling / status

    /// <summary>
    ///     Runs a UI action that talks to the daemon. Never throws: errors are logged and shown in a
    ///     non-blocking toast; the header status chip is refreshed. Returns true on success.
    /// </summary>
    private async Task<bool> RunSafe(Func<Task> action, string context)
    {
        try
        {
            await action();
            RefreshDaemonStatusChip();
            return true;
        }
        catch (DaemonPermissionException ex)
        {
            Console.Error.WriteLine($"[{context}] {ex.Message}");
            _isConnected = false;
            SetDaemonStatus(DaemonConnectionState.PermissionDenied);
            ShowStatus(ex.Message, true);
        }
        catch (DaemonCommandException ex)
        {
            Console.Error.WriteLine($"[{context}] {ex.Message}");
            RefreshDaemonStatusChip();
            ShowStatus(ex.Message, true);
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[{context}] {ex}");
            RefreshDaemonStatusChip();
            ShowStatus($"{context}: {ex.Message}", true);
        }

        return false;
    }

    /// <summary>Like RunSafe; on failure re-reads the daemon state so the UI shows the truth again.</summary>
    private async Task RunDaemonAction(string context, Func<Task> action, bool resyncOnFailure = true)
    {
        var ok = await RunSafe(action, context);
        if (!ok && resyncOnFailure && _client.IsConnected)
            await ReloadSettingsQuietlyAsync();
    }

    private static void EnsureSuccess(DaemonResult result, string what)
    {
        if (result.Success) return;
        var error = string.IsNullOrWhiteSpace(result.Error) ? "unknown error" : result.Error;
        throw new DaemonCommandException($"{what} failed: {error}", result.IsPermissionDenied);
    }

    /// <summary>Shows a short non-blocking message in the bottom-right corner.</summary>
    public void ShowStatus(string message, bool isError)
    {
        try
        {
            Console.WriteLine(isError ? $"[error] {message}" : $"[info] {message}");
            if (_statusToast == null || _statusToastText == null) return;

            _statusToastText.Text = message;
            if (isError)
            {
                if (!_statusToast.Classes.Contains("Error")) _statusToast.Classes.Add("Error");
            }
            else
            {
                _statusToast.Classes.Remove("Error");
            }

            if (_statusToastDot != null) _statusToastDot.Fill = isError ? HudPalette.DangerBrush : HudPalette.OkBrush;

            _statusToast.IsVisible = true;
            _toastTimer.Stop();
            _toastTimer.Interval = TimeSpan.FromSeconds(isError ? 9 : 4);
            _toastTimer.Start();
        }
        catch (Exception ex)
        {
            Console.WriteLine($"Could not show status message: {ex.Message}");
        }
    }

    private void RefreshDaemonStatusChip()
    {
        _isConnected = _client.IsConnected;
        SetDaemonStatus(_client.State);
    }

    private void SetDaemonStatus(DaemonConnectionState state)
    {
        var (text, brush) = state switch
        {
            DaemonConnectionState.Connected => ("ONLINE", HudPalette.OkBrush),
            DaemonConnectionState.PermissionDenied => ("NO PERMISSION", HudPalette.DangerBrush),
            DaemonConnectionState.Disconnected => ("OFFLINE", HudPalette.InkMutedBrush),
            _ => ("OFFLINE", HudPalette.DangerBrush)
        };

        SetText(_daemonStatusText, text);
        if (_daemonStatusDot != null) _daemonStatusDot.Fill = brush;
    }

    private void ShowDaemonError(DaemonConnectionState state, string? detail)
    {
        if (state == DaemonConnectionState.PermissionDenied)
        {
            SetText(_daemonErrorTitle, "NO PERMISSION");
            SetText(_daemonErrorMessage, DaemonPermissionException.UserMessage);
        }
        else
        {
            SetText(_daemonErrorTitle, "DAEMON OFFLINE");
            SetText(_daemonErrorMessage,
                "Could not reach the DAMX daemon." +
                (string.IsNullOrWhiteSpace(detail) ? "" : $"\n{detail}"));
        }

        if (_daemonErrorGrid != null) _daemonErrorGrid.IsVisible = true;
    }

    // ------------------------------------------------------------------ connection / settings

    public async void InitializeAsync()
    {
        await ReloadFromDaemonAsync();
    }

    /// <summary>(Re)connects, reloads every setting and refreshes the UI. Never throws.</summary>
    public async Task<bool> ReloadFromDaemonAsync()
    {
        if (_reloadInFlight) return _isConnected;
        _reloadInFlight = true;

        try
        {
            SetText(_daemonStatusText, "CONNECTING");
            if (_daemonStatusDot != null) _daemonStatusDot.Fill = HudPalette.InkMutedBrush;

            _isConnected = await _client.ConnectAsync();
            SetDaemonStatus(_client.State);

            if (_isConnected)
            {
                if (_daemonErrorGrid != null) _daemonErrorGrid.IsVisible = false;
                await LoadSettingsAsync();
                return true;
            }

            ShowDaemonError(_client.State, _client.LastError);
            return false;
        }
        catch (Exception ex)
        {
            Console.WriteLine($"Error initializing: {ex}");
            SetDaemonStatus(DaemonConnectionState.Error);
            ShowDaemonError(DaemonConnectionState.Error, ex.Message);
            return false;
        }
        finally
        {
            _reloadInFlight = false;
        }
    }

    private async Task LoadSettingsAsync()
    {
        try
        {
            _settings = await _client.GetAllSettingsAsync();
        }
        catch (Exception ex)
        {
            Console.WriteLine($"Error loading settings: {ex}");
            RefreshDaemonStatusChip();
            ShowStatus($"Could not load settings: {ex.Message}", true);
            _settings ??= new DAMXSettings();
        }

        ApplySettingsToUI();
    }

    private async Task ReloadSettingsQuietlyAsync()
    {
        try
        {
            _settings = await _client.GetAllSettingsAsync();
            ApplySettingsToUI();
        }
        catch (Exception ex)
        {
            Console.WriteLine($"Could not re-sync settings: {ex.Message}");
            RefreshDaemonStatusChip();
        }
    }

    /// <summary>Updates the cached modprobe parameter (used by the Internals window after a re-read).</summary>
    public void UpdateModprobeParameter(string parameter)
    {
        _settings ??= new DAMXSettings();
        _settings.ModprobeParameter = parameter;
    }

    // ------------------------------------------------------------------ thermal profiles

    private sealed record ProfileInfo(
        string Label,
        MaterialIconKind Icon,
        string Description,
        bool ShowOnBattery,
        bool ShowOnAc);

    private static ProfileInfo GetProfileInfo(string profile)
    {
        return KnownProfiles.TryGetValue(profile, out var info)
            ? info
            : new ProfileInfo(TitleCase(profile), MaterialIconKind.Speedometer,
                $"Firmware profile '{profile}'.", true, true);
    }

    /// <summary>"balanced-performance" -> "Balanced Performance" for names the GUI does not know.</summary>
    private static string TitleCase(string name)
    {
        var words = name.Split(new[] { '-', '_', ' ' }, StringSplitOptions.RemoveEmptyEntries)
            .Select(w => char.ToUpperInvariant(w[0]) + w.Substring(1).ToLowerInvariant());
        var result = string.Join(" ", words);
        return result.Length > 0 ? result : name;
    }

    /// <summary>Builds the segmented control from the profiles the daemon reported (never assumes any exist).</summary>
    private void UpdateProfileButtons()
    {
        if (_profileButtonsPanel == null) return;

        var available = _settings?.ThermalProfile?.Available ?? new List<string>();
        var current = _settings?.ThermalProfile?.Current?.Trim() ?? "";
        var isPluggedIn = _powerToggleSwitch?.IsChecked ?? true;

        var toShow = available
            .Where(p => !string.IsNullOrWhiteSpace(p))
            .Select(p => p.Trim())
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .Where(p =>
            {
                if (AppState.DevMode || string.Equals(p, current, StringComparison.OrdinalIgnoreCase)) return true;
                var info = GetProfileInfo(p);
                return isPluggedIn ? info.ShowOnAc : info.ShowOnBattery;
            })
            .ToList();

        LayoutProfileButtons(toShow);

        _currentProfile = current;
        SelectProfileButton(current);

        if (_settings == null)
            SetText(_thermalProfileInfoText, "Connect to the daemon to load the available profiles.");
        else if (toShow.Count == 0)
            SetText(_thermalProfileInfoText, "The firmware reported no thermal profiles.");
        else
            UpdateProfileInfoText(current);

        UpdateFanModeAvailability();
    }

    /// <summary>
    ///     Places the segments in the ProfileButtonsPanel grid: one row for up to 3 profiles,
    ///     otherwise 2 rows (4 -> 2x2, 5+ -> 3 columns), so full names fit in the 420 px card.
    /// </summary>
    private void LayoutProfileButtons(List<string> profiles)
    {
        if (_profileButtonsPanel == null) return;
        _profileButtonsPanel.Children.Clear();

        var count = profiles.Count;
        var columns = count <= 3 ? Math.Max(count, 1) : count == 4 ? 2 : 3;
        var rows = (count + columns - 1) / columns;

        if (_profileButtonsPanel is Grid grid)
        {
            grid.ColumnDefinitions.Clear();
            grid.RowDefinitions.Clear();
            for (var c = 0; c < columns; c++) grid.ColumnDefinitions.Add(new ColumnDefinition(1, GridUnitType.Star));
            for (var r = 0; r < rows; r++) grid.RowDefinitions.Add(new RowDefinition(GridLength.Auto));
        }

        for (var i = 0; i < count; i++)
        {
            var row = i / columns;
            var column = i % columns;
            var button = CreateProfileButton(profiles[i]);
            button.Margin = new Thickness(0, 0, column == columns - 1 ? 0 : 6, row == rows - 1 ? 0 : 6);
            Grid.SetRow(button, row);
            Grid.SetColumn(button, column);
            _profileButtonsPanel.Children.Add(button);
        }
    }

    private RadioButton CreateProfileButton(string profile)
    {
        var info = GetProfileInfo(profile);

        // Icon left of the text; both inherit the segment's Foreground
        // (off-white, near-black on the checked accent fill).
        var content = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            Spacing = 6,
            HorizontalAlignment = HorizontalAlignment.Center
        };
        content.Children.Add(new MaterialIcon
        {
            Kind = info.Icon,
            Width = 16,
            Height = 16,
            VerticalAlignment = VerticalAlignment.Center
        });
        content.Children.Add(new TextBlock
        {
            Text = info.Label,
            VerticalAlignment = VerticalAlignment.Center,
            TextTrimming = TextTrimming.CharacterEllipsis
        });

        var button = new RadioButton
        {
            GroupName = "ProfileGroup",
            Tag = profile,
            Content = content,
            Padding = new Thickness(8, 6),
            HorizontalAlignment = HorizontalAlignment.Stretch
        };

        if (this.TryFindResource("SegmentRadio", out var theme) && theme is ControlTheme controlTheme)
            button.Theme = controlTheme;
        ToolTip.SetTip(button, info.Description);
        button.Click += ProfileButton_Click;
        return button;
    }

    private void SelectProfileButton(string profile)
    {
        if (_profileButtonsPanel == null) return;
        foreach (var child in _profileButtonsPanel.Children)
            if (child is RadioButton rb)
                rb.IsChecked = rb.Tag is string tag &&
                               string.Equals(tag, profile, StringComparison.OrdinalIgnoreCase);
    }

    private void UpdateProfileInfoText(string profile)
    {
        if (string.IsNullOrWhiteSpace(profile))
        {
            SetText(_thermalProfileInfoText, "Current profile unknown.");
            return;
        }

        var info = GetProfileInfo(profile);
        SetText(_thermalProfileInfoText, $"{info.Label} — {info.Description}");
    }

    /// <summary>Quiet profile: fans stay automatic (manual / max disabled) unless dev mode is on.</summary>
    private void UpdateFanModeAvailability()
    {
        var restricted = !AppState.DevMode &&
                         string.Equals(_currentProfile, QuietProfile, StringComparison.OrdinalIgnoreCase);
        SetEnabled(_maxFanSpeedRadioButton, !restricted);
        SetEnabled(_manualFanSpeedRadioButton, !restricted);
        if (_manualDisabledInfoText != null) _manualDisabledInfoText.IsVisible = restricted;
    }

    private async void ProfileButton_Click(object? sender, RoutedEventArgs e)
    {
        if (_applyingSettings || sender is not RadioButton button || button.Tag is not string profile) return;

        if (string.Equals(profile, _currentProfile, StringComparison.OrdinalIgnoreCase))
        {
            button.IsChecked = true;
            return;
        }

        if (_profileChangeInFlight)
        {
            SelectProfileButton(_currentProfile);
            return;
        }

        _profileChangeInFlight = true;
        var previous = _currentProfile;

        try
        {
            var ok = await RunSafe(async () =>
            {
                var result = await _client.SetThermalProfileAsync(profile);
                EnsureSuccess(result, $"Thermal profile '{profile}'");

                _currentProfile = profile;
                if (_settings?.ThermalProfile != null) _settings.ThermalProfile.Current = profile;
                UpdateProfileInfoText(profile);
                UpdateFanModeAvailability();

                if (string.Equals(profile, QuietProfile, StringComparison.OrdinalIgnoreCase) && !AppState.DevMode)
                {
                    // Quiet keeps the fans on automatic control.
                    EnsureSuccess(await _client.SetFanSpeedAsync(0, 0), "Automatic fan mode");
                    _isManualFanControl = false;
                    SetRadioButton(_autoFanSpeedRadioButton, true);
                }

                ShowStatus($"Thermal profile: {GetProfileInfo(profile).Label}", false);
            }, "Thermal profile");

            if (!ok)
            {
                // Revert to what the firmware is really using.
                _currentProfile = previous;
                SelectProfileButton(previous);
                UpdateProfileInfoText(previous);
                UpdateFanModeAvailability();
            }

            if (_client.IsConnected)
            {
                await Task.Delay(800);
                await ReloadSettingsQuietlyAsync();
            }
        }
        finally
        {
            _profileChangeInFlight = false;
        }
    }

    // ------------------------------------------------------------------ apply settings

    private void ApplySettingsToUI()
    {
        if (_settings == null) return;

        _applyingSettings = true;
        try
        {
            UpdateProfileButtons();

            SetCheckBox(_backlightTimeoutCheckBox, IsEnabledSetting(_settings.BacklightTimeout));
            var limiterOn = IsEnabledSetting(_settings.BatteryLimiter);
            SetCheckBox(_batteryLimitCheckBox, limiterOn);
            _dashboard?.SetBatteryLimiterState(_client.IsFeatureAvailable("battery_limiter") ? limiterOn : null);

            var isCalibrating = IsEnabledSetting(_settings.BatteryCalibration);
            IsCalibrating = isCalibrating;
            SetEnabled(_startCalibrationButton, !isCalibrating);
            SetEnabled(_stopCalibrationButton, isCalibrating);
            SetText(_calibrationStatusTextBlock, isCalibrating ? "Status: Calibrating" : "Status: Not calibrating");

            SetCheckBox(_bootAnimAndSoundCheckBox, IsEnabledSetting(_settings.BootAnimationSound));
            SetCheckBox(_lcdOverrideCheckBox, IsEnabledSetting(_settings.LcdOverride));

            if (_usbChargingComboBox != null)
                _usbChargingComboBox.SelectedIndex = GetUsbChargingIndex(_settings.UsbCharging);

            ApplyFanModeFromSettings();
            ApplyKeyboardSettings();

            SetText(_keyBrightnessText, $"{_keyboardBrightness}%");
            SetText(_lightSpeedTextBlock, _lightingSpeed.ToString(Inv));
            SetText(_daemonVersionText, $"v{_settings.Version}");
            SetText(_driverVersionText, $"v{_settings.DriverVersion}");
            SetText(_laptopTypeText, _settings.LaptopType);
            SetText(_supportedFeaturesTextBlock, string.Join(", ", _settings.AvailableFeatures ?? new List<string>()));
            SetText(_modelNameText, _modelName);

            UpdateUIElementVisibility();
        }
        finally
        {
            _applyingSettings = false;
        }
    }

    /// <summary>
    ///     Initializes the fan-mode segments from the daemon's reported fan_speed ("cpu","gpu"):
    ///     0,0 = auto, 100,100 = max, anything else = manual (fixes "UI shows Auto while fans are at Max").
    /// </summary>
    private void ApplyFanModeFromSettings()
    {
        if (_settings == null) return;

        var cpuSpeed = ApplyFanSpeed(_settings.FanSpeed?.Cpu, ref _cpuFanSpeed, _cpuFanSlider, _cpuFanTextBlock);
        var gpuSpeed = ApplyFanSpeed(_settings.FanSpeed?.Gpu, ref _gpuFanSpeed, _gpuFanSlider, _gpuFanTextBlock);

        var isAutoMode = cpuSpeed == 0 && gpuSpeed == 0;
        var isMaxMode = cpuSpeed >= 100 && gpuSpeed >= 100;
        var isManualMode = !isAutoMode && !isMaxMode;

        _isManualFanControl = isManualMode;
        SetRadioButton(_autoFanSpeedRadioButton, isAutoMode);
        SetRadioButton(_maxFanSpeedRadioButton, isMaxMode);
        SetRadioButton(_manualFanSpeedRadioButton, isManualMode);
    }

    private void UpdateUIElementVisibility()
    {
        if (_settings == null) return;

        bool Has(string feature)
        {
            return _client.IsFeatureAvailable(feature) || AppState.DevMode;
        }

        SetVisible(FindInScopes<Control>("ThermalProfilePanel"), Has("thermal_profile"));
        SetVisible(FindInScopes<Control>("FanControlPanel"), Has("fan_speed"));

        SetVisible(FindInScopes<Control>("BatteryPanel"), Has("battery_calibration") || Has("battery_limiter") ||
                                                          Has("usb_charging"));
        SetVisible(FindInScopes<Control>("CalibrationControls"), Has("battery_calibration"));
        SetVisible(FindInScopes<Control>("LimiterControls"), Has("battery_limiter"));
        SetVisible(FindInScopes<Control>("UsbChargingPanel"), Has("usb_charging"));

        SetVisible(FindInScopes<Control>("KeyboardLightingPanel"),
            Has("backlight_timeout") || Has("per_zone_mode") || Has("four_zone_mode"));
        SetVisible(FindInScopes<Control>("ZoneColorControlPanel"), Has("per_zone_mode"));
        SetVisible(FindInScopes<Control>("KeyboardEffectsPanel"), Has("four_zone_mode"));
        SetVisible(FindInScopes<Control>("BacklightTimeoutControls"), Has("backlight_timeout"));

        SetVisible(FindInScopes<Control>("LcdOverrideControls"), Has("lcd_override"));
        SetVisible(FindInScopes<Control>("BootSoundControls"), Has("boot_animation_sound"));
    }

    private static void SetVisible(Control? control, bool visible)
    {
        if (control != null) control.IsVisible = visible;
    }

    private static bool IsEnabledSetting(string? value)
    {
        return (value ?? "0").Trim().Equals("1", StringComparison.OrdinalIgnoreCase);
    }

    private static int GetUsbChargingIndex(string? value)
    {
        return value?.Trim() switch
        {
            "10" => 1,
            "20" => 2,
            "30" => 3,
            _ => 0
        };
    }

    private static int ApplyFanSpeed(
        string? value,
        ref int backingField,
        Slider? slider,
        TextBlock? textBlock)
    {
        if (!int.TryParse((value ?? "0").Trim(), NumberStyles.Integer, Inv, out var speed))
            return 0;

        speed = Math.Clamp(speed, 0, 100);
        backingField = speed;

        if (slider != null)
            slider.Value = speed;

        SetText(textBlock, FormatFanSpeed(speed));

        return speed;
    }

    private static string FormatFanSpeed(int speed)
    {
        return speed == 0 ? "AUTO" : $"{speed.ToString(Inv)}%";
    }

    private static void SetCheckBox(CheckBox? checkBox, bool value)
    {
        if (checkBox != null)
            checkBox.IsChecked = value;
    }

    private static void SetRadioButton(RadioButton? radioButton, bool value)
    {
        if (radioButton != null)
            radioButton.IsChecked = value;
    }

    private static void SetEnabled(Control? control, bool value)
    {
        if (control != null)
            control.IsEnabled = value;
    }

    private static void SetText(TextBlock? textBlock, string? value)
    {
        if (textBlock != null)
            textBlock.Text = value ?? string.Empty;
    }

    private static string GetLinuxLaptopModel()
    {
        try
        {
            const string productName = "/sys/class/dmi/id/product_name";
            if (File.Exists(productName))
            {
                var model = File.ReadAllText(productName).Trim();
                if (model.Length > 0) return model;
            }

            using var process = Process.Start(new ProcessStartInfo
            {
                FileName = "dmidecode",
                Arguments = "-s system-product-name",
                RedirectStandardOutput = true,
                UseShellExecute = false,
                CreateNoWindow = true
            });
            if (process == null) return "Unknown";

            var output = process.StandardOutput.ReadToEndAsync();
            if (!process.WaitForExit(2000))
            {
                try
                {
                    process.Kill(true);
                }
                catch
                {
                    // ignore
                }

                return "Unknown";
            }

            var text = output.Wait(500) ? output.Result.Trim() : "";
            return text.Length > 0 ? text : "Unknown";
        }
        catch (Exception ex)
        {
            Console.WriteLine($"Error getting laptop model: {ex.Message}");
            return "Unknown";
        }
    }

    private void ApplyKeyboardSettings()
    {
        if (_settings == null || !_settings.HasFourZoneKb)
            return;

        ApplySavedZonePresetToUI();

        if (!HasSavedZonePreset())
            ApplyPerZoneSettingsToUI();

        ApplyFourZoneSettingsToUI();
        ApplySavedLightingEffectPresetToUI();
    }

    private void ApplyPerZoneSettingsToUI()
    {
        if (!TryParsePerZoneMode(
                _settings?.PerZoneMode,
                out var zone1,
                out var zone2,
                out var zone3,
                out var zone4,
                out var brightness))
            return;

        SetColorPicker(_zone1ColorPicker, zone1);
        SetColorPicker(_zone2ColorPicker, zone2);
        SetColorPicker(_zone3ColorPicker, zone3);
        SetColorPicker(_zone4ColorPicker, zone4);

        SetKeyboardBrightness(brightness);
    }

    private void ApplyFourZoneSettingsToUI()
    {
        if (!TryParseFourZoneMode(
                _settings?.FourZoneMode,
                out var mode,
                out var speed,
                out var brightness,
                out var direction,
                out var red,
                out var green,
                out var blue))
            return;

        if (_lightingModeComboBox != null)
            _lightingModeComboBox.SelectedIndex = mode;

        SetLightingSpeed(speed);
        SetKeyboardBrightness(brightness);

        SetDirectionRadioButtons(direction);

        // Mode 2 / Neon often reports 0,0,0. Do not overwrite the color picker with black for that.
        if (mode != 2 || red != 0 || green != 0 || blue != 0)
            SetColorPicker(_lightEffectColorPicker, $"{red:X2}{green:X2}{blue:X2}");
    }

    private async Task ShowMessageBox(string title, string message)
    {
        try
        {
            var box = MessageBoxManager.GetMessageBoxStandard(title, message);
            await box.ShowWindowDialogAsync(this);
        }
        catch (Exception ex)
        {
            Console.WriteLine($"Error showing message box: {ex.Message}");
        }
    }

    public void DeveloperMode_OnClick(object? sender, RoutedEventArgs e)
    {
        EnableDevMode(true);
    }

    public void EnableDevMode(bool toEnable)
    {
        AppState.DevMode = toEnable;
        if (_powerToggleSwitch != null)
            _powerToggleSwitch.IsHitTestVisible = toEnable;
        _settings ??= new DAMXSettings();
        ApplySettingsToUI();
    }

    private void RetryConnectionButton_OnClick(object? sender, RoutedEventArgs e)
    {
        InitializeAsync();
    }

    private void DismissDaemonErrorButton_OnClick(object? sender, RoutedEventArgs e)
    {
        if (_daemonErrorGrid != null) _daemonErrorGrid.IsVisible = false;
        ShowStatus("Running without the daemon: telemetry only. Click the status chip (top right) to reconnect.",
            false);
    }

    private void DaemonStatusChip_OnTapped(object? sender, TappedEventArgs e)
    {
        InitializeAsync();
    }

    private void OpenUrl(string url)
    {
        try
        {
            Process.Start(new ProcessStartInfo("xdg-open", url) { UseShellExecute = true });
        }
        catch (Exception ex)
        {
            ShowStatus($"Could not open {url}: {ex.Message}", true);
        }
    }

    private void UpdatesButton_OnClick(object? sender, RoutedEventArgs e)
    {
        OpenUrl("https://github.com/PXDiv/Div-Acer-Manager-Max/releases");
    }

    private void StarProject_OnClick(object? sender, RoutedEventArgs e)
    {
        OpenUrl("https://github.com/PXDiv/Div-Acer-Manager-Max/");
    }

    private void IssuePageButton_OnClick(object? sender, RoutedEventArgs e)
    {
        OpenUrl("https://github.com/PXDiv/Div-Acer-Manager-Max/issues");
    }

    private async void InternalsMangerWindow_OnClick(object? sender, RoutedEventArgs e)
    {
        try
        {
            var internalsManagerWindow = new InternalsManager(this);
            await internalsManagerWindow.ShowDialog(this);
        }
        catch (Exception ex)
        {
            Console.WriteLine($"Error opening Internals Manager: {ex}");
            ShowStatus($"Could not open the Internals Manager: {ex.Message}", true);
        }
    }

    // ------------------------------------------------------------------ fan control

    private void ManualFanControlRadioBox_Click(object? sender, RoutedEventArgs e)
    {
        // Only reveals the sliders; nothing is sent until "APPLY".
        _isManualFanControl = true;
        SetRadioButton(_manualFanSpeedRadioButton, true);
    }

    private void CpuFanSlider_ValueChanged(object? sender, AvaloniaPropertyChangedEventArgs e)
    {
        if (e.Property != Slider.ValueProperty) return;
        _cpuFanSpeed = Convert.ToInt32(e.NewValue, Inv);
        SetText(_cpuFanTextBlock, FormatFanSpeed(_cpuFanSpeed));
    }

    private void GpuFanSlider_ValueChanged(object? sender, AvaloniaPropertyChangedEventArgs e)
    {
        if (e.Property != Slider.ValueProperty) return;
        _gpuFanSpeed = Convert.ToInt32(e.NewValue, Inv);
        SetText(_gpuFanTextBlock, FormatFanSpeed(_gpuFanSpeed));
    }

    private async void SetManualSpeedButton_OnClick(object? sender, RoutedEventArgs e)
    {
        if (_isSettingFanSpeed)
            return;

        _isSettingFanSpeed = true;
        SetEnabled(_setManualSpeedButton, false);

        try
        {
            await RunDaemonAction("Fan speed", async () =>
            {
                EnsureSuccess(await _client.SetFanSpeedAsync(_cpuFanSpeed, _gpuFanSpeed), "Set fan speed");
                ShowStatus($"Fans: CPU {FormatFanSpeed(_cpuFanSpeed)} · GPU {FormatFanSpeed(_gpuFanSpeed)}", false);
            });
        }
        finally
        {
            _isSettingFanSpeed = false;
            SetEnabled(_setManualSpeedButton, true);
        }
    }

    private async void AutoFanSpeedRadioButtonClick(object? sender, RoutedEventArgs e)
    {
        await RunDaemonAction("Fan mode", async () =>
        {
            EnsureSuccess(await _client.SetFanSpeedAsync(0, 0), "Automatic fan mode");
            _isManualFanControl = false;
            ShowStatus("Fans: automatic", false);
        });
    }

    private async void MaxFanSpeedRadioButton_OnClick(object? sender, RoutedEventArgs e)
    {
        await RunDaemonAction("Fan mode", async () =>
        {
            EnsureSuccess(await _client.SetFanSpeedAsync(100, 100), "Max fan mode");

            _cpuFanSpeed = 100;
            _gpuFanSpeed = 100;
            _isManualFanControl = false;
            if (_cpuFanSlider != null) _cpuFanSlider.Value = 100;
            if (_gpuFanSlider != null) _gpuFanSlider.Value = 100;
            SetText(_cpuFanTextBlock, "100%");
            SetText(_gpuFanTextBlock, "100%");
            ShowStatus("Fans: maximum", false);
        });
    }

    // ------------------------------------------------------------------ battery

    private async void StartCalibrationButton_Click(object? sender, RoutedEventArgs e)
    {
        await RunDaemonAction("Battery calibration", async () =>
        {
            EnsureSuccess(await _client.SetBatteryCalibrationAsync(true), "Start calibration");
            IsCalibrating = true;
            SetEnabled(_startCalibrationButton, false);
            SetEnabled(_stopCalibrationButton, true);
            SetText(_calibrationStatusTextBlock, "Status: Calibrating");
        });
    }

    private async void StopCalibrationButton_Click(object? sender, RoutedEventArgs e)
    {
        await RunDaemonAction("Battery calibration", async () =>
        {
            EnsureSuccess(await _client.SetBatteryCalibrationAsync(false), "Stop calibration");
            IsCalibrating = false;
            SetEnabled(_startCalibrationButton, true);
            SetEnabled(_stopCalibrationButton, false);
            SetText(_calibrationStatusTextBlock, "Status: Not calibrating");
        });
    }

    private async void BatteryLimitCheckBox_Click(object? sender, RoutedEventArgs e)
    {
        if (sender is not CheckBox checkBox) return;
        var enabled = checkBox.IsChecked ?? false;

        await RunDaemonAction("Battery limiter", async () =>
        {
            EnsureSuccess(await _client.SetBatteryLimiterAsync(enabled), "Battery limiter");
            _dashboard?.SetBatteryLimiterState(enabled);
        });
    }

    private async void UsbChargingComboBox_OnSelectionChanged(object? sender, SelectionChangedEventArgs e)
    {
        if (_applyingSettings || !_isConnected || _usbChargingComboBox == null) return;

        var level = _usbChargingComboBox.SelectedIndex switch
        {
            1 => 10,
            2 => 20,
            3 => 30,
            _ => 0
        };

        await RunDaemonAction("USB charging",
            async () => EnsureSuccess(await _client.SetUsbChargingAsync(level), "USB charging"));
    }

    // ------------------------------------------------------------------ keyboard

    private void KeyboardBrightnessSlider_ValueChanged(object? sender, AvaloniaPropertyChangedEventArgs e)
    {
        if (e.Property == Slider.ValueProperty)
            SetKeyboardBrightness(Convert.ToInt32(e.NewValue, Inv), false);
    }

    private async void ApplyKeyboardColorsButton_Click(object? sender, RoutedEventArgs e)
    {
        await RunDaemonAction("Keyboard colours", async () =>
        {
            if (_settings?.HasFourZoneKb != true && !AppState.DevMode)
                throw new DaemonCommandException("This keyboard does not support per-zone colours.");

            EnsureSuccess(await _client.SetPerZoneModeAsync(
                ToRgbHex(_zone1ColorPicker?.Color ?? Color.Parse(DefaultZone1Color)),
                ToRgbHex(_zone2ColorPicker?.Color ?? Color.Parse(DefaultZone2Color)),
                ToRgbHex(_zone3ColorPicker?.Color ?? Color.Parse(DefaultZone3Color)),
                ToRgbHex(_zone4ColorPicker?.Color ?? Color.Parse(DefaultZone4Color)),
                _keyboardBrightness
            ), "Keyboard colours");

            SaveZonePresetFromUI();
            ShowStatus("Keyboard colours applied", false);
        }, false);
    }

    private void LightingSpeedSlider_ValueChanged(object? sender, AvaloniaPropertyChangedEventArgs e)
    {
        if (e.Property == Slider.ValueProperty)
            SetLightingSpeed(Convert.ToInt32(e.NewValue, Inv), false);
    }

    private async void LightingEffectsApplyButton_Click(object? sender, RoutedEventArgs e)
    {
        await RunDaemonAction("Lighting effect", async () =>
        {
            if (_settings?.HasFourZoneKb != true && !AppState.DevMode)
                throw new DaemonCommandException("This keyboard does not support lighting effects.");

            var mode = _lightingModeComboBox?.SelectedIndex ?? 0;
            if (mode < 0) mode = 0;
            var direction = GetSelectedDirection();
            var color = _lightEffectColorPicker?.Color ?? Color.Parse(DefaultEffectColor);

            if (mode == 0)
            {
                var rgb = ToRgbHex(color);
                EnsureSuccess(await _client.SetPerZoneModeAsync(rgb, rgb, rgb, rgb, _keyboardBrightness),
                    "Static colour");
            }
            else
            {
                EnsureSuccess(await _client.SetFourZoneModeAsync(mode, _lightingSpeed, _keyboardBrightness,
                    direction, color.R, color.G, color.B), "Lighting effect");
            }

            SaveLightingEffectPresetFromUI(mode, direction, color);
            ShowStatus("Lighting effect applied", false);
        }, false);
    }

    // ------------------------------------------------------------------ system

    private async void BacklightTimeoutCheckBox_Click(object? sender, RoutedEventArgs e)
    {
        if (sender is not CheckBox checkBox) return;
        var enabled = checkBox.IsChecked ?? false;
        await RunDaemonAction("Backlight timeout",
            async () => EnsureSuccess(await _client.SetBacklightTimeoutAsync(enabled), "Backlight timeout"));
    }

    private async void LcdOverrideCheckBox_Click(object? sender, RoutedEventArgs e)
    {
        if (sender is not CheckBox checkBox) return;
        var enabled = checkBox.IsChecked ?? false;
        await RunDaemonAction("LCD override",
            async () => EnsureSuccess(await _client.SetLcdOverrideAsync(enabled), "LCD override"));
    }

    private async void BootSoundCheckBox_Click(object? sender, RoutedEventArgs e)
    {
        if (sender is not CheckBox checkBox) return;
        var enabled = checkBox.IsChecked ?? false;
        await RunDaemonAction("Boot animation",
            async () => EnsureSuccess(await _client.SetBootAnimationSoundAsync(enabled), "Boot animation"));
    }

    // ------------------------------------------------------------------ keyboard helpers

    private void SetKeyboardBrightness(int brightness, bool updateSlider = true)
    {
        _keyboardBrightness = brightness;

        if (updateSlider && _keyBrightnessSlider != null)
            _keyBrightnessSlider.Value = brightness;

        SetText(_keyBrightnessText, $"{brightness.ToString(Inv)}%");
    }

    private void SetLightingSpeed(int speed, bool updateSlider = true)
    {
        _lightingSpeed = speed;

        if (updateSlider && _lightingSpeedSlider != null)
            _lightingSpeedSlider.Value = speed;

        SetText(_lightSpeedTextBlock, speed.ToString(Inv));
    }

    private static string ToRgbHex(Color color)
    {
        return $"{color.R:X2}{color.G:X2}{color.B:X2}";
    }

    private static void SetColorPicker(ColorPicker? picker, string rgbHex)
    {
        if (picker == null)
            return;

        var normalized = NormalizeRgbHex(rgbHex);
        if (normalized == null)
            return;

        picker.Color = Color.Parse($"#{normalized}");
    }

    private static string? NormalizeRgbHex(string? value)
    {
        if (string.IsNullOrWhiteSpace(value))
            return null;

        var hex = value.Trim();

        if (hex.StartsWith('#'))
            hex = hex[1..];

        if (hex.Length != 6)
            return null;

        foreach (var c in hex)
            if (!Uri.IsHexDigit(c))
                return null;

        return hex.ToUpperInvariant();
    }

    private static bool TryParsePerZoneMode(
        string? value,
        out string zone1,
        out string zone2,
        out string zone3,
        out string zone4,
        out int brightness)
    {
        zone1 = "";
        zone2 = "";
        zone3 = "";
        zone4 = "";
        brightness = 100;

        if (string.IsNullOrWhiteSpace(value))
            return false;

        var parts = value.Trim().Split(',', StringSplitOptions.TrimEntries);

        if (parts.Length != 5)
            return false;

        zone1 = NormalizeRgbHex(parts[0]) ?? "";
        zone2 = NormalizeRgbHex(parts[1]) ?? "";
        zone3 = NormalizeRgbHex(parts[2]) ?? "";
        zone4 = NormalizeRgbHex(parts[3]) ?? "";

        if (zone1.Length != 6 ||
            zone2.Length != 6 ||
            zone3.Length != 6 ||
            zone4.Length != 6)
            return false;

        if (!int.TryParse(parts[4], NumberStyles.Integer, Inv, out brightness))
            return false;

        return brightness is >= 0 and <= 100;
    }

    private static bool TryParseFourZoneMode(
        string? value,
        out int mode,
        out int speed,
        out int brightness,
        out int direction,
        out int red,
        out int green,
        out int blue)
    {
        mode = 0;
        speed = 5;
        brightness = 100;
        direction = DirectionRightToLeft;
        red = 0;
        green = 0;
        blue = 0;

        if (string.IsNullOrWhiteSpace(value))
            return false;

        var parts = value.Trim().Split(',', StringSplitOptions.TrimEntries);

        if (parts.Length != 7)
            return false;

        return int.TryParse(parts[0], NumberStyles.Integer, Inv, out mode) &&
               mode is >= 0 and <= 7 &&
               int.TryParse(parts[1], NumberStyles.Integer, Inv, out speed) &&
               speed is >= 0 and <= 9 &&
               int.TryParse(parts[2], NumberStyles.Integer, Inv, out brightness) &&
               brightness is >= 0 and <= 100 &&
               int.TryParse(parts[3], NumberStyles.Integer, Inv, out direction) &&
               direction is >= DirectionLeftToRight and <= DirectionRightToLeft &&
               int.TryParse(parts[4], NumberStyles.Integer, Inv, out red) &&
               red is >= 0 and <= 255 &&
               int.TryParse(parts[5], NumberStyles.Integer, Inv, out green) &&
               green is >= 0 and <= 255 &&
               int.TryParse(parts[6], NumberStyles.Integer, Inv, out blue) &&
               blue is >= 0 and <= 255;
    }

    private static bool HasSavedZonePreset()
    {
        return File.Exists(KeyboardZonePresetPath);
    }

    private void ApplySavedZonePresetToUI()
    {
        if (!File.Exists(KeyboardZonePresetPath))
            return;

        try
        {
            var value = File.ReadAllText(KeyboardZonePresetPath).Trim();

            if (!TryParsePerZoneMode(
                    value,
                    out var zone1,
                    out var zone2,
                    out var zone3,
                    out var zone4,
                    out var brightness))
                return;

            SetColorPicker(_zone1ColorPicker, zone1);
            SetColorPicker(_zone2ColorPicker, zone2);
            SetColorPicker(_zone3ColorPicker, zone3);
            SetColorPicker(_zone4ColorPicker, zone4);

            SetKeyboardBrightness(brightness);
        }
        catch
        {
            // Ignore broken preset file and let daemon settings win.
        }
    }

    private void SaveZonePresetFromUI()
    {
        try
        {
            WritePresetFile(KeyboardZonePresetPath, CreateZonePresetValueFromUI());
        }
        catch
        {
            // Failing to save a UI preset should not break keyboard control.
        }
    }

    private string CreateZonePresetValueFromUI()
    {
        var zone1 = ToRgbHex(_zone1ColorPicker?.Color ?? Color.Parse(DefaultZone1Color));
        var zone2 = ToRgbHex(_zone2ColorPicker?.Color ?? Color.Parse(DefaultZone2Color));
        var zone3 = ToRgbHex(_zone3ColorPicker?.Color ?? Color.Parse(DefaultZone3Color));
        var zone4 = ToRgbHex(_zone4ColorPicker?.Color ?? Color.Parse(DefaultZone4Color));

        return string.Create(Inv, $"{zone1},{zone2},{zone3},{zone4},{_keyboardBrightness}");
    }

    private void ApplySavedLightingEffectPresetToUI()
    {
        if (!File.Exists(KeyboardLightingEffectPresetPath))
            return;

        try
        {
            var value = File.ReadAllText(KeyboardLightingEffectPresetPath).Trim();

            if (!TryParseFourZoneMode(
                    value,
                    out var mode,
                    out var speed,
                    out var brightness,
                    out var direction,
                    out var red,
                    out var green,
                    out var blue))
                return;

            if (_lightingModeComboBox != null)
                _lightingModeComboBox.SelectedIndex = mode;

            SetLightingSpeed(speed);
            SetKeyboardBrightness(brightness);
            SetDirectionRadioButtons(direction);

            SetColorPicker(_lightEffectColorPicker, $"{red:X2}{green:X2}{blue:X2}");
        }
        catch
        {
            // Ignore broken preset file and let daemon settings win.
        }
    }

    private void SaveLightingEffectPresetFromUI(int mode, int direction, Color color)
    {
        try
        {
            WritePresetFile(
                KeyboardLightingEffectPresetPath,
                string.Create(Inv,
                    $"{mode},{_lightingSpeed},{_keyboardBrightness},{direction},{color.R},{color.G},{color.B}")
            );
        }
        catch
        {
            // Failing to save a UI preset should not break keyboard control.
        }
    }

    private static void WritePresetFile(string path, string value)
    {
        Directory.CreateDirectory(AppDataFolderPath);
        File.WriteAllText(path, value);
    }

    private int GetSelectedDirection()
    {
        if (_leftToRightRadioButton?.IsChecked == true)
            return DirectionLeftToRight;

        if (_rightToLeftRadioButton?.IsChecked == true)
            return DirectionRightToLeft;

        return DirectionLeftToRight;
    }

    private void SetDirectionRadioButtons(int direction)
    {
        if (_leftToRightRadioButton != null)
            _leftToRightRadioButton.IsChecked = direction == DirectionLeftToRight;

        if (_rightToLeftRadioButton != null)
            _rightToLeftRadioButton.IsChecked = direction == DirectionRightToLeft;
    }

    public static class AppState
    {
        public static bool DevMode { get; set; }
    }

    #region INotifyPropertyChanged

    public new event PropertyChangedEventHandler? PropertyChanged;

    protected virtual void OnPropertyChanged([CallerMemberName] string? propertyName = null)
    {
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(propertyName));
    }

    protected bool SetField<T>(ref T field, T value, [CallerMemberName] string? propertyName = null)
    {
        if (EqualityComparer<T>.Default.Equals(field, value)) return false;
        field = value;
        OnPropertyChanged(propertyName);
        return true;
    }

    #endregion
}
