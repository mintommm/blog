---
draft: true
slug: "fixed-lastmod-of-hugo"
title: "HugoのLastmodが正しく表示されるためにやったこと"
description: ""
tags: ["","",""]
# showtoc: false
# cover:
#     image: "<image path/url>" # image path/url
#     alt: "<alt text>" # alt text
#     caption: "<text>" # display caption under cover
#     relative: false # when using page bundles set this to true
#     hidden: true # only hide on current single page
date: 2023-08-12T15:32:22Z
# lastmod: 2023-08-12T15:32:22Z
---

- https://github.com/peaceiris/actions-hugo/issues/496
- TZ

- Depthではなかった
- 全部の記事のlastmodが最新日に揃って同じになっていた
- enableGitInfo = true だけではだめだった
- https://discourse.gohugo.io/t/problems-with-gitinfo-in-ci/22480
- CIでうまく行かないことがある、みたいなやつは↑だが、これも違った
- https://crieit.net/posts/github-actions-build-lastmod
- https://tech-wafter.net/2020/solved-issue-with-github-actions-lastmod-updates-being-applied-to-all-the-articles/

- https://molina.jp/blog/hugo%E3%81%A6%E6%9B%B4%E6%96%B0%E6%97%A5%E6%99%82%E3%81%AE%E7%AE%A1%E7%90%86/
- これで解決の緒が見えた
