using System;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Media;
using Avalonia.Media.Immutable;

namespace DivAcerManagerMax;

/// <summary>
/// Code-side copy of the design tokens defined in App.axaml (keep both in sync).
/// Used for control defaults and for things drawn outside XAML (charts).
/// </summary>
public static class HudPalette
{
    public static readonly Color Bg = Color.FromRgb(0x0B, 0x0C, 0x0E);
    public static readonly Color Surface = Color.FromRgb(0x12, 0x14, 0x17);
    public static readonly Color SurfaceRaised = Color.FromRgb(0x17, 0x1A, 0x1E);
    public static readonly Color Hairline = Color.FromRgb(0x2A, 0x2E, 0x33);
    public static readonly Color LineStrong = Color.FromRgb(0x3A, 0x3F, 0x45);
    public static readonly Color Ink = Color.FromRgb(0xEC, 0xE7, 0xDD);
    public static readonly Color InkSecondary = Color.FromRgb(0xB8, 0xB2, 0xA7);
    public static readonly Color InkMuted = Color.FromRgb(0x77, 0x73, 0x6C);
    public static readonly Color InkDisabled = Color.FromRgb(0x4A, 0x48, 0x43);
    public static readonly Color Accent = Color.FromRgb(0xE8, 0xF2, 0x3A);
    public static readonly Color Danger = Color.FromRgb(0xFF, 0x5A, 0x4E);
    public static readonly Color Ok = Color.FromRgb(0x7F, 0xD8, 0xC6);

    public static readonly IBrush SurfaceBrush = new ImmutableSolidColorBrush(Surface);
    public static readonly IBrush HairlineBrush = new ImmutableSolidColorBrush(Hairline);
    public static readonly IBrush LineStrongBrush = new ImmutableSolidColorBrush(LineStrong);
    public static readonly IBrush Ink20Brush = new ImmutableSolidColorBrush(Ink, 0.20);
    public static readonly IBrush Ink40Brush = new ImmutableSolidColorBrush(Ink, 0.40);
    public static readonly IBrush ScanlineBrush = new ImmutableSolidColorBrush(Ink, 0.035);

    // Status colours (daemon chip / toast dot)
    public static readonly IBrush AccentBrush = new ImmutableSolidColorBrush(Accent);
    public static readonly IBrush DangerBrush = new ImmutableSolidColorBrush(Danger);
    public static readonly IBrush OkBrush = new ImmutableSolidColorBrush(Ok);
    public static readonly IBrush InkMutedBrush = new ImmutableSolidColorBrush(InkMuted);
}

/// <summary>
/// Decorator drawn as a HUD panel: a filled polygon with chamfered (cut) corners,
/// an optional 1px outline, an optional short accent bar on the top edge and
/// optional small corner ticks on the corners that are not cut.
/// </summary>
public class HudPanel : Decorator
{
    public static readonly StyledProperty<IBrush?> BackgroundProperty =
        AvaloniaProperty.Register<HudPanel, IBrush?>(nameof(Background), HudPalette.SurfaceBrush);

    public static readonly StyledProperty<IBrush?> BorderBrushProperty =
        AvaloniaProperty.Register<HudPanel, IBrush?>(nameof(BorderBrush), HudPalette.HairlineBrush);

    public static readonly StyledProperty<double> BorderThicknessProperty =
        AvaloniaProperty.Register<HudPanel, double>(nameof(BorderThickness), 1);

    public static readonly StyledProperty<double> CutProperty =
        AvaloniaProperty.Register<HudPanel, double>(nameof(Cut), 12);

    public static readonly StyledProperty<bool> CutTopLeftProperty =
        AvaloniaProperty.Register<HudPanel, bool>(nameof(CutTopLeft));

    public static readonly StyledProperty<bool> CutTopRightProperty =
        AvaloniaProperty.Register<HudPanel, bool>(nameof(CutTopRight), true);

    public static readonly StyledProperty<bool> CutBottomRightProperty =
        AvaloniaProperty.Register<HudPanel, bool>(nameof(CutBottomRight));

    public static readonly StyledProperty<bool> CutBottomLeftProperty =
        AvaloniaProperty.Register<HudPanel, bool>(nameof(CutBottomLeft), true);

    public static readonly StyledProperty<IBrush?> AccentBrushProperty =
        AvaloniaProperty.Register<HudPanel, IBrush?>(nameof(AccentBrush));

    public static readonly StyledProperty<double> AccentLengthProperty =
        AvaloniaProperty.Register<HudPanel, double>(nameof(AccentLength), 56);

    public static readonly StyledProperty<IBrush?> TickBrushProperty =
        AvaloniaProperty.Register<HudPanel, IBrush?>(nameof(TickBrush));

    public static readonly StyledProperty<double> TickLengthProperty =
        AvaloniaProperty.Register<HudPanel, double>(nameof(TickLength), 6);

    static HudPanel()
    {
        AffectsRender<HudPanel>(BackgroundProperty, BorderBrushProperty, BorderThicknessProperty, CutProperty,
            CutTopLeftProperty, CutTopRightProperty, CutBottomRightProperty, CutBottomLeftProperty,
            AccentBrushProperty, AccentLengthProperty, TickBrushProperty, TickLengthProperty);
    }

    public IBrush? Background
    {
        get => GetValue(BackgroundProperty);
        set => SetValue(BackgroundProperty, value);
    }

    public IBrush? BorderBrush
    {
        get => GetValue(BorderBrushProperty);
        set => SetValue(BorderBrushProperty, value);
    }

    public double BorderThickness
    {
        get => GetValue(BorderThicknessProperty);
        set => SetValue(BorderThicknessProperty, value);
    }

    public double Cut
    {
        get => GetValue(CutProperty);
        set => SetValue(CutProperty, value);
    }

    public bool CutTopLeft
    {
        get => GetValue(CutTopLeftProperty);
        set => SetValue(CutTopLeftProperty, value);
    }

    public bool CutTopRight
    {
        get => GetValue(CutTopRightProperty);
        set => SetValue(CutTopRightProperty, value);
    }

    public bool CutBottomRight
    {
        get => GetValue(CutBottomRightProperty);
        set => SetValue(CutBottomRightProperty, value);
    }

    public bool CutBottomLeft
    {
        get => GetValue(CutBottomLeftProperty);
        set => SetValue(CutBottomLeftProperty, value);
    }

    public IBrush? AccentBrush
    {
        get => GetValue(AccentBrushProperty);
        set => SetValue(AccentBrushProperty, value);
    }

    public double AccentLength
    {
        get => GetValue(AccentLengthProperty);
        set => SetValue(AccentLengthProperty, value);
    }

    /// <summary>Brush for the small L-shaped ticks drawn on non-cut corners (null = no ticks).</summary>
    public IBrush? TickBrush
    {
        get => GetValue(TickBrushProperty);
        set => SetValue(TickBrushProperty, value);
    }

    public double TickLength
    {
        get => GetValue(TickLengthProperty);
        set => SetValue(TickLengthProperty, value);
    }

    private static double Sanitize(double value)
    {
        return double.IsNaN(value) || double.IsInfinity(value) || value < 0 ? 0 : value;
    }

    public override void Render(DrawingContext context)
    {
        var stroke = BorderBrush != null ? Sanitize(BorderThickness) : 0;
        var inset = stroke / 2;
        var rect = new Rect(Bounds.Size).Deflate(inset);
        if (rect.Width <= 0 || rect.Height <= 0) return;

        var cut = Math.Min(Sanitize(Cut), Math.Min(rect.Width, rect.Height) / 2);
        var tl = CutTopLeft ? cut : 0;
        var tr = CutTopRight ? cut : 0;
        var br = CutBottomRight ? cut : 0;
        var bl = CutBottomLeft ? cut : 0;

        var geometry = new StreamGeometry();
        using (var g = geometry.Open())
        {
            g.BeginFigure(new Point(rect.Left + tl, rect.Top), true);
            g.LineTo(new Point(rect.Right - tr, rect.Top));
            g.LineTo(new Point(rect.Right, rect.Top + tr));
            g.LineTo(new Point(rect.Right, rect.Bottom - br));
            g.LineTo(new Point(rect.Right - br, rect.Bottom));
            g.LineTo(new Point(rect.Left + bl, rect.Bottom));
            g.LineTo(new Point(rect.Left, rect.Bottom - bl));
            g.LineTo(new Point(rect.Left, rect.Top + tl));
            g.EndFigure(true);
        }

        context.DrawGeometry(Background, stroke > 0 ? new Pen(BorderBrush, stroke) : null, geometry);

        var accent = AccentBrush;
        if (accent != null)
        {
            var start = rect.Left + tl + 10;
            var length = Math.Min(Sanitize(AccentLength), rect.Width - tl - tr - 20);
            if (length > 0)
                context.FillRectangle(accent, new Rect(start, 0, length, Math.Max(2, stroke + 1)));
        }

        var tick = TickBrush;
        var tickLength = Math.Min(Sanitize(TickLength), Math.Min(rect.Width, rect.Height) / 3);
        if (tick != null && tickLength >= 2)
        {
            var w = Bounds.Width;
            var h = Bounds.Height;
            const double t = 1; // tick thickness
            if (!CutTopLeft)
            {
                context.FillRectangle(tick, new Rect(0, 0, tickLength, t));
                context.FillRectangle(tick, new Rect(0, 0, t, tickLength));
            }

            if (!CutTopRight)
            {
                context.FillRectangle(tick, new Rect(w - tickLength, 0, tickLength, t));
                context.FillRectangle(tick, new Rect(w - t, 0, t, tickLength));
            }

            if (!CutBottomRight)
            {
                context.FillRectangle(tick, new Rect(w - tickLength, h - t, tickLength, t));
                context.FillRectangle(tick, new Rect(w - t, h - tickLength, t, tickLength));
            }

            if (!CutBottomLeft)
            {
                context.FillRectangle(tick, new Rect(0, h - t, tickLength, t));
                context.FillRectangle(tick, new Rect(0, h - tickLength, t, tickLength));
            }
        }
    }
}

/// <summary>Diagonal hazard-style stripes filling the control's bounds.</summary>
public class Stripes : Control
{
    public static readonly StyledProperty<IBrush?> FillProperty =
        AvaloniaProperty.Register<Stripes, IBrush?>(nameof(Fill), HudPalette.Ink20Brush);

    public static readonly StyledProperty<double> StripeWidthProperty =
        AvaloniaProperty.Register<Stripes, double>(nameof(StripeWidth), 4);

    public static readonly StyledProperty<double> GapProperty =
        AvaloniaProperty.Register<Stripes, double>(nameof(Gap), 4);

    static Stripes()
    {
        AffectsRender<Stripes>(FillProperty, StripeWidthProperty, GapProperty);
        IsHitTestVisibleProperty.OverrideDefaultValue<Stripes>(false);
    }

    public IBrush? Fill
    {
        get => GetValue(FillProperty);
        set => SetValue(FillProperty, value);
    }

    public double StripeWidth
    {
        get => GetValue(StripeWidthProperty);
        set => SetValue(StripeWidthProperty, value);
    }

    public double Gap
    {
        get => GetValue(GapProperty);
        set => SetValue(GapProperty, value);
    }

    public override void Render(DrawingContext context)
    {
        var size = Bounds.Size;
        var stripeWidth = StripeWidth;
        var step = stripeWidth + Gap;
        if (Fill == null || double.IsNaN(step) || double.IsInfinity(step) || step < 1 || stripeWidth <= 0 ||
            size.Width <= 0 || size.Height <= 0)
            return;

        using (context.PushClip(new Rect(size)))
        {
            var h = size.Height;
            for (var x = -h; x < size.Width; x += step)
            {
                var stripe = new StreamGeometry();
                using (var g = stripe.Open())
                {
                    g.BeginFigure(new Point(x, h), true);
                    g.LineTo(new Point(x + h, 0));
                    g.LineTo(new Point(x + h + stripeWidth, 0));
                    g.LineTo(new Point(x + stripeWidth, h));
                    g.EndFigure(true);
                }

                context.DrawGeometry(Fill, null, stripe);
            }
        }
    }
}

/// <summary>Row of dots used as a leader between a label and its value ("KEY ....... VALUE").</summary>
public class DotLeader : Control
{
    public static readonly StyledProperty<IBrush?> FillProperty =
        AvaloniaProperty.Register<DotLeader, IBrush?>(nameof(Fill), HudPalette.Ink20Brush);

    public static readonly StyledProperty<double> SpacingProperty =
        AvaloniaProperty.Register<DotLeader, double>(nameof(Spacing), 5);

    static DotLeader()
    {
        AffectsRender<DotLeader>(FillProperty, SpacingProperty);
        IsHitTestVisibleProperty.OverrideDefaultValue<DotLeader>(false);
    }

    public IBrush? Fill
    {
        get => GetValue(FillProperty);
        set => SetValue(FillProperty, value);
    }

    public double Spacing
    {
        get => GetValue(SpacingProperty);
        set => SetValue(SpacingProperty, value);
    }

    protected override Size MeasureOverride(Size availableSize) => new(0, 4);

    public override void Render(DrawingContext context)
    {
        var spacing = Spacing;
        if (Fill == null || double.IsNaN(spacing) || double.IsInfinity(spacing) || spacing < 1.5) return;
        var y = Math.Round(Bounds.Height / 2) - 0.5;
        for (var x = 0.0; x + 1.5 <= Bounds.Width; x += spacing)
            context.FillRectangle(Fill, new Rect(x, y, 1.5, 1.5));
    }
}

/// <summary>Thin vertical gaps drawn over a bar to make it look segmented.</summary>
public class SegmentGaps : Control
{
    public static readonly StyledProperty<IBrush?> FillProperty =
        AvaloniaProperty.Register<SegmentGaps, IBrush?>(nameof(Fill), HudPalette.SurfaceBrush);

    public static readonly StyledProperty<double> SegmentWidthProperty =
        AvaloniaProperty.Register<SegmentGaps, double>(nameof(SegmentWidth), 5);

    public static readonly StyledProperty<double> GapProperty =
        AvaloniaProperty.Register<SegmentGaps, double>(nameof(Gap), 2);

    static SegmentGaps()
    {
        AffectsRender<SegmentGaps>(FillProperty, SegmentWidthProperty, GapProperty);
        IsHitTestVisibleProperty.OverrideDefaultValue<SegmentGaps>(false);
    }

    public IBrush? Fill
    {
        get => GetValue(FillProperty);
        set => SetValue(FillProperty, value);
    }

    public double SegmentWidth
    {
        get => GetValue(SegmentWidthProperty);
        set => SetValue(SegmentWidthProperty, value);
    }

    public double Gap
    {
        get => GetValue(GapProperty);
        set => SetValue(GapProperty, value);
    }

    public override void Render(DrawingContext context)
    {
        var segment = SegmentWidth;
        var gap = Gap;
        var step = segment + gap;
        // Guard against zero/negative/NaN sizes: a non-positive step would never advance x.
        if (Fill == null || double.IsNaN(step) || double.IsInfinity(step) || gap <= 0 || segment <= 0 || step < 1)
            return;

        for (var x = segment; x < Bounds.Width; x += step)
            context.FillRectangle(Fill, new Rect(x, 0, gap, Bounds.Height));
    }
}

/// <summary>CRT-style horizontal scanlines; meant as a non-interactive overlay.</summary>
public class Scanlines : Control
{
    public static readonly StyledProperty<IBrush?> FillProperty =
        AvaloniaProperty.Register<Scanlines, IBrush?>(nameof(Fill), HudPalette.ScanlineBrush);

    public static readonly StyledProperty<double> PitchProperty =
        AvaloniaProperty.Register<Scanlines, double>(nameof(Pitch), 3);

    static Scanlines()
    {
        AffectsRender<Scanlines>(FillProperty, PitchProperty);
        IsHitTestVisibleProperty.OverrideDefaultValue<Scanlines>(false);
    }

    public IBrush? Fill
    {
        get => GetValue(FillProperty);
        set => SetValue(FillProperty, value);
    }

    public double Pitch
    {
        get => GetValue(PitchProperty);
        set => SetValue(PitchProperty, value);
    }

    public override void Render(DrawingContext context)
    {
        var pitch = Pitch;
        if (Fill == null || double.IsNaN(pitch) || double.IsInfinity(pitch) || pitch < 2) return;
        for (var y = 0.0; y < Bounds.Height; y += pitch)
            context.FillRectangle(Fill, new Rect(0, y, Bounds.Width, 1));
    }
}

/// <summary>
/// Segmented horizontal meter (utilization, fan speed, charge). Draws discrete segments:
/// the ones below Value/Maximum use Fill, the rest use TrackBrush. Self-drawn, so it has no
/// template parts and does not depend on the card background colour.
/// </summary>
public class SegmentBar : Control
{
    public static readonly StyledProperty<double> ValueProperty =
        AvaloniaProperty.Register<SegmentBar, double>(nameof(Value));

    public static readonly StyledProperty<double> MaximumProperty =
        AvaloniaProperty.Register<SegmentBar, double>(nameof(Maximum), 100);

    public static readonly StyledProperty<IBrush?> FillProperty =
        AvaloniaProperty.Register<SegmentBar, IBrush?>(nameof(Fill), new ImmutableSolidColorBrush(HudPalette.Ink));

    public static readonly StyledProperty<IBrush?> TrackBrushProperty =
        AvaloniaProperty.Register<SegmentBar, IBrush?>(nameof(TrackBrush), HudPalette.HairlineBrush);

    public static readonly StyledProperty<double> SegmentWidthProperty =
        AvaloniaProperty.Register<SegmentBar, double>(nameof(SegmentWidth), 4);

    public static readonly StyledProperty<double> GapProperty =
        AvaloniaProperty.Register<SegmentBar, double>(nameof(Gap), 2);

    static SegmentBar()
    {
        AffectsRender<SegmentBar>(ValueProperty, MaximumProperty, FillProperty, TrackBrushProperty,
            SegmentWidthProperty, GapProperty);
        IsHitTestVisibleProperty.OverrideDefaultValue<SegmentBar>(false);
    }

    public double Value
    {
        get => GetValue(ValueProperty);
        set => SetValue(ValueProperty, value);
    }

    public double Maximum
    {
        get => GetValue(MaximumProperty);
        set => SetValue(MaximumProperty, value);
    }

    public IBrush? Fill
    {
        get => GetValue(FillProperty);
        set => SetValue(FillProperty, value);
    }

    public IBrush? TrackBrush
    {
        get => GetValue(TrackBrushProperty);
        set => SetValue(TrackBrushProperty, value);
    }

    public double SegmentWidth
    {
        get => GetValue(SegmentWidthProperty);
        set => SetValue(SegmentWidthProperty, value);
    }

    public double Gap
    {
        get => GetValue(GapProperty);
        set => SetValue(GapProperty, value);
    }

    protected override Size MeasureOverride(Size availableSize) => new(0, 6);

    public override void Render(DrawingContext context)
    {
        var width = Bounds.Width;
        var height = Bounds.Height;
        var segment = SegmentWidth;
        var gap = Gap;
        var step = segment + gap;
        if (width <= 0 || height <= 0 || double.IsNaN(step) || double.IsInfinity(step) || segment <= 0 ||
            gap < 0 || step < 1)
            return;

        var max = Maximum;
        var value = Value;
        var fraction = max > 0 && !double.IsNaN(value) && !double.IsNaN(max) ? Math.Clamp(value / max, 0, 1) : 0;
        var filledWidth = fraction * width;

        // Gap == 0: plain continuous bar (track + filled part).
        if (gap == 0)
        {
            if (TrackBrush != null) context.FillRectangle(TrackBrush, new Rect(0, 0, width, height));
            if (Fill != null && filledWidth > 0) context.FillRectangle(Fill, new Rect(0, 0, filledWidth, height));
            return;
        }

        for (var x = 0.0; x < width; x += step)
        {
            var w = Math.Min(segment, width - x);
            var brush = x < filledWidth ? Fill : TrackBrush;
            if (brush != null && w > 0)
                context.FillRectangle(brush, new Rect(x, 0, w, height));
        }
    }
}
