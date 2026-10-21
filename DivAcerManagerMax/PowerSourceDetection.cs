using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Timers;
using Avalonia.Controls;
using Avalonia.Threading;

/// <summary>
///     Polls the AC adapter state every 5 s and mirrors it into a (read-only) ToggleSwitch.
///     Dispose it when the window closes.
/// </summary>
public sealed class PowerSourceDetection : IDisposable
{
    private readonly List<string> _possiblePowerSupplyPaths;
    private readonly Timer _powerSourceCheckTimer;
    private readonly ToggleSwitch _powerToggleSwitch;
    private bool _disposed;

    public PowerSourceDetection(ToggleSwitch powerToggleSwitch)
    {
        _powerToggleSwitch = powerToggleSwitch;

        // Common paths for power supply status on Linux systems (Acer laptops usually expose ACAD)
        _possiblePowerSupplyPaths = new List<string>
        {
            "/sys/class/power_supply/AC/online",
            "/sys/class/power_supply/ACAD/online",
            "/sys/class/power_supply/ADP1/online",
            "/sys/class/power_supply/AC0/online"
        };

        _powerSourceCheckTimer = new Timer(5000);
        _powerSourceCheckTimer.Elapsed += OnTimerElapsed;
        _powerSourceCheckTimer.AutoReset = true;
        _powerSourceCheckTimer.Start();

        // Initial check of power source
        UpdatePowerSourceStatus();
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        _powerSourceCheckTimer.Stop();
        _powerSourceCheckTimer.Elapsed -= OnTimerElapsed;
        _powerSourceCheckTimer.Dispose();
    }

    private void OnTimerElapsed(object? sender, ElapsedEventArgs e)
    {
        UpdatePowerSourceStatus();
    }

    private void UpdatePowerSourceStatus()
    {
        if (_disposed) return;
        var isPluggedIn = IsLaptopPluggedIn();

        Dispatcher.UIThread.Post(() =>
        {
            try
            {
                if (!_disposed && _powerToggleSwitch.IsChecked != isPluggedIn)
                    _powerToggleSwitch.IsChecked = isPluggedIn;
            }
            catch (Exception ex)
            {
                Console.WriteLine($"Error updating power source indicator: {ex.Message}");
            }
        });
    }

    private bool IsLaptopPluggedIn()
    {
        try
        {
            foreach (var path in _possiblePowerSupplyPaths)
                if (File.Exists(path))
                    return File.ReadAllText(path).Trim() == "1";

            // Any supply of type "Mains"
            const string root = "/sys/class/power_supply";
            if (Directory.Exists(root))
            {
                var mains = Directory.GetDirectories(root)
                    .Where(dir => File.Exists(Path.Combine(dir, "type")) &&
                                  File.ReadAllText(Path.Combine(dir, "type")).Trim() == "Mains")
                    .ToList();
                if (mains.Count > 0)
                    return mains.Any(dir => File.Exists(Path.Combine(dir, "online")) &&
                                            File.ReadAllText(Path.Combine(dir, "online")).Trim() == "1");
            }

            return CheckUsingUPower();
        }
        catch (Exception ex)
        {
            Console.WriteLine($"Error checking power status: {ex.Message}");
            return false;
        }
    }

    private static string RunTool(string fileName, string arguments)
    {
        using var process = new Process();
        process.StartInfo.FileName = fileName;
        process.StartInfo.Arguments = arguments;
        process.StartInfo.UseShellExecute = false;
        process.StartInfo.RedirectStandardOutput = true;
        process.StartInfo.CreateNoWindow = true;

        process.Start();
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

            return string.Empty;
        }

        return output.Wait(500) ? output.Result : string.Empty;
    }

    private bool CheckUsingUPower()
    {
        try
        {
            var output = RunTool("upower", "-i /org/freedesktop/UPower/devices/line_power_AC");
            return output.Contains("online:") && output.Contains("yes");
        }
        catch
        {
            return CheckUsingLsAcpi();
        }
    }

    private bool CheckUsingLsAcpi()
    {
        try
        {
            return RunTool("acpi", "-a").Contains("on-line");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"Error checking ACPI power status: {ex.Message}");
            return false;
        }
    }
}
