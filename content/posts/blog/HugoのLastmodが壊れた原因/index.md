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

