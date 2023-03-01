---
# draft: true
title: "HugoのGoogle AnalyticsタグをGoogle Tag Managerタグに置き換える"
description: "HugoのGoogle AnalyticsタグをGoogle Tag Managerタグに置き換える方法"
tags: ["blog","Hugo","Google Analytics", "Google Tag Manager"]
# showtoc: false
# cover:
#     image: "<image path/url>" # image path/url
#     alt: "<alt text>" # alt text
#     caption: "<text>" # display caption under cover
#     relative: false # when using page bundles set this to true
#     hidden: true # only hide on current single page
date: 2023-02-25T14:12:28+09:00
---

## Hugoには組み込みのGoogle Analyticsタグ挿入機能

Hugoには組み込みのGoogle Analyticsタグ挿入機能がある。 \
[Internal Templates | Hugo](https://gohugo.io/templates/internal/#google-analytics) \
[hugo/google_analytics.html at master · gohugoio/hugo · GitHub](https://github.com/gohugoio/hugo/blob/master/tpl/tplimpl/embedded/templates/google_analytics.html)

しかしこれはGoogle Tag Manager (GTM)の埋め込みに対応していないので、GTMを利用したい場合は別途自前で埋め込む必要がある。

Hugoのテーマによって対応しているものがあるかもしれないが、このサイトで使っているPaperModは対応していなかった。

[Hugo自体への機能追加も提案されたことがある](https://github.com/gohugoio/hugo/pull/3956)ようだが、本記事投稿時点では取り込まれていない。

なので下記サイトを参考に実装していく。 \
[How to add Google Tag Manager to Hugo static website](https://martijnvanvreeden.nl/how-to-add-google-tag-manager-to-hugo-static-website/)


## 実装


### Partialの作成

まずは `/layouts/partials` に `gtm.html` というファイルを用意して、中身を以下のようにする。 \
[記事で紹介されているサンプル](https://github.com/martijnvv/GTM-integration-Hugo)を少々変更した。


```html
{{ if hugo.IsProduction }}
    {{ if .Site.Params.gtm_id}}
        {{ if .Site.Params.gtm_endpoint}}
            <link href='https://{{ .Site.Params.gtm_endpoint }}' rel="preconnect" crossorigin>
            <link rel="dns-prefetch" href='https://{{ .Site.Params.gtm_endpoint }}'>
        {{ else }}
            <link href='https://www.googletagmanager.com' rel="preconnect" crossorigin>
            <link rel="dns-prefetch" href='https://www.googletagmanager.com'>
        {{ end }}

        {{- if eq .Site.Params.gtm_datalayer "basic"}}
            <script>
            window.dataLayer = window.dataLayer || [];
            window.dataLayer.push({
                {{- if .ExpiryDate}}'pageExpiryDate': '{{ .ExpiryDate.format  "2006-01-02"  }}',{{- end }}
                'pagePublishDate': '{{ .PublishDate.Format "2006-01-02" }}',
                'pageModifiedDate': '{{ .Lastmod.Format "2006-01-02"  }}',
                {{- if eq .Kind "page" }}'pageReadingTimeMinutes': {{ .ReadingTime }},
                'pageReadingTimeSeconds': {{- $readTime := mul (div (countwords .Content) 220.0) 60 }}{{- math.Round $readTime}},
                'pageWordCount': {{- .WordCount }},
                'pageFuzzyWordCount': {{- .FuzzyWordCount }},{{- else }}{{- end }}
                'pageKind': '{{ .Kind }}',
                'pageId': '{{ with .File }}{{ .UniqueID }}{{ end }}',
                'pageTitle': '{{ .LinkTitle }}',
                'pagePermalink': '{{ .Permalink }}',
                'pageType': '{{ .Type }}',
                'pageTranslated': {{ .IsTranslated }},
                {{- if .Params.author -}} 'pageAuthor': '{{ if .Params.author -}}{{ .Params.author }}{{- else if .Site.Author.name -}}{{ .Site.Author.name }}{{- end }}',{{- end }}	
                {{- if .Params.categories}}{{$category := index (.Params.categories) 0}}'pageCategory':'{{ $category }}', {{- end }}
                {{- if .Params.tags}}'pageTags':'{{ delimit .Params.tags "|" }}', {{- end }}
                {{- if .IsHome }}'pageType2': 'home',{{- else if eq .Kind "taxonomy" }}'pageType2': 'tag',{{- else if eq .Type "page" }}'pageType2': 'page',{{- else }}'pageType2': 'post',{{- end }}
                'pageLanguage': '{{ .Language }}'
            });
            </script>
        {{- end}}

        <script>(function(w,d,s,l,i){w[l]=w[l]||[];w[l].push({'gtm.start':
        new Date().getTime(),event:'gtm.js'});var f=d.getElementsByTagName(s)[0],
        j=d.createElement(s),dl=l!='dataLayer'?'&l='+l:'';j.async=true;j.src=
        '//{{ if .Site.Params.gtm_endpoint}}{{ .Site.Params.gtm_endpoint }}{{ else }}www.googletagmanager.com{{ end }}/gtm.js?id='+i+dl;f.parentNode.insertBefore(j,f);
        })(window,document,'script','dataLayer','{{ .Site.Params.gtm_id }}');</script>
    {{ end }}
{{ end }}
```


開発環境でGTMを無効化するためのコードとして入っている元の \
 `{{ if not (in (.Site.BaseURL | string) "localhost") }}`  \
は、URLに”localhost”が入っていることを判定条件にしている。

しかしGoogle Cloud Shell上で `hugo server -p 8080` をしたときには “localhost” は入らないので、[Hugoが組み込みで提供している関数](https://gohugo.io/functions/hugo/)である `hugo.IsProduction` を使って判定するように変更した。

さらに条件分岐の入れ子が気持ち悪かったので変更したが、これについてはなにか理由があるのかもしれないので様子をみる。

データレイヤーも様子見。現状では特に使い道を考えていないのでサンプルのままにした。


### Partialの埋め込み

次にこのPartialを `/layouts/partials/head.html` に埋め込む。

このPartialが&lt;head>タグの中身になるので任意の場所に以下を追加すればOK。


```html
{{ if .Site.Params.gtm_id}}
    {{- partial "gtm.html" . }}
{{ end }}
```


最低限としてはこれだけでも構わないが、[GTMのiframeバージョン](https://developers.google.com/tag-manager/quickstart?hl=ja)（JavaScriptが無効な場合にもある程度の動作を期待するもの）も追加しておきたい。

`/layouts/_default/baseof.html` の&lt;body>タグの先頭に近い場所に以下を追加すればOK。


```html
{{ if .Site.Params.gtm_id}}
    <noscript><iframe src="//www.googletagmanager.com/ns.html?id={{ .Site.Params.gtm_id }}" height="0" width="0" style="display:none;visibility:hidden"></iframe></noscript>
{{ end }}
```



### パラメーターの設定

上記のコードではGTMのIDなどパラメーター化してあるので、Configファイルに以下を追加することで機能を有効化できる。


```yaml
params:
  gtm_id = "GTM-xxxx" //ここは自前のGTM IDを指定する
  gtm_datalayer = "basic"
  gtm_endpoint = "sgtm.example.com" //ここは自前のsGTM用ドメインを指定する
```


`gtm_id` はGTMのIDを指定しているほか、このPartialの有効/無効を切り替えるスイッチにもなっている。

`gtm_datalayer` はGTMの[データレイヤー](https://support.google.com/tagmanager/answer/6164391?hl=ja)を有効化するためのスイッチになっている。 \
サンプルでは指定できるのが”basic”のみだが、これはページの種類によって送信するデータレイヤーの内容を変更するときにFront matterで切り替えることを想定しているのだと思う。

`gtm_endpoint ` は[サーバサイドGTM (sGTM)](https://developers.google.com/tag-platform/tag-manager/server-side/intro?hl=ja)を有効化するスイッチになっている。 \
sGTMを使うことでGTM上のタグをサーバサイドにオフロードできるようになるため、GTMにタグを追加することでページが重くなるのを予防することができる。

sGTM自体の設置方法は[公式ドキュメント](https://developers.google.com/tag-platform/tag-manager/server-side/cloud-run-setup-guide?hl=ja)へどうぞ。 \
Wataru Inoueさんによる[ブログ](https://medium.com/google-cloud-jp/server-side-google-tag-manager-on-cloud-run-48451cee7f89)も参考になる。


### sGTMの費用について

ちなみにsGTMはサーバ側の処理のために費用がかかる。

[セットアップガイド](https://developers.google.com/tag-platform/tag-manager/server-side/cloud-run-setup-guide?hl=ja)の通りCloud Runを利用する場合、この費用は[計算ツール](https://developers.google.com/tag-platform/tag-manager/server-side/cloud-run-setup-guide?hl=ja#cloud_run_calculator)を利用することで予測ができる。

見積もりの詳細が書かれていないが、最低額だとCloud Runの「CPU を常に割り当てる」プランで最小インスタンス数を2にした場合の価格とほとんど同じ数値が出ている。 \
ただし[Cloud Runには無料枠がある](https://cloud.google.com/run/pricing?hl=ja)がここでは考慮されていない模様。

「CPU を常に割り当てる」プランはレスポンス後にも非同期的なバックグラウンド処理が可能であったりコールドスタートが問題にならない代わりに、リクエストがなくても常時費用が発生してしまう。

個人のファンブログのレベルでは非同期的なタグを利用することもないだろうし、いくらかのデータ欠損が発生しても致命的な問題にはならないと思うので「リクエストの処理中にのみ CPU を割り当てる」プランで安く運用するほうが向いていると思っている。

コールドスタートといっても[10秒はキューで待機する](https://cloud.google.com/run/docs/container-contract?hl=ja#startup)のでよっぽどのことがない限りエラーにもならないはず。

ここはもうちょっと仕様を調べてもいいかもしれない。


### GTMの動作チェック

実装が済んだら、最後にGTMの動作チェックをして終了。 \
[コンテナのプレビューとデバッグ - タグ マネージャー ヘルプ](https://support.google.com/tagmanager/answer/6107056?hl=ja)
