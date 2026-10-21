using System;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Media;

namespace DivAcerManagerMax;

/// <summary>
/// Decorator drawn as a HUD panel: a filled polygon with chamfered (cut) corners,
/// an optional outline and an optional short accent bar on the top edge.
/// </summary>
public class HudPanel : Decorator
{
    public static readonly StyledProperty<IBrush?> BackgroundProperty =
        AvaloniaProperty.Register<HudPanel, IBrush?>(nameof(Background));

    public static readonly StyledProperty<IBrush?> BorderBrushProperty =
        AvaloniaProperty.Register<HudPanel, IBrush?>(nameof(BorderBrush));

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

    static HudPanel()
    {
        AffectsRender<HudPanel>(BackgroundProperty, BorderBrushProperty, BorderThicknessProperty, CutProperty,
            CutTopLeftProperty, CutTopRightProperty, CutBottomRightProperty, CutBottomLeftProperty,
            AccentBrushProperty, AccentLengthProperty);
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

    public override void Render(DrawingContext context)
    {
        var stroke = BorderBrush != null ? BorderThickness : 0;
        var inset = stroke / 2;
        var rect = new Rect(Bounds.Size).Deflate(inset);
        if (rect.Width <= 0 || rect.Height <= 0) return;

        var cut = Math.Min(Cut, Math.Min(rect.Width, rect.Height) / 2);
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

        if (AccentBrush != null)
        {
            var start = rect.Left + tl + 10;
            var length = Math.Min(AccentLength, rect.Width - tl - tr - 20);
            if (length > 0)
                context.FillRectangle(AccentBrush, new Rect(start, 0, length, Math.Max(2, stroke + 1)));
        }
    }
}

/// <summary>Diagonal hazard-style stripes filling the control's bounds.</summary>
public class Stripes : Control
{
    public static readonly StyledProperty<IBrush?> FillProperty =
        AvaloniaProperty.Register<Stripes, IBrush?>(nameof(Fill));

    public static readonly StyledProperty<double> StripeWidthProperty =
        AvaloniaProperty.Register<Stripes, double>(nameof(StripeWidth), 4);

    public static readonly StyledProperty<double> GapProperty =
        AvaloniaProperty.Register<Stripes, double>(nameof(Gap), 4);

    static Stripes()
    {
        AffectsRender<Stripes>(FillProperty, StripeWidthProperty, GapProperty);
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
        var step = StripeWidth + Gap;
        if (Fill == null || step <= 0 || size.Width <= 0 || size.Height <= 0) return;

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
                    g.LineTo(new Point(x + h + StripeWidth, 0));
                    g.LineTo(new Point(x + StripeWidth, h));
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
        AvaloniaProperty.Register<DotLeader, IBrush?>(nameof(Fill));

    public static readonly StyledProperty<double> SpacingProperty =
        AvaloniaProperty.Register<DotLeader, double>(nameof(Spacing), 5);

    static DotLeader()
    {
        AffectsRender<DotLeader>(FillProperty, SpacingProperty);
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
        if (Fill == null || Spacing <= 0) return;
        var y = Math.Round(Bounds.Height / 2) - 0.5;
        for (var x = 0.0; x + 1.5 <= Bounds.Width; x += Spacing)
            context.FillRectangle(Fill, new Rect(x, y, 1.5, 1.5));
    }
}

/// <summary>Thin vertical gaps drawn over a bar to make it look segmented.</summary>
public class SegmentGaps : Control
{
    public static readonly StyledProperty<IBrush?> FillProperty =
        AvaloniaProperty.Register<SegmentGaps, IBrush?>(nameof(Fill));

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
        if (Fill == null || Gap <= 0) return;
        for (var x = SegmentWidth; x < Bounds.Width; x += SegmentWidth + Gap)
            context.FillRectangle(Fill, new Rect(x, 0, Gap, Bounds.Height));
    }
}

/// <summary>CRT-style horizontal scanlines; meant as a non-interactive overlay.</summary>
public class Scanlines : Control
{
    public static readonly StyledProperty<IBrush?> FillProperty =
        AvaloniaProperty.Register<Scanlines, IBrush?>(nameof(Fill));

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
        if (Fill == null || Pitch < 2) return;
        for (var y = 0.0; y < Bounds.Height; y += Pitch)
            context.FillRectangle(Fill, new Rect(0, y, Bounds.Width, 1));
    }
}
