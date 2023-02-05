---
# draft: true
title: "Hugo+PaperModのCSSをカスタマイズして見出しの先頭に#をつける"
description: "h2やh3要素の先頭に「##」や「###」を入れたい"
tags: ["blog","Hugo","PaperMod"]
showtoc: false
# cover:
#     image: "<image path/url>" # image path/url
#     alt: "<alt text>" # alt text
#     caption: "<text>" # display caption under cover
#     relative: false # when using page bundles set this to true
#     hidden: true # only hide on current single page
date: 2023-02-05T05:25:10Z
---
## やったこと

* PaperModでは`themes/PaperMod/assets/css/`に基本的なCSSが配置されている
* これらをファイル単位でオーバーライドすることによって実現する
* `extended`フォルダを使う[^1]ようなFAQがあるが、今回は既存のスタイル指定を変更したいので、Hugoのルートに`assets/css`フォルダを作って、その中に既存のCSSファイルをコピーし、コピーで作られたCSSファイルを編集することで行った


* 記事内の要素は`common/post-single.css`に記載されていたので以下を追記した


```css
.post-content h2:before {
    content: "## ";
}

.post-content h3:before {
    content: "### ";
}
```



<!-- Footnotes themselves at the bottom. -->
## Notes

[^1]:

     [FAQs · adityatelange/hugo-PaperMod Wiki · GitHub](https://github.com/adityatelange/hugo-PaperMod/wiki/FAQs#bundling-custom-css-with-themes-assets)
