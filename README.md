# 王者荣耀英雄基础数值抓取器（营地 API 全流程）

一条命令完成：**网页扫码登录 → 提取 SSO 凭据 → 批量抓取全英雄基础属性 → 输出 JSON/CSV**。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

数据源为王者营地官方 API（`ssl.kohsocialapp.qq.com:10001/hero/getheropageinfo`），
`updateTime` 为当天/前一天，即**当前正式服数值**（含新英雄）。

## 快速开始

```bash
# 1) 安装依赖（仅首次）
pip3 install playwright
python3 -m playwright install chromium

# 2) 完整流程：打开浏览器 → 点「未登录」→ 王者营地 App 扫码 → 自动抓取 132 英雄
python3 wzry_camp_scraper.py

# 3) 凭据未过期时跳过登录直接抓取
python3 wzry_camp_scraper.py --scrape-only

# 其他
python3 wzry_camp_scraper.py --login-only   # 只登录保存凭据
python3 wzry_camp_scraper.py --hero 105     # 抓单个英雄（调试）
python3 wzry_camp_scraper.py --extra-data   # 抓额外数据: 装备/铭文/召唤师技能 + 全英雄技能

# 仅抓装备宝典（独立脚本, 公开COS数据源, 无需登录/凭据）
python3 wzry_equip.py
python3 wzry_equip.py --out /path/to/dir    # 自定义输出目录
```

## 输出

| 文件 | 说明 |
|------|------|
| `data/wzry_heroes_stats.json` | 全量：每英雄 `attrInfo`（基础/攻击/防御属性）+ `gameInfo`（胜率/ban率/登场率/T级）+ `updateTime` |
| `data/wzry_heroes_stats.csv` | 英雄属性扁平表格 |
| `data/items.json` / `.csv` | 全量装备表（121 件：价格/总价/属性描述） |
| `data/mings.json` / `.csv` | 全量铭文表（93 个：类型红绿蓝/等级/属性） |
| `data/summoners.json` / `.csv` | 召唤师技能（11 个：CD/效果） |
| `data/hero_skills.json` / `.csv` | 全英雄技能（132 英雄 538 技能：被动/主动/大招，描述含当前数值+成长系数） |
| `data/equips_full.json` / `.csv` | 全量装备宝典（126 件：属性/被动主动/合成配方/适合英雄/攻略，来源官网 COS 公开数据） |
| `creds.json` | SSO 凭据（权限 0600，**勿泄露/勿提交 git**） |
| `.camp_profile/` | Playwright 持久会话，登录态复用（下次扫码免登录） |

字段：最大生命 / 最大法力 / 物理攻击 / 法术攻击 / 物理防御 / 法术防御 /
移速 / 攻速加成 / 暴击几率 / 暴击效果 / 攻击范围 / 每五秒回血 / 每五秒回蓝

## 原理

1. `yingdi.qq.com` 营地网页版登录后，`localStorage.loginInfo` 含
   `ssoOpenId / ssoAppId / ssoToken / ssoBusinessId`（SSO 会话），
   `userInfo.profile.userId` 为营地用户 ID。
2. 营地 API 支持 **SSO 鉴权**（AuthType.SSO）：
   `POST /hero/getheropageinfo`，参数 = SSO 四项 + `heroId` + `userId`。
   无需逆向 App 签名（msdk 的 openid/token/sig 需要手机抓包）。
3. 返回 `data.attrInfo` = `{base: 基础属性, attack: 攻击属性, defence: 防御属性}`。

## 注意事项

- **凭据时效**：ssoToken 约 24h 过期（`creds.json` 里 `expireTime`）。
  过期后 API 返回 `登录态失效`，脚本会提示重新运行完整流程扫码。
- **数据时效**：`updateTime` 字段即数据版本日期；基础属性随版本平衡调整，定期重跑刷新。
- **限速**：默认每英雄间隔 0.3s，132 英雄约 1–2 分钟跑完。
- 英雄列表取自官方 `pvp.qq.com/web201605/js/herolist.json`（含最新英雄如 蚩奼/大禹/孙权）。
