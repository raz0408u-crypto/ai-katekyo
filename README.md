# AI家庭教師（新規版）

Gemini Interactions API + Flask + SQLite で作った高校生向けAI家庭教師です。

## 必要なもの

- Python
- Gemini APIキー

## 初回セットアップ

プロジェクトフォルダで:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`.env.example` をコピーして `.env` を作り、APIキーを設定します。

```env
GEMINI_API_KEY=あなたのAPIキー
GEMINI_MODEL=gemini-3.6-flash
```

`.env` は他人に見せないでください。

## 起動

```powershell
.\.venv\Scripts\python.exe app.py
```

ブラウザ:

http://127.0.0.1:5000

## 機能

- Gemini Interactions APIによるAI家庭教師
- 会話履歴をSQLiteに保存
- Interactions APIの会話IDを保存して継続会話
- 生徒プロフィール
- 教科・単元・難易度を指定した問題生成
- AI採点
- 単元ごとの理解度0〜5保存
- 学習履歴を次の指導に利用

## 注意

APIキーを `app.py` やHTML/JavaScriptに直接書かないでください。
