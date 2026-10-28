using System;
using System.Collections.Generic;
using System.Globalization;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.Media;
using Avalonia.Media.Immutable;

namespace DivAcerManagerMax;

/// <summary>
///     Interactive fan-curve plot. X = 20–105 °C, Y = 0–100 %.
///     Drag points (snap 1 °C / 1 %), double-click empty space to add (max 8),
///     right-click a point to remove (min 2). Constraints are enforced live:
///     temperatures strictly increasing (≥ 1 °C apart), percent non-decreasing and ≥ 1.
///     The active series (CPU or GPU) is editable; the other one is drawn dimmed.
/// </summary>
public class FanCurveEditor : Control
{
    private const double PadLeft = 40, PadRight = 12, PadTop = 22, PadBottom = 24;
    private const double PointSize = 8, HitRadius = 10;

    private static readonly IBrush GridBrush = new ImmutableSolidColorBrush(HudPalette.Hairline);
    private static readonly IBrush LabelBrush = new ImmutableSolidColorBrush(HudPalette.InkMuted);
    private static readonly IBrush CpuBrush = new ImmutableSolidColorBrush(HudPalette.Ink);
    private static readonly IBrush GpuBrush = new ImmutableSolidColorBrush(HudPalette.Accent);
    private static readonly IBrush DimBrush = new ImmutableSolidColorBrush(HudPalette.Ink, 0.22);
    private static readonly IBrush SafetyBrush = new ImmutableSolidColorBrush(HudPalette.Danger, 0.07);
    private static readonly IBrush SafetyTextBrush = new ImmutableSolidColorBrush(HudPalette.Danger, 0.8);
    private static readonly IBrush MarkerBrush = new ImmutableSolidColorBrush(HudPalette.Ink, 0.45);
    private static readonly IBrush BackgroundHitBrush = Brushes.Transparent;

    private FanCurve _curve = FanCurve.Default();
    private int _dragIndex = -1;
    private bool _editGpu;
    private int _selectedIndex = -1;

    /// <summary>Raised whenever the user changes a point.</summary>
    public event EventHandler? CurveChanged;

    public FanCurve Curve
    {
        get => _curve;
        set
        {
            _curve = value;
            _selectedIndex = -1;
            _dragIndex = -1;
            InvalidateVisual();
        }
    }

    /// <summary>true = edit the GPU series, false = CPU.</summary>
    public bool EditGpu
    {
        get => _editGpu;
        set
        {
            _editGpu = value;
            _selectedIndex = -1;
            InvalidateVisual();
        }
    }

    public int SelectedIndex => _selectedIndex;

    /// <summary>Live engine sample (current temperature marker + output %).</summary>
    public FanCurveSample? Live
    {
        get => _live;
        set
        {
            _live = value;
            InvalidateVisual();
        }
    }

    private FanCurveSample? _live;

    private List<int[]> Active => _curve.Series(_editGpu);

    // ------------------------------------------------------------------ geometry

    private Rect Plot => new(PadLeft, PadTop,
        Math.Max(1, Bounds.Width - PadLeft - PadRight), Math.Max(1, Bounds.Height - PadTop - PadBottom));

    private double X(double temp)
    {
        var p = Plot;
        return p.Left + (temp - FanCurve.MinTemp) / (FanCurve.MaxTemp - FanCurve.MinTemp) * p.Width;
    }

    private double Y(double percent)
    {
        var p = Plot;
        return p.Bottom - percent / 100.0 * p.Height;
    }

    private int TempAt(double x)
    {
        var p = Plot;
        return (int)Math.Round(FanCurve.MinTemp + (x - p.Left) / p.Width * (FanCurve.MaxTemp - FanCurve.MinTemp));
    }

    private int PercentAt(double y)
    {
        var p = Plot;
        return (int)Math.Round((p.Bottom - y) / p.Height * 100.0);
    }

    // ------------------------------------------------------------------ rendering

    private Typeface LabelTypeface()
    {
        return this.TryFindResource("MonoFont", out var f) && f is FontFamily family
            ? new Typeface(family)
            : new Typeface(FontFamily.Default);
    }

    private void DrawLabel(DrawingContext context, string text, double x, double y, IBrush brush,
        bool centerX = false, bool rightAlign = false)
    {
        var formatted = new FormattedText(text, CultureInfo.InvariantCulture, FlowDirection.LeftToRight,
            LabelTypeface(), 10, brush);
        if (centerX) x -= formatted.Width / 2;
        if (rightAlign) x -= formatted.Width;
        context.DrawText(formatted, new Point(x, y));
    }

    public override void Render(DrawingContext context)
    {
        var bounds = new Rect(Bounds.Size);
        if (bounds.Width < 80 || bounds.Height < 60) return;

        // Transparent fill so the whole area receives pointer events.
        context.FillRectangle(BackgroundHitBrush, bounds);

        var plot = Plot;
        var gridPen = new Pen(GridBrush, 1);

        // Safety zone ≥ 95 °C
        var safetyX = X(FanCurve.SafetyTemp);
        context.FillRectangle(SafetyBrush, new Rect(safetyX, plot.Top, plot.Right - safetyX, plot.Height));
        DrawLabel(context, "100 % safety", plot.Right - 2, plot.Top - 16, SafetyTextBrush, rightAlign: true);

        // Grid: every 10 °C / 20 %
        for (var t = 20; t <= 100; t += 10)
        {
            var x = Math.Round(X(t)) + 0.5;
            context.DrawLine(gridPen, new Point(x, plot.Top), new Point(x, plot.Bottom));
            DrawLabel(context, t.ToString(CultureInfo.InvariantCulture), x, plot.Bottom + 6, LabelBrush, true);
        }

        for (var pct = 0; pct <= 100; pct += 20)
        {
            var y = Math.Round(Y(pct)) + 0.5;
            context.DrawLine(gridPen, new Point(plot.Left, y), new Point(plot.Right, y));
            DrawLabel(context, pct.ToString(CultureInfo.InvariantCulture) + "%", plot.Left - 6, y - 7, LabelBrush,
                rightAlign: true);
        }

        DrawLabel(context, "°C", plot.Right, plot.Bottom + 6, LabelBrush, rightAlign: true);

        // Inactive series first (dimmed), then the active one on top.
        DrawSeries(context, _curve.Series(!_editGpu), DimBrush, false);
        DrawSeries(context, Active, _editGpu ? GpuBrush : CpuBrush, true);

        DrawLiveMarker(context, plot);
    }

    private void DrawSeries(DrawingContext context, List<int[]> points, IBrush brush, bool active)
    {
        if (points.Count == 0) return;
        var pen = new Pen(brush, active ? 1.5 : 1);
        var plot = Plot;

        // Flat before the first point and after the last one.
        var prev = new Point(plot.Left, Y(points[0][1]));
        foreach (var p in points)
        {
            var pt = new Point(X(p[0]), Y(p[1]));
            context.DrawLine(pen, prev, pt);
            prev = pt;
        }

        context.DrawLine(pen, prev, new Point(plot.Right, prev.Y));

        if (!active) return;
        for (var i = 0; i < points.Count; i++)
        {
            var c = new Point(X(points[i][0]), Y(points[i][1]));
            var size = i == _selectedIndex ? PointSize + 4 : PointSize;
            context.FillRectangle(brush, new Rect(c.X - size / 2, c.Y - size / 2, size, size));
            if (i == _selectedIndex || i == _dragIndex)
                DrawLabel(context, $"{points[i][0]}°C · {points[i][1]}%", c.X, c.Y - 22, brush, true);
        }
    }

    private void DrawLiveMarker(DrawingContext context, Rect plot)
    {
        var live = _live;
        if (live == null) return;

        // When the dGPU sleeps the daemon drives the GPU fan from the CPU temperature.
        var temp = _editGpu && !live.GpuAsleep ? live.GpuTemp : live.CpuTemp;
        var pct = _editGpu ? live.GpuPercent : live.CpuPercent;
        if (temp == null || double.IsNaN(temp.Value)) return;

        var x = Math.Round(X(Math.Clamp(temp.Value, FanCurve.MinTemp, FanCurve.MaxTemp))) + 0.5;
        context.DrawLine(new Pen(MarkerBrush, 1), new Point(x, plot.Top), new Point(x, plot.Bottom));

        var label = temp.Value.ToString("0", CultureInfo.InvariantCulture) + " °C" +
                    (pct.HasValue ? " → " + pct.Value.ToString(CultureInfo.InvariantCulture) + " %" : "") +
                    (_editGpu && live.GpuAsleep ? " (GPU asleep)" : "");
        DrawLabel(context, label, Math.Min(x + 4, plot.Right - 120), plot.Top - 16, CpuBrush);

        if (pct.HasValue)
        {
            var y = Y(pct.Value);
            context.FillRectangle(MarkerBrush, new Rect(x - 3, y - 3, 6, 6));
        }
    }

    // ------------------------------------------------------------------ editing

    private int HitTestPoint(Point position)
    {
        var best = -1;
        var bestDistance = HitRadius;
        for (var i = 0; i < Active.Count; i++)
        {
            var dx = X(Active[i][0]) - position.X;
            var dy = Y(Active[i][1]) - position.Y;
            var d = Math.Sqrt(dx * dx + dy * dy);
            if (d <= bestDistance)
            {
                best = i;
                bestDistance = d;
            }
        }

        return best;
    }

    /// <summary>Moves point i to (temp, pct) clamped so the series stays valid.</summary>
    private void MovePoint(int i, int temp, int pct)
    {
        var pts = Active;
        var minT = i > 0 ? pts[i - 1][0] + 1 : FanCurve.MinTemp;
        var maxT = i < pts.Count - 1 ? pts[i + 1][0] - 1 : FanCurve.MaxTemp;
        var minP = Math.Max(1, i > 0 ? pts[i - 1][1] : 1);
        var maxP = i < pts.Count - 1 ? pts[i + 1][1] : 100;
        if (maxT < minT || maxP < minP) return;

        pts[i][0] = Math.Clamp(temp, minT, maxT);
        pts[i][1] = Math.Clamp(pct, minP, maxP);
    }

    /// <summary>Inserts a point at (temp, pct) if there is room; returns its index or -1.</summary>
    public int AddPoint(int temp, int pct)
    {
        var pts = Active;
        if (pts.Count >= FanCurve.MaxPoints) return -1;
        temp = Math.Clamp(temp, FanCurve.MinTemp, FanCurve.MaxTemp);

        var index = 0;
        while (index < pts.Count && pts[index][0] < temp) index++;
        if (index < pts.Count && pts[index][0] == temp) return -1; // occupied

        var minP = Math.Max(1, index > 0 ? pts[index - 1][1] : 1);
        var maxP = index < pts.Count ? pts[index][1] : 100;
        pts.Insert(index, new[] { temp, Math.Clamp(pct, minP, Math.Max(minP, maxP)) });
        _selectedIndex = index;
        RaiseChanged();
        return index;
    }

    /// <summary>"+" button: adds a point in the middle of the widest temperature gap.</summary>
    public bool AddPointInLargestGap()
    {
        var pts = Active;
        if (pts.Count >= FanCurve.MaxPoints || pts.Count < 2) return false;
        var gapIndex = -1;
        var gap = 1;
        for (var i = 1; i < pts.Count; i++)
            if (pts[i][0] - pts[i - 1][0] > gap)
            {
                gap = pts[i][0] - pts[i - 1][0];
                gapIndex = i;
            }

        if (gapIndex < 0) return false;
        var t = (pts[gapIndex - 1][0] + pts[gapIndex][0]) / 2;
        var p = (pts[gapIndex - 1][1] + pts[gapIndex][1]) / 2;
        return AddPoint(t, p) >= 0;
    }

    /// <summary>"−" button / right-click: removes a point (the selected one by default), keeping ≥ 2.</summary>
    public bool RemovePoint(int index = -1)
    {
        var pts = Active;
        if (index < 0) index = _selectedIndex >= 0 ? _selectedIndex : pts.Count - 2;
        if (pts.Count <= FanCurve.MinPoints || index < 0 || index >= pts.Count) return false;
        pts.RemoveAt(index);
        _selectedIndex = -1;
        RaiseChanged();
        return true;
    }

    private void RaiseChanged()
    {
        InvalidateVisual();
        CurveChanged?.Invoke(this, EventArgs.Empty);
    }

    protected override void OnPointerPressed(PointerPressedEventArgs e)
    {
        base.OnPointerPressed(e);
        var position = e.GetPosition(this);
        var properties = e.GetCurrentPoint(this).Properties;
        var hit = HitTestPoint(position);

        if (properties.IsRightButtonPressed)
        {
            if (hit >= 0) RemovePoint(hit);
            e.Handled = true;
            return;
        }

        if (!properties.IsLeftButtonPressed) return;

        if (hit >= 0)
        {
            _dragIndex = hit;
            _selectedIndex = hit;
            e.Pointer.Capture(this);
            InvalidateVisual();
        }
        else if (e.ClickCount >= 2 && Plot.Contains(position))
        {
            AddPoint(TempAt(position.X), PercentAt(position.Y));
        }
        else
        {
            _selectedIndex = -1;
            InvalidateVisual();
        }

        e.Handled = true;
    }

    protected override void OnPointerMoved(PointerEventArgs e)
    {
        base.OnPointerMoved(e);
        if (_dragIndex < 0 || _dragIndex >= Active.Count) return;

        var position = e.GetPosition(this);
        MovePoint(_dragIndex, TempAt(position.X), PercentAt(position.Y));
        RaiseChanged();
        e.Handled = true;
    }

    protected override void OnPointerReleased(PointerReleasedEventArgs e)
    {
        base.OnPointerReleased(e);
        if (_dragIndex < 0) return;
        _dragIndex = -1;
        e.Pointer.Capture(null);
        InvalidateVisual();
        e.Handled = true;
    }

    protected override void OnPointerCaptureLost(PointerCaptureLostEventArgs e)
    {
        base.OnPointerCaptureLost(e);
        _dragIndex = -1;
        InvalidateVisual();
    }
}
