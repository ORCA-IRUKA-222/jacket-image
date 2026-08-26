# jacket-image — mp3 / m4a のカバーアート自動付与

PC 内の `.mp3` / `.m4a` を再帰的に走査し、**カバーアート（ジャケット画像）が入っていないファイルだけ**を対象に、
曲情報から画像を自動で探して埋め込むコマンドラインツールです。

- APIキーの登録は不要（iTunes Search API を利用、見つからなければ MusicBrainz / Cover Art Archive）
- 外部ライブラリは `mutagen` のみ（HTTP は Python 標準ライブラリ）
- Windows / macOS / Linux で動作

## セットアップ

```bash
pip install -r requirements.txt
```

## 使い方

まずは **`--dry-run` で何が起きるか確認**するのがおすすめです（ファイルは一切変更されません）。

```bash
# 1. 変更せずに結果だけ確認
python add_cover_art.py "D:/Music" --dry-run

# 2. 数件だけ試す
python add_cover_art.py "D:/Music" --limit 10 --backup

# 3. 問題なければ全体に適用
python add_cover_art.py "D:/Music" --backup --report report.csv
```

macOS / Linux の例:

```bash
python3 add_cover_art.py ~/Music --dry-run
```

## 動作の流れ

各ファイルについて、次の順に処理します。

1. **既にカバーアートがあればスキップ**（`--force` を付けると上書き）
2. 曲情報を取得
   - タグ（アーティスト / アルバム / 曲名 / アルバムアーティスト）
   - タグが無ければファイル名（`01 - Artist - Title.mp3` などを解析）と親フォルダ名（アルバム名の推定）
3. 画像を探す
   - 同じ（または親）フォルダの `cover.jpg` / `folder.jpg` / `front.png` など … `--no-local` で無効化
   - iTunes Search API（アルバム検索 → 曲検索の順）
   - MusicBrainz で該当リリースを検索し、Cover Art Archive の表ジャケットを取得
4. **一致度チェック** — 検索結果のアーティスト名・アルバム名と手元の情報を比較してスコア化し、
   `--min-score`（既定 0.6）未満なら「別のアルバムの画像」を貼らないよう見送ります
5. 画像を検証（JPEG / PNG のマジックバイトとサイズ）してから埋め込み
   - mp3 … ID3v2.3 の `APIC`（front cover）。v2.3 で保存するので Windows のエクスプローラーでも表示されます
   - m4a … MP4 の `covr` アトム

同じアルバムのファイルは 1 回の検索結果を使い回すので、アルバム単位ではほぼ 1 リクエストで済みます。
API への負荷を避けるためレート制限（iTunes 0.4 秒間隔 / MusicBrainz 1.1 秒間隔）を入れています。

## 主なオプション

| オプション | 説明 |
| --- | --- |
| `--dry-run` | ファイルを変更せず、何をするかだけ表示 |
| `--force` | 既にカバーアートがあるファイルも上書き |
| `--backup` | 書き込み前に `.bak` を作成 |
| `--size 600` | 取得する画像サイズ（正方形ピクセル、既定 600。1200 なども可） |
| `--country JP` | iTunes ストアの国コード（既定 JP） |
| `--min-score 0.6` | 採用する最低一致度。誤った画像が付くようなら 0.75 程度に上げる |
| `--no-local` | フォルダ内の `cover.jpg` 等を使わず必ず Web から取得 |
| `--offline` | Web を使わず、フォルダ内の画像だけで付与 |
| `--save-folder-jpg` | 埋め込みに加えてフォルダにも `cover.jpg` を保存 |
| `--report report.csv` | 処理結果を CSV に出力（Excel でそのまま開けます） |
| `--limit 10` | 処理件数の上限（動作確認用） |
| `--no-recursive` | サブフォルダを辿らない |
| `--quiet` | 1 件ごとの表示を抑える |

## 注意点

- **タグもファイル名も手がかりが無いファイル**（`track01.mp3` など）は特定できないため見送られます。
  CSV レポートの `not-found` / `no-metadata` を見て、必要なら先にタグを整えてください。
- 自動判定である以上、**別版・ベスト盤の画像が付く可能性はゼロではありません**。
  最初は `--dry-run` と `--backup`、心配なら `--min-score` を上げて運用してください。
- 取得した画像は各サービスの著作物です。個人の手持ちライブラリの整理用途に留めてください。
- iTunes Search API / MusicBrainz は公開 API です。大量のファイルを一度に処理すると
  一時的に応答が返らなくなることがあります（その場合は自動でリトライ・スキップします）。

## テスト

```bash
python -m unittest discover -s tests -v
```

ネットワークへは接続せず、合成した最小の mp3 / m4a ファイルと API 応答のスタブで検証します（33 件）。
