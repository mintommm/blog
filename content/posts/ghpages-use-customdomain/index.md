---
# draft: true
title: "Github Pagesでカスタムドメインを使う"
description: "HugoをGithub Pagesでカスタムドメインを使うために必要な設定を追加した"
tags: ["blog","Hugo","Github Pages"]
# cover:
#     image: "<image path/url>" # image path/url
#     alt: "<alt text>" # alt text
#     caption: "<text>" # display caption under cover
#     relative: false # when using page bundles set this to true
#     hidden: true # only hide on current single page
date: 2023-01-29
---

## カスタムドメインの検証



* Github Pagesで使うドメインを検証しておくことで乗っ取りのリスクが低減する
* [GitHub Pagesのカスタムドメインの検証](https://docs.github.com/ja/pages/configuring-a-custom-domain-for-your-github-pages-site/verifying-your-custom-domain-for-github-pages)


## `mintommm.github.io`リポジトリでの設定



* **※このGithub Pagesの設定をDNS設定の前に進める必要がある**
    * セキュリティ上の理由[^1]による


    * 他人が自分のGithub Pagesを乗っ取れるタイミングが発生してしまうとのこと
    * DNS設定より先にこれをやるとドメインのDNSチェックがエラーになるので順番を守ること


### `mintommm.github.io`リポジトリ内の設定へ移動



* [Settings] > [Pages] > [Custom Domain]


### `tryk.dev` を入力して保存



* ここまではDNS設定前にやる必要がある


### DNSチェック



* 15分くらいでTLS証明書の設定が終わる
* これが次項のDNS設定が終わってからでないと完了しない


### Enforce HTTPSをONにする



* devドメインなのでHTTPSが必須？
* そうでなくてもhttpsにしない理由がないのでONにする
* DNSチェックが終わっていないとONにできない


## ドメインへのDNS設定



* **※このDNS設定の前にGithub Pagesを設定しておく必要がある**
    * 同上
* ドメインのルート（Apexドメイン）で運用したい
    * Github Pagesでは、Apexドメインを利用する場合にはサブドメイン`www`も同じページを指すように設定するのが推奨とのこと
    * サブドメイン`www`にも設定しておくことでApexドメインとサブドメイン`www`の間で自動的なリダイレクトが設定される
        * 例：`tryk.dev`で運用しているとき`www.tryk.dev`へのアクセスが`tryk.dev`にリダイレクトされる`
* [ルート](https://docs.github.com/ja/pages/configuring-a-custom-domain-for-your-github-pages-site/managing-a-custom-domain-for-your-github-pages-site#configuring-an-apex-domain)
    * A
        * `185.199.108.153`
        * `185.199.109.153`
        * `185.199.110.153`
        * `185.199.111.153`
    * AAA
        * `2606:50c0:8000::153`
        * `2606:50c0:8001::153`
        * `2606:50c0:8002::153`
        * `2606:50c0:8003::153`
* [www](https://docs.github.com/ja/pages/configuring-a-custom-domain-for-your-github-pages-site/managing-a-custom-domain-for-your-github-pages-site#configuring-a-subdomain)
    * CNAME
        * `mintommm.github.io`


## CNAMEファイルの追加



* これを設置しておくことで`mintommm.github.io`から`tryk.dev`への301リダイレクトが設定される
    * [GitHub Pages サイトのカスタムドメインを管理する](https://docs.github.com/ja/pages/configuring-a-custom-domain-for-your-github-pages-site/managing-a-custom-domain-for-your-github-pages-site#configuring-a-subdomain)
    * [ぎじゅつわふたー | GitHub-ActionsでカスタムドメインのGitHub-Pagesをデプロイすると、カスタムドメインの登録が消える](https://tech-wafter.net/2020/deploy-custom-domain-github-pages-on-github-actions/)
* [actions-gh-pages](https://github.com/peaceiris/actions-gh-pages)を使っているのでGithub Actionsのyamlファイルに以下を追加すればOK

    ```yaml
    jobs:
        deploy:
            steps:
            - name: Deploy
                with:
                cname: tryk.dev
    ```




## サーチコンソールへの登録



* Google Domainsを使っているので所有者確認はクリックするだけで完了した
* Hugoデフォルトでsitemap.xmlが生成されているはずなので、`https://tryk.dev/sitemap.xml`を登録する

<!-- Footnotes themselves at the bottom. -->
## Notes

[^1]:

     [GitHub Pages サイトのカスタムドメインを管理する](https://docs.github.com/ja/pages/configuring-a-custom-domain-for-your-github-pages-site/managing-a-custom-domain-for-your-github-pages-site#about-custom-domain-configuration)
    >DNS プロバイダでカスタムドメインを設定する前に、必ず GitHub Pages サイトをカスタムドメインに追加してください。 カスタムドメインを GitHub に追加せずに DNS プロバイダに設定すると、別のユーザがあなたのサブドメインにサイトをホストできることになります。
注: DNS の変更内容が反映されるまで最大で 24 時間かかることがあります。
