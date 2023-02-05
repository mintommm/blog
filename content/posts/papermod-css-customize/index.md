---
# draft: true
title: "Hugo+PaperModのCSSをカスタマイズする"
description: ""
tags: ["blog","Hugo","PaperMod", "CSS"]
# showtoc: false
# cover:
#     image: "<image path/url>" # image path/url
#     alt: "<alt text>" # alt text
#     caption: "<text>" # display caption under cover
#     relative: false # when using page bundles set this to true
#     hidden: true # only hide on current single page
date: 2023-02-05T05:25:10Z
---

## CSSの変更方法

* PaperModでは`themes/PaperMod/assets/css/`に基本的なCSSが配置されている
* これらをファイル単位でオーバーライドすることによって実現する
* `extended`フォルダを使う[^1]ようなFAQがあるが、今回は既存のスタイル指定を変更したいので、Hugoのルートに`assets/css`フォルダを作って、その中に既存のCSSファイルをコピーし、コピーで作られたCSSファイルを編集することで行った


## 見出しの先頭に#をつける

* 記事内の要素は`common/post-single.css`に記載されていたので以下を追記した

```css
.post-content h2:before {
    content: "## ";
}

.post-content h3:before {
    content: "### ";
}
```


## 見出しのmargin調整

* 記事内の要素は`common/post-single.css`に記載されていたので以下のように書き換えた

```css
.post-content h2 {
    margin: 96px auto 24px;
    font-size: 32px;
}

.post-content h3 {
    margin: 32px auto;
    font-size: 24px;
}
```




<!-- Footnotes themselves at the bottom. -->
## Notes

[^1]:

     [FAQs · adityatelange/hugo-PaperMod Wiki · GitHub](https://github.com/adityatelange/hugo-PaperMod/wiki/FAQs#bundling-custom-css-with-themes-assets)
