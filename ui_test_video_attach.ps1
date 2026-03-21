param(
    [int]$ChromePid,
    [string]$MediaPath,
    [string]$ScreenshotPath
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$ws = New-Object -ComObject WScript.Shell
$null = $ws.AppActivate($ChromePid)
Start-Sleep -Milliseconds 900

Set-Clipboard -Value 'https://x.com/compose/post'
[System.Windows.Forms.SendKeys]::SendWait('^l')
Start-Sleep -Milliseconds 250
[System.Windows.Forms.SendKeys]::SendWait('^v')
Start-Sleep -Milliseconds 250
[System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
Start-Sleep -Seconds 7

$js = 'javascript:(()=>{const el=document.querySelector(''input[type="file"]''); if(el){el.click();}})()'
Set-Clipboard -Value $js
[System.Windows.Forms.SendKeys]::SendWait('^l')
Start-Sleep -Milliseconds 250
[System.Windows.Forms.SendKeys]::SendWait('^v')
Start-Sleep -Milliseconds 250
[System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
Start-Sleep -Seconds 2

Set-Clipboard -Value $MediaPath
[System.Windows.Forms.SendKeys]::SendWait('^v')
Start-Sleep -Milliseconds 300
[System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
Start-Sleep -Seconds 12

$bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
$bmp.Save($ScreenshotPath, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose()
$bmp.Dispose()
