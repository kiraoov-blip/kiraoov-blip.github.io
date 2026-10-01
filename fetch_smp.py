#!/usr/bin/env python3
"""전력거래소 제주 SMP(하루전시장) 표를 받아 data/smp-jeju.json 으로 저장한다.

- 기본 화면(오늘 거래분, 최근 7일)을 GET 으로 받고,
- 날짜 칸에 '내일'을 넣어 POST 로 한 번 더 받아(내일 거래분) 두 표를 합친다.
- --probe : 접속 가능 여부와 페이지 구조(날짜 입력 칸, 토큰 등)만 출력한다. 파일은 바꾸지 않는다.

환경변수(선택, 실제 POST 값 이름을 알게 되면 여기서 바로 고칠 수 있다)
  KPX_URL          기본 주소
  DATE_FIELD       날짜 값을 넣을 폼 이름 (기본: 페이지의 <input type=date> 의 name/id 에서 자동 감지)
  EXTRA_FIELDS     JSON. 함께 보낼 고정 값 예: {"mid":"a10606080200"}
  CSRF_FIELD       토큰 값의 폼 이름 (기본: 페이지의 hidden 입력에서 자동 감지, 없으면 XSRF-TOKEN 쿠키 사용)
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

URL = os.environ.get("KPX_URL", "https://new.kpx.or.kr/smpJeju.es?mid=a10606080200&device=pc")
KST = timezone(timedelta(hours=9))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "smp-jeju.json")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
}


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- 표 읽기
def parse_heading_date(soup):
    """'2026년 10월 01일 거래분' -> date(2026,10,1)"""
    m = re.search(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일\s*거래분", soup.get_text(" ", strip=True))
    if not m:
        return None
    return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()


def parse_table(html):
    """표(1h..24h)를 읽어 {'heading': date|None, 'cols': {'YYYY-MM-DD': {'hourly':[24], 'max':..,'min':..,'avg':..}}} 반환"""
    soup = BeautifulSoup(html, "html.parser")
    heading = parse_heading_date(soup)
    for table in soup.find_all("table"):
        head = [re.sub(r"\s+", " ", th.get_text(" ", strip=True)) for th in table.find_all("th")]
        date_heads = [(i, h) for i, h in enumerate(head) if re.match(r"^\d{2}\.\d{2}\b", h)]
        if not date_heads:
            continue
        ref = heading or datetime.now(KST).date()
        dates = []
        for _, h in date_heads:
            mm, dd = int(h[0:2]), int(h[3:5])
            year = ref.year - 1 if mm > ref.month + 6 else (ref.year + 1 if mm < ref.month - 6 else ref.year)
            dates.append(datetime(year, mm, dd).date().isoformat())
        cols = {d: {"hourly": [None] * 24, "max": None, "min": None, "avg": None} for d in dates}
        for tr in table.find_all("tr"):
            cells = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
            if len(cells) < 1 + len(dates):
                continue
            label, values = cells[0], cells[1:1 + len(dates)]
            m = re.match(r"^(\d{1,2})\s*h$", label)
            key = None
            if m and 1 <= int(m.group(1)) <= 24:
                key = int(m.group(1)) - 1
            elif label.startswith("최대"):
                key = "max"
            elif label.startswith("최소"):
                key = "min"
            elif label.startswith("가중평균") or label.startswith("평균"):
                key = "avg"
            if key is None:
                continue
            for d, v in zip(dates, values):
                try:
                    num = float(v.replace(",", ""))
                except ValueError:
                    num = None
                if isinstance(key, int):
                    cols[d]["hourly"][key] = num
                else:
                    cols[d][key] = num
        return {"heading": heading.isoformat() if heading else None, "cols": cols}
    return {"heading": heading.isoformat() if heading else None, "cols": {}}


# ---------------------------------------------------------------- 요청
def looks_blocked(html):
    return len(html) < 2000 and ("firewall" in html.lower() or "blocked" in html.lower())


def get_page(s):
    r = s.get(URL, headers=HEADERS, timeout=30)
    return r


def find_form_info(html, cookies):
    """날짜 입력 칸 이름과 토큰/숨은 값 찾기"""
    soup = BeautifulSoup(html, "html.parser")
    date_in = soup.find("input", attrs={"type": "date"})
    date_field = os.environ.get("DATE_FIELD") or (date_in and (date_in.get("name") or date_in.get("id"))) or None
    form = date_in.find_parent("form") if date_in else None
    fields = {}
    scope = form if form else soup
    for inp in scope.find_all("input"):
        n = inp.get("name")
        if not n or inp.get("type") in ("submit", "button", "image", "file"):
            continue
        if form or inp.get("type") == "hidden":
            fields[n] = inp.get("value", "")
    csrf_field = os.environ.get("CSRF_FIELD")
    if not csrf_field:
        for n in fields:
            if "csrf" in n.lower() or n.lower() in ("_token", "token"):
                csrf_field = n
                break
    xsrf = cookies.get("XSRF-TOKEN")
    if csrf_field and not fields.get(csrf_field) and xsrf:
        fields[csrf_field] = xsrf
    if not csrf_field and xsrf:
        csrf_field = "_csrf"
        fields["_csrf"] = xsrf
    extra = json.loads(os.environ.get("EXTRA_FIELDS", "{}") or "{}")
    fields.update(extra)
    return date_field, fields


def post_date(s, html, target_iso):
    date_field, fields = find_form_info(html, s.cookies.get_dict())
    if not date_field:
        raise RuntimeError("날짜 입력 칸(<input type=date>)의 이름을 찾지 못했습니다. DATE_FIELD 환경변수로 지정해 주세요.")
    payload = dict(fields)
    payload[date_field] = target_iso
    h = dict(HEADERS)
    h.update({"Content-Type": "application/x-www-form-urlencoded", "Origin": re.match(r"^https?://[^/]+", URL).group(0), "Referer": URL})
    log(f"POST 값 이름: {sorted(payload.keys())} / 날짜칸={date_field}={target_iso}")
    r = s.post(URL, data=payload, headers=h, timeout=30)
    return r


# ---------------------------------------------------------------- 진단
def probe():
    s = requests.Session()
    r = get_page(s)
    html = r.text
    log(f"[GET] HTTP {r.status_code}, 본문 {len(html)}자, 쿠키 이름: {sorted(s.cookies.get_dict().keys())}")
    if looks_blocked(html):
        log("=> 전력거래소 보안 장치가 이 서버의 접속을 막은 것으로 보입니다.")
        log("본문 앞부분: " + re.sub(r"\s+", " ", html)[:400])
        return
    p = parse_table(html)
    log(f"표 기준일(heading): {p['heading']} / 표 날짜: {list(p['cols'].keys())}")
    soup = BeautifulSoup(html, "html.parser")
    di = soup.find("input", attrs={"type": "date"})
    log("날짜 입력 칸: " + (str(di)[:300] if di else "없음"))
    log("form 개수: %d" % len(soup.find_all("form")))
    for f in soup.find_all("form"):
        log("  form action=%s method=%s inputs=%s" % (f.get("action"), f.get("method"), [i.get("name") for i in f.find_all("input")][:12]))
    hidden = [(i.get("name"), (i.get("value") or "")[:8] + ("…" if len(i.get("value") or "") > 8 else "")) for i in soup.find_all("input", attrs={"type": "hidden"})]
    log(f"hidden 입력(값은 앞 8자만): {hidden[:20]}")
    # 날짜 이동 버튼과 관련된 스크립트 조각
    hits = 0
    for sc in soup.find_all("script"):
        t = sc.string or ""
        if re.search(r"거래분|submit\(|searchDate|fn_|goDate|prevDay|nextDay", t):
            log("스크립트 조각: " + re.sub(r"\s+", " ", t)[:500])
            hits += 1
            if hits >= 4:
                break
    log("외부 스크립트: " + str([sc.get("src") for sc in soup.find_all("script") if sc.get("src")][:30]))
    try:
        tgt = (datetime.now(KST).date() + timedelta(days=1)).isoformat()
        r2 = post_date(s, html, tgt)
        p2 = parse_table(r2.text)
        log(f"[POST {tgt}] HTTP {r2.status_code}, 본문 {len(r2.text)}자, 표 기준일: {p2['heading']} / 표 날짜: {list(p2['cols'].keys())}")
    except Exception as e:  # noqa: BLE001
        log(f"[POST 시도 실패] {e}")


# ---------------------------------------------------------------- 저장
def is_published(c):
    """발표된 날짜인지. 전력거래소는 아직 발표하지 않은 날짜를 0(또는 빈칸)으로 채워 보여주므로,
    24시간 값이 모두 있고 그중 0이 아닌 값이 하나라도 있어야 '발표됨'으로 본다."""
    h = c.get("hourly") or []
    vals = [v for v in h if v is not None]
    return len(h) == 24 and len(vals) == 24 and any(v != 0 for v in vals)


def merge_into(store, parsed):
    for d, c in parsed["cols"].items():
        if is_published(c):
            store["days"][d] = c


def main():
    if "--probe" in sys.argv:
        probe()
        return 0
    store = {"updatedAt": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"), "source": URL, "days": {}}
    if os.path.exists(OUT):
        try:
            old = json.load(open(OUT, encoding="utf-8"))
            # 이전 실행에서 '미발표(전부 0)' 날짜가 잘못 저장돼 있으면 걸러낸다
            store["days"] = {d: c for d, c in old.get("days", {}).items() if is_published(c)}
        except Exception:  # noqa: BLE001
            pass
    s = requests.Session()
    r = get_page(s)
    if r.status_code != 200 or looks_blocked(r.text):
        log(f"::warning::기본 화면을 받지 못했습니다 (HTTP {r.status_code}, {len(r.text)}자)")
        return 0
    base = parse_table(r.text)
    merge_into(store, base)
    log(f"기본 화면 기준일 {base['heading']}, 날짜 {list(base['cols'].keys())}")
    tomorrow = (datetime.now(KST).date() + timedelta(days=1)).isoformat()
    if tomorrow not in store["days"]:
        try:
            r2 = post_date(s, r.text, tomorrow)
            p2 = parse_table(r2.text)
            merge_into(store, p2)
            log(f"내일({tomorrow}) 요청 결과: 기준일 {p2['heading']}, 날짜 {list(p2['cols'].keys())}")
        except Exception as e:  # noqa: BLE001
            log(f"::warning::내일 자료 요청 실패: {e}")
    # 오래된 날짜 정리 (최근 14일만 보관)
    cutoff = (datetime.now(KST).date() - timedelta(days=14)).isoformat()
    store["days"] = {d: v for d, v in sorted(store["days"].items()) if d >= cutoff}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=1)
    log(f"저장 완료: {OUT} / 날짜 {list(store['days'].keys())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
