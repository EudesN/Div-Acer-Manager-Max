using System;
using System.Collections.Generic;
using System.Globalization;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Interactivity;
using Avalonia.Threading;

namespace DivAcerManagerMax;

/// <summary>
///     Modal fan-curve editor (get_fan_curve / set_fan_curve, see feature-contract.md).
///     Apply = save + activate (fan_mode "curve"); Save = save only; Cancel = discard.
/// </summary>
public partial class FanCurveWindow : Window
{
    private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

    private readonly DAMXClient _client;
    private readonly DispatcherTimer _liveTimer;
    private FanCurve _curve = FanCurve.Default();
    private bool _liveInFlight;
    private bool _loading;
    private Dictionary<string, FanCurve> _presets = new(StringComparer.OrdinalIgnoreCase);
    private bool _busy;
    private bool _closed;

    public FanCurveWindow(DAMXClient client)
    {
        InitializeComponent();
        _client = client;

        var editor = Editor;
        if (editor != null)
        {
            editor.Curve = _curve;
            editor.CurveChanged += (_, _) => ValidateAndShow();
        }

        var hysteresis = this.FindControl<Slider>("HysteresisSlider");
        if (hysteresis != null) hysteresis.PropertyChanged += OnSliderChanged;
        var interval = this.FindControl<Slider>("IntervalSlider");
        if (interval != null) interval.PropertyChanged += OnSliderChanged;

        _liveTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(2) };
        _liveTimer.Tick += async (_, _) => await RefreshLiveAsync();

        Opened += async (_, _) =>
        {
            await LoadAsync();
            if (!_closed) _liveTimer.Start();
        };
        Closed += (_, _) =>
        {
            _closed = true;
            _liveTimer.Stop();
        };
    }

    /// <summary>True when the curve was applied (fan mode is now "curve").</summary>
    public bool Applied { get; private set; }

    /// <summary>True when the curve was saved (with or without activation).</summary>
    public bool Saved { get; private set; }

    private FanCurveEditor? Editor => this.FindControl<FanCurveEditor>("CurveEditor");

    // ------------------------------------------------------------------ loading / live data

    private async Task LoadAsync()
    {
        _loading = true;
        try
        {
            var info = await _client.GetFanCurveAsync();
            _presets = info.Presets;
            SetCurve(info.Curve.Clone());
            ApplyLive(info.Last);
            ShowError(null);
        }
        catch (Exception ex)
        {
            SetCurve(FanCurve.Default());
            ShowError($"Could not load the saved curve ({ex.Message}). Editing a default curve.");
        }
        finally
        {
            _loading = false;
        }
    }

    private async Task RefreshLiveAsync()
    {
        if (_liveInFlight || _busy) return;
        _liveInFlight = true;
        try
        {
            var info = await _client.GetFanCurveAsync();
            ApplyLive(info.Last);
        }
        catch
        {
            // live data is best effort
        }
        finally
        {
            _liveInFlight = false;
        }
    }

    private void ApplyLive(FanCurveSample sample)
    {
        var editor = Editor;
        if (editor != null) editor.Live = sample;
        var text = this.FindControl<TextBlock>("CurveLiveText");
        if (text != null) text.Text = "Live: " + sample.Summary();
    }

    private void SetCurve(FanCurve curve)
    {
        _curve = curve;
        var editor = Editor;
        if (editor != null) editor.Curve = _curve;

        _loading = true;
        try
        {
            var hysteresis = this.FindControl<Slider>("HysteresisSlider");
            if (hysteresis != null) hysteresis.Value = curve.Hysteresis;
            var interval = this.FindControl<Slider>("IntervalSlider");
            if (interval != null) interval.Value = curve.Interval;
        }
        finally
        {
            _loading = false;
        }

        UpdateSliderTexts();
        ValidateAndShow();
    }

    // ------------------------------------------------------------------ settings sliders

    private void OnSliderChanged(object? sender, AvaloniaPropertyChangedEventArgs e)
    {
        if (e.Property != Slider.ValueProperty) return;
        if (!_loading)
        {
            var value = (int)Math.Round(Convert.ToDouble(e.NewValue, Inv));
            if (sender is Slider { Name: "HysteresisSlider" }) _curve.Hysteresis = value;
            else if (sender is Slider { Name: "IntervalSlider" }) _curve.Interval = value;
        }

        UpdateSliderTexts();
    }

    private void UpdateSliderTexts()
    {
        var h = this.FindControl<TextBlock>("HysteresisValueText");
        if (h != null) h.Text = _curve.Hysteresis.ToString(Inv) + " °C";
        var i = this.FindControl<TextBlock>("IntervalValueText");
        if (i != null) i.Text = _curve.Interval.ToString(Inv) + " s";
    }

    // ------------------------------------------------------------------ validation / errors

    private bool ValidateAndShow()
    {
        var error = _curve.Validate();
        ShowError(error);
        return error == null;
    }

    private void ShowError(string? error)
    {
        var text = this.FindControl<TextBlock>("CurveErrorText");
        if (text == null) return;
        text.Classes.Remove("Warning");
        if (error != null)
        {
            text.Text = error;
            text.Classes.Add("Warning");
        }
        else
        {
            text.Text = "≥ 95 °C always runs the fans at 100 %. 0 % is not allowed (it means firmware auto).";
        }
    }

    // ------------------------------------------------------------------ toolbar

    private void CpuSeriesRadioButton_OnClick(object? sender, RoutedEventArgs e)
    {
        var editor = Editor;
        if (editor != null) editor.EditGpu = false;
    }

    private void GpuSeriesRadioButton_OnClick(object? sender, RoutedEventArgs e)
    {
        var editor = Editor;
        if (editor != null) editor.EditGpu = true;
    }

    private void ApplyPreset(string name)
    {
        if (_presets.TryGetValue(name, out var preset))
        {
            SetCurve(preset.Clone());
            return;
        }

        ShowError($"The daemon did not provide the '{name}' preset.");
    }

    private void PresetSilentButton_OnClick(object? sender, RoutedEventArgs e) => ApplyPreset("silent");

    private void PresetBalancedButton_OnClick(object? sender, RoutedEventArgs e) => ApplyPreset("balanced");

    private void PresetAggressiveButton_OnClick(object? sender, RoutedEventArgs e) => ApplyPreset("aggressive");

    private void CopyCpuToGpuButton_OnClick(object? sender, RoutedEventArgs e)
    {
        _curve.Gpu = FanCurve.ClonePoints(_curve.Cpu);
        var editor = Editor;
        if (editor != null) editor.Curve = _curve;
        ValidateAndShow();
    }

    private void AddPointButton_OnClick(object? sender, RoutedEventArgs e)
    {
        if (Editor?.AddPointInLargestGap() != true)
            ShowError("No room for another point (max 8, and points need at least 1 °C between them).");
    }

    private void RemovePointButton_OnClick(object? sender, RoutedEventArgs e)
    {
        if (Editor?.RemovePoint() != true)
            ShowError("A curve needs at least 2 points.");
    }

    // ------------------------------------------------------------------ actions

    private void CurveCancelButton_OnClick(object? sender, RoutedEventArgs e)
    {
        Close();
    }

    private async void CurveSaveButton_OnClick(object? sender, RoutedEventArgs e)
    {
        await SendAsync(false);
    }

    private async void CurveApplyButton_OnClick(object? sender, RoutedEventArgs e)
    {
        await SendAsync(true);
    }

    private async Task SendAsync(bool activate)
    {
        if (_busy || !ValidateAndShow()) return;
        _busy = true;
        SetButtonsEnabled(false);
        try
        {
            var result = await _client.SetFanCurveAsync(_curve, activate);
            if (!result.Success)
            {
                ShowError(result.Error ?? "The daemon rejected the curve.");
                return;
            }

            Saved = true;
            Applied = activate || result.GetDataBool("active");
            Close();
        }
        catch (Exception ex)
        {
            ShowError($"Could not send the curve: {ex.Message}");
        }
        finally
        {
            _busy = false;
            SetButtonsEnabled(true);
        }
    }

    private void SetButtonsEnabled(bool enabled)
    {
        foreach (var name in new[] { "CurveSaveButton", "CurveApplyButton" })
        {
            var button = this.FindControl<Button>(name);
            if (button != null) button.IsEnabled = enabled;
        }
    }
}
