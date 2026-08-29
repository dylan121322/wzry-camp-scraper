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

# 恢复 TLS 证书校验: 优先用 certifi 提供 CA 链(解决 Python 3.14 缺系统证书问题)
try:
    import certifi
    CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    CTX = ssl.create_default_context()


def atomic_json_dump(obj, path):
    """原子写 JSON: 先写 .tmp 再 os.replace, 避免中断损坏数据文件。"""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def load_json_checked(path, default=None):
    """读取 JSON, 损坏/缺失时告警并返回 default(空 dict)。"""
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        print(f"⚠ 警告: {path} 无法读取(可能损坏), 按空数据继续")
        return default if default is not None else {}


def is_auth_error(d):
    """判断响应是否为登录态失效 (错误码 str 化比较 + 中文子串兜底)。"""
    code = str(d.get("returnCode"))
    return code in {str(c) for c in AUTH_ERROR_CODES} or "登录态失效" in str(d.get("returnMsg", ""))


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
        context = None
        try:
            context = p.chromium.launch_persistent_context(
                PROFILE_DIR, headless=False, viewport={"width": 1280, "height": 900},
            )
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                sys.exit(f"✗ 打开登录页失败: {e}")

            # 尝试自动点击「未登录」(可能被遮罩挡, 失败则让用户手动点)
            try:
                page.get_by_text("未登录", exact=True).first.click(timeout=3000, force=True)
                print("  已自动点击「未登录」")
            except Exception:
                print("  请在页面中手动点击「未登录」按钮...")

            # 轮询 localStorage 直到出现 ssoOpenId + ssoToken
            deadline = time.time() + timeout_s
            login_info = None
            while time.time() < deadline:
                try:
                    raw = page.evaluate("() => localStorage.getItem('loginInfo')")
                    if raw:
                        info = json.loads(raw)
                        sess = info.get("session", {})
                        if sess.get("ssoOpenId") and sess.get("ssoToken"):
                            login_info = info
                            break
                except Exception:
                    pass
                time.sleep(2)

            if not login_info:
                sys.exit("✗ 等待扫码超时(300s), 请重跑脚本重新扫码。")

            try:
                user_info_raw = page.evaluate("() => localStorage.getItem('userInfo')")
                user_info = json.loads(user_info_raw or "{}")
            except (ValueError, TypeError):
                user_info = {}
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
            uid = creds["userId"]
            print(f"✓ 登录成功: {creds['ssoAppId']} / userId={uid[:3]}*** / 过期={creds['expireTime']}")
        finally:
            if context:
                context.close()
    return creds


def save_creds(creds, creds_path=CREDS_PATH):
    # 原子创建并设 0600 权限: 先写 .tmp(os.open 指定 mode) 再 os.replace
    tmp = creds_path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(creds, f, ensure_ascii=False, indent=2)
    os.replace(tmp, creds_path)


def load_creds(creds_path=CREDS_PATH):
    if not os.path.exists(creds_path):
        sys.exit("✗ 未找到 creds.json, 请先运行完整流程登录: python3 wzry_camp_scraper.py")
    try:
        with open(creds_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        sys.exit(f"✗ creds.json 损坏或无法读取 ({e}), 请重新登录: python3 wzry_camp_scraper.py")


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
    if is_auth_error(d):
        return {"heroId": hero_id, "error": "登录态失效, 请重新登录"}
    if d.get("result") != 0 or d.get("returnCode") != 0:
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
    result = load_json_checked(JSON_PATH)

    todo = [h for h in herolist if h[0] not in result or "error" in result[h[0]] or not result[h[0]].get("attrInfo")]
    print(f"STEP 2/3  英雄总数 {len(herolist)}, 已完成 {len(herolist)-len(todo)}, 待抓 {len(todo)}")

    for i, (hid, name) in enumerate(todo):
        rec = fetch_hero(creds, hid)
        rec["name"] = name
        if rec.get("error") == "登录态失效, 请重新登录":
            atomic_json_dump(result, JSON_PATH)
            sys.exit("✗ SSO 凭据已过期! 重跑完整流程重新扫码: python3 wzry_camp_scraper.py")
        result[hid] = rec
        ok = "OK" if rec.get("attrInfo") else f"ERR {rec.get('error','')[:40]}"
        print(f"  [{i+1}/{len(todo)}] {hid} {name}: {ok}", flush=True)
        if (i + 1) % 10 == 0:
            atomic_json_dump(result, JSON_PATH)
            print(f"    -- checkpoint: {len(result)} 英雄", flush=True)
        time.sleep(REQUEST_DELAY)

    atomic_json_dump(result, JSON_PATH)
    write_csv(result, CSV_PATH)
    n_ok = sum(1 for v in result.values() if v.get("attrInfo"))
    n_err = sum(1 for v in result.values() if "error" in v)
    print(f"STEP 3/3  ✓ 完成: {len(result)} 英雄, {n_ok} 含属性, {n_err} 失败")
    print(f"  JSON: {JSON_PATH}")
    print(f"  CSV : {CSV_PATH}")
    if n_err:
        sys.exit(f"✗ {n_err} 个英雄抓取失败, 退出码 1 (重跑可续抓)")
    return result


def write_csv(result, path):
    import csv
    cols = ["heroId", "name", "最大生命", "最大法力", "物理攻击", "法术攻击", "物理防御", "法术防御",
            "移速", "攻速加成", "暴击几率", "暴击效果", "攻击范围", "每五秒回血", "每五秒回蓝", "updateTime"]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
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
            w.writerow(["" if v is None else v for v in row])


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
    """官网静态 JSON; 失败时友好退出(与 get_herolist 行为一致)。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20, context=CTX) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))
    except Exception as e:
        sys.exit(f"✗ 官网数据下载失败 {url}: {e}")


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
    if is_auth_error(d):
        return None, "登录态失效, 请重新登录"
    if d.get("result") != 0 or d.get("returnCode") != 0:
        return None, str(d.get("returnMsg") or d.get("returnCode"))
    return d.get("data") or [], None


# ---------------------------------------------------------------------------
# 装备全量详情 (官网"装备宝典" COS 数据, 公开无需登录)
# ---------------------------------------------------------------------------
COS_EQUIP_LIST = "https://wuji-1254960240.file.myqcloud.com/smoba_weapon/pages/p{}.json"
COS_EQUIP_DETAIL = "https://wuji-1254960240.file.myqcloud.com/smoba_weapon_detail/{}.json"


def cos_get(url, retries=3):
    """COS 公开 JSON; 带重试, 失败返回 None。"""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=15, context=CTX) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except Exception:
            time.sleep(1 + i)
    return None


def scrape_equips(out_dir=DATA_DIR):
    """全量装备详情: 列表(p1~p6) + 逐件详情(属性/被动/合成/适合英雄/攻略)。"""
    equips = {}
    for p in range(1, 7):
        d = cos_get(COS_EQUIP_LIST.format(p))
        if d:
            for e in d:
                equips[str(e["equipment_id"])] = e
    print(f"装备列表: {len(equips)} 件")

    details, fails = {}, []
    for i, eid in enumerate(equips):
        d = cos_get(COS_EQUIP_DETAIL.format(eid))
        if d and d.get("equipment_id") is not None:
            details[eid] = d
        else:
            fails.append(eid)
        if (i + 1) % 30 == 0:
            print(f"  [{i+1}/{len(equips)}] 详情 {len(details)}", flush=True)
        time.sleep(0.15)

    rows = []
    for eid, d in sorted(details.items(), key=lambda x: int(x[0])):
        attrs = d.get("attributes") or {}
        compound = d.get("compound_url") or {}
        sub_ids = [str(c.get("equipment_id")) for c in compound.get("compound") or []]
        fit = d.get("fit_heroes") or {}
        rows.append({
            "equipment_id": eid,
            "name": d.get("name", ""),
            "type": d.get("type", ""),
            "sub_type": d.get("sub_type", ""),
            "price": d.get("price", ""),
            "attributes": strip_html(attrs.get("attributes", "")),
            "passive_skill": strip_html(attrs.get("sub", "")),
            "compound_ids": "|".join(sub_ids),
            "fit_heroes": "、".join(h.get("desc", "") for h in fit.get("fit_heroes") or []),
            "tips": strip_html(d.get("tips", "")),
            "icon": d.get("icon", ""),
        })
    jp, cp = os.path.join(out_dir, "equips_full.json"), os.path.join(out_dir, "equips_full.csv")
    atomic_json_dump(rows, jp)
    with open(cp, "w", newline="") as f:
        import csv as _csv
        w = _csv.writer(f)
        w.writerow(list(rows[0].keys()))
        for r in rows:
            w.writerow([r[k] for k in rows[0].keys()])
    n_passive = sum(1 for r in rows if r["passive_skill"])
    print(f"✓ 全量装备: {len(rows)} 件, {n_passive} 含被动/主动 | 失败 {len(fails)} | {os.path.basename(jp)} / {os.path.basename(cp)}")
    if fails:
        print(f"  失败ID: {fails}")
    return rows


def scrape_extra(creds, out_dir=DATA_DIR):
    """抓取装备/铭文/召唤师技能(官网JSON) + 全英雄技能(营地API)。"""
    os.makedirs(out_dir, exist_ok=True)

    # 1) 官网 JSON: 装备 / 铭文 / 召唤师技能
    for key, meta in EXTRA_FILES.items():
        data = fetch_official_json(meta["url"])
        jp, cp = os.path.join(out_dir, key + ".json"), os.path.join(out_dir, key + ".csv")
        atomic_json_dump(data, jp)
        # CSV: 自动按字段输出, 描述列去 HTML
        if data:
            cols = list(data[0].keys())
            with open(cp, "w", newline="") as f:
                import csv as _csv
                w = _csv.writer(f)
                w.writerow(cols)
                for row in data:
                    w.writerow([strip_html(str(row.get(c, ""))) for c in cols])
        print(f"✓ {meta['name']}: {len(data)} 条 -> {os.path.basename(jp)} / {os.path.basename(cp)}")

    # 2) 全英雄技能 (营地 API)
    herolist = get_herolist()
    skills_path = os.path.join(out_dir, "hero_skills.json")
    skills = load_json_checked(skills_path)
    todo = [h for h in herolist if h[0] not in skills]
    print(f"英雄技能: 总 {len(herolist)}, 已完成 {len(herolist)-len(todo)}, 待抓 {len(todo)}")
    for i, (hid, name) in enumerate(todo):
        data, err = fetch_skills(creds, hid)
        if err:
            if "登录态失效" in err:
                atomic_json_dump(skills, skills_path)
                sys.exit("✗ SSO 凭据已过期! 重跑完整流程重新扫码: python3 wzry_camp_scraper.py")
            skills[hid] = {"heroId": hid, "name": name, "error": err}
        else:
            skill_array = ((data or [{}])[0] or {}).get("skillArray", []) if data else []
            skills[hid] = {"heroId": hid, "name": name, "skillArray": skill_array}
        print(f"  [{i+1}/{len(todo)}] {hid} {name}: {'OK' if not err else err[:40]}", flush=True)
        if (i + 1) % 10 == 0:
            atomic_json_dump(skills, skills_path)
        time.sleep(REQUEST_DELAY)
    atomic_json_dump(skills, skills_path)

    # 技能 CSV (展开为每英雄每技能一行, 含结构化数值成长)
    sp = os.path.join(out_dir, "hero_skills.csv")
    import csv as _csv
    with open(sp, "w", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["heroId", "heroName", "skillId", "skillTitle", "skillType", "skillLabel",
                    "coolDown", "loss", "skillData1", "skillData2", "skillData3", "skillData4",
                    "skillData5", "gameLabels", "skillDesc"])
        for hid, h in sorted(skills.items(), key=lambda x: int(x[0])):
            for s in h.get("skillArray", []):
                desc = strip_html(s.get("szDesc", ""))
                labels = ",".join(g.get("Text", "") for g in s.get("gameLabels", []) or [])
                w.writerow([
                    hid, h.get("name", ""), s.get("iSkillId", ""), s.get("szTitle", ""),
                    s.get("szType", ""), s.get("szLabel", ""), s.get("iCoolDown", ""), s.get("iLoss", ""),
                    s.get("szSkillData1", "") or "", s.get("szSkillData2", "") or "",
                    s.get("szSkillData3", "") or "", s.get("szSkillData4", "") or "",
                    s.get("szSkillData5", "") or "", labels, desc,
                ])
    n_err = sum(1 for v in skills.values() if "error" in v)
    print(f"✓ 英雄技能: {len(skills)} 英雄, {n_err} 失败 -> hero_skills.json / hero_skills.csv")
    if n_err:
        sys.exit(f"✗ {n_err} 个英雄技能抓取失败, 退出码 1 (重跑可续抓)")
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

    # 互斥: --hero 是独立调试模式, 不与其余模式混用
    if args.hero and (args.scrape_only or args.login_only or args.extra_data):
        ap.error("--hero 是独立调试模式, 不能与 --scrape-only/--login-only/--extra-data 混用")

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
        scrape_equips()
    else:
        scrape_all(creds)


if __name__ == "__main__":
    main()
