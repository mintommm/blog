---
# draft: true
slug: "hugo-parmalink-setting"
title: "Hugoのパーマリンクの設定"
description: "Hugoでパーマリンクを設定する"
tags: ["blog", "Hugo"]
# showtoc: false
# cover:
#     image: "<image path/url>" # image path/url
#     alt: "<alt text>" # alt text
#     caption: "<text>" # display caption under cover
#     relative: false # when using page bundles set this to true
#     hidden: true # only hide on current single page
date: 2023-03-05T21:52:57+09:00
lastmod: 2023-03-05T21:52:57+09:00
---

デフォルトのHugoでは`contents`以下のディレクトリ構造がそのままURLに反映される。

しかし、ページ数が増えてくると1つのディレクトリに名前順でズラズラと並ぶファイルやフォルダから目的のものを見つけるのは難しくなってくる。

パーマリンクを設定して各ページにslugを持たせることで、ディレクトリ構造とURLを分離させることができる。

[URL Management | Hugo](https://gohugo.io/content-management/urls/)

posts/ 配下のみにこの設定を反映させるようにするために、以下のように `config.yml` に追記した。

```yaml
permalinks:
  posts: /posts/:slug
```

各記事の Front Matter には `slug: "xxx"` を追記すれば完了。

フォルダやファイル名がURLから分離されたので、管理しやすいようにいろいろ試行錯誤していく。