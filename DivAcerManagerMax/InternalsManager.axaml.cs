using System;
using System.Diagnostics;
using System.IO;
using System.Threading.Tasks;
using Avalonia.Controls;
using Avalonia.Interactivity;
using MsBox.Avalonia;

namespace DivAcerManagerMax;

public partial class InternalsManager : Window
{
    private const string logPath = "/var/log/DAMX_Daemon_Log.log";
    private const string RemoveParameterCommand = "remove_modprobe_parameter";

    private readonly MainWindow _mainWindow;
    private bool _isBusy;
    private bool _isInitializing;
    private int _lastSelectedIndex = -1;
    private bool _suppressSelectionEvents;

    public InternalsManager(MainWindow mainWindow)
    {
        _isInitializing = true;
        InitializeComponent();
        _mainWindow = mainWindow;
        InitializeUiComponents();
        _isInitializing = false;

        // The cached settings may be stale (or absent): read the real value from the daemon.
        Opened += async (_, _) => await RefreshModprobeParameterStateAsync();
    }

    private DAMXClient Client => _mainWindow._client;

    public void InitializeUiComponents()
    {
        DevModeToggleSwitch.IsChecked = MainWindow.AppState.DevMode;

        // Null-safe: _settings is null when the daemon has never been reached.
        SetComboSelection(ParameterToIndex(_mainWindow._settings?.ModprobeParameter));
    }

    private static int ParameterToIndex(string? parameter)
    {
        return parameter?.Trim() switch
        {
            "predator_v4" => 1,
            "nitro_v4" => 2,
            "enable_all" => 3,
            _ => 0 // "No Parameter" for empty string or unknown values
        };
    }

    /// <summary>Changes the combobox without triggering a daemon command.</summary>
    private void SetComboSelection(int index)
    {
        _suppressSelectionEvents = true;
        try
        {
            ForceParameterPermanentlyComboBox.SelectedIndex = index;
            _lastSelectedIndex = index;
        }
        finally
        {
            _suppressSelectionEvents = false;
        }
    }

    private void SetStatus(string message, bool isError)
    {
        Console.WriteLine(isError ? $"[internals][error] {message}" : $"[internals] {message}");
        var status = this.FindControl<TextBlock>("InternalsStatusText");
        if (status == null) return;
        status.Text = message;
        status.Classes.Remove("Warning");
        if (isError) status.Classes.Add("Warning");
    }

    private void SetBusy(bool busy)
    {
        _isBusy = busy;
        foreach (var name in new[]
                 {
                     "ForceNitroButton", "ForcePredatorButton", "ForceEnableAll", "RestartSuiteButton",
                     "RestartDaemon", "ForceParameterPermanentlyComboBox"
                 })
        {
            var control = this.FindControl<Control>(name);
            if (control != null) control.IsEnabled = !busy;
        }
    }

    private void DevModeSwitch_OnClick(object? sender, RoutedEventArgs e)
    {
        _mainWindow.EnableDevMode(DevModeToggleSwitch.IsChecked == true);
    }

    public void ReinitializeDamxGUI()
    {
        _mainWindow.InitializeAsync();
    }

    private void DaemonLogsButton_OnClick(object? sender, RoutedEventArgs e)
    {
        try
        {
            Process.Start(new ProcessStartInfo("xdg-open", logPath) { UseShellExecute = true });
        }
        catch (Exception ex)
        {
            SetStatus($"Could not open {logPath}: {ex.Message}", true);
        }
    }

    private async void RestartSuiteButton_OnClick(object? sender, RoutedEventArgs e)
    {
        await RunPrivilegedAsync("restart_drivers_and_daemon", "Restarting drivers and daemon");
    }

    private async void RestartDaemon_OnClick(object? sender, RoutedEventArgs e)
    {
        await RunPrivilegedAsync("restart_daemon", "Restarting daemon");
    }

    private async void ForcePredatorButton_OnClick(object? sender, RoutedEventArgs e)
    {
        await RunPrivilegedAsync("force_predator_model", "Loading driver with predator_v4");
    }

    private async void ForceNitroButton_OnClick(object? sender, RoutedEventArgs e)
    {
        await RunPrivilegedAsync("force_nitro_model", "Loading driver with nitro_v4");
    }

    private async void ForceEnableAll_OnClick(object? sender, RoutedEventArgs e)
    {
        await RunPrivilegedAsync("force_enable_all", "Loading driver with enable_all");
    }

    /// <summary>
    ///     Sends a command that reloads the driver and/or restarts the daemon. The reply is awaited;
    ///     losing the connection afterwards is expected (the daemon restarts). A {"success": false}
    ///     reply (e.g. "permission denied: ...") is shown to the user.
    /// </summary>
    private async Task RunPrivilegedAsync(string command, string actionName)
    {
        if (_isBusy) return;
        SetBusy(true);

        try
        {
            if (!await EnsureConnectedAsync()) return;

            SetStatus($"{actionName}…", false);
            var outcome = await SendExpectingRestartAsync(command);
            if (outcome.Error != null)
            {
                SetStatus($"{actionName} failed: {outcome.Error}", true);
                return;
            }

            await WaitForDaemonAndRefreshAsync(actionName);
        }
        catch (Exception ex)
        {
            SetStatus($"{actionName} failed: {ex.Message}", true);
        }
        finally
        {
            SetBusy(false);
        }
    }

    private async Task<bool> EnsureConnectedAsync()
    {
        if (await Client.ConnectAsync()) return true;

        SetStatus(Client.State == DaemonConnectionState.PermissionDenied
            ? DaemonPermissionException.UserMessage
            : $"Daemon is not connected: {Client.LastError}", true);
        return false;
    }

    /// <summary>Result of a command after which the daemon may restart.</summary>
    private sealed class RestartOutcome
    {
        public string? Error { get; init; }
        public bool ConnectionLost { get; init; }
        public DaemonResult? Result { get; init; }
    }

    private async Task<RestartOutcome> SendExpectingRestartAsync(string command)
    {
        try
        {
            var result = await Client.SendCommandResultAsync(command);
            return result.Success
                ? new RestartOutcome { Result = result }
                : new RestartOutcome { Result = result, Error = result.Error ?? "unknown error" };
        }
        catch (DaemonPermissionException ex)
        {
            return new RestartOutcome { Error = ex.Message };
        }
        catch (Exception ex) when (ex is IOException or TimeoutException)
        {
            // The command was delivered and the daemon went away while handling it (old daemons
            // restart before replying). The real state is read back afterwards.
            Console.WriteLine($"'{command}': connection lost after sending ({ex.Message}); assuming restart");
            return new RestartOutcome { ConnectionLost = true };
        }
    }

    /// <summary>Polls until the daemon answers again, then reloads settings and the modprobe state.</summary>
    private async Task WaitForDaemonAndRefreshAsync(string actionName)
    {
        SetStatus($"{actionName}: waiting for the daemon…", false);

        var back = false;
        await Task.Delay(3000);
        for (var attempt = 0; attempt < 15 && !back; attempt++)
        {
            back = await _mainWindow.ReloadFromDaemonAsync();
            if (!back) await Task.Delay(2000);
        }

        if (!back)
        {
            SetStatus($"{actionName}: the daemon did not come back within 30 s. Check the logs.", true);
            return;
        }

        await RefreshModprobeParameterStateAsync();
        SetStatus($"{actionName}: done.", false);

        // The daemon may restart a few seconds after replying; re-read once more so the feature
        // set and the parameter shown come from the new instance.
        await Task.Delay(6000);
        if (await _mainWindow.ReloadFromDaemonAsync())
            await RefreshModprobeParameterStateAsync();
    }

    private async void ForceParameterPermanently_OnSelectionChanged(object? sender, SelectionChangedEventArgs e)
    {
        if (_isInitializing || _suppressSelectionEvents || _isBusy) return;

        if (sender is not ComboBox comboBox || comboBox.SelectedIndex == -1) return;

        // Check if this is a real change
        if (comboBox.SelectedIndex == _lastSelectedIndex) return;

        var previousIndex = _lastSelectedIndex;
        var newIndex = comboBox.SelectedIndex;
        _lastSelectedIndex = newIndex;

        SetBusy(true);
        try
        {
            var (command, actionName) = newIndex switch
            {
                1 => ("set_modprobe_parameter_predator", "Setting permanent predator_v4"),
                2 => ("set_modprobe_parameter_nitro", "Setting permanent nitro_v4"),
                3 => ("set_modprobe_parameter_enable_all", "Setting permanent enable_all"),
                _ => (RemoveParameterCommand, "Removing permanent parameter")
            };

            var ok = await SendParameterCommand(command, actionName);
            if (!ok) SetComboSelection(previousIndex < 0 ? 0 : previousIndex);
        }
        catch (Exception ex)
        {
            Console.WriteLine($"Error in ForceParameterPermanently_OnSelectionChanged: {ex}");
            SetStatus($"Failed to change parameter: {ex.Message}", true);
            SetComboSelection(previousIndex < 0 ? 0 : previousIndex);
        }
        finally
        {
            SetBusy(false);
        }
    }

    /// <summary>
    ///     Applies a permanent modprobe parameter (GitHub #191/#48/#118). The set/remove command is awaited
    ///     and its result checked. "set_modprobe_parameter_*" makes the daemon reload the module itself
    ///     (after replying), so no separate restart is sent. "remove_modprobe_parameter" only deletes the
    ///     config file, so an explicit driver reload is requested unless the daemon says it already
    ///     scheduled one. Afterwards the value is read back from the daemon (never from the stale cache).
    /// </summary>
    private async Task<bool> SendParameterCommand(string command, string actionName)
    {
        if (!await EnsureConnectedAsync()) return false;

        SetStatus($"{actionName}…", false);
        var outcome = await SendExpectingRestartAsync(command);
        if (outcome.Error != null)
        {
            SetStatus($"{actionName} failed: {outcome.Error}", true);
            return false;
        }

        if (command == RemoveParameterCommand && !outcome.ConnectionLost && !DaemonWillReload(outcome.Result))
        {
            var reload = await SendExpectingRestartAsync("restart_drivers_and_daemon");
            if (reload.Error != null)
            {
                // The file is gone; only the reload failed. Show it, but keep the new selection.
                SetStatus($"Parameter removed, but reloading the driver failed: {reload.Error}", true);
                await RefreshModprobeParameterStateAsync();
                return true;
            }
        }

        await WaitForDaemonAndRefreshAsync(actionName);
        return true;
    }

    private static bool DaemonWillReload(DaemonResult? result)
    {
        if (result == null) return false;
        if (result.GetDataBool("reload_scheduled") || result.GetDataBool("reloading")) return true;
        var message = result.Message ?? "";
        return message.Contains("reload", StringComparison.OrdinalIgnoreCase) ||
               message.Contains("restart", StringComparison.OrdinalIgnoreCase);
    }

    /// <summary>Reads the parameter with get_modprobe_parameter and updates the cache + combobox.</summary>
    public async Task RefreshModprobeParameterStateAsync()
    {
        try
        {
            var parameter = await Client.GetModprobeParameterAsync() ?? "";
            _mainWindow.UpdateModprobeParameter(parameter);
            SetComboSelection(ParameterToIndex(parameter));
        }
        catch (Exception ex)
        {
            // Keep whatever is shown; this is only a refresh.
            Console.WriteLine($"Error refreshing parameter state: {ex.Message}");
        }
    }

    public async void RefreshModprobeParameterState()
    {
        await RefreshModprobeParameterStateAsync();
    }

    private async Task ShowMessagebox(string title, string message)
    {
        try
        {
            var box = MessageBoxManager.GetMessageBoxStandard(title, message);
            await box.ShowWindowDialogAsync(this);
        }
        catch (Exception ex)
        {
            Console.WriteLine($"Error showing messagebox: {ex.Message}");
        }
    }
}
