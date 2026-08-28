#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
王者荣耀英雄基础数值抓取器 — 全流程版
=====================================
链路: Playwright 打开营地网页版 → 扫码登录 → 提取 SSO 凭据 → 调用营地官方 API
      (/hero/getheropageinfo) 批量抓取全英雄基础属性 → 输出 JSON + CSV

用法:
  python3 wzry_camp_scraper.py                    # 完整流程: 登录(如无凭据) + 抓取全部
  python3 wzry_camp_scraper.py --scrape-only      # 跳过登录, 用已有凭据直接抓取
  python3 wzry_camp_scraper.py --login-only       # 只登录并保存凭据
  python3 wzry_camp_scraper.py --hero 105         # 只抓单个英雄(调试用)

依赖:
  pip3 install playwright
  python3 -m playwright install chromium

输出:
  data/wzry_heroes_stats.json   — 全量数据(含胜率/ban率/T级)
  data/wzry_heroes_stats.csv    — 扁平表格
  creds.json                    — SSO 凭据(0600 权限, 请勿泄露)

数据时效: 营地 API 返回的 updateTime 为当天/前一天, 即当前正式服数值。
凭据时效: ssoToken 约 24h 过期; 过期后重跑完整流程重新扫码即可。
"""

import argparse
import json
import os
import ssl
import sys
import time
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
CREDS_PATH = os.path.join(HERE, "creds.json")
DATA_DIR = os.path.join(HERE, "data")
JSON_PATH = os.path.join(DATA_DIR, "wzry_heroes_stats.json")
CSV_PATH = os.path.join(DATA_DIR, "wzry_heroes_stats.csv")
PROFILE_DIR = os.path.join(HERE, ".camp_profile")   # Playwright 持久会话(登录态可复用)

# 额外数据 (--extra-data)
OFFICIAL_JS = "https://pvp.qq.com/web201605/js"
EXTRA_FILES = {
    "items":      {"url": f"{OFFICIAL_JS}/item.json",     "name": "全量装备表"},
    "mings":      {"url": f"{OFFICIAL_JS}/ming.json",     "name": "全量铭文表"},
    "summoners":  {"url": f"{OFFICIAL_JS}/summoner.json", "name": "召唤师技能"},
}

API_HOST = "https://ssl.kohsocialapp.qq.com:10001"
HEROLIST_URL = "https://pvp.qq.com/web201605/js/herolist.json"
LOGIN_URL = "https://yingdi.qq.com/"
LOGIN_TIMEOUT_S = 300          # 扫码等待上限
REQUEST_DELAY = 0.3            # 每英雄间隔, 礼貌限速
RETRY = 3

# 登录态失效错误码 → 提示重新登录
AUTH_ERROR_CODES = (-30003, -30314, -59005)

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


# ---------------------------------------------------------------------------
# 登录部分 (Playwright)
# ---------------------------------------------------------------------------
def login(creds_path=CREDS_PATH, timeout_s=LOGIN_TIMEOUT_S):
    """打开营地网页版, 引导用户扫码登录, 提取 SSO 凭据并保存。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("缺少 playwright: pip3 install playwright && python3 -m playwright install chromium")

    print("=" * 60, flush=True)
    print("STEP 1/3  打开王者营地网页版等待扫码登录...", flush=True)
    print("  如浏览器未自动弹出登录框: 点右上角「未登录」→ 用「王者营地」App", flush=True)
    print("  (我的 → 右上角扫一扫) 扫码即可。", flush=True)
    print("=" * 60, flush=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            PROFILE_DIR, headless=False, viewport={"width": 1280, "height": 900},
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)

        # 尝试自动点击「未登录」(可能被遮罩挡, 失败则让用户手动点)
        try:
            page.get_by_text("未登录", exact=True).first.click(timeout=3000, force=True)
            print("  已自动点击「未登录」")
        except Exception:
            print("  请在页面中手动点击「未登录」按钮...")

        # 轮询 localStorage 直到出现 ssoOpenId
        deadline = time.time() + timeout_s
        login_info = None
        while time.time() < deadline:
            try:
                raw = page.evaluate("() => localStorage.getItem('loginInfo')")
                if raw:
                    info = json.loads(raw)
                    if info.get("session", {}).get("ssoOpenId"):
                        login_info = info
                        break
            except Exception:
                pass
            time.sleep(2)

        if not login_info:
            sys.exit("✗ 等待扫码超时(300s), 请重跑脚本重新扫码。")

        user_info_raw = page.evaluate("() => localStorage.getItem('userInfo')")
        user_info = json.loads(user_info_raw or "{}")
        session = login_info["session"]

        creds = {
            "ssoOpenId": session["ssoOpenId"],
            "ssoAppId": session.get("ssoAppId", "campPc"),
            "ssoToken": session["ssoToken"],
            "ssoBusinessId": session.get("ssoBusinessId", "pc"),
            "userId": (user_info.get("profile") or {}).get("userId", ""),
            "expireTime": login_info.get("expireTime"),
            "savedAt": int(time.time()),
        }
        save_creds(creds, creds_path)
        print(f"✓ 登录成功: {creds['ssoAppId']} / userId={creds['userId']} / 过期={creds['expireTime']}")
        context.close()
    return creds


def save_creds(creds, creds_path=CREDS_PATH):
    with open(creds_path, "w") as f:
        json.dump(creds, f, ensure_ascii=False, indent=2)
    os.chmod(creds_path, 0o600)


def load_creds(creds_path=CREDS_PATH):
    if not os.path.exists(creds_path):
        sys.exit("✗ 未找到 creds.json, 请先运行完整流程登录: python3 wzry_camp_scraper.py")
    with open(creds_path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# API 调用
# ---------------------------------------------------------------------------
def api_call(path, params, retries=RETRY):
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(API_HOST + path, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    last_err = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=20, context=CTX) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except Exception as e:
            last_err = str(e)
            time.sleep(1 + i)
    return {"error": last_err, "result": 1}


def fetch_hero(creds, hero_id):
    """抓取单个英雄的完整数据 (attrInfo + gameInfo)。"""
    params = {
        "ssoOpenId": creds["ssoOpenId"],
        "ssoAppId": creds["ssoAppId"],
        "ssoToken": creds["ssoToken"],
        "ssoBusinessId": creds["ssoBusinessId"],
        "heroId": str(hero_id),
        "userId": creds.get("userId", ""),
    }
    d = api_call("/hero/getheropageinfo", params)
    code = d.get("returnCode")
    if code in AUTH_ERROR_CODES or "登录态失效" in str(d.get("returnMsg", "")):
        return {"heroId": hero_id, "error": "登录态失效, 请重新登录"}
    if d.get("result") != 0 or code != 0:
        return {"heroId": hero_id, "error": str(d.get("returnMsg") or d.get("returnCode"))}
    data = d.get("data", {})
    return {
        "heroId": hero_id,
        "updateTime": data.get("updateTime"),
        "gameInfo": data.get("gameInfo"),
        "attrInfo": data.get("attrInfo", {}),
    }


def get_herolist():
    """官方英雄列表 (ename=id, cname=名字)。"""
    try:
        req = urllib.request.Request(HEROLIST_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20, context=CTX) as r:
            raw = r.read().decode("utf-8", "ignore")
        return [(str(h["ename"]), h["cname"]) for h in json.loads(raw)]
    except Exception as e:
        sys.exit(f"✗ 获取官方英雄列表失败: {e}")


# ---------------------------------------------------------------------------
# 抓取 + 输出
# ---------------------------------------------------------------------------
def scrape_all(creds, out_dir=DATA_DIR):
    os.makedirs(out_dir, exist_ok=True)
    herolist = get_herolist()
    result = {}
    if os.path.exists(JSON_PATH):
        result = json.load(open(JSON_PATH))

    todo = [h for h in herolist if h[0] not in result or "error" in result[h[0]] or not result[h[0]].get("attrInfo")]
    print(f"STEP 2/3  英雄总数 {len(herolist)}, 已完成 {len(herolist)-len(todo)}, 待抓 {len(todo)}")

    for i, (hid, name) in enumerate(todo):
        rec = fetch_hero(creds, hid)
        rec["name"] = name
        if rec.get("error") == "登录态失效, 请重新登录":
            json.dump(result, open(JSON_PATH, "w"), ensure_ascii=False)
            sys.exit("✗ SSO 凭据已过期! 重跑完整流程重新扫码: python3 wzry_camp_scraper.py")
        result[hid] = rec
        ok = "OK" if rec.get("attrInfo") else f"ERR {rec.get('error','')[:40]}"
        print(f"  [{i+1}/{len(todo)}] {hid} {name}: {ok}", flush=True)
        if (i + 1) % 10 == 0:
            json.dump(result, open(JSON_PATH, "w"), ensure_ascii=False)
            print(f"    -- checkpoint: {len(result)} 英雄", flush=True)
        time.sleep(REQUEST_DELAY)

    json.dump(result, open(JSON_PATH, "w"), ensure_ascii=False)
    write_csv(result, CSV_PATH)
    n_ok = sum(1 for v in result.values() if v.get("attrInfo"))
    print(f"STEP 3/3  ✓ 完成: {len(result)} 英雄, {n_ok} 含属性")
    print(f"  JSON: {JSON_PATH}")
    print(f"  CSV : {CSV_PATH}")
    return result


def write_csv(result, path):
    cols = ["heroId", "name", "最大生命", "最大法力", "物理攻击", "法术攻击", "物理防御", "法术防御",
            "移速", "攻速加成", "暴击几率", "暴击效果", "攻击范围", "每五秒回血", "每五秒回蓝", "updateTime"]
    with open(path, "w") as f:
        f.write(",".join(cols) + "\n")
        for hid, h in sorted(result.items(), key=lambda x: int(x[0])):
            attr = h.get("attrInfo") or {}

            def gv(sec, nm):
                for it in attr.get(sec, []):
                    if it.get("name") == nm:
                        return it.get("data")
                return ""

            row = [hid, h.get("name", ""), gv("base", "最大生命"), gv("base", "最大法力"),
                   gv("base", "物理攻击"), gv("base", "法术攻击"),
                   gv("base", "物理防御"), gv("base", "法术防御"),
                   gv("attack", "移速"), gv("attack", "攻速加成"),
                   gv("attack", "暴击几率"), gv("attack", "暴击效果"),
                   gv("attack", "攻击范围"), gv("defence", "每五秒回血"), gv("defence", "每五秒回蓝"),
                   h.get("updateTime", "")]
            f.write(",".join("" if v is None else str(v) for v in row) + "\n")


def show_hero(creds, hero_id):
    """抓取并打印单个英雄(调试)。"""
    herolist = {hid: name for hid, name in get_herolist()}
    rec = fetch_hero(creds, hero_id)
    if rec.get("error"):
        print(f"✗ heroId={hero_id}: {rec['error']}")
        return
    print(f"\n=== {herolist.get(str(hero_id), hero_id)} (id={hero_id}) updateTime={rec.get('updateTime')} ===")
    for sec, label in (("base", "基础属性"), ("attack", "攻击属性"), ("defence", "防御属性")):
        print(f"[{label}]")
        for it in rec["attrInfo"].get(sec, []):
            print(f"  {it['name']}: {it['data']}")
    gi = rec.get("gameInfo") or {}
    if gi:
        print(f"[对局数据] 胜率 {gi.get('winRate')} / ban {gi.get('banRate')} / 登场 {gi.get('showRate')} / T级 {gi.get('tRank')}")


# ---------------------------------------------------------------------------
# 额外数据: 装备 / 铭文 / 召唤师技能 / 英雄技能
# ---------------------------------------------------------------------------
def strip_html(text):
    """去除 HTML 标签, 保留纯文本。"""
    import re
    if not text:
        return ""
    t = re.sub(r"<[^>]+>", "", text)
    t = t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return t.strip()


def fetch_official_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20, context=CTX) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def fetch_skills(creds, hero_id):
    """抓取单个英雄的全部技能 (含描述中的数值成长)。"""
    params = {
        "ssoOpenId": creds["ssoOpenId"],
        "ssoAppId": creds["ssoAppId"],
        "ssoToken": creds["ssoToken"],
        "ssoBusinessId": creds["ssoBusinessId"],
        "heroId": str(hero_id),
    }
    d = api_call("/hero/getheroskillinfo", params)
    code = d.get("returnCode")
    if code in AUTH_ERROR_CODES or "登录态失效" in str(d.get("returnMsg", "")):
        return None, "登录态失效, 请重新登录"
    if d.get("result") != 0 or code != 0:
        return None, str(d.get("returnMsg") or d.get("returnCode"))
    return d.get("data", []), None


def scrape_extra(creds, out_dir=DATA_DIR):
    """抓取装备/铭文/召唤师技能(官网JSON) + 全英雄技能(营地API)。"""
    os.makedirs(out_dir, exist_ok=True)

    # 1) 官网 JSON: 装备 / 铭文 / 召唤师技能
    for key, meta in EXTRA_FILES.items():
        data = fetch_official_json(meta["url"])
        jp, cp = os.path.join(out_dir, key + ".json"), os.path.join(out_dir, key + ".csv")
        json.dump(data, open(jp, "w"), ensure_ascii=False, indent=1)
        # CSV: 自动按字段输出, 描述列去 HTML
        if data:
            cols = list(data[0].keys())
            with open(cp, "w") as f:
                f.write(",".join(cols) + "\n")
                for row in data:
                    f.write(",".join(
                        '"' + strip_html(str(row.get(c, ""))).replace('"', '""') + '"' for c in cols) + "\n")
        print(f"✓ {meta['name']}: {len(data)} 条 -> {os.path.basename(jp)} / {os.path.basename(cp)}")

    # 2) 全英雄技能 (营地 API)
    herolist = get_herolist()
    skills = {}
    if os.path.exists(os.path.join(out_dir, "hero_skills.json")):
        skills = json.load(open(os.path.join(out_dir, "hero_skills.json")))
    todo = [h for h in herolist if h[0] not in skills]
    print(f"英雄技能: 总 {len(herolist)}, 已完成 {len(herolist)-len(todo)}, 待抓 {len(todo)}")
    for i, (hid, name) in enumerate(todo):
        data, err = fetch_skills(creds, hid)
        if err:
            if "登录态失效" in err:
                json.dump(skills, open(os.path.join(out_dir, "hero_skills.json"), "w"), ensure_ascii=False)
                sys.exit("✗ SSO 凭据已过期! 重跑完整流程重新扫码: python3 wzry_camp_scraper.py")
            skills[hid] = {"heroId": hid, "name": name, "error": err}
        else:
            skills[hid] = {"heroId": hid, "name": name, "skillArray": (data[0] or {}).get("skillArray", [])}
        print(f"  [{i+1}/{len(todo)}] {hid} {name}: {'OK' if not err else err[:40]}", flush=True)
        if (i + 1) % 10 == 0:
            json.dump(skills, open(os.path.join(out_dir, "hero_skills.json"), "w"), ensure_ascii=False)
        time.sleep(REQUEST_DELAY)
    json.dump(skills, open(os.path.join(out_dir, "hero_skills.json"), "w"), ensure_ascii=False)

    # 技能 CSV (展开为每英雄每技能一行, 含结构化数值成长)
    sp = os.path.join(out_dir, "hero_skills.csv")
    with open(sp, "w") as f:
        f.write("heroId,heroName,skillId,skillTitle,skillType,skillLabel,coolDown,loss,skillData1,skillData2,skillData3,skillData4,skillData5,gameLabels,skillDesc\n")
        for hid, h in sorted(skills.items(), key=lambda x: int(x[0])):
            for s in h.get("skillArray", []):
                desc = strip_html(s.get("szDesc", ""))
                labels = ",".join(g.get("Text", "") for g in s.get("gameLabels", []) or [])
                row = [
                    hid, h.get("name", ""), s.get("iSkillId", ""), s.get("szTitle", ""),
                    s.get("szType", ""), s.get("szLabel", ""), s.get("iCoolDown", ""), s.get("iLoss", ""),
                    s.get("szSkillData1", "") or "", s.get("szSkillData2", "") or "",
                    s.get("szSkillData3", "") or "", s.get("szSkillData4", "") or "",
                    s.get("szSkillData5", "") or "", labels, desc,
                ]
                f.write(",".join('"' + str(v).replace('"', '""') + '"' for v in row) + "\n")
    print(f"✓ 英雄技能: {len(skills)} 英雄 -> hero_skills.json / hero_skills.csv")
    return skills


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="王者荣耀英雄基础数值抓取器(营地API)")
    ap.add_argument("--scrape-only", action="store_true", help="跳过登录, 用已有凭据直接抓取")
    ap.add_argument("--login-only", action="store_true", help="只登录并保存凭据")
    ap.add_argument("--hero", type=str, default=None, help="只抓单个英雄 id")
    ap.add_argument("--extra-data", action="store_true",
                    help="抓额外数据: 装备/铭文/召唤师技能(官网) + 全英雄技能(营地API)")
    args = ap.parse_args()

    if args.hero:
        creds = load_creds()
        show_hero(creds, args.hero)
        return

    if args.scrape_only:
        creds = load_creds()
    else:
        creds = login()
        if args.login_only:
            print("✓ 凭据已保存:", CREDS_PATH)
            return

    if args.extra_data:
        scrape_extra(creds)
    else:
        scrape_all(creds)


if __name__ == "__main__":
    main()
