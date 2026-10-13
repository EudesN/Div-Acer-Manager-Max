using System.Linq;
using Avalonia;
using Avalonia.Controls.ApplicationLifetimes;
using Avalonia.Markup.Xaml;
using Avalonia.Media;
using Avalonia.Themes.Fluent;

namespace DivAcerManagerMax;

public class App : Application
{
    public override void Initialize()
    {
        // The accent colour and its variants are defined in App.axaml.
        AvaloniaXamlLoader.Load(this);
    }

    public override void OnFrameworkInitializationCompleted()
    {
        if (ApplicationLifetime is IClassicDesktopStyleApplicationLifetime desktop)
            desktop.MainWindow = new MainWindow();


        base.OnFrameworkInitializationCompleted();
    }
}