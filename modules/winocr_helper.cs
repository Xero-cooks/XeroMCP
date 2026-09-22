// winocr_helper.cs - Windows.Media.Ocr wrapper compiled to a plain .NET 4.x DLL.
// Compiled ONCE with csc.exe referencing WinRT .winmd files; PowerShell then
// LoadFrom()s the DLL and calls WinOcr.Recognize - no struct projection issues,
// no pip packages, no external processes.
//
// Build:
//   csc /nologo /target:library /out:winocr_helper.dll winocr_helper.cs ^
//     /r:System.Runtime.WindowsRuntime.dll ^
//     /r:"%WINDIR%\System32\WinMetadata\Windows.Foundation.winmd" ^
//     /r:"%WINDIR%\System32\WinMetadata\Windows.Graphics.winmd" ^
//     /r:"%WINDIR%\System32\WinMetadata\Windows.Storage.winmd" ^
//     /r:"%WINDIR%\System32\WinMetadata\Windows.Globalization.winmd" ^
//     /r:"%WINDIR%\System32\WinMetadata\Windows.Media.winmd"
using System;
using System.Collections.Generic;
using System.IO;
using System.Threading.Tasks;
using Windows.Foundation;
using Windows.Globalization;
using Windows.Graphics.Imaging;
using Windows.Media.Ocr;
using Windows.Storage.Streams;

public class OcrLineDto
{
    public string Text;
    public double X;
    public double Y;
    public double W;
    public double H;
}

public static class WinOcr
{
    public static List<OcrLineDto> Recognize(string imagePath, string lang)
    {
        var lines = new List<OcrLineDto>();

        OcrEngine engine = null;
        try { engine = OcrEngine.TryCreateFromLanguage(new Language(lang)); }
        catch { engine = null; }
        if (engine == null) { engine = OcrEngine.TryCreateFromUserProfileLanguages(); }
        if (engine == null) throw new Exception("no-ocr-language-pack");

        byte[] bytes = File.ReadAllBytes(imagePath);
        var stream = new InMemoryRandomAccessStream();
        var writer = new DataWriter(stream.GetOutputStreamAt(0));
        writer.WriteBytes(bytes);
        writer.StoreAsync().AsTask().Wait();
        writer.FlushAsync().AsTask().Wait();

        var decoder = BitmapDecoder.CreateAsync(stream).AsTask().Result;
        var bmp = decoder.GetSoftwareBitmapAsync().AsTask().Result;
        var result = engine.RecognizeAsync(bmp).AsTask().Result;

        foreach (OcrLine line in result.Lines)
        {
            // NOTE: modern SDK projections expose BoundingRect on OcrWord only;
            // a line's box is the union of its word rects (also more precise).
            double minX = double.MaxValue, minY = double.MaxValue;
            double maxX = double.MinValue, maxY = double.MinValue;
            foreach (OcrWord word in line.Words)
            {
                Rect wr = word.BoundingRect;
                if (wr.X < minX) minX = wr.X;
                if (wr.Y < minY) minY = wr.Y;
                if (wr.X + wr.Width > maxX) maxX = wr.X + wr.Width;
                if (wr.Y + wr.Height > maxY) maxY = wr.Y + wr.Height;
            }
            if (minX == double.MaxValue) continue;
            lines.Add(new OcrLineDto
            {
                Text = line.Text,
                X = minX, Y = minY, W = maxX - minX, H = maxY - minY,
            });
        }
        return lines;
    }
}
