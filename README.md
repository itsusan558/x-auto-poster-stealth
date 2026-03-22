# X Auto Poster

既存の Chrome プロフィールを使って X に投稿するローカル向けツールです。

## できること

- 本文の投稿
- 画像を最大 4 件まで添付
- 画像 + 動画 1 本の混在添付
- ローカル予約投稿
- `hf-video-compiler` と連携して、作成した動画や画像を取り込み

## ローカル起動

```powershell
pip install -r requirements.txt
$env:LOCAL_MODE='1'
$env:PORT='8093'
py app.py
```

起動後は [http://127.0.0.1:8093/](http://127.0.0.1:8093/) を開いて使います。

## 既存 Chrome 投稿

Windows ローカルでは、既存の Chrome プロフィールを再利用して投稿します。

```powershell
py existing_profile_media_post.py --profile-directory Default --text "テスト投稿"
```

画像や動画を付ける場合は `--media-path` を複数回指定できます。

## デプロイ

`deploy.ps1` は `X_USERNAME` と `X_PASSWORD` を環境変数から読み込みます。

```powershell
$env:X_USERNAME='your_username'
$env:X_PASSWORD='your_password'
.\deploy.ps1 -FirstDeploy
```
