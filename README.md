# X Auto Poster

X への自動投稿を管理する Flask アプリです。通常の Playwright 投稿に加えて、Windows ローカルでは既存の Chrome プロフィールを再利用した画像・動画投稿も行えます。

## 主な機能

- 複数アカウントの投稿設定管理
- 予約時刻、Webhook、メディアの設定
- `今すぐ投稿`
- `既存Chromeで投稿`
- Cloud Run / Cloud Scheduler 前提のデプロイ

## ローカル起動

```powershell
pip install -r requirements.txt
$env:LOCAL_MODE='1'
py app.py
```

既定では `http://127.0.0.1:8080/` で起動します。

## 既存 Chrome 投稿

Windows ローカルでは、既存の Chrome プロフィールを使う投稿ヘルパーを利用できます。

```powershell
py existing_profile_media_post.py --media-path .\data\media\video-post-test.mp4
```

この経路は Chrome を再起動します。作業中の Chrome ウィンドウがある場合は注意してください。

## デプロイ

`deploy.ps1` は `X_USERNAME` と `X_PASSWORD` を環境変数から読み込みます。

```powershell
$env:X_USERNAME='your_username'
$env:X_PASSWORD='your_password'
.\deploy.ps1 -FirstDeploy
```
