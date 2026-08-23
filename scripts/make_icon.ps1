$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Drawing

$atlasRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$iconPath = Join-Path $atlasRoot "ui\atlas.ico"
$sizes = @(256, 48, 32, 16)

function New-AtlasPng([int] $size) {
    $bitmap = New-Object System.Drawing.Bitmap($size, $size, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $stream = New-Object System.IO.MemoryStream

    try {
        $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
        $graphics.Clear([System.Drawing.Color]::Transparent)

        $center = $size / 2.0
        $outerRadius = $size * 0.45
        $innerRadius = $size * 0.20
        $purple = [System.Drawing.ColorTranslator]::FromHtml("#7C5CFF")

        $outer = New-Object 'System.Drawing.PointF[]' 4
        $outer[0] = [System.Drawing.PointF]::new($center, ($center - $outerRadius))
        $outer[1] = [System.Drawing.PointF]::new(($center + $outerRadius), $center)
        $outer[2] = [System.Drawing.PointF]::new($center, ($center + $outerRadius))
        $outer[3] = [System.Drawing.PointF]::new(($center - $outerRadius), $center)
        $outerBrush = New-Object System.Drawing.SolidBrush($purple)
        $graphics.FillPolygon($outerBrush, $outer)
        $outerBrush.Dispose()

        $inner = New-Object 'System.Drawing.PointF[]' 4
        $inner[0] = [System.Drawing.PointF]::new($center, ($center - $innerRadius))
        $inner[1] = [System.Drawing.PointF]::new(($center + $innerRadius), $center)
        $inner[2] = [System.Drawing.PointF]::new($center, ($center + $innerRadius))
        $inner[3] = [System.Drawing.PointF]::new(($center - $innerRadius), $center)
        $graphics.FillPolygon([System.Drawing.Brushes]::White, $inner)

        $bitmap.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
        return ,($stream.ToArray())
    }
    finally {
        $stream.Dispose()
        $graphics.Dispose()
        $bitmap.Dispose()
    }
}

$images = @($sizes | ForEach-Object { New-AtlasPng $_ })
$output = New-Object System.IO.FileStream($iconPath, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write)
$writer = New-Object System.IO.BinaryWriter($output)

try {
    $writer.Write([UInt16]0)
    $writer.Write([UInt16]1)
    $writer.Write([UInt16]$images.Count)

    $offset = 6 + (16 * $images.Count)
    for ($index = 0; $index -lt $images.Count; $index++) {
        $size = $sizes[$index]
        $writer.Write([byte]$(if ($size -eq 256) { 0 } else { $size }))
        $writer.Write([byte]$(if ($size -eq 256) { 0 } else { $size }))
        $writer.Write([byte]0)
        $writer.Write([byte]0)
        $writer.Write([UInt16]1)
        $writer.Write([UInt16]32)
        $writer.Write([UInt32]$images[$index].Length)
        $writer.Write([UInt32]$offset)
        $offset += $images[$index].Length
    }

    foreach ($image in $images) {
        $writer.Write($image)
    }
}
finally {
    $writer.Dispose()
    $output.Dispose()
}

Write-Output "Created Atlas icon: $iconPath ($($sizes -join ', ') px)"
