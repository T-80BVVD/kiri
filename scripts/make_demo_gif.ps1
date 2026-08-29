# make_demo_gif.ps1 — 自动把 Kiri demo 录成 gif (无 ffmpeg, 用 Pillow 拼)
# 原理: 启动 demo → 等念流浮现 → Edge headless 多次采样截图 → Pillow 拼 GIF
# 产物: D:\project\kiri-public\docs\demo.gif
$ErrorActionPreference = "Stop"
$repo = "D:\project\kiri-public"
$edge = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
$shot = "$env:TEMP\_kiri_gif"
$out = "$repo\docs\demo.gif"

# 清理旧帧
if (Test-Path $shot) { Remove-Item $shot -Recurse -Force }
New-Item -ItemType Directory -Force -Path $shot | Out-Null
New-Item -ItemType Directory -Force -Path "$repo\docs" | Out-Null

# 1. 启动 demo
$runner = "$env:TEMP\_kiri_demo.py"
Set-Content -Path $runner -Value "import sys; sys.path.insert(0, 'src/kiri'); import demo; demo.main()" -Encoding ascii
$proc = Start-Process -FilePath python -ArgumentList $runner -WorkingDirectory $repo -PassThru -WindowStyle Hidden
Start-Sleep -Seconds 3
Write-Host "[1/6] demo 已启动 (pid=$($proc.Id))，等待念流浮现..."

# 2. 让念流浮几条 (睡眠期让服务器端 tick 跑)
Start-Sleep -Seconds 10
Write-Host "[2/6] 念流已浮现，开始采样..."

# 3. 多次采样截图 (每次间隔 ~1.5s, 抓念流/状态变化)
for ($i = 1; $i -le 6; $i++) {
    $frame = "$shot\frame_$i.png"
    & $edge --headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage `
        --virtual-time-budget=1200 --window-size=1280,800 `
        --user-data-dir="$env:TEMP\_edge_gif_$i" `
        --screenshot="$frame" "http://127.0.0.1:8766" 2>$null | Out-Null
    Write-Host "  帧 $i 已拍"
    Start-Sleep -Seconds 1
}

# 4. 停 demo
Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
Remove-Item $runner -ErrorAction SilentlyContinue

# 5. 用 Pillow 拼 gif
$py = @"
from PIL import Image
import glob, os
files = sorted(glob.glob(r'$shot\frame_*.png'))
imgs = [Image.open(f).convert('RGB') for f in files]
imgs[0].save(r'$out', save_all=True, append_images=imgs[1:], duration=1500, loop=0)
print('gif 已生成:', r'$out', '共', len(imgs), '帧')
"@
Set-Content -Path "$env:TEMP\_make_gif.py" -Value $py -Encoding ascii
python "$env:TEMP\_make_gif.py"
Remove-Item "$env:TEMP\_make_gif.py" -ErrorAction SilentlyContinue

if (Test-Path $out) { Write-Host "[完成] $out ($((Get-Item $out).Length) bytes)" }
